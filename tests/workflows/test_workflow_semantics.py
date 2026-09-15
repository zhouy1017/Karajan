"""Regressions for the semantic fixes: identity, completion, scheduler, schema.

Each case was reproduced by independent review against an earlier head and is
kept here so it cannot regress: a literal and a reference must stay distinct, a
delivery target must be met by the *selected* contract, the real aggregate must
emit every field its schema promises, a declared instruction template must be a
real file, and declared role and scheduler constraints must reach the compiled
identity and its diff.
"""

import pytest
from karajan.workflows import bundle as bundles
from karajan.workflows.compiler import compile_workflow, diff, projection
from karajan.workflows.errors import WorkflowError
from karajan.workflows.registry import artifact_aggregate, contract_schema

MANIFEST = {"schema_version": "karajan.workflow-bundle.v1", "bundle_id": "review"}

ROLE = {
    "id": "custom",
    "revision": 1,
    "responsibilities": ["read"],
    "tool_constraints": {},
}


def workflow(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "schema_version": "workflow.v1",
        "id": "review",
        "revision": 1,
        "delivery_kind": "report",
        "input_contract": "text@1",
        "inputs": [],
        "roles": {"reader": "role:custom@1"},
        "steps": [
            {
                "id": "first",
                "execution_kind": "artifact_aggregate@1",
                "role": "reader",
                "output_contract": "aggregated-report@1",
                "inputs": {"sources": "literal:hello"},
            },
            {
                "id": "final",
                "execution_kind": "artifact_aggregate@1",
                "role": "reader",
                "output_contract": "aggregated-report@1",
                "depends_on": ["first"],
                "inputs": {"sources": "first.output"},
            },
        ],
        "completion": {"required_steps": ["first", "final"], "artifact": "final.output"},
    }
    document.update(overrides)
    return document


def compile_document(
    document: dict[str, object],
    *,
    role: dict[str, object] | None = None,
    bindings: dict[str, dict[str, object]] | None = None,
) -> object:
    return compile_workflow(
        MANIFEST,
        document,
        {"roles/researcher.yaml": role or ROLE},
        bundle_digest="bundle",
        role_digest="role",
        binding_index=bindings,
    )


def test_a_delivery_target_needs_the_selected_contract_not_a_possible_one() -> None:
    """The declared contract decides, not the kinds the execution kind might emit."""
    document = workflow()
    document["delivery_kind"] = "patch"
    document["steps"] = [
        {
            "id": "final",
            "execution_kind": "agent_task@1",
            "role": "reader",
            "output_contract": "text@1",
            "inputs": {},
        }
    ]
    document["completion"] = {"required_steps": ["final"], "artifact": "final.output"}
    with pytest.raises(WorkflowError) as raised:
        compile_document(document)
    assert raised.value.code == "WORKFLOW_DELIVERY_ARTIFACT_INCOMPATIBLE"
    assert raised.value.diagnostics

    # A report target given a Candidate artifact is the same mismatch.
    document["delivery_kind"] = "report"
    document["steps"][0]["output_contract"] = "repair-patch@1"
    with pytest.raises(WorkflowError) as raised:
        compile_document(document)
    assert raised.value.code == "WORKFLOW_DELIVERY_ARTIFACT_INCOMPATIBLE"


def test_the_real_aggregate_emits_every_field_its_schema_promises() -> None:
    """A condition may read any declared field because the adapter emits it."""
    result = artifact_aggregate({"sources": "literal:hello", "title": "literal:Report"})
    promised = contract_schema("aggregated-report@1")["fields"]
    missing = sorted(name for name in promised if name not in result)
    assert missing == []
    assert result["input_count"] == 2
    assert isinstance(result["byte_length"], int)
    assert result["content_digest"]

    # The condition that was previously accepted against a field nobody emitted
    # now reads a field the adapter really produces. The conditioned step is not
    # a required outcome, so it does not gate the completion artifact.
    document = workflow()
    document["steps"][1]["condition"] = {"at_least": ["result.first.input_count", 1]}
    document["steps"][1]["depends_on"] = ["first"]
    document["steps"][1]["required"] = False
    document["completion"] = {"required_steps": ["first"], "artifact": "first.output"}
    compiled = compile_document(document)
    assert compiled.steps[1].condition == {"at_least": ["result.first.input_count", 1]}
    assert compiled.executable is True


def test_a_missing_instruction_template_is_refused() -> None:
    """A supported template name is not enough: its bytes must be supplied."""
    role = {**ROLE, "instruction_template_ref": "templates/researcher.md"}
    with pytest.raises(WorkflowError) as raised:
        compile_document(workflow(), role=role)
    assert raised.value.code == "WORKFLOW_TEMPLATE_MISSING"
    assert raised.value.diagnostics[0].location == "templates/researcher.md"


def test_independence_requirements_enter_the_identity_and_diff() -> None:
    """A declared independence condition is frozen, not discarded."""
    plain = compile_document(workflow())
    independent = compile_document(
        workflow(),
        role={
            **ROLE,
            "independence_requirements": {"different_model_family": True, "non_author": True},
        },
    )
    assert plain.compiled_digest != independent.compiled_digest
    difference = diff(plain, independent)
    assert difference["roles_changed"] is True
    assert difference["changed_roles"] == ["reader"]
    projected = projection(independent)["roles"][0]
    assert projected["independence_requirements"] == {
        "different_model_family": True,
        "non_author": True,
    }

    # An unsupported independence key is refused rather than silently carried.
    with pytest.raises(WorkflowError) as raised:
        compile_document(
            workflow(), role={**ROLE, "independence_requirements": {"whatever": True}}
        )
    assert raised.value.code == "WORKFLOW_SCHEMA_INVALID"


def test_the_selected_scheduler_alias_and_binding_are_frozen() -> None:
    """Switching scheduler alias with the same role ref is a real change."""
    bindings = {
        "binding:a@1": {
            "binding": {"id": "a", "revision": 1, "digest": "a" * 64},
            "execution_eligible": False,
        },
        "binding:b@1": {
            "binding": {"id": "b", "revision": 1, "digest": "b" * 64},
            "execution_eligible": False,
        },
    }
    document = workflow()
    document["roles"] = {"reader": "role:custom@1", "other": "role:custom@1"}
    document["bindings"] = {"reader": "binding:a@1", "other": "binding:b@1"}
    document["scheduling"] = {"role": "reader", "actions": ["set_priority"]}
    before = compile_document(document, bindings=bindings)

    other = dict(document)
    other["scheduling"] = {"role": "other", "actions": ["set_priority"]}
    after = compile_document(other, bindings=bindings)

    assert before.compiled_digest != after.compiled_digest
    assert diff(before, after)["scheduling_changed"] is True
    scheduling = projection(after)["scheduling"]
    assert scheduling["role_alias"] == "other"
    assert scheduling["role_ref"] == "role:custom@1"
    assert scheduling["binding_ref"] == "binding:b@1"
    assert scheduling["permitted_role_aliases"] == ["other", "reader"]
    # Selection is still not authority.
    assert scheduling["grants_authority"] is False


def test_an_unknown_workflow_field_is_refused_rather_than_ignored() -> None:
    """A field the compiler never reads never reaches storage."""
    with pytest.raises(WorkflowError) as raised:
        compile_document(workflow(api_key="fixture-not-a-real-credential"))
    assert raised.value.code == "WORKFLOW_CREDENTIAL_FIELD_REFUSED"
    assert "fixture-not-a-real-credential" not in str(raised.value.document())

    with pytest.raises(WorkflowError) as raised:
        compile_document(workflow(metadata={"owner": "someone"}))
    assert raised.value.code == "WORKFLOW_FIELD_UNSUPPORTED"


def test_a_credential_nested_in_a_structured_role_field_is_refused() -> None:
    """A nested credential is refused by field name, and its value is not echoed."""
    marker = "fixture-not-a-real-credential"
    role = {
        **ROLE,
        "tool_constraints": {"auth": {"api_key": marker}, "paths": ["docs/**"]},
    }
    with pytest.raises(WorkflowError) as raised:
        compile_document(workflow(), role=role)
    assert raised.value.code == "WORKFLOW_CREDENTIAL_FIELD_REFUSED"
    assert marker not in str(raised.value.document())

    scripted = {**ROLE, "tool_constraints": {"script": "rm -rf /"}}
    with pytest.raises(WorkflowError) as raised:
        compile_document(workflow(), role=scripted)
    assert raised.value.code == "WORKFLOW_FIELD_UNSUPPORTED"
    assert "rm -rf" not in str(raised.value.document())


def test_ordinary_prose_and_limits_survive_validation() -> None:
    """A legitimate prose responsibility and a legitimate limit are accepted."""
    role = {
        **ROLE,
        "responsibilities": [
            "Read the approved material, then summarise it for a non-expert reader."
        ],
        "tool_constraints": {"paths": ["docs/**"], "network": "denied"},
    }
    compiled = compile_document(workflow(), role=role)
    frozen = compiled.role_definitions[0]
    assert frozen.responsibilities == tuple(role["responsibilities"])
    assert frozen.tool_constraints == {"paths": ["docs/**"], "network": "denied"}


def test_unpaired_surrogate_text_is_refused_rather_than_raising() -> None:
    """Text that is not storable UTF-8 is a located refusal, not an error.

    JSON can carry an unpaired surrogate, which Python accepts as a ``str`` and
    cannot encode. This is service-level validation hardening: the real HTTP
    boundary already rejects such a body, and this asserts the service does too.
    """
    # A lone surrogate is what a JSON body can carry: Python holds it as a
    # ``str`` but cannot encode it as UTF-8.
    declared = [{"path": "workflow.yaml", "content": 'id: "' + "\ud800" + '"'}]
    with pytest.raises(WorkflowError) as raised:
        bundles.prepare_files(declared)
    assert raised.value.code == "WORKFLOW_CONTENT_NOT_ENCODABLE"
    assert raised.value.diagnostics
