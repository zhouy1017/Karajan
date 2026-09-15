"""Compiler behaviour: fixed references, real resolution and located refusals.

Every case here runs the production compiler on a bundle-shaped document. No
model, network call or adapter invocation takes place: the compiler is pure, and
these tests assert what it refuses as much as what it accepts.
"""

import pytest
from karajan.workflows.compiler import (
    COMPILER_REVISION,
    CompiledWorkflow,
    compile_workflow,
    diff,
    escape_label,
    projection,
)
from karajan.workflows.errors import WorkflowError
from karajan.workflows.registry import artifact_aggregate

MANIFEST = {"schema_version": "karajan.workflow-bundle.v1", "bundle_id": "compare"}

ROLE_FILE = {
    "id": "source-researcher",
    "revision": 2,
    "display_name": "Source researcher",
    "responsibilities": ["read approved material", "record sources"],
    "stop_conditions": ["material is outside the approved scope"],
    "required_capabilities": ["read_only_research"],
    "tool_constraints": {"paths": ["docs/**"], "network": "denied"},
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
    for item in raised.value.diagnostics:
        # Every location is bundle-relative: a diagnostic never carries a host
        # path, and the path a caller submitted is never used as a location.
        assert item.location.startswith(("workflow.yaml", "roles/", "manifest.json"))
    return raised.value


def test_two_custom_roles_resolve_by_alias_and_fixed_reference() -> None:
    """The alias is the workflow key; the reference must name a bundled role."""
    roles = {
        "roles/researcher.yaml": ROLE_FILE,
        "roles/editor.yaml": {
            "id": "technical-editor",
            "revision": 1,
            "responsibilities": ["edit the comparison"],
            "stop_conditions": [],
            "tool_constraints": {"paths": ["reports/**"]},
        },
    }
    document = workflow(
        roles={
            "researcher": "role:source-researcher@2",
            "editor": "role:technical-editor@1",
        }
    )
    steps = [dict(step) for step in document["steps"]]  # type: ignore[arg-type]
    steps[1]["role"] = "editor"
    document["steps"] = steps
    compiled = compile_document(document, roles=roles)
    assert compiled.roles == {
        "editor": "role:technical-editor@1",
        "researcher": "role:source-researcher@2",
    }
    assert {step.role_ref for step in compiled.steps} == set(compiled.roles.values())
    assert {role.alias for role in compiled.role_definitions} == {"editor", "researcher"}


def test_role_alias_pointing_at_an_absent_role_file_is_refused() -> None:
    """A reference to a role revision this bundle does not carry is not resolved."""
    error = rejection(workflow(roles={"researcher": "role:researcher@1"}),
    "WORKFLOW_ROLE_UNRESOLVED")
    assert error.diagnostics[0].location == "workflow.yaml#/roles/id=researcher"


def test_role_content_is_part_of_the_compiled_identity_and_diff() -> None:
    """Changing a responsibility changes the template digest, not just the name."""
    baseline = compile_document()
    changed_role = {**ROLE_FILE, "responsibilities": ["read approved material"]}
    changed = compile_document(roles={"roles/researcher.yaml": changed_role})
    # The alias and the reference are unchanged; only the frozen content differs.
    assert baseline.roles == changed.roles
    assert baseline.role_definitions[0].digest != changed.role_definitions[0].digest
    assert baseline.compiled_digest != changed.compiled_digest
    document = diff(baseline, changed)
    assert document["roles_changed"] is True
    assert document["changed_roles"] == ["researcher"]

    constraints_changed = compile_document(
        roles={
            "roles/researcher.yaml": {
                **ROLE_FILE,
                "tool_constraints": {"paths": ["docs/**", "src/**"], "network": "denied"},
            }
        }
    )
    assert constraints_changed.compiled_digest != baseline.compiled_digest
    table = projection(constraints_changed)["roles"]
    assert table[0]["tool_constraints"]["paths"] == ["docs/**", "src/**"]
    assert table[0]["responsibilities"] == ROLE_FILE["responsibilities"]


def test_artifact_reference_must_name_a_real_depended_on_producer() -> None:
    """A reference-shaped input is resolved, never accepted by spelling."""
    steps = [dict(step) for step in workflow()["steps"]]  # type: ignore[arg-type]
    steps[1]["inputs"] = {"sources": ["ghost.output"]}
    error = rejection(workflow(steps=steps), "WORKFLOW_INPUT_REFERENCE_UNRESOLVED")
    assert error.diagnostics[0].location.endswith("/inputs/id=sources")

    steps = [dict(step) for step in workflow()["steps"]]  # type: ignore[arg-type]
    steps[0]["inputs"] = {"sources": "input.undeclared"}
    rejection(workflow(steps=steps), "WORKFLOW_INPUT_REFERENCE_UNRESOLVED")

    steps = [dict(step) for step in workflow()["steps"]]  # type: ignore[arg-type]
    steps[0]["inputs"] = {"sources": "option-a.output"}
    rejection(workflow(steps=steps), "WORKFLOW_INPUT_SELF_REFERENCE")

    steps = [dict(step) for step in workflow()["steps"]]  # type: ignore[arg-type]
    steps[1]["depends_on"] = []
    error = rejection(workflow(steps=steps), "WORKFLOW_INPUT_DEPENDENCY_MISSING")
    assert error.diagnostics[0].step_id == "comparison"


def test_plain_prose_stays_a_literal_while_a_reference_is_validated() -> None:
    """Unambiguous literal text remains usable without being read as a reference."""
    literal = compile_document(
        workflow(
            steps=[
                {
                    "id": "note",
                    "execution_kind": "artifact_aggregate@1",
                    "output_contract": "aggregated-report@1",
                    "inputs": {"sources": "literal:Hello. World"},
                }
            ],
            completion={"required_steps": ["note"], "artifact": "note.output"},
        )
    )
    # The marker is syntax; the compiled binding carries the data it denotes, so
    # the value a step is compiled against and the value the adapter receives
    # are the same string with the same meaning.
    assert literal.steps[0].inputs["sources"] == "Hello. World"
    # A dotted identity that is not a declared input is still an unresolved reference.
    rejection(
        workflow(
            steps=[
                {
                    "id": "note",
                    "execution_kind": "artifact_aggregate@1",
                    "output_contract": "aggregated-report@1",
                    "inputs": {"sources": "ghost.output"},
                }
            ],
            completion={"required_steps": ["note"], "artifact": "note.output"},
        ),
        "WORKFLOW_INPUT_REFERENCE_UNRESOLVED",
    )


def test_condition_facts_resolve_against_real_steps_and_inputs() -> None:
    """A fact names a declared input or a result of a step this step depends on."""
    steps = [dict(step) for step in workflow()["steps"]]  # type: ignore[arg-type]
    steps[1]["condition"] = {"at_least": ["result.nonexistent.score", 2]}
    rejection(workflow(steps=steps), "WORKFLOW_CONDITION_FACT_UNRESOLVED")

    steps = [dict(step) for step in workflow()["steps"]]  # type: ignore[arg-type]
    steps[1]["condition"] = {"equals": ["requirement.missing", "yes"]}
    rejection(workflow(steps=steps), "WORKFLOW_CONDITION_FACT_UNRESOLVED")

    steps = [dict(step) for step in workflow()["steps"]]  # type: ignore[arg-type]
    steps[1]["condition"] = {"equals": ["result.option-a.status", "ready"]}
    steps[1]["depends_on"] = []
    rejection(workflow(steps=steps), "WORKFLOW_INPUT_DEPENDENCY_MISSING")


def test_unknown_execution_kind_is_located_and_never_guessed() -> None:
    """An unregistered kind is refused wherever it appears, with a location."""
    steps = [dict(step) for step in workflow()["steps"]]  # type: ignore[arg-type]
    steps[0]["execution_kind"] = "ghost_kind@999"
    error = rejection(workflow(steps=steps), "WORKFLOW_KIND_UNREGISTERED")
    assert error.fields["execution_kind_ref"] == "ghost_kind@999"

    rejection(
        workflow(
            scheduling={
                "role": "researcher",
                "actions": ["expand_graph"],
                "expansions": [
                    {
                        "id": "e",
                        "roles": ["researcher"],
                        "execution_kinds": ["unregistered@999"],
                    }
                ],
            }
        ),
        "WORKFLOW_KIND_UNREGISTERED",
    )
    # A syntactically valid revision of a registered kind is still refused.
    rejection(
        workflow(
            scheduling={
                "role": "researcher",
                "actions": ["expand_graph"],
                "expansions": [
                    {
                        "id": "e",
                        "roles": ["researcher"],
                        "execution_kinds": ["artifact_aggregate@7"],
                    }
                ],
            }
        ),
        "WORKFLOW_KIND_UNREGISTERED",
    )


def test_dynamic_boundaries_contribute_to_capability_availability() -> None:
    """A dynamic reference to an unavailable kind makes the template unavailable."""
    compiled = compile_document(
        workflow(
            scheduling={
                "role": "researcher",
                "actions": ["expand_graph", "seal"],
                "expansions": [
                    {
                        "id": "fan-out",
                        "roles": ["researcher"],
                        "execution_kinds": ["agent_task@1"],
                    }
                ],
            }
        )
    )
    assert "agent_task@1" in compiled.unavailable_execution_kinds
    assert compiled.executable is False
    assert compiled.reviewable is True


def test_floating_references_are_never_resolved() -> None:
    """``latest`` and friends are refused rather than resolved at load time."""
    steps = [dict(step) for step in workflow()["steps"]]  # type: ignore[arg-type]
    steps[0]["execution_kind"] = "artifact_aggregate@latest"
    rejection(workflow(steps=steps), "WORKFLOW_REFERENCE_NOT_PINNED")


def test_missing_references_cycles_and_contract_mismatches_are_located() -> None:
    steps = [dict(step) for step in workflow()["steps"]]  # type: ignore[arg-type]
    steps[1]["depends_on"] = ["nowhere"]
    rejection(workflow(steps=steps), "WORKFLOW_DEPENDENCY_UNRESOLVED")

    steps = [dict(step) for step in workflow()["steps"]]  # type: ignore[arg-type]
    steps[0]["depends_on"] = ["comparison"]
    rejection(workflow(steps=steps), "WORKFLOW_DEPENDENCY_CYCLE")

    steps = [dict(step) for step in workflow()["steps"]]  # type: ignore[arg-type]
    steps[1]["inputs"] = {"title": "ok"}
    rejection(workflow(steps=steps), "WORKFLOW_INPUT_MISSING")

    steps = [dict(step) for step in workflow()["steps"]]  # type: ignore[arg-type]
    steps[0]["output_contract"] = "review-verdict@1"
    rejection(workflow(steps=steps), "WORKFLOW_OUTPUT_CONTRACT_INCOMPATIBLE")

    steps = [dict(step) for step in workflow()["steps"]]  # type: ignore[arg-type]
    steps[0]["output_contract"] = "unregistered-contract@1"
    rejection(workflow(steps=steps), "WORKFLOW_CONTRACT_UNREGISTERED")


def test_delivery_target_cannot_be_met_by_changing_its_name() -> None:
    """A text-producing kind cannot satisfy a patch or pr target."""
    error = rejection(workflow(delivery_kind="patch"), "WORKFLOW_DELIVERY_ARTIFACT_INCOMPATIBLE")
    assert error.diagnostics[0].step_id == "comparison"
    # A pr workflow with no delivery step is reported as missing that gate first;
    # once it declares one, the capability check applies to that step's artifact.
    rejection(workflow(delivery_kind="pr"), "WORKFLOW_DELIVERY_GATE_MISSING")

    pr = workflow(delivery_kind="pr")
    steps = [dict(step) for step in pr["steps"]]  # type: ignore[arg-type]
    steps.append(
        {
            "id": "publish",
            "execution_kind": "publish_pr@1",
            "output_contract": "repair-patch@1",
            "depends_on": ["comparison"],
            "role": None,
            "inputs": {},
        }
    )
    steps[-1].pop("role")
    pr["steps"] = steps
    pr["completion"] = {
        "required_steps": ["option-a", "comparison", "publish"],
        "artifact": "publish.output",
    }
    compiled = compile_document(pr)
    # The capability named is real but unimplemented here, so the template is
    # reviewable and explicitly not executable.
    assert compiled.reviewable is True
    assert compiled.executable is False
    assert "publish_pr@1" in compiled.unavailable_execution_kinds
    assert compiled.delivery_gate["publish_executor"] == "trusted_delivery_entrypoint"


def test_protected_gate_survives_optional_and_conditional_evasion() -> None:
    """An optional or conditional step cannot carry a required outcome."""
    steps = [dict(step) for step in workflow()["steps"]]  # type: ignore[arg-type]
    steps[1]["required"] = False
    rejection(workflow(steps=steps), "WORKFLOW_COMPLETION_OPTIONAL_OBLIGATION")

    steps = [dict(step) for step in workflow()["steps"]]  # type: ignore[arg-type]
    steps[1]["condition"] = {"equals": ["requirement.option_a", "skip"]}
    rejection(workflow(steps=steps), "WORKFLOW_COMPLETION_CONDITIONAL_OBLIGATION")

    # A publish step inside a report workflow is a target/side-effect mismatch.
    steps = [dict(step) for step in workflow()["steps"]]  # type: ignore[arg-type]
    steps.append(
        {
            "id": "publish",
            "execution_kind": "publish_pr@1",
            "output_contract": "repair-patch@1",
            "depends_on": ["comparison"],
            "inputs": {},
        }
    )
    steps[1]["inputs"] = {"sources": ["option-a.output"]}
    rejection(
        workflow(
            steps=steps,
            completion={"required_steps": ["option-a", "comparison"],
            "artifact": "comparison.output"},
        ),
        "WORKFLOW_DELIVERY_GATE_MISMATCH",
    )


def test_rework_must_be_bounded_and_resolve_its_targets() -> None:
    rejection(
        workflow(
            rework=[
                {
                    "trigger": {"step": "comparison", "outcome": "changes_requested"},
                    "returns_to": ["option-a"],
                    "maximum_rounds": 0,
                }
            ]
        ),
        "WORKFLOW_REWORK_UNBOUNDED",
    )
    rejection(
        workflow(
            rework=[
                {
                    "trigger": {"step": "comparison", "outcome": "changes_requested"},
                    "returns_to": ["ghost"],
                    "maximum_rounds": 2,
                }
            ]
        ),
        "WORKFLOW_REWORK_UNRESOLVED",
    )
    compiled = compile_document(
        workflow(
            rework=[
                {
                    "trigger": {"step": "comparison", "outcome": "changes_requested"},
                    "returns_to": ["option-a"],
                    "maximum_rounds": 2,
                    "invalidates": ["comparison"],
                }
            ]
        )
    )
    assert compiled.rework[0]["maximum_rounds"] == 2
    assert compiled.rework[0]["batch_identity"] == [
        "run_id",
        "repair_chain_id",
        "validation_cycle_id",
    ]


def test_more_than_one_hundred_nodes_compile_without_truncation() -> None:
    """The old hundred-item Plan ceiling does not apply to a legal workflow."""
    inputs = [f"requirement.item_{index:03d}" for index in range(120)]
    steps: list[dict[str, object]] = [
        {
            "id": f"fan_{index:03d}",
            "execution_kind": "artifact_aggregate@1",
            "output_contract": "aggregated-report@1",
            "inputs": {"sources": f"requirement.item_{index:03d}"},
        }
        for index in range(120)
    ]
    steps.append(
        {
            "id": "join",
            "execution_kind": "artifact_aggregate@1",
            "output_contract": "aggregated-report@1",
            "depends_on": [f"fan_{index:03d}" for index in range(120)],
            "inputs": {"sources": [f"fan_{index:03d}.output" for index in range(120)]},
        }
    )
    compiled = compile_document(
        workflow(
            inputs=inputs,
            steps=steps,
            completion={
                "required_steps": [
                    *(f"fan_{index:03d}" for index in range(120)),
                    "join",
                ],
                "artifact": "join.output",
            },
        )
    )
    assert len(compiled.steps) == 121
    assert compiled.executable is True
    rendered = projection(compiled)
    assert len(rendered["graph"]["nodes"]) == 121
    assert len(rendered["graph"]["edges"]) == 120


def test_dynamic_expansion_has_no_engine_count_cap() -> None:
    """An omitted count stays omitted and a large declared one is preserved."""
    omitted = compile_document(
        workflow(
            scheduling={
                "role": "researcher",
                "actions": ["expand_graph", "seal"],
                "expansions": [
                    {
                        "id": "fan-out",
                        "roles": ["researcher"],
                        "execution_kinds": ["artifact_aggregate@1"],
                    }
                ],
            }
        )
    )
    expansion = omitted.expansions[0]
    assert expansion["declared_member_policy"] is None
    assert expansion["engine_member_cap"] is None
    assert omitted.scheduling is not None
    assert omitted.scheduling["product_agent_limit"] is None

    large = compile_document(
        workflow(
            scheduling={
                "role": "researcher",
                "actions": ["expand_graph", "seal"],
                "expansions": [
                    {
                        "id": "fan-out",
                        "maximum_members": 100_000,
                        "roles": ["researcher"],
                        "execution_kinds": ["artifact_aggregate@1"],
                    }
                ],
            }
        )
    )
    assert large.expansions[0]["declared_member_policy"] == 100_000
    assert large.expansions[0]["engine_member_cap"] is None

    user_policy = compile_document(
        workflow(
            scheduling={
                "role": "researcher",
                "actions": ["expand_graph"],
                "agent_policy": {"maximum": 7, "policy_ref": "policy:user-budget@1"},
            }
        )
    )
    assert user_policy.scheduling is not None
    assert user_policy.scheduling["agent_policy"]["maximum"] == 7
    assert user_policy.scheduling["agent_policy"]["source"] == "user_declared"

    unspecified = compile_document(
        workflow(scheduling={"role": "researcher", "actions": ["expand_graph"]})
    )
    assert unspecified.scheduling is not None
    assert unspecified.scheduling["agent_policy"] == {
        "declared": False,
        "maximum": None,
        "source": "unspecified",
    }


def test_scheduling_never_grants_authority_by_role_name() -> None:
    compiled = compile_document(
        workflow(
            scheduling={
                "role": "researcher",
                "actions": ["expand_graph", "bind_role", "seal"],
                "allow_delegation": True,
                "maximum_delegation_depth": 2,
            }
        )
    )
    assert compiled.scheduling is not None
    assert compiled.scheduling["grants_authority"] is False
    assert compiled.scheduling["authority_source"] == "scheduler_grant_required"
    assert compiled.scheduling["delegation"] == {"allowed": True, "maximum_depth": 2}

    rejection(
        workflow(scheduling={"role": "editor", "actions": ["expand_graph"]}),
        "WORKFLOW_SCHEMA_INVALID",
    )
    rejection(
        workflow(scheduling={"role": "researcher", "actions": ["run_shell"]}),
        "WORKFLOW_SCHEDULING_ACTION_UNSUPPORTED",
    )


def test_mermaid_node_identities_never_collide() -> None:
    """Two differently spelled steps keep distinct nodes and correct edges."""
    document = workflow(
        steps=[
            {
                "id": "a-b",
                "execution_kind": "artifact_aggregate@1",
                "output_contract": "aggregated-report@1",
                "inputs": {"sources": "requirement.option_a"},
            },
            {
                "id": "a_2db",
                "execution_kind": "artifact_aggregate@1",
                "output_contract": "aggregated-report@1",
                "inputs": {"sources": "requirement.option_b"},
            },
            {
                "id": "join",
                "execution_kind": "artifact_aggregate@1",
                "output_contract": "aggregated-report@1",
                "depends_on": ["a-b", "a_2db"],
                "inputs": {"sources": ["a-b.output", "a_2db.output"]},
            },
        ],
        completion={"required_steps": ["a-b", "a_2db", "join"], "artifact": "join.output"},
    )
    diagram = projection(compile_document(document))["mermaid"]
    identities = {
        line.strip().split("[").pop(0).split(" ")[0]
        for line in diagram.splitlines()
        if line.strip().startswith("n")
    }
    # Three distinct nodes, and both dependency edges are present.
    assert len(identities) == 3
    assert diagram.count("-->") == 2
    assert "a-b" in diagram and "a_2db" in diagram
    assert diagram.count('"') == 6


def test_labels_are_escaped_for_mermaid_and_html() -> None:
    """A step identity cannot inject diagram or markup syntax."""
    assert escape_label('<script>alert("x")</script>') == (
        "#lt;script#gt;alert(#quot;x#quot;)#lt;/script#gt;"
    )
    assert escape_label("a#b") == "a#35;b"
    document = workflow(
        steps=[
            {
                "id": "note",
                "execution_kind": "artifact_aggregate@1",
                "output_contract": "aggregated-report@1",
                "inputs": {"sources": "literal:free text"},
            }
        ],
        completion={"required_steps": ["note"], "artifact": "note.output"},
        roles={},
    )
    diagram = projection(compile_document(document))["mermaid"]
    assert "<" not in diagram and ">" not in diagram


def test_compiled_identity_excludes_run_inputs_and_is_reproducible() -> None:
    """The template digest covers the template, not one Run's concrete inputs."""
    first = compile_document()
    second = compile_document()
    assert first.compiled_digest == second.compiled_digest
    assert first.compiler_revision == COMPILER_REVISION
    document = first.as_document()
    assert "run_id" not in str(document)
    assert "input_digest" not in str(document)
    assert document["resolved_bindings"] == {}
    # A different declared input set is a different template.
    other = compile_document(
        workflow(inputs=["requirement.other"], steps=[
            {
                "id": "option-a",
                "execution_kind": "artifact_aggregate@1",
                "output_contract": "aggregated-report@1",
                "inputs": {"sources": "requirement.other"},
            }
        ], completion={"required_steps": ["option-a"], "artifact": "option-a.output"})
    )
    assert other.compiled_digest != first.compiled_digest


def test_resolved_gateway_binding_identity_is_part_of_the_template() -> None:
    """A resolved fixed binding is recorded by exact revision and digest."""
    from karajan.workflows.compiler import compile_workflow

    resolved = {"binding:vendor-binding@1": {
        "binding": {"id": "vendor-binding", "revision": 1, "digest": "a" * 64},
        "connection": {"id": "local-gateway", "revision": 2, "digest": "b" * 64},
        "execution_eligible": False,
        "verified": False,
        "draft_only": True,
    }}
    plain = compile_document()
    bound = compile_workflow(
        MANIFEST,
        workflow(bindings={"researcher": "binding:vendor-binding@1"}),
        {"roles/researcher.yaml": ROLE_FILE},
        bundle_digest="bundle-digest-placeholder",
        role_digest="role-digest-placeholder",
        binding_index=resolved,
    )
    assert bound.binding_index["binding:vendor-binding@1"]["connection"]["revision"] == 2
    assert bound.binding_index["binding:vendor-binding@1"]["execution_eligible"] is False
    assert bound.compiled_digest != plain.compiled_digest


def test_the_real_adapter_is_reachable_from_the_registry() -> None:
    """The registered callable is the production one and is deterministic."""
    result = artifact_aggregate({"sources": ["option-a.output"], "title": "Comparison"})
    assert result["ordering"] == "input_name_ascending"
    assert result["encoding"] == "utf-8"
    assert result["content"].startswith("## sources\n")
    assert artifact_aggregate({"sources": ["option-a.output"], "title": "Comparison"}) == result
    assert result["content_digest"] == artifact_aggregate(
        {"title": "Comparison", "sources": ["option-a.output"]}
    )["content_digest"]
    # Input order in the mapping never changes the result; only the sorted names do.
    assert result["byte_length"] == len(result["content"].encode("utf-8"))
