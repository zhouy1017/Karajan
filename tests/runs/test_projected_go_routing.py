"""Public approved Run to real qualification/Capacity stores, no model effects.

SyntheticSuite is an explicit trusted-producer substitute. Planning admission
receipts are scripted. Neither substitute is evidence of actual Go qualification.
"""

import pytest
from karajan.orchestration.admission import ApprovedTaskAdmission
from karajan.orchestration.routing import ApprovedRunRouting
from karajan.projects.demand import AttemptEstimateStore
from karajan.projects.publication import digest
from karajan.runs import RunPlanner
from test_approved_routing_capacity import capacity_for_plan
from test_planning import ScriptedAdmissionReader
from test_projected_qualification_store import CONTEXT, case, projected, qualify
from test_routing_authorization import approve_request, policy_request, request_v2, submit_request

__all__ = ["case", "projected"]


def approved_task(projected, tmp_path, *, change=None):
    projects = projected["projects"]
    project_id = projected["project_id"]
    if change == "fixture":
        projected["suite"].source_value.update(
            observation_origin="http_fixture", qualification_scope="projected_native_tools_fixture"
        )
    configuration = projects.get_configuration(project_id)["configuration"]
    registration = configuration["resources"]["profiles"][0]
    if change == "v1_suite":
        registration["profile"]["binding"]["native_settings"]["suite_ref"]["revision"] = 1
        projected["suite"].source_value.update(
            schema_version="karajan.fixed-go-suite-source.v1",
            qualification_scope="fixed_native_tools",
        )
        projected["suite"].source_value["suite_ref"]["revision"] = 1
    # Catalog evidence is explicitly fixture metadata for the planning gate.
    # ApprovedRunRouting replaces these declarations with current Store facts.
    for declaration in registration["capability_evidence"]:
        declaration.update(
            profile_digest=digest(registration["profile"]),
            runtime_version="1.18.29",
            provenance="fixture",
            evidence_ref="synthetic-catalog-declaration",
        )
    preview = projects.preview_configuration(
        project_id, configuration, principal="owner", command_key="routing-fixture-preview"
    )
    projects.apply_configuration(
        project_id,
        preview["preview_id"],
        principal="owner",
        command_key="routing-fixture-apply",
        expected_revision=projects.get(project_id)["revision"],
    )
    projected["registration"] = registration
    observation = qualify(projected)
    assert observation["status"] == "passed", observation
    configured = projects.get(project_id)
    policy = policy_request(configured)
    policy.update(schema_version="karajan.execution-policy.v2", max_context_tokens=12000)
    policy["constraints"].update(tools=["read", "edit"], data_destinations=["opencode-go"])
    policy["channel_destinations"] = {"fixture-channel": "opencode-go"}
    policy["tool_policy"]["tool_permissions"] = {"read": ["read"], "edit": ["edit"]}
    policy["context_policy"].update(
        reserved_output_tokens=4096,
        measurement={
            "method": "reference_tokenizer_estimate",
            "source_sha256": CONTEXT["source_sha256"],
            "fixed_margin": 2300,
            "ratio_margin_basis_points": 2200,
        },
    )
    environment = {"id": "python-validation", "revision": 1}
    policy["validation"] = {
        "id": "candidate-validation",
        "revision": 1,
        "checks": [
            {
                "id": "tests",
                "revision": 1,
                "argv": ["python", "-m", "pytest"],
                "environment_ref": environment,
                "timeout_seconds": 60,
            }
        ],
        "environments": [
            {
                **environment,
                "runtime_kind": "isolated-command",
                "platform": "linux_x64",
                "source_sha256": "e" * 64,
                "filesystem": "candidate_copy",
                "network": "none",
                "env": {},
                "max_log_bytes": 65536,
            }
        ],
        "review": {
            "id": "independent_review",
            "revision": 1,
            "environment_ref": environment,
            "context_policy": "candidate_and_acceptance_only",
            "independence_policy": "existing_candidate_independence_v1",
        },
    }
    if change == "v1_policy":
        policy["schema_version"] = "karajan.execution-policy.v1"
        del policy["validation"]
        del policy["context_policy"]["measurement"]
    elif change == "source":
        policy["context_policy"]["measurement"]["source_sha256"] = "a" * 64
    elif change == "margin":
        policy["context_policy"]["measurement"]["fixed_margin"] = 2047
    elif change == "output":
        policy["context_policy"]["reserved_output_tokens"] = 2048
    registered = projects.register_execution_policy(
        project_id, policy, principal="owner", command_key="execution-policy"
    )
    authority = ScriptedAdmissionReader()
    planner = RunPlanner(
        tmp_path / "approved-runs.sqlite", projects, admissions=authority, clock=lambda: 1000.0
    )
    creation = request_v2(configured, registered)
    creation["authorization"].update(
        tools=["read", "edit"],
        data_destinations=["opencode-go"],
        stage_permissions={
            "mechanical-worker": {"normal": True, "quality_indices": []},
            "critical-worker": {"normal": True, "quality_indices": []},
            "standard-review": {"normal": True, "quality_indices": []},
        },
    )
    run = planner.create(creation, principal="owner", command_key="run")
    intent = planner.planning_intent(run["id"], term=1, principal="lead", command_key="intent")
    planner.attach_planning_receipt(
        run["id"],
        intent["id"],
        receipt_ref=authority.grant(intent),
        principal="owner",
        command_key="planning-receipt",
    )
    submission = submit_request(run, intent)
    for task in submission["plan"]["tasks"]:
        task.update(
            tools=["read"] if task["role"] == "reviewer" else ["read", "edit"],
            complexity="T1",
            risk="standard",
            context_tokens=6000,
        )
    if change == "T3":
        submission["plan"]["tasks"][0]["risk"] = "critical"
    plan = planner.submit_plan(run["id"], submission, principal="lead", command_key="plan")
    planner.approve_plan(run["id"], approve_request(plan), principal="owner", command_key="approve")
    configuration = run["configuration_snapshot"]["configuration"]
    capacity = capacity_for_plan(tmp_path, configuration)
    estimates = AttemptEstimateStore(planner, clock=lambda: 1000.0)
    estimates.register(
        run["id"],
        "implement",
        {"id": "fixture-profile", "revision": 1},
        {
            "id": "owner-estimate",
            "revision": 1,
            "source_kind": "owner_conservative_estimate",
            "validity_seconds": 60,
            "measurement_semantics": "window_independent_attempt",
            "demand": [
                {"pool_id": p["id"], "unit": p["unit"], "window_kind": "fixed", "amount": "3"}
                for p in configuration["resources"]["quota_pools"]
            ],
            "completion_seconds": None,
            "basis": "Synthetic finite local acceptance forecast.",
        },
        principal="owner",
        command_key="estimate",
    )
    routing = ApprovedRunRouting(planner, projected["store"], capacity, estimates=estimates)
    admission = ApprovedTaskAdmission(tmp_path / "admission.sqlite", routing)
    return admission, routing, run, observation


def test_public_approval_persists_narrowed_execution_limits_and_reserves_once(projected, tmp_path):
    admission, routing, run, observation = approved_task(projected, tmp_path)
    queued = admission.enqueue(run["id"], "implement", principal="owner", command_key="enqueue")
    assert queued["state"] == "queued", queued
    assessment = queued["assessment"]
    limits = assessment["sources"]["profiles"][0]["execution_context"]
    assert limits["context"] == {
        **CONTEXT,
        "approved_input_tokens": 6000,
        "operating_context_tokens": 12000,
        "fixed_margin": 2300,
        "ratio_margin_basis_points": 2200,
    }
    assert limits["qualification_ref"] == "projected-go-qualification:" + observation["id"]
    assert limits["execution_policy_digest"] == run["execution_policy_snapshot"]["digest"]
    assert routing.get(run["id"], assessment["id"], principal="owner") == assessment
    reserved = admission.advance(run["id"], queued["id"], principal="owner")
    assert reserved["state"] == "reserved", reserved
    assert len(routing.capacity.snapshot()["reservations"]) == 1
    assert admission.advance(run["id"], queued["id"], principal="owner") == reserved
    assert reserved["dispatch_enabled"] is False


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("fixture", "RUNTIME_TOOLS_NOT_QUALIFIED"),
        ("v1_suite", "TASK_PERMISSION_SCOPE_NOT_QUALIFIED"),
        ("v1_policy", "PROJECTED_CONTEXT_POLICY_REQUIRED"),
        ("source", "PROJECTED_CONTEXT_SOURCE_OR_MARGIN_UNQUALIFIED"),
        ("margin", "PROJECTED_CONTEXT_SOURCE_OR_MARGIN_UNQUALIFIED"),
        ("output", "PROJECTED_CONTEXT_LIMIT_UNQUALIFIED"),
        ("T3", "PROJECTED_TASK_SCOPE_UNQUALIFIED"),
    ],
)
def test_unqualified_scope_or_policy_cannot_reserve_capacity(projected, tmp_path, change, reason):
    admission, routing, run, _ = approved_task(projected, tmp_path, change=change)
    operation = admission.enqueue(run["id"], "implement", principal="owner", command_key="enqueue")
    assert operation["state"] == "blocked", operation
    assert reason in operation["assessment"]["sources"]["profiles"][0]["reason_codes"]
    assert admission.advance(run["id"], operation["id"], principal="owner") == operation
    assert routing.capacity.snapshot()["reservations"] == []


@pytest.mark.parametrize("change", ["requalified", "revoked"])
def test_requalification_or_revocation_blocks_original_reserved_execution(
    projected, tmp_path, change
):
    admission, routing, run, observation = approved_task(projected, tmp_path)
    queued = admission.enqueue(run["id"], "implement", principal="owner", command_key="enqueue")
    reserved = admission.advance(run["id"], queued["id"], principal="owner")
    assert reserved["state"] == "reserved"
    if change == "requalified":
        new = qualify(projected, "replacement-qualification")
        assert new["id"] != observation["id"] and new["status"] == "passed"
    else:
        projected["store"].revoke(
            projected["project_id"], observation["id"], principal="owner", reason="withdrawn"
        )
    before = routing.capacity.snapshot()
    with routing.reserved_execution_guard(
        run["id"], reserved["assessment"]["id"], principal="owner"
    ) as checked:
        assert checked["state"] == "blocked", checked
        assert checked["activation_allowed"] is False
        assert checked["dispatch_enabled"] is False
    assert routing.capacity.snapshot() == before
    assert len(before["reservations"]) == 1
