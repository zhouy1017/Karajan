"""Second-pass compiler semantics: aliases, literals, kinds and conditions.

Each case here corresponds to a defect an independent review reproduced on the
first candidate, so it is a regression rather than a new feature: the selected
alias must reach the table, a marked literal must mean the same thing to the
compiler and the adapter, an unknown kind must always be located, and a
condition must be typed and cycle-free.
"""

import pytest
from karajan.workflows.compiler import (
    CompiledWorkflow,
    compile_workflow,
    diff,
    projection,
)
from karajan.workflows.errors import WorkflowError
from karajan.workflows.registry import artifact_aggregate, kind_for

MANIFEST = {"schema_version": "karajan.workflow-bundle.v1", "bundle_id": "compare"}

ROLE_FILE = {
    "id": "source-researcher",
    "revision": 2,
    "display_name": "Source researcher",
    "responsibilities": ["read approved material"],
    "stop_conditions": [],
    "required_capabilities": ["read_only_research"],
    "tool_constraints": {"paths": ["docs/**"]},
}


def workflow(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "schema_version": "workflow.v1",
        "id": "compare-options",
        "revision": 1,
        "delivery_kind": "report",
        "input_contract": "text@1",
        "inputs": ["requirement.option_a", "requirement.option_b"],
        "roles": {"researcher": "role:source-researcher@2"},
        "steps": [
            {
                "id": "option-a",
                "execution_kind": "artifact_aggregate@1",
                "output_contract": "aggregated-report@1",
                "role": "researcher",
                "inputs": {"sources": "requirement.option_a"},
            },
            {
                "id": "comparison",
                "execution_kind": "artifact_aggregate@1",
                "output_contract": "aggregated-report@1",
                "role": "researcher",
                "depends_on": ["option-a"],
                "inputs": {"sources": ["option-a.output"]},
            },
        ],
        "completion": {
            "required_steps": ["option-a", "comparison"],
            "artifact": "comparison.output",
        },
    }
    document.update(overrides)
    return document


def compile_document(
    document: dict[str, object] | None = None,
    *,
    roles: dict[str, dict[str, object]] | None = None,
) -> CompiledWorkflow:
    return compile_workflow(
        MANIFEST,
        document or workflow(),
        roles if roles is not None else {"roles/researcher.yaml": ROLE_FILE},
        bundle_digest="bundle-digest-placeholder",
        role_digest="role-digest-placeholder",
    )


def rejection(document: dict[str, object], code: str) -> WorkflowError:
    with pytest.raises(WorkflowError) as raised:
        compile_document(document)
    assert raised.value.code == code
    assert raised.value.diagnostics, f"{code} must carry at least one location"
    for item in raised.value.diagnostics:
        assert item.location.startswith(("workflow.yaml", "roles/", "manifest.json"))
    return raised.value


def test_two_aliases_of_one_role_keep_their_own_binding_in_the_table() -> None:
    """A table row is built from the step's own alias, never a reverse map.

    Two aliases may name the same fixed role definition while binding different
    sources. A reverse lookup from the role reference would report one alias's
    binding for both steps, which is exactly why the row reads the saved alias.
    """
    roles = {
        "roles/researcher.yaml": {
            "id": "source-researcher",
            "revision": 2,
            "responsibilities": ["read"],
            "tool_constraints": {},
        }
    }
    resolved = {
        "binding:source-a@1": {
            "binding": {"id": "source-a", "revision": 1, "digest": "a" * 64},
            "connection": {"id": "gateway", "revision": 1, "digest": "b" * 64},
            "execution_eligible": False,
            "draft_only": True,
        },
        "binding:source-b@1": {
            "binding": {"id": "source-b", "revision": 1, "digest": "c" * 64},
            "connection": {"id": "gateway", "revision": 1, "digest": "d" * 64},
            "execution_eligible": False,
            "draft_only": True,
        },
    }
    document = workflow(
        roles={"left": "role:source-researcher@2", "right": "role:source-researcher@2"},
        bindings={"left": "binding:source-a@1", "right": "binding:source-b@1"},
        steps=[
            {
                "id": "first",
                "execution_kind": "artifact_aggregate@1",
                "output_contract": "aggregated-report@1",
                "role": "left",
                "inputs": {"sources": "requirement.option_a"},
            },
            {
                "id": "second",
                "execution_kind": "artifact_aggregate@1",
                "output_contract": "aggregated-report@1",
                "role": "right",
                "inputs": {"sources": "requirement.option_b"},
            },
        ],
        completion={"required_steps": ["first", "second"], "artifact": "second.output"},
    )
    compiled = compile_workflow(
        MANIFEST,
        document,
        roles,
        bundle_digest="bundle",
        role_digest="role",
        binding_index=resolved,
    )
    rows = {row["step_id"]: row for row in projection(compiled)["table"]}
    assert rows["first"]["role_alias"] == "left"
    assert rows["first"]["binding_ref"] == "binding:source-a@1"
    assert rows["second"]["role_alias"] == "right"
    assert rows["second"]["binding_ref"] == "binding:source-b@1"
    # The step, the table and the graph agree on the same selected identities.
    steps = {step.step_id: step for step in compiled.steps}
    assert steps["first"].role_alias == rows["first"]["role_alias"]
    assert steps["second"].binding_ref == rows["second"]["binding_ref"]
    nodes = {node["id"]: node for node in projection(compiled)["graph"]["nodes"]}
    assert nodes["first"]["binding_ref"] == "binding:source-a@1"
    assert nodes["second"]["binding_ref"] == "binding:source-b@1"


def test_switching_between_two_aliases_changes_the_compiled_identity() -> None:
    """The selected alias is part of the template, not only the role reference."""
    roles = {
        "roles/researcher.yaml": {
            "id": "source-researcher",
            "revision": 2,
            "responsibilities": ["read"],
            "tool_constraints": {},
        }
    }

    def compile_with(role_alias: str) -> CompiledWorkflow:
        document = workflow(
            roles={"left": "role:source-researcher@2", "right": "role:source-researcher@2"},
            steps=[
                {
                    "id": "note",
                    "execution_kind": "artifact_aggregate@1",
                    "output_contract": "aggregated-report@1",
                    "role": role_alias,
                    "inputs": {"sources": "requirement.option_a"},
                }
            ],
            completion={"required_steps": ["note"], "artifact": "note.output"},
        )
        return compile_workflow(
            MANIFEST, document, roles, bundle_digest="bundle", role_digest="role"
        )

    left, right = compile_with("left"), compile_with("right")
    assert left.roles == right.roles
    assert left.compiled_digest != right.compiled_digest
    assert diff(left, right)["changed_steps"] == ["note"]


def test_a_marked_literal_must_carry_data_and_the_adapter_agrees() -> None:
    """A compiled binding and the value the adapter receives are one contract.

    An empty marked literal compiles to no data at all, which the real adapter
    refuses; compilation therefore refuses it too, and it never reports a
    known-invalid concrete input as executable.
    """
    rejection(
        workflow(
            steps=[
                {
                    "id": "note",
                    "execution_kind": "artifact_aggregate@1",
                    "output_contract": "aggregated-report@1",
                    "inputs": {"sources": "literal:"},
                }
            ],
            completion={"required_steps": ["note"], "artifact": "note.output"},
        ),
        "WORKFLOW_INPUT_INVALID",
    )

    compiled = compile_document(
        workflow(
            steps=[
                {
                    "id": "note",
                    "execution_kind": "artifact_aggregate@1",
                    "output_contract": "aggregated-report@1",
                    "inputs": {"sources": "literal:hello"},
                }
            ],
            completion={"required_steps": ["note"], "artifact": "note.output"},
        )
    )
    assert compiled.executable is True
    binding = compiled.steps[0].inputs["sources"]
    assert binding == "hello"
    # The exact end-to-end step: the compiled binding feeds the real adapter.
    result = artifact_aggregate(compiled.steps[0].inputs)
    assert "hello" in result["content"]
    assert result["content_digest"]
    assert artifact_aggregate(compiled.steps[0].inputs) == result


def test_a_marked_literal_and_plain_text_reach_the_adapter_identically() -> None:
    """A title as free text and as a marked literal resolves to the same data."""
    marked = artifact_aggregate({"sources": "literal:x", "title": "literal:Report"})
    plain = artifact_aggregate({"sources": "x", "title": "Report"})
    assert marked["content_digest"] == plain["content_digest"]
    assert marked["content"] == plain["content"]


def test_the_adapter_enforces_the_same_declared_contract_as_the_compiler() -> None:
    """Runtime values are checked against the same registry contract.

    The adapter receives resolved data, so a value it is handed is content rather
    than a reference. What it still enforces is the contract shape: the declared
    input names and a non-empty value for a required input. The compile-time
    half — that a reference names an accepted artifact contract — has already
    been decided before anything reaches here.
    """
    with pytest.raises(WorkflowError):
        artifact_aggregate({"unknown_input": "literal:x"})
    with pytest.raises(WorkflowError):
        artifact_aggregate({"sources": ""})
    with pytest.raises(WorkflowError):
        artifact_aggregate({"sources": ["literal:"]})
    with pytest.raises(WorkflowError):
        artifact_aggregate({})
    # Data of the declared type is accepted whether or not it carries the marker,
    # because by this point the marker has already been resolved away.
    assert artifact_aggregate({"sources": "x"})["content"]
    assert artifact_aggregate({"sources": "literal:x"})["content"]


def test_unknown_kind_rejections_always_carry_a_location() -> None:
    """Every kind rejection is located, asserted non-empty before iteration."""
    for reference in ("ghost_kind@999", "artifact_aggregate@7"):
        with pytest.raises(WorkflowError) as raised:
            kind_for(reference, location="workflow.yaml#/steps/id=x/execution_kind")
        assert raised.value.code == "WORKFLOW_KIND_UNREGISTERED"
        assert raised.value.diagnostics, "a kind rejection must be located"
        assert raised.value.diagnostics[0].location.endswith("/execution_kind")

    steps = [dict(step) for step in workflow()["steps"]]  # type: ignore[arg-type]
    steps[0]["execution_kind"] = "ghost_kind@999"
    error = rejection(workflow(steps=steps), "WORKFLOW_KIND_UNREGISTERED")
    assert error.diagnostics[0].location == (
        "workflow.yaml#/steps/id=option-a/execution_kind"
    )

    dynamic = rejection(
        workflow(
            scheduling={
                "role": "researcher",
                "actions": ["expand_graph"],
                "expansions": [
                    {"id": "e", "roles": ["researcher"], "execution_kinds": ["ghost_kind@1"]}
                ],
            }
        ),
        "WORKFLOW_KIND_UNREGISTERED",
    )
    assert dynamic.diagnostics[0].location == (
        "workflow.yaml#/scheduling/expansions/0/execution_kinds"
    )


def test_condition_types_are_checked_against_declared_contract_fields() -> None:
    """A count operator needs a count field; an unknown field is unresolved."""
    collect = {
        "id": "collect",
        "execution_kind": "artifact_aggregate@1",
        "output_contract": "aggregated-report@1",
        "inputs": {"sources": "requirement.option_a"},
    }
    gate = {
        "id": "gate",
        "execution_kind": "artifact_aggregate@1",
        "output_contract": "aggregated-report@1",
        "depends_on": ["collect"],
        "inputs": {"sources": ["collect.output"]},
        "condition": {"at_least": ["result.collect.nonexistent", 2]},
    }
    rejection(
        workflow(
            steps=[collect, gate],
            completion={"required_steps": ["collect"], "artifact": "collect.output"},
        ),
        "WORKFLOW_CONDITION_FIELD_UNRESOLVED",
    )

    # ``content`` is declared as text, so an integer count operator is a mismatch.
    gate = {**gate, "condition": {"at_least": ["result.collect.content", 2]}}
    rejection(
        workflow(
            steps=[collect, gate],
            completion={"required_steps": ["collect"], "artifact": "collect.output"},
        ),
        "WORKFLOW_CONDITION_TYPE_MISMATCH",
    )

    # ``input_count`` is declared as a count, so the same operator is accepted.
    gate = {**gate, "condition": {"at_least": ["result.collect.input_count", 2]}}
    compiled = compile_document(
        workflow(
            steps=[collect, gate],
            completion={"required_steps": ["collect"], "artifact": "collect.output"},
        )
    )
    assert compiled.steps[-1].condition == {"at_least": ["result.collect.input_count", 2]}


def test_a_condition_cannot_create_a_hidden_cycle() -> None:
    """Reading a result without depending on it is refused, not silently ordered.

    ``collect`` waits on ``approval`` while ``approval`` reads ``collect``'s
    result. The implicit edge would close a cycle that the ordinary dependency
    check cannot see, so the undeclared read is refused.
    """
    steps = [
        {
            "id": "collect",
            "execution_kind": "artifact_aggregate@1",
            "output_contract": "aggregated-report@1",
            "depends_on": ["approval"],
            "inputs": {"sources": "requirement.option_a"},
        },
        {
            "id": "approval",
            "execution_kind": "human_decision@1",
            "output_contract": "review-verdict@1",
            "condition": {"equals": ["result.collect.content", "ready"]},
        },
    ]
    rejection(
        workflow(
            steps=steps,
            completion={"required_steps": ["collect"], "artifact": "collect.output"},
        ),
        "WORKFLOW_CONDITION_FACT_DEPENDENCY_MISSING",
    )

    # Declaring the dependency makes both the edge and the cycle explicit.
    steps[0]["depends_on"] = []
    steps[1]["depends_on"] = ["collect"]
    compiled = compile_document(
        workflow(
            steps=steps,
            completion={"required_steps": ["collect"], "artifact": "collect.output"},
        )
    )
    assert compiled.steps[1].depends_on == ("collect",)


def test_a_report_expansion_cannot_smuggle_in_remote_delivery() -> None:
    """A dynamic boundary cannot introduce a capability the target forbids."""
    rejection(
        workflow(
            delivery_kind="report",
            scheduling={
                "role": "researcher",
                "actions": ["expand_graph"],
                "expansions": [
                    {
                        "id": "publish-batch",
                        "roles": ["researcher"],
                        "execution_kinds": ["publish_pr@1"],
                    }
                ],
            },
        ),
        "WORKFLOW_DELIVERY_GATE_MISMATCH",
    )


def test_a_candidate_kind_cannot_declare_a_text_contract() -> None:
    """The kind and the contract must agree on what is produced."""
    rejection(
        workflow(
            steps=[
                {
                    "id": "integrate",
                    "execution_kind": "candidate_integrate@1",
                    "output_contract": "aggregated-report@1",
                    "inputs": {},
                }
            ],
            completion={"required_steps": ["integrate"], "artifact": "integrate.output"},
        ),
        "WORKFLOW_OUTPUT_CONTRACT_INCOMPATIBLE",
    )


def test_a_role_contract_and_instruction_template_must_resolve() -> None:
    """A role cannot reference an unregistered contract or an unknown template."""
    broken_contract = {
        **ROLE_FILE,
        "input_contract_ref": "missing@999",
    }
    with pytest.raises(WorkflowError) as raised:
        compile_document(roles={"roles/researcher.yaml": broken_contract})
    assert raised.value.code == "WORKFLOW_CONTRACT_UNREGISTERED"

    broken_template = {**ROLE_FILE, "instruction_template_ref": "templates/ghost.md"}
    with pytest.raises(WorkflowError) as raised:
        compile_document(roles={"roles/researcher.yaml": broken_template})
    assert raised.value.code == "WORKFLOW_TEMPLATE_UNRESOLVED"

    # A supported template is accepted and its identity is part of the template.
    supported = {**ROLE_FILE, "instruction_template_ref": "templates/researcher.md"}
    compiled = compile_document(roles={"roles/researcher.yaml": supported})
    assert compiled.role_definitions[0].instruction_template_ref == "templates/researcher.md"


def test_an_instruction_change_changes_the_compiled_identity() -> None:
    """A template's content digest is part of the role's frozen identity."""
    plain = compile_document()
    templated = compile_document(
        roles={"roles/researcher.yaml": {**ROLE_FILE,
        "instruction_template_ref": "templates/researcher.md"}}
    )
    assert plain.compiled_digest != templated.compiled_digest
    assert diff(plain, templated)["roles_changed"] is True


def test_a_work_contract_scope_must_be_a_list_of_patterns() -> None:
    """A bare string is refused rather than iterated into characters."""
    rejection(
        workflow(
            scheduling={
                "role": "researcher",
                "actions": ["expand_graph"],
                "expansions": [
                    {
                        "id": "e",
                        "roles": ["researcher"],
                        "execution_kinds": ["artifact_aggregate@1"],
                        "work_contract": {"inputs": [], "scope": "src/**"},
                    }
                ],
            }
        ),
        "WORKFLOW_SCHEMA_INVALID",
    )
    compiled = compile_document(
        workflow(
            scheduling={
                "role": "researcher",
                "actions": ["expand_graph"],
                "expansions": [
                    {
                        "id": "e",
                        "roles": ["researcher"],
                        "execution_kinds": ["artifact_aggregate@1"],
                        "work_contract": {"inputs": [], "scope": ["src/**", "tests/**"]},
                    }
                ],
            }
        )
    )
    assert compiled.expansions[0]["work_contract"]["scope"] == ["src/**", "tests/**"]


def test_five_thousand_steps_compile_from_real_documents() -> None:
    """A large but ordinary configuration is not capped by a collection limit."""
    count = 5_000
    inputs = [f"requirement.item_{index:04d}" for index in range(count)]
    steps: list[dict[str, object]] = [
        {
            "id": f"fan_{index:04d}",
            "execution_kind": "artifact_aggregate@1",
            "output_contract": "aggregated-report@1",
            "inputs": {"sources": f"requirement.item_{index:04d}"},
        }
        for index in range(count)
    ]
    steps.append(
        {
            "id": "join",
            "execution_kind": "artifact_aggregate@1",
            "output_contract": "aggregated-report@1",
            "depends_on": [f"fan_{index:04d}" for index in range(count)],
            "inputs": {"sources": [f"fan_{index:04d}.output" for index in range(count)]},
        }
    )
    compiled = compile_document(
        workflow(
            inputs=inputs,
            steps=steps,
            completion={
                "required_steps": [f"fan_{index:04d}" for index in range(count)] + ["join"],
                "artifact": "join.output",
            },
        )
    )
    assert len(compiled.steps) == count + 1
    assert compiled.executable is True
    assert compiled.reviewable is True
    rendered = projection(compiled)
    assert len(rendered["graph"]["nodes"]) == count + 1
    assert len(rendered["graph"]["edges"]) == count
    assert compiled.steps[0].step_id == "fan_0000"
    assert compiled.steps[-1].step_id == "join"
    assert compiled.scheduling is None
