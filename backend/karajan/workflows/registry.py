"""Trusted registry of execution kinds and the one real local adapter.

A workflow may only reference an execution kind this module registers. The
registry is code, not data: a bundle cannot name a module, a path or a callable,
so no upload can make the controller import or run anything. A registered kind
whose adapter is not implemented here is reported as unavailable, and an
unregistered kind is refused, so a configuration that merely names a capability
never becomes executable by registration alone (docs/architecture/09 §3).

This slice registers exactly one working adapter, ``artifact_aggregate@1``: it
merges typed text inputs into one deterministic report artifact. It opens no
socket, starts no process, imports nothing from the bundle and calls no model.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from .digests import byte_digest, content_digest
from .errors import WorkflowError, located

#: Kinds named by docs/architecture/09 §3. All but ``artifact_aggregate`` are
#: declared-only in this slice and therefore report ``available=False``.
DECLARED_KINDS = (
    "agent_task",
    "deterministic_check",
    "human_decision",
    "artifact_aggregate",
    "candidate_integrate",
    "publish_pr",
)

SIDE_EFFECTS = Literal["none", "local_artifact", "workspace_write", "external_write", "human"]

#: Contract identities this slice can validate against, with the artifact kind
#: each one produces and the fields a consumer may read from it. A contract that
#: is not listed is unknown, and an unknown contract is a located rejection
#: rather than a silently accepted string. The declared fields are what a
#: condition may reference: a fact naming a field that no contract declares is
#: unresolved here rather than failing at run time.
CONTRACT_SCHEMAS: Mapping[str, Mapping[str, Any]] = {
    "text@1": {"kind": "text", "fields": {"content": "text", "digest": "text"}},
    "sourced-notes@1": {"kind": "text", "fields": {"content": "text", "digest": "text"}},
    "comparison-report@1": {
        "kind": "text",
        "fields": {"content": "text", "digest": "text", "sources": "count"},
    },
    "repair-patch@1": {
        "kind": "candidate",
        "fields": {"content": "text", "digest": "text", "files_changed": "count"},
    },
    "review-verdict@1": {
        "kind": "decision",
        "fields": {"verdict": "text", "reason": "text", "findings": "count"},
    },
    "aggregated-report@1": {
        "kind": "text",
        "fields": {
            "content": "text",
            "digest": "text",
            "input_count": "count",
            "byte_length": "count",
        },
    },
}

CONTRACT_KINDS: Mapping[str, str] = {
    name: str(schema["kind"]) for name, schema in CONTRACT_SCHEMAS.items()
}

REGISTERED_CONTRACTS = frozenset(CONTRACT_SCHEMAS)

#: Templates an instruction reference may name. A role cannot point at an
#: arbitrary host path, and an unreferenced template is never resolved.
REGISTERED_INSTRUCTION_TEMPLATES = frozenset(
    {
        "templates/researcher.md",
        "templates/editor.md",
        "templates/coordinator.md",
        "templates/reviewer.md",
    }
)

#: Field types a comparison operator accepts. A count is comparable with an
#: integer bound or an integer literal; text is comparable with text only.
FIELD_TYPES = frozenset({"text", "count"})


def contract_schema(contract_ref: str) -> Mapping[str, Any]:
    """The declared schema of one registered contract."""
    schema = CONTRACT_SCHEMAS.get(contract_ref)
    if schema is None:
        raise WorkflowError(
            "WORKFLOW_CONTRACT_UNREGISTERED",
            fields={"contract_ref": str(contract_ref)[:120]},
        )
    return schema


def contract_field_type(contract_ref: str, field: str) -> str | None:
    """The declared type of one field, or ``None`` when the contract has none."""
    fields: Mapping[str, str] = CONTRACT_SCHEMAS[contract_ref]["fields"]
    return fields.get(field)


@dataclass(frozen=True, slots=True)
class OutputContract:
    """The identity and shape one execution kind promises to produce."""

    contract_ref: str
    kind: Literal["text", "structured", "candidate", "decision"]
    required: bool = True

    @property
    def id(self) -> str:
        return self.contract_ref.split("@", 1)[0]

    @property
    def revision(self) -> int:
        return int(self.contract_ref.split("@", 1)[1])


@dataclass(frozen=True, slots=True)
class InputContract:
    """The named, typed inputs one execution kind accepts.

    ``required`` inputs must be bound by a step; ``optional`` inputs may be
    omitted. ``accepts`` lists the output contracts a reference-shaped value may
    name; an empty ``accepts`` means the input is literal text only. Nothing
    accepts an arbitrary payload, so an incompatible binding is decidable at
    compile time.
    """

    name: str
    type: Literal["text", "reference", "structured"]
    required: bool = True
    accepts: tuple[str, ...] = ()


#: An explicit marker for literal text in an input binding. Without it, a string
#: is a reference that must resolve; with it, the remainder is data. This keeps
#: ``Hello. World`` usable while making a mistyped reference ambiguous no longer.
LITERAL_PREFIX = "literal:"


def literal_text(value: str) -> str | None:
    """The literal payload of a marked value, or ``None`` for a reference."""
    return value[len(LITERAL_PREFIX) :] if value.startswith(LITERAL_PREFIX) else None


def resolved_text(value: str) -> str:
    """The data a binding contributes at run time.

    A marked literal contributes its payload; an unmarked value is already the
    resolved data. The compiler and the adapter both use this, so the value a
    step is compiled against and the value the adapter receives are the same
    string with the same meaning.
    """
    payload = literal_text(value)
    return payload if payload is not None else value


def validate_bindings(
    inputs: Sequence[InputContract],
    bound: Mapping[str, Any],
    *,
    enforce_contract: bool,
    location: str = "workflow.yaml#/steps",
) -> dict[str, Any]:
    """Apply one execution kind's declared input contract to concrete values.

    Both the compiler and the adapter call this, so a value cannot satisfy one
    side and fail the other: the registry states the contract once and two
    callers apply it.

    ``enforce_contract`` selects which half of the same contract applies.
    Declaration time (``True``) additionally checks that a reference-shaped text
    value names an *accepted artifact contract*, because a binding is a promise
    about what will be produced. Run time (``False``) receives already-resolved
    values, where the same text is the artifact's content and is not required to
    equal a contract identifier; names, required inputs and value shapes are
    still enforced.
    """
    accepted = {item.name: item for item in inputs}
    unknown = sorted(name for name in bound if name not in accepted)
    if unknown:
        raise _input_error(
            "WORKFLOW_INPUT_UNKNOWN",
            location,
            "not a declared input",
            name=unknown[0],
        )
    missing = sorted(
        name for name, contract in accepted.items() if contract.required and name not in bound
    )
    if missing:
        raise _input_error(
            "WORKFLOW_INPUT_MISSING",
            location,
            "a required input is not bound",
            name=missing[0],
        )
    checked: dict[str, Any] = {}
    for name, contract in accepted.items():
        if name not in bound:
            continue
        checked[name] = _checked_value(
            bound[name],
            contract=contract,
            enforce_contract=enforce_contract,
            location=location,
        )
    return checked


def resolve_for_adapter(inputs: Mapping[str, Any]) -> dict[str, Any]:
    """Reduce compiled bindings to the values a real adapter receives.

    This is the one place the compile-time representation is turned into runtime
    data, so a binding the compiler accepted and the value the adapter validates
    cannot disagree: the marker is removed here, and the registry contract is
    then applied to the result.
    """
    resolved: dict[str, Any] = {}
    for name, value in inputs.items():
        if isinstance(value, list):
            resolved[name] = [resolved_text(item) for item in value]
        elif isinstance(value, str):
            resolved[name] = resolved_text(value)
        else:
            resolved[name] = value
    return resolved


def _input_error(code: str, location: str, detail: str, *, name: str) -> WorkflowError:
    return WorkflowError(
        code,
        diagnostics=[located(code, f"{location}/id={name}", detail)],
        fields={"input": name[:120]},
    )


def _checked_value(
    value: Any,
    *,
    contract: InputContract,
    enforce_contract: bool,
    location: str,
) -> Any:
    here = f"{location}/id={contract.name}"
    if contract.type == "structured":
        if not isinstance(value, Mapping):
            raise _input_error(
                "WORKFLOW_INPUT_INVALID", location, "not a structured value", name=contract.name
            )
        return dict(value)
    if isinstance(value, list):
        if not value:
            raise _input_error(
                "WORKFLOW_INPUT_INVALID",
                location,
                "an empty list binds nothing",
                name=contract.name,
            )
        return [
            _checked_text(item, contract=contract, enforce_contract=enforce_contract, location=here)
            for item in value
        ]
    if not isinstance(value, str) or not value:
        raise _input_error(
            "WORKFLOW_INPUT_INVALID", location, f"not a {contract.type} value", name=contract.name
        )
    return _checked_text(
        value, contract=contract, enforce_contract=enforce_contract, location=here
    )


def _checked_text(
    value: Any, *, contract: InputContract, enforce_contract: bool, location: str
) -> str:
    if not isinstance(value, str) or not value:
        raise _input_error(
            "WORKFLOW_INPUT_INVALID", location, "not a text value", name=contract.name
        )
    if not enforce_contract:
        # A resolved runtime value is data of the declared type.
        return value
    if value in contract.accepts:
        return value
    if contract.type == "text":
        payload = literal_text(value)
        if payload is not None:
            # An explicitly marked literal is data. The marker is required; an
            # unmarked reference-shaped value is a reference. An empty payload
            # resolves to no data at all, which the adapter refuses, so it is
            # refused here too: compilation must not call a known-invalid
            # concrete input executable.
            if not payload:
                raise _input_error(
                    "WORKFLOW_INPUT_INVALID",
                    location,
                    "a marked literal must carry data",
                    name=contract.name,
                )
            return value
    raise _input_error(
        "WORKFLOW_INPUT_CONTRACT_INCOMPATIBLE",
        location,
        "a reference must name an accepted artifact contract",
        name=contract.name,
    )


@dataclass(frozen=True, slots=True)
class ExecutionKind:
    """One registered kind: its revision, contracts, capability and adapter."""

    id: str
    revision: int
    available: bool
    inputs: tuple[InputContract, ...]
    outputs: tuple[OutputContract, ...]
    required_capabilities: tuple[str, ...] = ()
    side_effects: SIDE_EFFECTS = "none"
    adapter: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None
    reason_codes: tuple[str, ...] = field(default_factory=tuple)
    requires_role: bool = True
    #: Declared separately from ``requires_role`` so a kind that is neither a
    #: model step nor a human step cannot be confused with either.
    deterministic: bool = False
    capability_evidence_refs: tuple[str, ...] = ()
    #: Artifact kinds this execution kind can produce. A delivery target that
    #: needs a repository Candidate cannot be met by a text-producing kind, so
    #: changing ``delivery_kind`` alone never manufactures the capability.
    produces: tuple[str, ...] = ()
    #: The delivery target this kind is a side effect *of*. ``publish_pr`` may
    #: only appear in a ``pr`` workflow, whatever its adapter availability.
    required_delivery_kind: str | None = None

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.revision}"


def artifact_aggregate(inputs: Mapping[str, Any]) -> Mapping[str, Any]:
    """Merge typed text inputs into one deterministic report artifact.

    The adapter validates its arguments through the same declared contract the
    compiler uses, so a value the compiler accepted is the only kind of value
    that reaches this body, and a value it refused cannot arrive here instead.
    ``arbitrary`` report text with no declared input is refused rather than
    stringified.

    The result depends only on the ordered inputs and their bytes: the ordering is
    a fixed ascending sort of the (already deterministic) input names, the
    separator is a fixed constant, and the encoding is UTF-8. The same inputs
    always produce byte-identical content, which is what makes the output digest
    meaningful evidence rather than a coincidence.

    The function reads no clock, opens no file, starts no process, resolves no
    credential, performs no network request and never calls a model. That is a
    property of this body, not a policy statement: there is nothing in it that
    could read or send anything.
    """
    if not isinstance(inputs, Mapping):
        raise WorkflowError("WORKFLOW_ADAPTER_INPUT_INVALID")
    agg_registry = _registry()[AGGREGATE_REF]
    checked = validate_bindings(
        agg_registry.inputs, resolve_for_adapter(inputs), enforce_contract=False
    )
    names = sorted(checked)
    pieces: list[str] = []
    digests: list[dict[str, str]] = []
    for name in names:
        value = checked[name]
        if isinstance(value, list):
            # A list of references is aggregated in its declared order, which is
            # the order the workflow author wrote, not a set.
            text = "\n".join(str(item) for item in value)
        else:
            text = str(value)
        body = text.encode("utf-8")
        pieces.append(f"## {name}\n{body.decode('utf-8')}\n")
        digests.append({"name": name, "content_digest": byte_digest(body)})
    content = "".join(pieces)
    encoded = content.encode("utf-8")
    return {
        "content": content,
        "encoding": "utf-8",
        "separator": "\n",
        "ordering": "input_name_ascending",
        "input_digests": digests,
        "aggregate_digest": content_digest({"ordering": "input_name_ascending", "inputs": digests}),
        "content_digest": byte_digest(encoded),
        "byte_length": len(encoded),
    }


#: The one adapter-backed reference; named here so the adapter can consult the
#: same declared contract the compiler does.
AGGREGATE_REF = "artifact_aggregate@1"


def _registry() -> dict[str, ExecutionKind]:
    """Build the one trusted registry this slice consults."""
    aggregated = ExecutionKind(
        id="artifact_aggregate",
        revision=1,
        available=True,
        inputs=(
            InputContract(
                "sources",
                "text",
                required=True,
                accepts=(
                    "text@1",
                    "sourced-notes@1",
                    "comparison-report@1",
                    "aggregated-report@1",
                ),
            ),
            InputContract("title", "text", required=False),
        ),
        outputs=(OutputContract("aggregated-report@1", "text"),),
        required_capabilities=(),
        side_effects="local_artifact",
        adapter=artifact_aggregate,
        deterministic=True,
        requires_role=False,
        produces=("text", "report"),
        capability_evidence_refs=("evidence:deterministic-artifact-aggregate@1",),
    )
    kinds: dict[str, ExecutionKind] = {
        aggregated.ref: aggregated,
    }
    for name in DECLARED_KINDS:
        if name == "artifact_aggregate":
            continue
        kinds[f"{name}@1"] = ExecutionKind(
            id=name,
            revision=1,
            available=False,
            inputs=(),
            outputs=(),
            side_effects="external_write" if name == "publish_pr" else "workspace_write",
            reason_codes=("WORKFLOW_KIND_ADAPTER_NOT_IMPLEMENTED",),
            requires_role=name == "agent_task",
            deterministic=name == "deterministic_check",
            # Declared here so a delivery target can be checked against the
            # capability that would actually produce it, and so an unavailable
            # kind stays visibly unavailable rather than silently satisfying one.
            produces=DECLARED_PRODUCES.get(name, ()),
            required_delivery_kind="pr" if name == "publish_pr" else None,
        )
    return kinds


#: The artifact kinds each declared execution kind would produce once it has a
#: real adapter. Declared now so the delivery gate can be checked against the
#: capability a step names, and so changing ``delivery_kind`` alone cannot make a
#: text-producing workflow satisfy a patch or pr target.
DECLARED_PRODUCES: Mapping[str, tuple[str, ...]] = {
    "agent_task": ("text", "structured", "candidate"),
    "deterministic_check": ("decision", "structured"),
    "human_decision": ("decision",),
    "candidate_integrate": ("candidate",),
    "publish_pr": ("candidate", "pr"),
}


#: The registry instance the compiler and any later trusted loader consult. It
#: is module-private state so a request handler cannot replace an entry, and it
#: is read-only by construction: :func:`kind_for` never mutates it.
_REGISTRY: Mapping[str, ExecutionKind] = _registry()


def kind_for(execution_kind_ref: str, *, location: str | None = None) -> ExecutionKind:
    """Resolve one exact ``id@revision``; never resolve a latest revision.

    ``location`` is the bundle-relative pointer of the reference site. Supplying
    it makes the rejection located, which is what lets a caller see which step or
    dynamic boundary named the unknown kind; every call site supplies one.
    """
    kind = _REGISTRY.get(execution_kind_ref)
    if kind is None:
        diagnostic = located(
            "WORKFLOW_KIND_UNREGISTERED",
            location or "workflow.yaml#/steps",
            "no registered execution kind has this exact revision",
        )
        raise WorkflowError(
            "WORKFLOW_KIND_UNREGISTERED",
            diagnostics=[diagnostic],
            fields={"execution_kind_ref": execution_kind_ref[:120]},
        )
    return kind


def registered_refs() -> list[str]:
    """Return every registered revision, sorted, for a catalog read."""
    return sorted(_REGISTRY)


def contract_is_registered(contract_ref: str) -> bool:
    return contract_ref in REGISTERED_CONTRACTS


def contract_kind(contract_ref: str) -> str:
    """The artifact kind one registered contract produces."""
    kind = CONTRACT_KINDS.get(contract_ref)
    if kind is None:
        raise WorkflowError(
            "WORKFLOW_CONTRACT_UNREGISTERED",
            fields={"contract_ref": str(contract_ref)[:120]},
        )
    return kind
