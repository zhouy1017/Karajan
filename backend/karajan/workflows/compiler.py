"""The pure declarative compiler: bundle bytes in, CompiledWorkflow out.

The compiler is a function of the validated bundle bytes and the registered
execution kinds. It reads no clock, opens no file, performs no request, imports
nothing named by the bundle and calls no model, so the same inputs always yield
the same compiled digest. Nothing here mutates its arguments or holds state
between calls, which is what lets #177 load a bundle and re-derive exactly the
identity that was published here.

What it checks, and what a rejection looks like:

* every referenced role, contract and execution kind resolves at a fixed
  revision (never ``latest``), including inside declared dynamic boundaries;
* every step input is a declared workflow input, a declared artifact reference
  naming an actual producing step the step depends on, or a literal, and its
  type is accepted by the target input contract;
* every condition fact names something real: a declared input, a trusted result
  of a step this step depends on, or a named human decision;
* dependencies are ordinary and acyclic, with every dependency target defined;
* rework is a finite bounded rule, not an arbitrary back edge;
* the report/patch/pr delivery gate is not removable by a role name, an
  optional flag or a condition.

A rejection carries located diagnostics. A missing, unknown or self-referential
reference is a rejection, not a guess: nothing is ever silently compiled as a
worker task, and nothing unresolved is reported as executable.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from .digests import content_digest
from .errors import Diagnostic, WorkflowError, located
from .registry import (
    REGISTERED_INSTRUCTION_TEMPLATES,
    ExecutionKind,
    contract_field_type,
    contract_is_registered,
    contract_schema,
    kind_for,
    literal_text,
    registered_refs,
    resolved_text,
    validate_bindings,
)

#: Bumped whenever the compiled document's shape changes. A compiled digest is
#: only comparable between two bundles compiled by the same revision.
COMPILER_REVISION = 1
COMPILER_IDENTITY = f"karajan.workflow-compiler.v{COMPILER_REVISION}"

SCHEMA_VERSION = "workflow.v1"
MANIFEST_SCHEMA_VERSION = "karajan.workflow-bundle.v1"

#: This compiler imposes no engine-level cap on how many steps, agents or
#: expansion members a workflow may declare. Actual concurrency is bounded by
#: real resources and by whatever user policy is configured, and admission is
#: decided at run time (#178), never by a hidden number here. The only limits are
#: transport-safety bounds on the upload itself, which are a parsing concern and
#: are not a task-count policy.
MAXIMUM_REWORK_ROUNDS = 64

Operator = Literal["equals", "not_equals", "all", "any", "not", "at_least", "at_most"]
OPERATORS: frozenset[str] = frozenset(
    {"equals", "not_equals", "all", "any", "not", "at_least", "at_most"}
)

#: The fields each declared document may carry. An unknown field is refused
#: rather than ignored: a bundle that names something the compiler never reads
#: would otherwise be accepted as though it had been validated, and a field such
#: as ``api_key`` would be stored and echoed back while meaning nothing here.
#: Architecture/10 §3: a bundle carries references, never actual credentials.
WORKFLOW_FIELDS = frozenset(
    {
        "schema_version",
        "id",
        "revision",
        "delivery_kind",
        "input_contract",
        "inputs",
        "roles",
        "bindings",
        "steps",
        "completion",
        "scheduling",
        "rework",
    }
)

ROLE_FIELDS = frozenset(
    {
        "id",
        "revision",
        "display_name",
        "responsibilities",
        "stop_conditions",
        "required_capabilities",
        "tool_constraints",
        "input_contract_ref",
        "output_contract_ref",
        "instruction_template_ref",
        "independence_requirements",
    }
)

#: Names that identify a credential or an authentication factor. A declarative
#: configuration references a secret; it never carries the material, so a
#: credential-shaped field is refused by name. The submitted *value* is never
#: echoed: the diagnostic names the field only, and never prints what was sent.
CREDENTIAL_MARKERS = frozenset(
    {
        "api_key",
        "apikey",
        "api_secret",
        "access_key",
        "secret_key",
        "private_key",
        "client_secret",
        "auth_token",
        "access_token",
        "refresh_token",
        "id_token",
        "session_token",
        "password",
        "passwd",
        "passphrase",
        "credential",
        "credentials",
        "bearer",
        "authorization",
        "cookie",
        "cookies",
    }
)

#: Reference-shaped names that are legitimate: they name where a credential
#: lives rather than carrying it, so they are not treated as credential fields.
CREDENTIAL_REFERENCE_FIELDS = frozenset({"secret_ref", "credential_ref", "auth_ref"})


def _looks_like_credential(name: object) -> bool:
    """Whether a declared field name identifies credential material."""
    if not isinstance(name, str):
        return False
    normalised = name.strip().casefold().replace("-", "_").replace(" ", "_")
    if normalised in CREDENTIAL_REFERENCE_FIELDS:
        return False
    if normalised in CREDENTIAL_MARKERS:
        return True
    return any(
        normalised.endswith(marker) or normalised.startswith(marker)
        for marker in CREDENTIAL_MARKERS
    )


def _refuse_unknown_fields(
    document: Mapping[str, Any], allowed: frozenset[str], *, file: str
) -> None:
    """Refuse an undeclared field, and refuse credential-shaped names outright."""
    diagnostics: list[Diagnostic] = []
    for name in sorted(document, key=str):
        if _looks_like_credential(name):
            diagnostics.append(
                located(
                    "WORKFLOW_CREDENTIAL_FIELD_REFUSED",
                    f"{file}#",
                    "a declarative bundle references a secret; it never carries "
                    f"credential material (field: {str(name)[:48]})",
                )
            )
        elif name not in allowed:
            diagnostics.append(
                located(
                    "WORKFLOW_FIELD_UNSUPPORTED",
                    f"{file}#",
                    f"unknown field: {str(name)[:48]}",
                )
            )
    if diagnostics:
        raise WorkflowError(diagnostics[0].code, diagnostics=diagnostics)


def _refuse_nested_credentials(value: Any, *, file: str, depth: int = 0) -> None:
    """Refuse a credential-shaped name anywhere inside a supported structure.

    Checking only the top level of a document is not enough: a legitimate
    structured field such as a role's ``tool_constraints`` is a declared place to
    put a *limit*, so a credential hidden one level down inside it would be
    stored and echoed back while the schema itself looked satisfied. Every
    mapping key and list element of the supported structure is therefore
    inspected, and only the field *name* is reported — never its value, so a
    fixture value (or, in principle, a real one) is never echoed.

    A reference-shaped name such as ``secret_ref`` is a location, not material,
    and stays permitted.
    """
    if depth > MAXIMUM_SCHEMA_DEPTH:
        raise WorkflowError(
            "WORKFLOW_SCHEMA_INVALID",
            diagnostics=[
                located("WORKFLOW_SCHEMA_INVALID", file, "structure is nested too deeply")
            ],
        )
    if isinstance(value, Mapping):
        for name, nested in value.items():
            if _looks_like_credential(name):
                raise WorkflowError(
                    "WORKFLOW_CREDENTIAL_FIELD_REFUSED",
                    diagnostics=[
                        located(
                            "WORKFLOW_CREDENTIAL_FIELD_REFUSED",
                            f"{file}#",
                            "a declarative bundle references a secret; it never "
                            "carries credential material in a nested field "
                            f"(field: {str(name)[:48]})",
                        )
                    ],
                )
            _refuse_nested_credentials(nested, file=file, depth=depth + 1)
    elif isinstance(value, list):
        for item in value:
            _refuse_nested_credentials(item, file=file, depth=depth + 1)


def _refuse_nested_scripts(value: Any, *, file: str, depth: int = 0) -> None:
    """Refuse a script, expression or import declaration at any depth.

    A supported nested position takes declarative data. A key that names an
    action, a script, an import or an executable expression is not part of any
    supported structure, so it is refused by name rather than silently ignored —
    a caller must not be able to believe an embedded command took effect.
    """
    if depth > MAXIMUM_SCHEMA_DEPTH:
        raise WorkflowError(
            "WORKFLOW_SCHEMA_INVALID",
            diagnostics=[
                located("WORKFLOW_SCHEMA_INVALID", file, "structure is nested too deeply")
            ],
        )
    if isinstance(value, Mapping):
        for name, nested in value.items():
            if isinstance(name, str) and name.strip().casefold() in SCRIPT_FIELD_NAMES:
                raise WorkflowError(
                    "WORKFLOW_FIELD_UNSUPPORTED",
                    diagnostics=[
                        located(
                            "WORKFLOW_FIELD_UNSUPPORTED",
                            f"{file}#",
                            "a declarative structure carries no script, import or "
                            f"executable expression (field: {name[:48]})",
                        )
                    ],
                )
            _refuse_nested_scripts(nested, file=file, depth=depth + 1)
    elif isinstance(value, list):
        for item in value:
            _refuse_nested_scripts(item, file=file, depth=depth + 1)


#: Names that would embed an executable instruction where declarative data is
#: expected. A supported nested position carries limits, not code.
SCRIPT_FIELD_NAMES = frozenset(
    {
        "script",
        "scripts",
        "shell",
        "command",
        "commands",
        "exec",
        "eval",
        "expression",
        "expression_source",
        "import",
        "imports",
        "module",
        "modules",
        "entrypoint",
        "code",
        "python",
        "javascript",
    }
)

#: Bounded nesting for the recursive schema checks, so an unusual document is a
#: located refusal rather than a recursion error.
MAXIMUM_SCHEMA_DEPTH = 16


#: The independence conditions a role may declare. Each is a boolean declaration
#: carried into the compiled identity; the effective requirement is later taken
#: as the stricter of this and the project/delivery policy (architecture/09 §2).
INDEPENDENCE_REQUIREMENTS = frozenset(
    {"non_author", "new_context", "different_model_family"}
)

DELIVERY_GATES: Mapping[str, Mapping[str, Any]] = {
    "pr": {
        "required_checks": "project_policy",
        "independent_review": "required",
        "same_candidate_evidence": True,
        "publish_executor": "trusted_delivery_entrypoint",
    },
    "patch": {
        "required_checks": "project_policy",
        "independent_review": "required",
        "same_candidate_evidence": True,
        "publish_executor": "none",
    },
    "report": {
        "required_checks": "declared_only",
        "independent_review": "declared_only",
        "same_candidate_evidence": False,
        "publish_executor": "none",
    },
}


@dataclass(frozen=True, slots=True)
class CompiledRole:
    """One resolved RoleDefinition frozen into the template identity.

    The reference alone would not change when a responsibility or a tool
    constraint changes, so the role's own content digest is part of the compiled
    document. That makes a role edit visible in the compiled digest and in the
    diff instead of silently reusing the previous identity.
    """

    alias: str
    role_ref: str
    display_name: str
    digest: str
    responsibilities: tuple[str, ...]
    stop_conditions: tuple[str, ...]
    required_capabilities: tuple[str, ...]
    tool_constraints: Mapping[str, Any]
    input_contract_ref: str | None
    output_contract_ref: str | None
    instruction_template_ref: str | None
    instruction_template_digest: str | None
    independence_requirements: Mapping[str, Any]

    def as_document(self) -> dict[str, Any]:
        return {
            "alias": self.alias,
            "role_ref": self.role_ref,
            "display_name": self.display_name,
            "digest": self.digest,
            "responsibilities": list(self.responsibilities),
            "stop_conditions": list(self.stop_conditions),
            "required_capabilities": list(self.required_capabilities),
            "tool_constraints": dict(sorted(self.tool_constraints.items())),
            "input_contract_ref": self.input_contract_ref,
            "output_contract_ref": self.output_contract_ref,
            "instruction_template_ref": self.instruction_template_ref,
            # The template's own digest is part of the role's identity, so
            # changing an instruction changes the compiled template rather than
            # silently reusing the previous one.
            "instruction_template_digest": self.instruction_template_digest,
            # Declared independence conditions are a real constraint on who may
            # perform the work, so they are frozen into the identity rather than
            # discarded when they are added to the role file.
            "independence_requirements": dict(
                sorted(self.independence_requirements.items())
            ),
        }


@dataclass(frozen=True, slots=True)
class CompiledStep:
    step_id: str
    execution_kind_ref: str
    execution_kind_available: bool
    role_alias: str | None
    role_ref: str | None
    binding_ref: str | None
    binding_identity: Mapping[str, Any] | None
    depends_on: tuple[str, ...]
    inputs: Mapping[str, Any]
    output_contract_ref: str
    output_contract_kind: str
    required: bool
    condition: Mapping[str, Any] | None
    join: str
    location: str


@dataclass(frozen=True, slots=True)
class CompiledWorkflow:
    """The parameterised template; it never contains a concrete Run input."""

    schema_version: str
    workflow_id: str
    revision: int
    delivery_kind: str
    input_contract_ref: str
    roles: Mapping[str, str]
    role_definitions: tuple[CompiledRole, ...]
    bindings: Mapping[str, str]
    binding_index: Mapping[str, Mapping[str, Any]]
    steps: tuple[CompiledStep, ...]
    completion: Mapping[str, Any]
    scheduling: Mapping[str, Any] | None
    rework: tuple[Mapping[str, Any], ...]
    expansions: tuple[Mapping[str, Any], ...]
    delivery_gate: Mapping[str, Any]
    declared_inputs: tuple[str, ...]
    available_execution_kinds: tuple[str, ...]
    unavailable_execution_kinds: tuple[str, ...]
    diagnostics: tuple[Diagnostic, ...]
    compiler_revision: int = COMPILER_REVISION
    compiler_identity: str = COMPILER_IDENTITY
    bundle_digest: str = ""
    role_digest: str = ""

    def as_document(self) -> dict[str, Any]:
        """The exact structure the compiled digest is taken over."""
        return {
            "schema_version": self.schema_version,
            "compiler_identity": self.compiler_identity,
            "workflow": {
                "id": self.workflow_id,
                "revision": self.revision,
                "delivery_kind": self.delivery_kind,
                "input_contract_ref": self.input_contract_ref,
            },
            "roles": dict(sorted(self.roles.items())),
            "role_definitions": [role.as_document() for role in self.role_definitions],
            "bindings": dict(sorted(self.bindings.items())),
            "resolved_bindings": {
                reference: dict(resolved)
                for reference, resolved in sorted(self.binding_index.items())
            },
            "declared_inputs": list(self.declared_inputs),
            "steps": [self._step_document(step) for step in self.steps],
            "completion": dict(self.completion),
            "scheduling": dict(self.scheduling) if self.scheduling else None,
            "rework": [dict(rule) for rule in self.rework],
            "expansions": [dict(item) for item in self.expansions],
            "delivery_gate": dict(self.delivery_gate),
            "execution_kinds": {
                "available": list(self.available_execution_kinds),
                "unavailable": list(self.unavailable_execution_kinds),
            },
        }

    @staticmethod
    def _step_document(step: CompiledStep) -> dict[str, Any]:
        return {
            "step_id": step.step_id,
            "execution_kind_ref": step.execution_kind_ref,
            "execution_kind_available": step.execution_kind_available,
            # The alias the author selected is recorded on the step itself, so
            # two steps naming the same role definition through different aliases
            # (and therefore different bindings) are not the same identity.
            "role_alias": step.role_alias,
            "role_ref": step.role_ref,
            "binding_ref": step.binding_ref,
            "binding_identity": (
                dict(step.binding_identity) if step.binding_identity else None
            ),
            "depends_on": list(step.depends_on),
            "inputs": step.inputs,
            "output_contract_ref": step.output_contract_ref,
            "output_contract_kind": step.output_contract_kind,
            "required": step.required,
            "condition": step.condition,
            "join": step.join,
        }

    @property
    def compiled_digest(self) -> str:
        """Digest the template only; no Run input and no dynamic task instance."""
        return content_digest(self.as_document())

    @property
    def readiness(self) -> str:
        return "executable" if self.executable else "unavailable_capability"

    @property
    def reviewable(self) -> bool:
        """Whether the configuration itself is internally sound.

        A compiled template always is: a configuration the compiler cannot
        accept is raised as a located rejection and never returned. What may
        still be missing is a *capability*, which is reported separately. This is
        the documented split between "can be reviewed" and "can be executed"
        (docs/architecture/10 §5).
        """
        return True

    @property
    def executable(self) -> bool:
        """Whether every referenced execution kind has a real adapter here."""
        return not self.unavailable_execution_kinds

    @property
    def capability_gaps(self) -> tuple[Diagnostic, ...]:
        """Every registered-but-unimplemented kind this template references."""
        return self.diagnostics


class _Index:
    """Resolved identities the reference checks consult."""

    def __init__(self, declared_inputs: Sequence[str]) -> None:
        self.declared_inputs = frozenset(declared_inputs)
        self.steps: dict[str, CompiledStep] = {}


def compile_workflow(
    manifest: Mapping[str, Any],
    workflow: Mapping[str, Any],
    roles: Mapping[str, Mapping[str, Any]],
    *,
    bundle_digest: str,
    role_digest: str,
    template_refs: Sequence[str] = (),
    template_digests: Mapping[str, str] | None = None,
    binding_index: Mapping[str, Mapping[str, Any]] | None = None,
) -> CompiledWorkflow:
    """Compile one validated bundle into a parameterised workflow template."""
    del template_refs  # the bundle hashes them; identities arrive via template_digests
    template_digests = template_digests or {}
    if not isinstance(manifest, Mapping):
        raise _schema("manifest.json", "#/", "expected a mapping")
    if not isinstance(workflow, Mapping):
        raise _schema("workflow.yaml", "#/", "expected a mapping")
    _refuse_unknown_fields(workflow, WORKFLOW_FIELDS, file="workflow.yaml")
    _refuse_nested_credentials(workflow, file="workflow.yaml")
    _refuse_nested_scripts(workflow, file="workflow.yaml")

    declared_inputs = tuple(sorted(_declared_inputs(workflow.get("inputs"))))
    available_roles = _roles(roles, template_digests)
    chosen = _role_map(workflow.get("roles"), available_roles)
    role_definitions = _role_definitions(chosen, available_roles, template_digests)
    bindings = _bindings(workflow.get("bindings"), chosen)
    raw_steps = _steps(workflow)

    input_contract_ref = _reference(
        workflow.get("input_contract"), file="workflow.yaml", pointer="#/input_contract"
    )
    _require_registered_contract(input_contract_ref, "workflow.yaml#/input_contract")

    compiled_steps, kinds, raw_inputs = _compile_steps(raw_steps, chosen)
    _check_dependencies(compiled_steps)
    compiled_steps = _check_references(
        compiled_steps, raw_inputs, declared_inputs, bindings, binding_index or {}
    )
    _check_condition_dependencies(compiled_steps)
    _check_bindings_resolved(compiled_steps, binding_index or {})
    completion = _completion(workflow.get("completion"), compiled_steps)
    delivery_kind = _delivery_kind(workflow.get("delivery_kind"))
    rework = _rework(workflow.get("rework"), compiled_steps)
    expansions, scheduling, dynamic_kinds = _scheduling(
        workflow.get("scheduling"),
        chosen,
        declared_inputs,
        bindings,
        binding_index or {},
    )
    # The delivery target is checked against both the static steps and the
    # declared dynamic boundaries, so an expansion cannot smuggle in a
    # remote-delivery capability the target does not authorise.
    _check_delivery_gate(delivery_kind, compiled_steps, completion, expansions)
    kinds = [*kinds, *dynamic_kinds]

    available = sorted({kind.ref for kind in kinds if kind.available})
    unavailable = sorted({kind.ref for kind in kinds if not kind.available})
    diagnostics: list[Diagnostic] = []
    for kind in kinds:
        if not kind.available:
            for code in kind.reason_codes:
                diagnostics.append(
                    located(
                        code,
                        "workflow.yaml#/steps",
                        f"{kind.ref} is registered without an adapter in this build",
                    )
                )

    return CompiledWorkflow(
        schema_version=str(workflow.get("schema_version", SCHEMA_VERSION)),
        workflow_id=_text(workflow.get("id"), file="workflow.yaml", pointer="#/id"),
        revision=_revision(workflow.get("revision"), file="workflow.yaml", pointer="#/revision"),
        delivery_kind=delivery_kind,
        input_contract_ref=input_contract_ref,
        roles=dict(sorted(chosen.items())),
        role_definitions=role_definitions,
        bindings=bindings,
        binding_index={key: dict(value) for key, value in (binding_index or {}).items()},
        steps=tuple(compiled_steps),
        completion=completion,
        scheduling=scheduling,
        rework=tuple(rework),
        expansions=tuple(expansions),
        delivery_gate=dict(DELIVERY_GATES[delivery_kind]),
        declared_inputs=declared_inputs,
        available_execution_kinds=tuple(available),
        unavailable_execution_kinds=tuple(unavailable),
        diagnostics=tuple(diagnostics),
        bundle_digest=bundle_digest,
        role_digest=role_digest,
    )


def _schema(file: str, pointer: str, detail: str) -> WorkflowError:
    return WorkflowError(
        "WORKFLOW_SCHEMA_INVALID",
        diagnostics=[located("WORKFLOW_SCHEMA_INVALID", f"{file}{pointer}", detail)],
    )


def _revision(value: object, *, file: str, pointer: str) -> int:
    if type(value) is not int or not 1 <= value <= 1_000_000:
        raise _schema(file, pointer, "invalid revision")
    return value


def _require_registered_contract(contract_ref: str, location: str) -> None:
    if not contract_is_registered(contract_ref):
        raise WorkflowError(
            "WORKFLOW_CONTRACT_UNREGISTERED",
            diagnostics=[
                located(
                    "WORKFLOW_CONTRACT_UNREGISTERED",
                    location,
                    "contract is not registered in this build",
                )
            ],
        )


def _roles(
    roles: Mapping[str, Mapping[str, Any]],
    instruction_digests: Mapping[str, str],
) -> dict[str, dict[str, Any]]:
    """Index the bundle's own role files by their fixed ``role:id@revision``."""
    indexed: dict[str, dict[str, Any]] = {}
    for name, document in roles.items():
        if not isinstance(document, Mapping):
            raise _schema(name, "#/", "role file must be a mapping")
        _refuse_unknown_fields(document, ROLE_FIELDS, file=name)
        # A supported structured position is checked all the way down, so a
        # credential or an embedded script cannot hide inside a legitimate field
        # such as ``tool_constraints``.
        _refuse_nested_credentials(document, file=name)
        _refuse_nested_scripts(document, file=name)
        role_id = _text(document.get("id"), file=name, pointer="#/id")
        pinned_revision = _revision(
            document.get("revision"), file=name, pointer="#/revision"
        )
        reference = f"role:{role_id}@{pinned_revision}"
        if reference in indexed:
            raise WorkflowError(
                "WORKFLOW_ROLE_DUPLICATE",
                diagnostics=[
                    located("WORKFLOW_ROLE_DUPLICATE", name, "two role files declare one revision")
                ],
            )
        for field_name in ("responsibilities", "stop_conditions", "required_capabilities"):
            declared = document.get(field_name, [])
            if not isinstance(declared, list) or not all(
                isinstance(item, str) for item in declared
            ):
                raise _schema(name, f"#/{field_name}", "must be a list of text")
        if not isinstance(document.get("tool_constraints", {}), dict):
            raise _schema(name, "#/tool_constraints", "must be a mapping of declared limits")
        # Declared independence requirements are a real constraint on who may do
        # the work, so an unsupported shape is refused and a supported one is
        # carried into the frozen role identity rather than discarded.
        independence = document.get("independence_requirements", {})
        if not isinstance(independence, Mapping):
            raise _schema(
                name,
                "#/independence_requirements",
                "must be a mapping of declared requirements",
            )
        for key, requirement in independence.items():
            if not isinstance(key, str) or key not in INDEPENDENCE_REQUIREMENTS:
                raise _schema(
                    name,
                    "#/independence_requirements",
                    "an independence requirement must be one of the declared set",
                )
            if not isinstance(requirement, bool):
                raise _schema(
                    name,
                    f"#/independence_requirements/{key}",
                    "an independence requirement is a boolean declaration",
                )
        for contract_field in ("input_contract_ref", "output_contract_ref"):
            declared = document.get(contract_field)
            if declared is None:
                continue
            pinned = _reference(declared, file=name, pointer=f"#/{contract_field}")
            if not contract_is_registered(pinned):
                raise WorkflowError(
                    "WORKFLOW_CONTRACT_UNREGISTERED",
                    diagnostics=[
                        located(
                            "WORKFLOW_CONTRACT_UNREGISTERED",
                            f"{name}#/{contract_field}",
                            "a role contract must be a registered supported contract",
                        )
                    ],
                )
        template = document.get("instruction_template_ref")
        if template is not None:
            if not isinstance(template, str) or template not in REGISTERED_INSTRUCTION_TEMPLATES:
                raise WorkflowError(
                    "WORKFLOW_TEMPLATE_UNRESOLVED",
                    diagnostics=[
                        located(
                            "WORKFLOW_TEMPLATE_UNRESOLVED",
                            f"{name}#/instruction_template_ref",
                            "an instruction template must be one of the supported templates",
                        )
                    ],
                )
            if template not in instruction_digests:
                # A referenced template must be a real file of this bundle. A
                # supported name that was never supplied would otherwise resolve
                # with no digest, letting a role claim an instruction whose bytes
                # no one has stored or verified (docs/architecture/09 §2).
                raise WorkflowError(
                    "WORKFLOW_TEMPLATE_MISSING",
                    diagnostics=[
                        located(
                            "WORKFLOW_TEMPLATE_MISSING",
                            template,
                            "the referenced instruction template is not part of this bundle",
                        )
                    ],
                )
        indexed[reference] = dict(document)
    return indexed


def _role_definitions(
    chosen: Mapping[str, str],
    available: Mapping[str, Mapping[str, Any]],
    instruction_digests: Mapping[str, str],
) -> tuple[CompiledRole, ...]:
    """Freeze every referenced role's content into the template identity."""
    frozen: list[CompiledRole] = []
    for alias, reference in sorted(chosen.items()):
        document = available[reference]
        frozen.append(
            CompiledRole(
                alias=alias,
                role_ref=reference,
                display_name=_text(
                    document.get("display_name", alias), file="workflow.yaml", pointer="#/roles"
                ),
                digest=content_digest(
                    {
                        "id": document["id"],
                        "revision": document["revision"],
                        "responsibilities": list(document.get("responsibilities", [])),
                        "stop_conditions": list(document.get("stop_conditions", [])),
                        "required_capabilities": list(
                            document.get("required_capabilities", [])
                        ),
                        "tool_constraints": document.get("tool_constraints", {}),
                        "input_contract_ref": document.get("input_contract_ref"),
                        "output_contract_ref": document.get("output_contract_ref"),
                        "instruction_template_ref": document.get("instruction_template_ref"),
                        "instruction": instruction_digests.get(
                            str(document.get("instruction_template_ref"))
                        ),
                        "independence_requirements": document.get(
                            "independence_requirements", {}
                        ),
                    }
                ),
                responsibilities=tuple(document.get("responsibilities", [])),
                stop_conditions=tuple(document.get("stop_conditions", [])),
                required_capabilities=tuple(document.get("required_capabilities", [])),
                tool_constraints=dict(document.get("tool_constraints", {})),
                input_contract_ref=document.get("input_contract_ref"),
                output_contract_ref=document.get("output_contract_ref"),
                instruction_template_ref=document.get("instruction_template_ref"),
                instruction_template_digest=instruction_digests.get(
                    str(document.get("instruction_template_ref"))
                ),
                independence_requirements=dict(
                    document.get("independence_requirements", {})
                ),
            )
        )
    return tuple(frozen)


def _role_map(
    declared: object, available: Mapping[str, Mapping[str, Any]],
) -> dict[str, str]:
    """Bind each workflow role alias to one fixed RoleDefinition revision.

    The workflow declares aliases (``researcher``) and their references
    (``role:researcher@1``). The alias is the key; the reference is what must
    resolve to a role file in this bundle.
    """
    if declared is None:
        declared = {}
    if not isinstance(declared, Mapping):
        raise _schema("workflow.yaml", "#/roles", "expected a mapping")
    chosen: dict[str, str] = {}
    for name, reference in declared.items():
        alias = _text(name, file="workflow.yaml", pointer="#/roles")
        pinned = _reference(reference, file="workflow.yaml", pointer=f"#/roles/id={alias}")
        if pinned not in available:
            raise WorkflowError(
                "WORKFLOW_ROLE_UNRESOLVED",
                diagnostics=[
                    located(
                        "WORKFLOW_ROLE_UNRESOLVED",
                        f"workflow.yaml#/roles/id={alias}",
                        "the referenced role revision is not part of this bundle",
                        step_id=alias,
                    )
                ],
            )
        chosen[alias] = pinned
    return chosen


def _bindings(declared: object, roles: Mapping[str, str]) -> dict[str, str]:
    """Resolve a role's fixed execution binding, never a floating source."""
    if declared is None:
        return {}
    if not isinstance(declared, Mapping):
        raise _schema("workflow.yaml", "#/bindings", "expected a mapping")
    bindings: dict[str, str] = {}
    for name, reference in declared.items():
        alias = _text(name, file="workflow.yaml", pointer="#/bindings")
        if alias not in roles:
            raise WorkflowError(
                "WORKFLOW_ROLE_UNRESOLVED",
                diagnostics=[
                    located(
                        "WORKFLOW_ROLE_UNRESOLVED",
                        f"workflow.yaml#/bindings/id={alias}",
                        "binding names a role this workflow does not declare",
                    )
                ],
            )
        bindings[alias] = _reference(
            reference, file="workflow.yaml", pointer=f"#/bindings/id={alias}"
        )
    return dict(sorted(bindings.items()))


def _steps(workflow: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    declared = workflow.get("steps")
    if not isinstance(declared, list) or not declared:
        raise _schema("workflow.yaml", "#/steps", "steps are required")
    for index, step in enumerate(declared):
        if not isinstance(step, Mapping):
            raise _schema("workflow.yaml", f"#/steps/{index}", "expected a mapping")
    return list(declared)


def _compile_steps(
    steps: Sequence[Mapping[str, Any]],
    roles: Mapping[str, str],
) -> tuple[list[CompiledStep], list[ExecutionKind], dict[str, Mapping[str, Any]]]:
    """Compile the declared steps, deferring cross-step reference resolution.

    Inputs are returned separately rather than attached to the step, because an
    artifact reference can only be checked once every step identity is known.
    """
    compiled: list[CompiledStep] = []
    kinds: list[ExecutionKind] = []
    raw_inputs: dict[str, Mapping[str, Any]] = {}
    seen: set[str] = set()
    for index, step in enumerate(steps):
        pointer = f"#/steps/{index}"
        step_id = _text(step.get("id"), file="workflow.yaml", pointer=f"{pointer}/id")
        if step_id in seen:
            raise WorkflowError(
                "WORKFLOW_STEP_ID_DUPLICATE",
                diagnostics=[
                    located(
                        "WORKFLOW_STEP_ID_DUPLICATE",
                        f"workflow.yaml#/steps/id={step_id}",
                        "step identity is declared twice",
                        step_id=step_id,
                    )
                ],
            )
        seen.add(step_id)
        kind_ref = _reference(
            step.get("execution_kind"), file="workflow.yaml", pointer=f"{pointer}/execution_kind"
        )
        location = f"workflow.yaml#/steps/id={step_id}"
        kind = kind_for(kind_ref, location=f"{location}/execution_kind")
        kinds.append(kind)
        role_alias, role_ref = _step_role(step, roles, kind, location, step_id)
        depends_on = _dependencies(step, location, step_id)
        output_contract_ref = _reference(
            step.get("output_contract"),
            file="workflow.yaml",
            pointer=f"{pointer}/output_contract",
        )
        output_kind = _check_output_contract(kind, output_contract_ref, location, step_id)
        condition = _condition(step.get("condition"), location, step_id)
        join = step.get("join", "all_required")
        if join not in {"all_required", "any_required", "all_committed"}:
            raise _schema(location, "/join", "unsupported join policy")
        declared_inputs = step.get("inputs", {})
        if not isinstance(declared_inputs, Mapping):
            raise _schema(location, "/inputs", "inputs must be a mapping of typed bindings")
        raw_inputs[step_id] = dict(declared_inputs)
        compiled.append(
            CompiledStep(
                step_id=step_id,
                execution_kind_ref=kind.ref,
                execution_kind_available=kind.available,
                role_alias=role_alias,
                role_ref=role_ref,
                binding_ref=None,
                binding_identity=None,
                depends_on=depends_on,
                inputs={},
                output_contract_ref=output_contract_ref,
                output_contract_kind=output_kind,
                required=step.get("required", True) is not False,
                condition=condition,
                join=join,
                location=location,
            )
        )
    return compiled, kinds, raw_inputs


def _step_role(
    step: Mapping[str, Any],
    roles: Mapping[str, str],
    kind: ExecutionKind,
    location: str,
    step_id: str,
) -> tuple[str | None, str | None]:
    """The selected alias and the fixed role revision it resolves to."""
    declared = step.get("role")
    if declared is None:
        if kind.requires_role:
            raise WorkflowError(
                "WORKFLOW_ROLE_REQUIRED",
                diagnostics=[
                    located(
                        "WORKFLOW_ROLE_REQUIRED",
                        f"{location}/role",
                        f"{kind.ref} must name a role",
                        step_id=step_id,
                    )
                ],
            )
        return None, None
    alias = _text(declared, file="workflow.yaml", pointer=f"{location}/role")
    if alias not in roles:
        raise WorkflowError(
            "WORKFLOW_ROLE_UNRESOLVED",
            diagnostics=[
                located(
                    "WORKFLOW_ROLE_UNRESOLVED",
                    f"{location}/role",
                    "role is not one this workflow declares",
                    step_id=step_id,
                )
            ],
        )
    return alias, roles[alias]


def _dependencies(step: Mapping[str, Any], location: str, step_id: str) -> tuple[str, ...]:
    declared = step.get("depends_on", [])
    if not isinstance(declared, list) or not all(isinstance(item, str) for item in declared):
        raise _schema(location, "/depends_on", "dependencies must be a list of step identities")
    if len(set(declared)) != len(declared):
        raise WorkflowError(
            "WORKFLOW_DEPENDENCY_DUPLICATE",
            diagnostics=[
                located(
                    "WORKFLOW_DEPENDENCY_DUPLICATE",
                    f"{location}/depends_on",
                    "a dependency is declared twice",
                    step_id=step_id,
                )
            ],
        )
    if step_id in declared:
        raise WorkflowError(
            "WORKFLOW_DEPENDENCY_SELF",
            diagnostics=[
                located(
                    "WORKFLOW_DEPENDENCY_SELF",
                    f"{location}/depends_on",
                    "a step cannot depend on itself",
                    step_id=step_id,
                )
            ],
        )
    return tuple(declared)


def _check_dependencies(steps: Sequence[CompiledStep]) -> None:
    """Every dependency resolves, and the ordinary dependency graph is acyclic."""
    known = {step.step_id for step in steps}
    diagnostics: list[Diagnostic] = []
    for step in steps:
        for dependency in step.depends_on:
            if dependency not in known:
                diagnostics.append(
                    located(
                        "WORKFLOW_DEPENDENCY_UNRESOLVED",
                        f"{step.location}/depends_on",
                        f"dependency {dependency} is not defined in this workflow",
                        step_id=step.step_id,
                    )
                )
    if diagnostics:
        raise WorkflowError("WORKFLOW_DEPENDENCY_UNRESOLVED", diagnostics=diagnostics)
    graph = {step.step_id: list(step.depends_on) for step in steps}
    cycle = _find_cycle(graph)
    if cycle is not None:
        raise WorkflowError(
            "WORKFLOW_DEPENDENCY_CYCLE",
            diagnostics=[
                located(
                    "WORKFLOW_DEPENDENCY_CYCLE",
                    "workflow.yaml#/steps",
                    "dependency cycle: " + " -> ".join(cycle[:8]),
                    step_id=cycle[0],
                )
            ],
        )


def _find_cycle(graph: Mapping[str, Sequence[str]]) -> list[str] | None:
    """Iterative depth-first search, so a deep graph cannot exhaust the stack."""
    state: dict[str, int] = {}
    for origin in graph:
        if state.get(origin):
            continue
        stack: list[tuple[str, int]] = [(origin, 0)]
        path: list[str] = [origin]
        state[origin] = 1
        while stack:
            node, index = stack[-1]
            edges = graph[node]
            if index >= len(edges):
                state[node] = 2
                stack.pop()
                path.pop()
                continue
            stack[-1] = (node, index + 1)
            target = edges[index]
            flag = state.get(target, 0)
            if flag == 1:
                return [*path[path.index(target) :], target]
            if flag == 0:
                state[target] = 1
                stack.append((target, 0))
                path.append(target)
    return None


# ------------------------------------------------------------------ references


def _check_references(
    steps: Sequence[CompiledStep],
    raw_inputs: Mapping[str, Mapping[str, Any]],
    declared_inputs: Sequence[str],
    bindings: Mapping[str, str],
    binding_index: Mapping[str, Mapping[str, Any]],
) -> list[CompiledStep]:
    """Resolve every input binding and condition fact against real declarations."""
    index = _Index(declared_inputs)
    index.steps = {step.step_id: step for step in steps}
    resolved: list[CompiledStep] = []
    for step in steps:
        kind = kind_for(step.execution_kind_ref, location=step.location)
        inputs = _resolve_inputs(step, raw_inputs[step.step_id], kind, index)
        condition = (
            _resolve_condition(step.condition, step=step, index=index)
            if step.condition is not None
            else None
        )
        binding_ref = bindings.get(step.role_alias or "")
        resolved.append(
            CompiledStep(
                step_id=step.step_id,
                execution_kind_ref=step.execution_kind_ref,
                execution_kind_available=step.execution_kind_available,
                role_alias=step.role_alias,
                role_ref=step.role_ref,
                binding_ref=binding_ref,
                binding_identity=(
                    dict(binding_index[binding_ref])
                    if binding_ref and binding_ref in binding_index
                    else None
                ),
                depends_on=step.depends_on,
                inputs=inputs,
                output_contract_ref=step.output_contract_ref,
                output_contract_kind=step.output_contract_kind,
                required=step.required,
                condition=condition,
                join=step.join,
                location=step.location,
            )
        )
    return resolved


def _check_bindings_resolved(
    steps: Sequence[CompiledStep], binding_index: Mapping[str, Mapping[str, Any]]
) -> None:
    """A declared ``binding:`` reference must have been resolved to a revision."""
    for step in steps:
        if step.binding_ref is None:
            continue
        if step.binding_ref.startswith("binding:") and step.binding_ref not in binding_index:
            raise WorkflowError(
                "WORKFLOW_GATEWAY_BINDING_UNRESOLVED",
                diagnostics=[
                    located(
                        "WORKFLOW_GATEWAY_BINDING_UNRESOLVED",
                        f"{step.location}/role",
                        "the selected role's binding was not resolved to a fixed revision",
                        step_id=step.step_id,
                    )
                ],
            )


def _check_condition_dependencies(steps: Sequence[CompiledStep]) -> None:
    """A condition's fact source must be an ordinary dependency of that step.

    Reading ``result.x`` or ``decision.x`` from a step that is not declared in
    ``depends_on`` would create an implicit edge. Implicit edges are what make a
    cycle invisible to the ordinary dependency check, so they are refused here
    rather than being allowed to produce an unsatisfiable graph.
    """
    known = {step.step_id for step in steps}
    for step in steps:
        for fact in _condition_facts(step.condition):
            producer = _fact_producer(fact)
            if producer is None or producer == step.step_id:
                if producer == step.step_id and producer in known:
                    raise WorkflowError(
                        "WORKFLOW_CONDITION_SELF_REFERENCE",
                        diagnostics=[
                            located(
                                "WORKFLOW_CONDITION_SELF_REFERENCE",
                                f"{step.location}/condition",
                                "a step cannot read its own result",
                                step_id=step.step_id,
                            )
                        ],
                    )
                continue
            if producer in known and producer not in step.depends_on:
                raise WorkflowError(
                    "WORKFLOW_CONDITION_FACT_DEPENDENCY_MISSING",
                    diagnostics=[
                        located(
                            "WORKFLOW_CONDITION_FACT_DEPENDENCY_MISSING",
                            f"{step.location}/condition",
                            f"reading {producer} requires declaring it in depends_on",
                            step_id=step.step_id,
                        )
                    ],
                )


def _condition_facts(condition: Mapping[str, Any] | None) -> list[str]:
    """Every fact named anywhere in one compiled condition tree."""
    if not condition:
        return []
    ((operator, operand),) = condition.items()
    if operator in {"all", "any"}:
        return [fact for item in operand for fact in _condition_facts(item)]
    if operator == "not":
        return _condition_facts(operand)
    return [str(operand[0])]


def _fact_producer(fact: str) -> str | None:
    """The step a ``result.x.y`` or ``decision.x`` fact reads, if any."""
    parts = fact.split(".")
    if fact.startswith("result."):
        return parts[1] if len(parts) == 3 else None
    if fact.startswith("decision."):
        return parts[1] if len(parts) == 2 else None
    return None


def _resolve_inputs(
    step: CompiledStep,
    declared: Mapping[str, Any],
    kind: ExecutionKind,
    index: _Index,
) -> dict[str, Any]:
    """Bind a step's declared inputs, resolving artifact references for real.

    The compiled ``inputs`` keep the symbolic references the author wrote, which
    is what a review table must show. ``resolved_inputs`` is the same binding
    reduced to the artifact contracts those references actually produce, which is
    what the trusted registry contract is checked against — the same check the
    adapter applies to a runtime value.
    """
    if not kind.inputs:
        if declared:
            raise WorkflowError(
                "WORKFLOW_INPUT_INVALID",
                diagnostics=[
                    located(
                        "WORKFLOW_INPUT_INVALID",
                        f"{step.location}/inputs",
                        f"{kind.ref} accepts no declared input",
                        step_id=step.step_id,
                    )
                ],
            )
        return {}
    checked = validate_bindings(
        kind.inputs,
        _normalized_inputs(step, declared, index),
        enforce_contract=True,
        location=f"{step.location}/inputs",
    )
    # The compiled table keeps every binding exactly as the author wrote it,
    # including the ``literal:`` marker. Reducing a literal to its payload here
    # would make ``first.output`` and ``literal:first.output`` compile to the same
    # binding — and therefore the same template identity and an empty diff —
    # even though one is a reference and the other is data. The marker is syntax
    # that carries meaning, so it stays part of the compiled binding and is
    # decoded exactly once, in the adapter that consumes the value.
    return {
        name: _compiled_value(value)
        for name, value in dict(sorted(declared.items())).items()
        if name in checked
    }


def _compiled_value(value: Any) -> Any:
    if isinstance(value, list):
        return [_compiled_value(item) for item in value]
    return value


def _normalized_inputs(
    step: CompiledStep, declared: Mapping[str, Any], index: _Index
) -> dict[str, Any]:
    """Reduce declared bindings to the shapes the registry contract compares.

    A reference becomes the output contract its producer declares, a list stays a
    list of those, and a marked literal stays as written. Unknown input names are
    left in place so the contract check reports them by name.
    """
    kind = kind_for(step.execution_kind_ref, location=step.location)
    accepted = {item.name: item for item in kind.inputs}
    normalized: dict[str, Any] = {}
    for name, value in declared.items():
        contract = accepted.get(name)
        if contract is None:
            normalized[name] = value
            continue
        location = f"{step.location}/inputs/id={name}"
        if isinstance(value, list):
            normalized[name] = [
                _contract_of(item, step=step, index=index, location=location) for item in value
            ]
        elif isinstance(value, str) and contract.accepts:
            normalized[name] = _contract_of(
                value, step=step, index=index, location=location
            )
        else:
            normalized[name] = value
    return normalized


def _contract_of(value: Any, *, step: CompiledStep, index: _Index, location: str) -> str:
    """The artifact contract one binding value ends up carrying at run time.

    A marked ``literal:`` value is data of the default text contract; an
    unmarked string is a reference and must resolve to a real producer, so a
    mistyped reference is never silently treated as prose.
    """
    if not isinstance(value, str):
        raise WorkflowError(
            "WORKFLOW_INPUT_INVALID",
            diagnostics=[
                located(
                    "WORKFLOW_INPUT_INVALID",
                    location,
                    "a reference is text",
                    step_id=step.step_id,
                )
            ],
        )
    if literal_text(value) is not None:
        if not resolved_text(value):
            # An empty literal compiles to no data, which the adapter refuses.
            # Refusing it here keeps compilation and execution the same contract,
            # so a known-invalid concrete input is never called executable.
            raise WorkflowError(
                "WORKFLOW_INPUT_INVALID",
                diagnostics=[
                    located(
                        "WORKFLOW_INPUT_INVALID",
                        location,
                        "a marked literal must carry data",
                        step_id=step.step_id,
                    )
                ],
            )
        return "text@1"
    if value.startswith(("requirement.", "input.")):
        if value not in index.declared_inputs:
            raise WorkflowError(
                "WORKFLOW_INPUT_REFERENCE_UNRESOLVED",
                diagnostics=[
                    located(
                        "WORKFLOW_INPUT_REFERENCE_UNRESOLVED",
                        location,
                        f"{value} is not a declared workflow input",
                        step_id=step.step_id,
                    )
                ],
            )
        return "text@1"
    return _artifact_reference(value, step=step, index=index, location=location)


def _artifact_reference(
    value: str, *, step: CompiledStep, index: _Index, location: str
) -> str:
    """Resolve one ``X.output`` reference to a real, depended-on producer."""
    if not 1 <= len(value) <= 512 or not value.strip():
        raise WorkflowError(
            "WORKFLOW_INPUT_REFERENCE_INVALID",
            diagnostics=[
                located(
                    "WORKFLOW_INPUT_REFERENCE_INVALID",
                    location,
                    "a reference is a bounded identity",
                    step_id=step.step_id,
                )
            ],
        )
    if "/" in value or "\\" in value:
        # A path-shaped input would be a host location; the compiler accepts an
        # identity, not a path, so nothing here can read a file.
        raise WorkflowError(
            "WORKFLOW_INPUT_PATH_UNSUPPORTED",
            diagnostics=[
                located(
                    "WORKFLOW_INPUT_PATH_UNSUPPORTED",
                    location,
                    "input references name a declared identity, never a path",
                    step_id=step.step_id,
                )
            ],
        )
    if not value.endswith(".output"):
        raise WorkflowError(
            "WORKFLOW_INPUT_REFERENCE_INVALID",
            diagnostics=[
                located(
                    "WORKFLOW_INPUT_REFERENCE_INVALID",
                    location,
                    "an artifact reference names step_id.output",
                    step_id=step.step_id,
                )
            ],
        )
    producer_id = value[: -len(".output")]
    producer = index.steps.get(producer_id)
    if producer is None:
        raise WorkflowError(
            "WORKFLOW_INPUT_REFERENCE_UNRESOLVED",
            diagnostics=[
                located(
                    "WORKFLOW_INPUT_REFERENCE_UNRESOLVED",
                    location,
                    f"{producer_id} is not a step in this workflow",
                    step_id=step.step_id,
                )
            ],
        )
    if producer_id == step.step_id:
        raise WorkflowError(
            "WORKFLOW_INPUT_SELF_REFERENCE",
            diagnostics=[
                located(
                    "WORKFLOW_INPUT_SELF_REFERENCE",
                    location,
                    "a step cannot consume its own output",
                    step_id=step.step_id,
                )
            ],
        )
    if producer_id not in step.depends_on:
        raise WorkflowError(
            "WORKFLOW_INPUT_DEPENDENCY_MISSING",
            diagnostics=[
                located(
                    "WORKFLOW_INPUT_DEPENDENCY_MISSING",
                    location,
                    f"consuming {producer_id}.output requires declaring it in depends_on",
                    step_id=step.step_id,
                )
            ],
        )
    return producer.output_contract_ref


def _resolve_condition(
    condition: Mapping[str, Any], *, step: CompiledStep, index: _Index
) -> dict[str, Any]:
    """Re-resolve every fact in a compiled condition against real declarations."""
    ((operator, operand),) = condition.items()
    if operator in {"all", "any"}:
        return {
            operator: [
                _resolve_condition(item, step=step, index=index) for item in operand
            ]
        }
    if operator == "not":
        return {"not": _resolve_condition(operand, step=step, index=index)}
    fact = operand[0]
    fact_type = _check_fact(fact, step=step, index=index, location=f"{step.location}/condition")
    _check_operand_type(operator, fact_type, operand[1], f"{step.location}/condition", step.step_id)
    return {operator: [fact, operand[1]]}


def _check_fact(fact: str, *, step: CompiledStep, index: _Index, location: str) -> str:
    """A fact must name a real field, and return that field's declared type."""
    if fact.startswith(("requirement.", "input.")):
        if fact not in index.declared_inputs:
            raise WorkflowError(
                "WORKFLOW_CONDITION_FACT_UNRESOLVED",
                diagnostics=[
                    located(
                        "WORKFLOW_CONDITION_FACT_UNRESOLVED",
                        location,
                        f"{fact} is not a declared workflow input",
                        step_id=step.step_id,
                    )
                ],
            )
        return "text"
    if fact.startswith("result."):
        parts = fact.split(".")
        if len(parts) != 3:
            raise WorkflowError(
                "WORKFLOW_CONDITION_FACT_INVALID",
                diagnostics=[
                    located(
                        "WORKFLOW_CONDITION_FACT_INVALID",
                        location,
                        "a result fact names result.step_id.field",
                        step_id=step.step_id,
                    )
                ],
            )
        producer_id, field = parts[1], parts[2]
        producer = index.steps.get(producer_id)
        if producer is None:
            raise WorkflowError(
                "WORKFLOW_CONDITION_FACT_UNRESOLVED",
                diagnostics=[
                    located(
                        "WORKFLOW_CONDITION_FACT_UNRESOLVED",
                        location,
                        f"{producer_id} is not a step in this workflow",
                        step_id=step.step_id,
                    )
                ],
            )
        if producer_id not in step.depends_on:
            raise WorkflowError(
                "WORKFLOW_CONDITION_FACT_DEPENDENCY_MISSING",
                diagnostics=[
                    located(
                        "WORKFLOW_CONDITION_FACT_DEPENDENCY_MISSING",
                        location,
                        f"reading {producer_id} requires declaring it in depends_on",
                        step_id=step.step_id,
                    )
                ],
            )
        field_type = contract_field_type(producer.output_contract_ref, field)
        if field_type is None:
            raise WorkflowError(
                "WORKFLOW_CONDITION_FIELD_UNRESOLVED",
                diagnostics=[
                    located(
                        "WORKFLOW_CONDITION_FIELD_UNRESOLVED",
                        location,
                        f"{producer.output_contract_ref} declares no field {field}",
                        step_id=step.step_id,
                    )
                ],
            )
        return field_type
    if fact.startswith("decision."):
        parts = fact.split(".")
        if len(parts) != 2:
            raise WorkflowError(
                "WORKFLOW_CONDITION_FACT_INVALID",
                diagnostics=[
                    located(
                        "WORKFLOW_CONDITION_FACT_INVALID",
                        location,
                        "a decision fact names decision.step_id",
                        step_id=step.step_id,
                    )
                ],
            )
        decision_id = parts[1]
        producer = index.steps.get(decision_id)
        if producer is None or not producer.execution_kind_ref.startswith("human_decision@"):
            raise WorkflowError(
                "WORKFLOW_CONDITION_FACT_UNRESOLVED",
                diagnostics=[
                    located(
                        "WORKFLOW_CONDITION_FACT_UNRESOLVED",
                        location,
                        "a decision fact names a declared human decision step",
                        step_id=step.step_id,
                    )
                ],
            )
        if decision_id not in step.depends_on:
            raise WorkflowError(
                "WORKFLOW_CONDITION_FACT_DEPENDENCY_MISSING",
                diagnostics=[
                    located(
                        "WORKFLOW_CONDITION_FACT_DEPENDENCY_MISSING",
                        location,
                        f"reading {decision_id} requires declaring it in depends_on",
                        step_id=step.step_id,
                    )
                ],
            )
        return "text"
    raise WorkflowError(
        "WORKFLOW_CONDITION_FACT_INVALID",
        diagnostics=[
            located(
                "WORKFLOW_CONDITION_FACT_INVALID",
                location,
                "a fact names an approved input, a trusted result or a human decision",
                step_id=step.step_id,
            )
        ],
    )


def _check_operand_type(
    operator: str, fact_type: str, literal: Any, location: str, step_id: str
) -> None:
    """A comparison must compare a value with something of its declared type.

    A count compares with an integer and a text value compares with text, so an
    integer bound on a text field is refused instead of being coerced.
    """
    if operator in {"at_least", "at_most"}:
        if fact_type != "count":
            raise WorkflowError(
                "WORKFLOW_CONDITION_TYPE_MISMATCH",
                diagnostics=[
                    located(
                        "WORKFLOW_CONDITION_TYPE_MISMATCH",
                        location,
                        f"{operator} compares a count, and this fact is {fact_type}",
                        step_id=step_id,
                    )
                ],
            )
        if type(literal) is not int:
            raise WorkflowError(
                "WORKFLOW_CONDITION_TYPE_MISMATCH",
                diagnostics=[
                    located(
                        "WORKFLOW_CONDITION_TYPE_MISMATCH",
                        location,
                        f"{operator} takes an integer bound",
                        step_id=step_id,
                    )
                ],
            )
        return
    literal_type = "count" if type(literal) is int else "text"
    if fact_type != literal_type:
        raise WorkflowError(
            "WORKFLOW_CONDITION_TYPE_MISMATCH",
            diagnostics=[
                located(
                    "WORKFLOW_CONDITION_TYPE_MISMATCH",
                    location,
                    f"{operator} compares {fact_type} with {literal_type}",
                    step_id=step_id,
                )
            ],
        )


def _check_output_contract(
    kind: ExecutionKind, contract_ref: str, location: str, step_id: str
) -> str:
    """Return the contract's declared kind, refusing a contradictory pairing.

    Two independent things must agree: the contract must be registered, and the
    execution kind's declared output kind must equal the contract's own declared
    kind. A ``candidate_integrate`` step therefore cannot declare a text contract
    and be treated as producing a Candidate, whether or not an adapter exists.
    """
    if not contract_is_registered(contract_ref):
        raise WorkflowError(
            "WORKFLOW_CONTRACT_UNREGISTERED",
            diagnostics=[
                located(
                    "WORKFLOW_CONTRACT_UNREGISTERED",
                    f"{location}/output_contract",
                    "output contract is not registered",
                    step_id=step_id,
                )
            ],
        )
    declared_kind: str = str(contract_schema(contract_ref)["kind"])
    produced: dict[str, str] = {
        output.contract_ref: str(output.kind) for output in kind.outputs
    }
    if kind.outputs:
        output_kind = produced.get(contract_ref)
        if output_kind is None:
            raise WorkflowError(
                "WORKFLOW_OUTPUT_CONTRACT_INCOMPATIBLE",
                diagnostics=[
                    located(
                        "WORKFLOW_OUTPUT_CONTRACT_INCOMPATIBLE",
                        f"{location}/output_contract",
                        f"{kind.ref} does not produce this contract",
                        step_id=step_id,
                    )
                ],
            )
    elif not kind.available:
        # A declared-but-unimplemented kind names a capability; the contract it
        # declares must still match the artifact kind that capability produces.
        allowed = _produced_kinds(kind.ref)
        if allowed and declared_kind not in allowed:
            raise WorkflowError(
                "WORKFLOW_OUTPUT_CONTRACT_INCOMPATIBLE",
                diagnostics=[
                    located(
                        "WORKFLOW_OUTPUT_CONTRACT_INCOMPATIBLE",
                        f"{location}/output_contract",
                        f"{kind.ref} produces {', '.join(sorted(allowed))}, "
                        f"not {declared_kind}",
                        step_id=step_id,
                    )
                ],
            )
        output_kind = declared_kind
    else:
        output_kind = declared_kind
    if output_kind != declared_kind:
        raise WorkflowError(
            "WORKFLOW_OUTPUT_CONTRACT_INCOMPATIBLE",
            diagnostics=[
                located(
                    "WORKFLOW_OUTPUT_CONTRACT_INCOMPATIBLE",
                    f"{location}/output_contract",
                    f"{contract_ref} declares {declared_kind}, and {kind.ref} "
                    f"declares {output_kind}",
                    step_id=step_id,
                )
            ],
        )
    return str(output_kind)


# ------------------------------------------------------------------ conditions


def _condition(value: Any, location: str, step_id: str) -> dict[str, Any] | None:
    """Compile a typed boolean expression; free text is never a condition."""
    if value is None:
        return None
    return _expression(value, location=f"{location}/condition", step_id=step_id, depth=0)


def _expression(value: Any, *, location: str, step_id: str, depth: int) -> dict[str, Any]:
    if depth > 8:
        raise WorkflowError(
            "WORKFLOW_CONDITION_TOO_DEEP",
            diagnostics=[
                located(
                    "WORKFLOW_CONDITION_TOO_DEEP", location, "condition nesting", step_id=step_id
                )
            ],
        )
    if not isinstance(value, dict) or len(value) != 1:
        raise WorkflowError(
            "WORKFLOW_CONDITION_INVALID",
            diagnostics=[
                located(
                    "WORKFLOW_CONDITION_INVALID",
                    location,
                    "a condition is exactly one typed operator",
                    step_id=step_id,
                )
            ],
        )
    ((operator, operand),) = value.items()
    if operator not in OPERATORS:
        raise WorkflowError(
            "WORKFLOW_CONDITION_INVALID",
            diagnostics=[
                located(
                    "WORKFLOW_CONDITION_INVALID",
                    f"{location}/{operator}",
                    "operator is not part of the declarative condition set",
                    step_id=step_id,
                )
            ],
        )
    if operator in {"all", "any"}:
        if not isinstance(operand, list) or not operand:
            raise WorkflowError(
                "WORKFLOW_CONDITION_INVALID",
                diagnostics=[
                    located(
                        "WORKFLOW_CONDITION_INVALID",
                        location,
                        f"{operator} takes a non-empty list",
                        step_id=step_id,
                    )
                ],
            )
        return {
            operator: [
                _expression(
                    item,
                    location=f"{location}/{operator}",
                    step_id=step_id,
                    depth=depth + 1,
                )
                for item in operand
            ]
        }
    if operator == "not":
        return {
            "not": _expression(
                operand, location=f"{location}/not", step_id=step_id, depth=depth + 1
            )
        }
    if operator in {"equals", "not_equals"}:
        if not isinstance(operand, list) or len(operand) != 2:
            raise WorkflowError(
                "WORKFLOW_CONDITION_INVALID",
                diagnostics=[
                    located(
                        "WORKFLOW_CONDITION_INVALID",
                        location,
                        f"{operator} takes [fact, literal]",
                        step_id=step_id,
                    )
                ],
            )
        fact = _fact(operand[0], location=location, step_id=step_id)
        literal = operand[1]
        if not isinstance(literal, (str, int, bool)) or isinstance(literal, float):
            raise WorkflowError(
                "WORKFLOW_CONDITION_INVALID",
                diagnostics=[
                    located(
                        "WORKFLOW_CONDITION_INVALID",
                        location,
                        "literal is not comparable",
                        step_id=step_id,
                    )
                ],
            )
        return {operator: [fact, literal]}
    if not isinstance(operand, list) or len(operand) != 2:
        raise WorkflowError(
            "WORKFLOW_CONDITION_INVALID",
            diagnostics=[
                located(
                    "WORKFLOW_CONDITION_INVALID",
                    location,
                    f"{operator} takes [fact, integer bound]",
                    step_id=step_id,
                )
            ],
        )
    fact = _fact(operand[0], location=location, step_id=step_id)
    bound = operand[1]
    if type(bound) is not int or bound < 0:
        raise WorkflowError(
            "WORKFLOW_CONDITION_INVALID",
            diagnostics=[
                located(
                    "WORKFLOW_CONDITION_INVALID",
                    location,
                    "bound is not a count",
                    step_id=step_id,
                )
            ],
        )
    return {operator: [fact, bound]}


def _fact(value: Any, *, location: str, step_id: str) -> str:
    """The syntactic fact shape; resolution happens after all steps are known."""
    if not isinstance(value, str) or not 1 <= len(value) <= 512:
        raise WorkflowError(
            "WORKFLOW_CONDITION_FACT_INVALID",
            diagnostics=[
                located(
                    "WORKFLOW_CONDITION_FACT_INVALID",
                    location,
                    "a fact is a bounded identity",
                    step_id=step_id,
                )
            ],
        )
    if value.startswith(("requirement.", "input.", "decision.", "result.")):
        return value
    raise WorkflowError(
        "WORKFLOW_CONDITION_FACT_INVALID",
        diagnostics=[
            located(
                "WORKFLOW_CONDITION_FACT_INVALID",
                location,
                "a fact names an approved input, a trusted result or a human decision",
                step_id=step_id,
            )
        ],
    )


# ------------------------------------------------------------------ completion


def _completion(value: Any, steps: Sequence[CompiledStep]) -> dict[str, Any]:
    if value is None:
        raise WorkflowError(
            "WORKFLOW_COMPLETION_MISSING",
            diagnostics=[
                located("WORKFLOW_COMPLETION_MISSING", "workflow.yaml#/completion", "required")
            ],
        )
    if not isinstance(value, Mapping):
        raise _schema("workflow.yaml", "#/completion", "expected a mapping")
    declared = value.get("required_steps")
    if not isinstance(declared, list) or not declared:
        raise WorkflowError(
            "WORKFLOW_COMPLETION_INVALID",
            diagnostics=[
                located(
                    "WORKFLOW_COMPLETION_INVALID",
                    "workflow.yaml#/completion/required_steps",
                    "at least one required step must be declared",
                )
            ],
        )
    known = {step.step_id: step for step in steps}
    diagnostics: list[Diagnostic] = []
    for name in declared:
        if not isinstance(name, str) or name not in known:
            diagnostics.append(
                located(
                    "WORKFLOW_COMPLETION_INVALID",
                    "workflow.yaml#/completion/required_steps",
                    f"required step {str(name)[:64]} is not defined",
                )
            )
            continue
        if not known[name].required:
            diagnostics.append(
                located(
                    "WORKFLOW_COMPLETION_OPTIONAL_OBLIGATION",
                    f"workflow.yaml#/completion/required_steps/id={name}",
                    "a required outcome cannot be produced by an optional step",
                    step_id=name,
                )
            )
        if known[name].condition is not None:
            diagnostics.append(
                located(
                    "WORKFLOW_COMPLETION_CONDITIONAL_OBLIGATION",
                    f"workflow.yaml#/completion/required_steps/id={name}",
                    "a required outcome cannot be gated behind a condition",
                    step_id=name,
                )
            )
    if diagnostics:
        raise WorkflowError(diagnostics[0].code, diagnostics=diagnostics)
    artifact = _text(value.get("artifact"), file="workflow.yaml", pointer="#/completion/artifact")
    producer = artifact[: -len(".output")] if artifact.endswith(".output") else None
    if producer is None or producer not in known:
        raise WorkflowError(
            "WORKFLOW_COMPLETION_INVALID",
            diagnostics=[
                located(
                    "WORKFLOW_COMPLETION_INVALID",
                    "workflow.yaml#/completion/artifact",
                    "the completion artifact must be a declared step output",
                )
            ],
        )
    if producer not in declared:
        raise WorkflowError(
            "WORKFLOW_COMPLETION_INVALID",
            diagnostics=[
                located(
                    "WORKFLOW_COMPLETION_INVALID",
                    "workflow.yaml#/completion/artifact",
                    "the producing step is not a required step",
                    step_id=producer,
                )
            ],
        )
    return {"required_steps": list(declared), "artifact": artifact}


def _delivery_kind(value: Any) -> str:
    if value not in DELIVERY_GATES:
        raise WorkflowError(
            "WORKFLOW_DELIVERY_KIND_INVALID",
            diagnostics=[
                located(
                    "WORKFLOW_DELIVERY_KIND_INVALID",
                    "workflow.yaml#/delivery_kind",
                    "delivery_kind must be report, patch or pr",
                )
            ],
        )
    return str(value)


def _check_delivery_gate(
    delivery_kind: str,
    steps: Sequence[CompiledStep],
    completion: Mapping[str, Any],
    expansions: Sequence[Mapping[str, Any]] = (),
) -> None:
    """The declared target, the declared steps and the capability must agree.

    A ``publish_pr`` step in a report or patch workflow is a target/side-effect
    mismatch, whether it is declared statically or inside a dynamic boundary: a
    dynamic expansion cannot smuggle in a remote-delivery capability the target
    does not authorise. A ``patch`` target additionally needs a step that can
    actually produce a repository Candidate, so merely changing ``delivery_kind``
    never manufactures the capability. The protected gate is compiled from policy
    rather than from the workflow's own text, so a role name, an optional flag or
    a condition cannot remove it (docs/architecture/09 §6). Availability is never
    a substitute for this check: a contradictory target is refused even when the
    kind has no adapter.
    """
    publishers = [step for step in steps if step.execution_kind_ref.startswith("publish_pr@")]
    if publishers and delivery_kind != "pr":
        raise WorkflowError(
            "WORKFLOW_DELIVERY_GATE_MISMATCH",
            diagnostics=[
                located(
                    "WORKFLOW_DELIVERY_GATE_MISMATCH",
                    f"{step.location}/execution_kind",
                    f"a {delivery_kind} workflow declares no remote delivery",
                    step_id=step.step_id,
                )
                for step in publishers
            ],
        )
    for expansion in expansions:
        if "external_write" in expansion.get("side_effects", []):
            raise WorkflowError(
                "WORKFLOW_DELIVERY_GATE_MISMATCH",
                diagnostics=[
                    located(
                        "WORKFLOW_DELIVERY_GATE_MISMATCH",
                        "workflow.yaml#/scheduling/expansions",
                        f"a {delivery_kind} workflow cannot expand into remote delivery",
                    )
                ],
            )
    if delivery_kind == "pr":
        if not publishers:
            raise WorkflowError(
                "WORKFLOW_DELIVERY_GATE_MISSING",
                diagnostics=[
                    located(
                        "WORKFLOW_DELIVERY_GATE_MISSING",
                        "workflow.yaml#/completion",
                        "a pr workflow declares its delivery step",
                    )
                ],
            )
        required = set(completion["required_steps"])
        for step in publishers:
            if not step.required:
                raise WorkflowError(
                    "WORKFLOW_DELIVERY_GATE_OPTIONAL",
                    diagnostics=[
                        located(
                            "WORKFLOW_DELIVERY_GATE_OPTIONAL",
                            step.location,
                            "the remote delivery step cannot be optional",
                            step_id=step.step_id,
                        )
                    ],
                )
            if step.condition is not None:
                raise WorkflowError(
                    "WORKFLOW_DELIVERY_GATE_CONDITIONAL",
                    diagnostics=[
                        located(
                            "WORKFLOW_DELIVERY_GATE_CONDITIONAL",
                            f"{step.location}/condition",
                            "the remote delivery step cannot be gated behind a condition",
                            step_id=step.step_id,
                        )
                    ],
                )
            if step.step_id not in required:
                raise WorkflowError(
                    "WORKFLOW_DELIVERY_GATE_OMITTED",
                    diagnostics=[
                        located(
                            "WORKFLOW_DELIVERY_GATE_OMITTED",
                            "workflow.yaml#/completion/required_steps",
                            "the remote delivery step must be a required outcome",
                            step_id=step.step_id,
                        )
                    ],
                )
    producer_id = str(completion["artifact"])[: -len(".output")]
    producer = next((step for step in steps if step.step_id == producer_id), None)
    if producer is None:
        return
    required_kind = DELIVERY_ARTIFACT_KIND[delivery_kind]
    # The *selected* contract decides, not the set of kinds the execution kind
    # might be able to produce. A step that declares ``text@1`` cannot meet a
    # patch target merely because its kind could in principle emit a Candidate,
    # and a step that declares ``repair-patch@1`` cannot meet a report target.
    # Checking the declared contract is what makes an incompatible target a
    # located rejection instead of a name that happens to be allowed.
    actual = producer.output_contract_kind
    if actual != required_kind:
        raise WorkflowError(
            "WORKFLOW_DELIVERY_ARTIFACT_INCOMPATIBLE",
            diagnostics=[
                located(
                    "WORKFLOW_DELIVERY_ARTIFACT_INCOMPATIBLE",
                    f"{producer.location}/output_contract",
                    f"a {delivery_kind} target needs a {required_kind} artifact, "
                    f"and {producer.output_contract_ref} is {actual}",
                    step_id=producer.step_id,
                )
            ],
        )


#: The artifact kind each delivery target's completion artifact must be.
DELIVERY_ARTIFACT_KIND: Mapping[str, str] = {
    "report": "text",
    "patch": "candidate",
    "pr": "candidate",
}


def _produced_kinds(execution_kind_ref: str) -> frozenset[str]:
    """Artifact kinds one registered execution kind can actually produce."""
    return frozenset(kind_for(execution_kind_ref, location="workflow.yaml#/steps").produces)


# ---------------------------------------------------------------------- rework


def _rework(value: Any, steps: Sequence[CompiledStep]) -> list[dict[str, Any]]:
    """Compile finite rework; an unbounded rule is refused, not truncated."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise _schema("workflow.yaml", "#/rework", "expected a list")
    known = {step.step_id for step in steps}
    rules: list[dict[str, Any]] = []
    for index, rule in enumerate(value):
        location = f"workflow.yaml#/rework/{index}"
        if not isinstance(rule, Mapping):
            raise _schema(location, "", "expected a mapping")
        trigger = rule.get("trigger")
        if not isinstance(trigger, Mapping) or "step" not in trigger:
            raise _schema(location, "/trigger", "needs a step")
        trigger_step = trigger["step"]
        if trigger_step not in known:
            raise WorkflowError(
                "WORKFLOW_REWORK_UNRESOLVED",
                diagnostics=[
                    located(
                        "WORKFLOW_REWORK_UNRESOLVED",
                        f"{location}/trigger/step",
                        "the triggering step is not defined",
                        step_id=str(trigger_step)[:64],
                    )
                ],
            )
        outcome = trigger.get("outcome", "changes_requested")
        if outcome not in {"changes_requested", "failed", "inconclusive"}:
            raise _schema(location, "/trigger/outcome", "unsupported outcome")
        returns = rule.get("returns_to")
        if (
            not isinstance(returns, list)
            or not returns
            or not all(isinstance(item, str) for item in returns)
        ):
            raise _schema(location, "/returns_to", "expected a list of steps")
        unresolved = sorted(set(returns) - known)
        if unresolved:
            raise WorkflowError(
                "WORKFLOW_REWORK_UNRESOLVED",
                diagnostics=[
                    located(
                        "WORKFLOW_REWORK_UNRESOLVED",
                        f"{location}/returns_to/id={name}",
                        "a rework target is not defined",
                        step_id=name,
                    )
                    for name in unresolved
                ],
            )
        rounds = rule.get("maximum_rounds")
        if type(rounds) is not int or not 1 <= rounds <= MAXIMUM_REWORK_ROUNDS:
            raise WorkflowError(
                "WORKFLOW_REWORK_UNBOUNDED",
                diagnostics=[
                    located(
                        "WORKFLOW_REWORK_UNBOUNDED",
                        f"{location}/maximum_rounds",
                        f"a finite bound between 1 and {MAXIMUM_REWORK_ROUNDS} is required",
                    )
                ],
            )
        invalidates = rule.get("invalidates", [])
        if not isinstance(invalidates, list) or not all(
            isinstance(item, str) for item in invalidates
        ):
            raise _schema(location, "/invalidates", "expected a list of steps")
        unknown_invalidation = sorted(set(invalidates) - known)
        if unknown_invalidation:
            raise WorkflowError(
                "WORKFLOW_REWORK_UNRESOLVED",
                diagnostics=[
                    located(
                        "WORKFLOW_REWORK_UNRESOLVED",
                        f"{location}/invalidates/id={name}",
                        "an invalidated step is not defined",
                        step_id=name,
                    )
                    for name in unknown_invalidation
                ],
            )
        rules.append(
            {
                "trigger": {"step": trigger_step, "outcome": outcome},
                "returns_to": sorted(returns),
                "maximum_rounds": rounds,
                "invalidates": sorted(invalidates),
                "batch_identity": ["run_id", "repair_chain_id", "validation_cycle_id"],
            }
        )
    return rules


# ------------------------------------------------------------------ scheduling

SCHEDULING_ACTIONS = frozenset(
    {
        "expand_graph",
        "bind_role",
        "set_dependencies",
        "set_priority",
        "request_dispatch",
        "seal",
    }
)


def _scheduling(
    value: Any,
    roles: Mapping[str, str],
    declared_inputs: Sequence[str],
    bindings: Mapping[str, str],
    binding_index: Mapping[str, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None, list[ExecutionKind]]:
    """Compile declared dynamic boundaries; nothing here grants authority."""
    if value is None:
        return [], None, []
    if not isinstance(value, Mapping):
        raise _schema("workflow.yaml", "#/scheduling", "expected a mapping")
    role_alias = value.get("role")
    if not isinstance(role_alias, str) or role_alias not in roles:
        raise _schema(
            "workflow.yaml", "#/scheduling/role", "the scheduling role must be declared here"
        )
    actions = value.get("actions", [])
    if not isinstance(actions, list) or not all(isinstance(item, str) for item in actions):
        raise _schema("workflow.yaml", "#/scheduling/actions", "expected text")
    undeclared = sorted(set(actions) - SCHEDULING_ACTIONS)
    if undeclared:
        raise WorkflowError(
            "WORKFLOW_SCHEDULING_ACTION_UNSUPPORTED",
            diagnostics=[
                located(
                    "WORKFLOW_SCHEDULING_ACTION_UNSUPPORTED",
                    f"workflow.yaml#/scheduling/actions/id={name}",
                    "a scheduling action must be one of the declared set",
                )
                for name in undeclared
            ],
        )
    expansions: list[dict[str, Any]] = []
    kinds: list[ExecutionKind] = []
    declared_expansions = value.get("expansions", [])
    if not isinstance(declared_expansions, list):
        raise _schema("workflow.yaml", "#/scheduling/expansions", "expected a list")
    for index, item in enumerate(declared_expansions):
        location = f"workflow.yaml#/scheduling/expansions/{index}"
        if not isinstance(item, Mapping):
            raise _schema(location, "", "expected a mapping")
        maximum = item.get("maximum_members")
        declared_cap: int | None
        if maximum is None:
            # Omitting the count means "no user-declared cap". The engine does not
            # substitute one: actual concurrency and admission are decided by real
            # resources and by whatever policy the user configured (#178).
            declared_cap = None
        elif type(maximum) is int and maximum >= 1:
            declared_cap = maximum
        else:
            raise _schema(
                location,
                "/maximum_members",
                "a declared member policy is a positive integer, or omitted",
            )
        candidate_roles = item.get("roles", [])
        if not isinstance(candidate_roles, list) or not all(
            isinstance(name, str) for name in candidate_roles
        ):
            raise _schema(location, "/roles", "expected role aliases")
        unknown_roles = sorted(set(candidate_roles) - set(roles))
        if unknown_roles:
            raise WorkflowError(
                "WORKFLOW_ROLE_UNRESOLVED",
                diagnostics=[
                    located(
                        "WORKFLOW_ROLE_UNRESOLVED",
                        f"{location}/roles/id={name}",
                        "an expansion may only bind roles this workflow declares",
                        step_id=name,
                    )
                    for name in unknown_roles
                ],
            )
        declared_kinds = item.get("execution_kinds", [])
        if not isinstance(declared_kinds, list) or not all(
            isinstance(name, str) for name in declared_kinds
        ):
            raise _schema(location, "/execution_kinds", "expected execution kind references")
        resolved_kinds: list[str] = []
        dynamic_side_effects: list[tuple[str, str]] = []
        for kind_ref in declared_kinds:
            pinned = _reference(
                kind_ref, file="workflow.yaml", pointer=f"{location}/execution_kinds"
            )
            # A declared dynamic boundary must name a real registered revision,
            # so a syntactically valid ``anything@999`` cannot claim a capability.
            resolved = kind_for(pinned, location=f"{location}/execution_kinds")
            resolved_kinds.append(resolved.ref)
            kinds.append(resolved)
            dynamic_side_effects.append((resolved.ref, resolved.side_effects))
        work_contract = item.get("work_contract", {})
        if not isinstance(work_contract, Mapping):
            raise _schema(location, "/work_contract", "expected a mapping")
        work_inputs = work_contract.get("inputs", [])
        if not isinstance(work_inputs, list) or not all(
            isinstance(name, str) for name in work_inputs
        ):
            raise _schema(location, "/work_contract/inputs", "expected declared input names")
        unknown_inputs = sorted(set(work_inputs) - set(declared_inputs))
        if unknown_inputs:
            raise WorkflowError(
                "WORKFLOW_INPUT_REFERENCE_UNRESOLVED",
                diagnostics=[
                    located(
                        "WORKFLOW_INPUT_REFERENCE_UNRESOLVED",
                        f"{location}/work_contract/inputs/id={name}",
                        "a work contract may only read declared workflow inputs",
                    )
                    for name in unknown_inputs
                ],
            )
        # A scope is a list of declared path patterns. A bare string is refused
        # rather than iterated: iterating one would silently turn ``src/**`` into
        # its characters, quietly changing an authority boundary.
        raw_scope = work_contract.get("scope", [])
        if not isinstance(raw_scope, list) or not all(
            isinstance(name, str) and name for name in raw_scope
        ):
            raise _schema(
                location,
                "/work_contract/scope",
                "scope must be a list of declared path patterns",
            )
        for pattern in raw_scope:
            if len(pattern) > 200 or any(character < " " for character in pattern):
                raise _schema(location, "/work_contract/scope", "scope pattern is not usable")
        expansions.append(
            {
                "expansion_id": _text(
                    item.get("id"), file="workflow.yaml", pointer=f"{location}/id"
                ),
                # ``None`` is an explicit "no user-declared cap", preserved as
                # declared rather than replaced by an engine default.
                "declared_member_policy": declared_cap,
                "engine_member_cap": None,
                "roles": sorted(candidate_roles),
                "execution_kinds": sorted(set(resolved_kinds)),
                "seal_required": True,
                "side_effects": sorted({effect for _, effect in dynamic_side_effects}),
                "work_contract": {
                    "inputs": sorted(work_inputs),
                    "scope": sorted(raw_scope),
                },
            }
        )
    scheduler_binding_ref = bindings.get(role_alias)
    scheduling = {
        # The alias the author selected and the binding it selects are part of
        # the compiled identity. Two aliases can point at the same role revision
        # while binding different sources, so recording only the role reference
        # would make switching between them invisible.
        "role_alias": role_alias,
        "role_ref": roles[role_alias],
        "binding_ref": scheduler_binding_ref,
        "binding_identity": (
            dict(binding_index[scheduler_binding_ref])
            if scheduler_binding_ref and scheduler_binding_ref in binding_index
            else None
        ),
        # The aliases a dynamic decision may bind, so the permitted surface is
        # explicit rather than inferred from whatever the workflow happens to use.
        "permitted_role_aliases": sorted(roles),
        "declared_actions": sorted(actions),
        # Declared here and granted nowhere: the compiled result states what the
        # workflow asks for, and a later SchedulerGrant decides what is allowed.
        "grants_authority": False,
        "authority_source": "scheduler_grant_required",
        "product_agent_limit": None,
        "agent_policy": _agent_policy(value.get("agent_policy")),
        "delegation": {
            "allowed": value.get("allow_delegation") is True,
            "maximum_depth": _bounded_depth(value.get("maximum_delegation_depth")),
        },
    }
    return expansions, scheduling, kinds


def _agent_policy(value: Any) -> dict[str, Any]:
    """Preserve a user-declared count policy without imposing an engine one.

    A workflow may state how many agents or tasks the user wants. That statement
    is validated and preserved so it can be respected, but the compiler never
    supplies a number of its own and never truncates a legal task set to fit one.
    """
    if value is None:
        return {"declared": False, "maximum": None, "source": "unspecified"}
    if not isinstance(value, Mapping):
        raise _schema("workflow.yaml", "#/scheduling/agent_policy", "expected a mapping")
    maximum = value.get("maximum")
    if maximum is None:
        return {"declared": True, "maximum": None, "source": "declared_without_count"}
    if type(maximum) is not int or maximum < 1:
        raise _schema(
            "workflow.yaml",
            "#/scheduling/agent_policy/maximum",
            "a declared maximum is a positive integer, or omitted",
        )
    return {
        "declared": True,
        "maximum": maximum,
        "source": "user_declared",
        "policy_ref": value.get("policy_ref")
        if isinstance(value.get("policy_ref"), str)
        else None,
    }


def _bounded_depth(value: Any) -> int:
    if value is None:
        return 0
    if type(value) is not int or not 0 <= value <= 8:
        raise _schema(
            "workflow.yaml",
            "#/scheduling/maximum_delegation_depth",
            "a delegation depth must be an explicit small bound",
        )
    return value


def _declared_inputs(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise _schema("workflow.yaml", "#/inputs", "expected declared input names")
    for name in value:
        if not 1 <= len(name) <= 96 or any(character.isspace() for character in name):
            raise _schema("workflow.yaml", "#/inputs", "input names are bounded identities")
    return list(value)


# -------------------------------------------------------------------- checks


def _text(value: object, *, file: str, pointer: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise _schema(file, pointer, "invalid text")
    return value


def _reference(value: object, *, file: str, pointer: str) -> str:
    """Require one fixed ``id@revision`` reference and refuse a floating one."""
    text = _text(value, file=file, pointer=pointer)
    if "@" not in text:
        raise WorkflowError(
            "WORKFLOW_REFERENCE_NOT_PINNED",
            diagnostics=[
                located(
                    "WORKFLOW_REFERENCE_NOT_PINNED",
                    f"{file}{pointer}",
                    "a reference must name an exact revision",
                )
            ],
        )
    name, _, revision = text.rpartition("@")
    if name.casefold() in {"latest", "head", "next"} or revision.casefold() in {
        "latest",
        "head",
        "*",
        "next",
    }:
        raise WorkflowError(
            "WORKFLOW_REFERENCE_NOT_PINNED",
            diagnostics=[
                located(
                    "WORKFLOW_REFERENCE_NOT_PINNED",
                    f"{file}{pointer}",
                    "a latest reference is never resolved at compile or load time",
                )
            ],
        )
    try:
        number = int(revision)
    except ValueError:
        raise WorkflowError(
            "WORKFLOW_REFERENCE_INVALID",
            diagnostics=[
                located(
                    "WORKFLOW_REFERENCE_INVALID", f"{file}{pointer}", "revision is not a number"
                )
            ],
        ) from None
    if number < 1:
        raise WorkflowError(
            "WORKFLOW_REFERENCE_INVALID",
            diagnostics=[
                located(
                    "WORKFLOW_REFERENCE_INVALID", f"{file}{pointer}", "revision must be positive"
                )
            ],
        )
    return text


# ------------------------------------------------------------------ projection


def projection(compiled: CompiledWorkflow) -> dict[str, Any]:
    """The same-source review projection: graph, Mermaid, table and diff sources.

    Every field is derived from one :class:`CompiledWorkflow`, so the diagram,
    the table and the graph cannot describe different versions. A consumer that
    only needs the graph never has to re-parse YAML.
    """
    return {
        "compiled_digest": compiled.compiled_digest,
        "compiler_identity": compiled.compiler_identity,
        "bundle_digest": compiled.bundle_digest,
        "workflow": {
            "id": compiled.workflow_id,
            "revision": compiled.revision,
            "delivery_kind": compiled.delivery_kind,
            "input_contract_ref": compiled.input_contract_ref,
        },
        "readiness": compiled.readiness,
        "executable": compiled.executable,
        "execution_kinds": {
            "available": list(compiled.available_execution_kinds),
            "unavailable": list(compiled.unavailable_execution_kinds),
        },
        "delivery_gate": dict(compiled.delivery_gate),
        "graph": _graph(compiled),
        "mermaid": _mermaid(compiled),
        "table": _table(compiled),
        "roles": _role_table(compiled),
        "scheduling": dict(compiled.scheduling) if compiled.scheduling else None,
        "rework": [dict(rule) for rule in compiled.rework],
        "expansions": [dict(item) for item in compiled.expansions],
        "diagnostics": [item.as_document() for item in compiled.diagnostics],
    }


def _graph(compiled: CompiledWorkflow) -> dict[str, Any]:
    return {
        "nodes": [
            {
                "id": step.step_id,
                "execution_kind_ref": step.execution_kind_ref,
                "execution_kind_available": step.execution_kind_available,
                "role_alias": step.role_alias,
                "role_ref": step.role_ref,
                "binding_ref": step.binding_ref,
                "output_contract_ref": step.output_contract_ref,
                "required": step.required,
                "join": step.join,
                "conditional": step.condition is not None,
                "location": step.location,
            }
            for step in compiled.steps
        ],
        "edges": [
            {"from": dependency, "to": step.step_id}
            for step in compiled.steps
            for dependency in step.depends_on
        ],
        "required_steps": list(compiled.completion["required_steps"]),
        "artifact": compiled.completion["artifact"],
    }


def _table(compiled: CompiledWorkflow) -> list[dict[str, Any]]:
    """The role/step table, built from each step's own selected alias.

    The row reads the alias and binding the step actually saved. A reverse lookup
    from ``role_ref`` back to an alias would be lossy: two aliases can name the
    same role definition while binding different sources, so the reverse map
    would report one alias's binding for both steps.
    """
    return [
        {
            "step_id": step.step_id,
            "execution_kind_ref": step.execution_kind_ref,
            "role_alias": step.role_alias,
            "role_ref": step.role_ref,
            "binding_ref": step.binding_ref,
            "depends_on": list(step.depends_on),
            "inputs": step.inputs,
            "output_contract_ref": step.output_contract_ref,
            "required": step.required,
            "join": step.join,
            "condition": step.condition,
            "location": step.location,
        }
        for step in compiled.steps
    ]


def _role_table(compiled: CompiledWorkflow) -> list[dict[str, Any]]:
    """The role/responsibility/constraint table, derived from the same source.

    Every field here is the frozen role content the compiled digest covers, so
    the table cannot describe a different role version than the one that was
    compiled.
    """
    return [
        {
            **role.as_document(),
            "binding_ref": compiled.bindings.get(role.alias),
        }
        for role in compiled.role_definitions
    ]


#: Characters Mermaid treats as syntax. A role or step name is data, so it is
#: quoted and its own quotes are encoded rather than emitted raw.
_MERMAID_ESCAPES = {"#": "#35;", '"': "#quot;", "<": "#lt;", ">": "#gt;", "&": "#38;"}


def escape_label(value: object) -> str:
    """Encode a label for Mermaid and HTML; never emit raw markup."""
    text = "" if value is None else str(value)
    escaped = "".join(_MERMAID_ESCAPES.get(character, character) for character in text)
    return escaped.replace("\n", " ").replace("\r", " ")[:120]


def _mermaid(compiled: CompiledWorkflow) -> str:
    """Render the diagram with collision-free node identities.

    A node identity is derived from the step's *ordinal position*, not from its
    name, so two steps cannot collide however they are spelled: encoding ``a-b``
    as ``a_2db`` would collide with a step literally named ``a_2db``. The step id
    itself appears only inside the quoted label, where escaping applies.
    """
    lines = ["flowchart TD"]
    identities = {step.step_id: f"n{index}" for index, step in enumerate(compiled.steps)}
    for step in compiled.steps:
        label = escape_label(
            f"{step.step_id}: {step.execution_kind_ref}"
            + (f" ({step.role_ref})" if step.role_ref else "")
        )
        lines.append(f'    {identities[step.step_id]}["{label}"]')
    for step in compiled.steps:
        for dependency in step.depends_on:
            lines.append(f"    {identities[dependency]} --> {identities[step.step_id]}")
    return "\n".join(lines)


def diff(left: CompiledWorkflow | None, right: CompiledWorkflow) -> dict[str, Any]:
    """A deterministic structural diff between two compiled revisions."""
    if left is None:
        # No comparison was requested, so nothing has "changed". The document
        # still lists what this revision introduces, and says explicitly that it
        # is a baseline rather than a no-op comparison.
        return {
            "baseline": True,
            "from_digest": None,
            "to_digest": right.compiled_digest,
            "changed": None,
            "added_steps": [step.step_id for step in right.steps],
            "removed_steps": [],
            "changed_steps": [],
            "roles_changed": False,
            "delivery_kind_changed": False,
            "completion_changed": False,
            "scheduling_changed": right.scheduling is not None,
            "rework_changed": bool(right.rework),
        }
    before = {step.step_id: step for step in left.steps}
    after = {step.step_id: step for step in right.steps}
    before_roles = {role.alias: role.as_document() for role in left.role_definitions}
    after_roles = {role.alias: role.as_document() for role in right.role_definitions}
    return {
        "baseline": False,
        "from_digest": left.compiled_digest,
        "to_digest": right.compiled_digest,
        "changed": left.compiled_digest != right.compiled_digest,
        "added_steps": [name for name in after if name not in before],
        "removed_steps": [name for name in before if name not in after],
        "changed_steps": [
            name
            for name in after
            if name in before
            and CompiledWorkflow._step_document(after[name])
            != CompiledWorkflow._step_document(before[name])
        ],
        "roles_changed": before_roles != after_roles,
        "added_roles": sorted(name for name in after_roles if name not in before_roles),
        "removed_roles": sorted(name for name in before_roles if name not in after_roles),
        "changed_roles": sorted(
            name
            for name in after_roles
            if name in before_roles and after_roles[name] != before_roles[name]
        ),
        "delivery_kind_changed": left.delivery_kind != right.delivery_kind,
        "completion_changed": dict(left.completion) != dict(right.completion),
        "scheduling_changed": dict(left.scheduling or {}) != dict(right.scheduling or {}),
        "rework_changed": [dict(rule) for rule in left.rework]
        != [dict(rule) for rule in right.rework],
    }


def referenced_kinds(steps: Iterable[CompiledStep]) -> list[str]:
    """Every kind reference a compiled workflow actually uses."""
    return sorted({step.execution_kind_ref for step in steps})


__all__ = [
    "COMPILER_IDENTITY",
    "COMPILER_REVISION",
    "CompiledRole",
    "CompiledStep",
    "CompiledWorkflow",
    "DELIVERY_ARTIFACT_KIND",
    "DELIVERY_GATES",
    "MANIFEST_SCHEMA_VERSION",
    "SCHEMA_VERSION",
    "compile_workflow",
    "diff",
    "escape_label",
    "projection",
    "referenced_kinds",
    "registered_refs",
]
