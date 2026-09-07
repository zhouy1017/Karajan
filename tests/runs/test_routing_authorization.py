"""Versioned routing approval through real public commands and persisted reads."""

from copy import deepcopy
from pathlib import Path

import pytest
from karajan.projects import ProjectError, ProjectRegistry
from karajan.runs import RunError, RunPlanner
from test_planning import (
    ScriptedAdmissionReader,
    create_request,
    project,
    proposal,
)

__all__ = ["project"]


def policy_request(configured: dict) -> dict:
    return {
        "schema_version": "karajan.execution-policy.v1",
        "id": "project-execution",
        "revision": 1,
        "configuration_digest": configured["configuration"]["digest"],
        "constraints": {
            "profile_refs": [{"id": "fixture-profile", "revision": 1}],
            "channel_ids": ["fixture-channel"],
            "tools": ["fixture-tools"],
            "data_destinations": ["local-fixture"],
            "required_capabilities": [],
            "min_isolation": "tool_sandboxed",
        },
        "risk_policy": {
            "id": "repository-risk",
            "revision": 1,
            "mapping": {"standard": "T1", "critical": "T3"},
            "path_floors": [{"prefix": "src/auth", "minimum_class": "T3"}],
        },
        "channel_destinations": {"fixture-channel": "local-fixture"},
        "tool_policy": {
            "id": "local-tools",
            "revision": 1,
            "tool_permissions": {"fixture-tools": ["fixture-tools"]},
        },
        "context_policy": {
            "id": "explicit-context-envelope",
            "revision": 1,
            "input_accounting": "explicit_approved_upper_bound",
            "reserved_output_tokens": 1024,
        },
        "max_context_tokens": 8192,
    }


def request_v2(configured: dict, policy: dict) -> dict:
    request = create_request(configured)
    request["schema_version"] = "karajan.create-run.v2"
    request["execution_policy"] = {
        "id": policy["id"],
        "revision": policy["revision"],
        "digest": policy["digest"],
    }
    request["authorization"].update(
        channel_ids=["fixture-channel"],
        tools=["fixture-tools"],
        data_destinations=["local-fixture"],
        required_capabilities=["controlled_tools"],
        min_isolation="tool_sandboxed",
        currency_limits={"USD": "0", "CNY": "0"},
        max_attempt_duration_seconds=25,
        max_quality_repair_rounds=2,
        stage_permissions={
            "bounded-worker": {"normal": True, "quality_indices": [0]},
            "standard-review": {"normal": True, "quality_indices": []},
        },
    )
    return request


def submit_request(run: dict, intent: dict) -> dict:
    request = deepcopy(proposal(run, intent))
    request["schema_version"] = "karajan.submit-plan.v2"
    for task in request["plan"]["tasks"]:
        task.update(
            purpose=None,
            domains=["code"],
            required_capabilities=[],
            tools=["fixture-tools"],
            context_tokens=4096,
            duration_seconds=20,
        )
    return request


def approve_request(plan: dict) -> dict:
    return {
        "schema_version": "karajan.approve-plan.v2",
        **{
            key: plan[key]
            for key in (
                "term",
                "plan_revision",
                "plan_digest",
                "authorization_digest",
                "configuration_digest",
                "routing_digest",
            )
        },
    }


def admitted_v2(tmp_path: Path, project: tuple) -> tuple:
    registry, configured, _ = project
    fixed = registry.register_execution_policy(
        configured["id"], policy_request(configured), command_key="policy", principal="owner"
    )
    authority = ScriptedAdmissionReader()
    planner = RunPlanner(tmp_path / "runs.sqlite", registry, admissions=authority)
    run = planner.create(request_v2(configured, fixed), command_key="run", principal="owner")
    intent = planner.planning_intent(run["id"], term=1, command_key="intent", principal="lead")
    planner.attach_planning_receipt(
        run["id"],
        intent["id"],
        receipt_ref=authority.grant(intent),
        command_key="receipt",
        principal="owner",
    )
    return planner, run, intent


def test_owner_fixes_policy_and_exact_v2_approval_survives_reopen(
    tmp_path: Path, project: tuple
) -> None:
    registry, configured, _ = project
    fixed = registry.register_execution_policy(
        configured["id"], policy_request(configured), command_key="policy", principal="owner"
    )
    authority = ScriptedAdmissionReader()
    planner = RunPlanner(tmp_path / "runs.sqlite", registry, admissions=authority)
    run = planner.create(request_v2(configured, fixed), command_key="run", principal="owner")
    assert run["schema_version"] == "karajan.run-planning.v2"
    intent = planner.planning_intent(run["id"], term=1, command_key="intent", principal="lead")
    planner.attach_planning_receipt(
        run["id"],
        intent["id"],
        receipt_ref=authority.grant(intent),
        command_key="receipt",
        principal="owner",
    )
    plan = planner.submit_plan(
        run["id"], submit_request(run, intent), command_key="plan", principal="lead"
    )
    grants = plan["routing_binding"]["stage_grants"]["bounded-worker"]
    assert grants == {
        "normal": {"standard_qualified": [{"id": "fixture-profile", "revision": 1}]},
        "quality": [
            {
                "index": 0,
                "group": "critical_qualified",
                "profiles": [{"id": "fixture-profile", "revision": 1}],
            }
        ],
    }
    approved = planner.approve_plan(
        run["id"], approve_request(plan), command_key="approve", principal="owner"
    )
    reopened_registry = ProjectRegistry(registry.database, [tmp_path])
    reopened = RunPlanner(planner.database, reopened_registry)
    stored = reopened.get(run["id"], principal="owner")
    assert stored["plans"] == [plan]
    assert stored["approvals"] == [approved]
    assert stored["execution_policy_snapshot"] == fixed
    assert stored["active_plan_revision"] == 1
    assert stored["dispatch_enabled"] is False
    assert approved["routing_digest"] == plan["routing_digest"]
    assert (
        reopened.create(request_v2(configured, fixed), command_key="run", principal="owner") == run
    )
    assert (
        reopened.submit_plan(
            run["id"], submit_request(run, intent), command_key="plan", principal="lead"
        )
        == plan
    )
    assert (
        reopened.approve_plan(
            run["id"], approve_request(plan), command_key="approve", principal="owner"
        )
        == approved
    )


def test_policy_identity_is_owned_and_immutable_and_cannot_be_replaced_by_supplied_text(
    tmp_path: Path, project: tuple
) -> None:
    registry, configured, _ = project
    request = policy_request(configured)
    with pytest.raises(ProjectError, match="USER_DECISION_REQUIRED"):
        registry.register_execution_policy(
            configured["id"], request, command_key="rogue", principal="lead"
        )
    fixed = registry.register_execution_policy(
        configured["id"], request, command_key="policy", principal="owner"
    )
    changed = deepcopy(request)
    changed["max_context_tokens"] = 9999
    with pytest.raises(ProjectError, match="EXECUTION_POLICY_REVISION_CONFLICT"):
        registry.register_execution_policy(
            configured["id"], changed, command_key="replacement", principal="owner"
        )
    with pytest.raises(ProjectError, match="IDEMPOTENCY_CONFLICT"):
        registry.register_execution_policy(
            configured["id"], changed, command_key="policy", principal="owner"
        )
    assert (
        registry.register_execution_policy(
            configured["id"], request, command_key="policy", principal="owner"
        )
        == fixed
    )
    planner = RunPlanner(tmp_path / "runs.sqlite", registry)
    creation = request_v2(configured, fixed)
    creation["execution_policy"]["digest"] = "0" * 64
    with pytest.raises(RunError, match="EXECUTION_POLICY_BINDING_MISMATCH"):
        planner.create(creation, command_key="bad-digest", principal="owner")
    creation["execution_policy"] = {**creation["execution_policy"], "id": "not-registered"}
    with pytest.raises(RunError, match="EXECUTION_POLICY_NOT_FOUND"):
        planner.create(creation, command_key="missing", principal="owner")
    assert planner.list(principal="owner") == []


@pytest.mark.parametrize(
    "kind",
    [
        "tools",
        "channel_ids",
        "data_destinations",
        "cash",
        "duration",
        "quality",
        "stage",
        "context",
        "task_tools",
        "missing",
        "profile",
        "capability",
        "task_duration",
        "purpose",
        "duplicate_stage",
    ],
)
def test_v2_proposal_cannot_expand_or_invent_execution_authority(
    tmp_path: Path, project: tuple, kind: str
) -> None:
    planner, run, intent = admitted_v2(tmp_path, project)
    request = submit_request(run, intent)
    auth = request["plan"]["authorization"]
    task = request["plan"]["tasks"][0]
    if kind in {"tools", "channel_ids", "data_destinations"}:
        auth[kind].append("unapproved")
    elif kind == "cash":
        auth["currency_limits"]["USD"] = "0.01"
    elif kind == "duration":
        auth["max_attempt_duration_seconds"] = 26
    elif kind == "quality":
        auth["max_quality_repair_rounds"] = 3
    elif kind == "stage":
        auth["stage_permissions"]["bounded-worker"]["quality_indices"].append(1)
    elif kind == "context":
        task["context_tokens"] = 8193
    elif kind == "task_tools":
        task["tools"].append("shell")
    elif kind == "profile":
        auth["profile_refs"].append({"id": "not-approved", "revision": 1})
    elif kind == "capability":
        auth["required_capabilities"] = []
    elif kind == "task_duration":
        task["duration_seconds"] = 26
    elif kind == "purpose":
        task["purpose"] = "lead"
    elif kind == "duplicate_stage":
        auth["stage_permissions"]["bounded-worker"]["quality_indices"] = [0, 0]
    else:
        del task["duration_seconds"]
    with pytest.raises(RunError):
        planner.submit_plan(run["id"], request, command_key="rejected", principal="lead")
    assert planner.get(run["id"])["plans"] == []


def test_narrowing_stages_and_limits_is_bound_and_stale_approval_does_not_activate(
    tmp_path: Path, project: tuple
) -> None:
    planner, run, intent = admitted_v2(tmp_path, project)
    request = submit_request(run, intent)
    original = planner.submit_plan(run["id"], request, command_key="first", principal="lead")
    request["expected_plan_revision"] = 1
    request["plan"]["authorization"]["max_attempt_duration_seconds"] = 21
    request["plan"]["authorization"]["stage_permissions"]["bounded-worker"]["quality_indices"] = []
    changed = planner.submit_plan(run["id"], request, command_key="narrow", principal="lead")
    assert changed["routing_binding"]["stage_grants"]["bounded-worker"]["quality"] == []
    assert changed["authorization_digest"] != original["authorization_digest"]
    assert changed["plan_digest"] != original["plan_digest"]
    with pytest.raises(RunError, match="PLAN_REVISION_STALE"):
        planner.approve_plan(
            run["id"], approve_request(original), command_key="stale", principal="owner"
        )
    bad = approve_request(changed)
    bad["routing_digest"] = original["routing_digest"]
    with pytest.raises(RunError, match="APPROVAL_BINDING_MISMATCH"):
        planner.approve_plan(run["id"], bad, command_key="bad-routing", principal="owner")
    assert planner.get(run["id"])["active_plan_revision"] is None
    approved = planner.approve_plan(
        run["id"], approve_request(changed), command_key="approve", principal="owner"
    )
    assert approved["dispatch_enabled"] is False


def test_policy_names_without_tool_and_context_definitions_are_not_authority(
    project: tuple,
) -> None:
    registry, configured, _ = project
    request = policy_request(configured)
    request["tool_policy"] = {"id": "name-only", "revision": 1}
    request["context_policy"] = {"id": "name-only", "revision": 1}
    with pytest.raises(ProjectError, match="EXECUTION_POLICY_INVALID"):
        registry.register_execution_policy(
            configured["id"], request, command_key="empty-policy", principal="owner"
        )


def test_nested_policy_revision_is_immutable_and_existing_run_keeps_its_registered_body(
    tmp_path: Path,
    project: tuple,
) -> None:
    planner, run, _ = admitted_v2(tmp_path, project)
    registry, configured, _ = project
    newer = policy_request(configured)
    newer["revision"] = 2
    newer["risk_policy"]["path_floors"].append({"prefix": "src/data", "minimum_class": "T3"})
    with pytest.raises(ProjectError, match="EXECUTION_POLICY_COMPONENT_REVISION_CONFLICT"):
        registry.register_execution_policy(
            configured["id"], newer, command_key="reused-risk", principal="owner"
        )
    newer["risk_policy"]["revision"] = 2
    second = registry.register_execution_policy(
        configured["id"], newer, command_key="new-risk", principal="owner"
    )
    assert second["digest"] != run["execution_policy_snapshot"]["digest"]
    assert planner.get(run["id"])["execution_policy_snapshot"] == run["execution_policy_snapshot"]


def test_task_requirements_require_a_new_revision_and_preserve_v1_receipts(
    tmp_path: Path,
    project: tuple,
) -> None:
    planner, run, intent = admitted_v2(tmp_path, project)
    request = submit_request(run, intent)
    first = planner.submit_plan(run["id"], request, command_key="first", principal="lead")
    changed = deepcopy(request)
    changed["expected_plan_revision"] = 1
    changed["plan"]["tasks"][0]["context_tokens"] = 5000
    with pytest.raises(RunError, match="TASK_REVISION_REUSED"):
        planner.submit_plan(run["id"], changed, command_key="reuse", principal="lead")
    changed["plan"]["tasks"][0]["revision"] = 2
    second = planner.submit_plan(run["id"], changed, command_key="new-task", principal="lead")
    assert second["routing_digest"] != first["routing_digest"]
    assert second["authorization_digest"] != first["authorization_digest"]
    _, configured, _ = project
    legacy = planner.create(create_request(configured), command_key="legacy", principal="owner")
    assert legacy["schema_version"] == "karajan.run-planning.v1"
    assert "execution_policy_snapshot" not in legacy
    reopened = RunPlanner(planner.database, planner.projects)
    assert (
        reopened.create(create_request(configured), command_key="legacy", principal="owner")
        == legacy
    )
    assert reopened.get(legacy["id"]) == legacy
    fake_upgrade = submit_request(legacy, intent)
    fake_upgrade["plan"]["authorization"] = run["authorization_ceiling"]
    with pytest.raises(RunError, match="RUN_PROTOCOL_VERSION_MISMATCH"):
        reopened.submit_plan(
            legacy["id"], fake_upgrade, command_key="fake-upgrade", principal="lead"
        )


def test_legacy_coordinator_cannot_start_a_v2_run_by_ignoring_its_new_authorization(
    tmp_path: Path,
    project: tuple,
) -> None:
    from karajan.candidates import CandidateStore
    from karajan.execution import RunnerHost
    from karajan.orchestration import LocalFixtureRunner, SerialCoordinator

    planner, run, intent = admitted_v2(tmp_path, project)
    submitted = planner.submit_plan(
        run["id"], submit_request(run, intent), command_key="plan", principal="lead"
    )
    planner.approve_plan(
        run["id"], approve_request(submitted), command_key="approve", principal="owner"
    )
    host = RunnerHost(tmp_path / "host")
    coordinator = SerialCoordinator(
        tmp_path / "coordinator",
        planner,
        host,
        CandidateStore(tmp_path / "candidates"),
        fixture_runner=LocalFixtureRunner(tmp_path),
    )
    result = coordinator.enqueue(
        run["id"],
        "implement",
        profile_ref={"id": "fixture-profile", "revision": 1},
        command_key="enqueue",
        principal="owner",
    )
    assert result["reason_codes"] == ["APPROVED_ROUTING_INTEGRATION_REQUIRED"]
    assert result["attempts"] == []
    assert host.reconcile() == []
    assert not (tmp_path / "workspaces").exists()


def test_absent_quality_stage_is_a_stable_rejection_not_a_key_error(
    tmp_path: Path,
    project: tuple,
) -> None:
    registry, configured, _ = project
    fixed = registry.register_execution_policy(
        configured["id"], policy_request(configured), command_key="policy", principal="owner"
    )
    request = request_v2(configured, fixed)
    request["authorization"]["stage_permissions"] = {
        "lead-planning": {"normal": True, "quality_indices": [0]},
    }
    with pytest.raises(RunError, match="ROUTING_STAGE_NOT_AUTHORIZED"):
        RunPlanner(tmp_path / "runs.sqlite", registry).create(
            request, command_key="invalid-stage", principal="owner"
        )


@pytest.mark.parametrize("entry", ["create", "submit_plan", "approve_plan"])
@pytest.mark.parametrize("payload", [[], None, "not-an-object"])
def test_invalid_command_root_preserves_the_public_input_error(
    tmp_path: Path,
    project: tuple,
    entry: str,
    payload: object,
) -> None:
    registry, _, _ = project
    planner = RunPlanner(tmp_path / "runs.sqlite", registry)
    expected = "RUN_INPUT_INVALID" if entry == "create" else "PLANNING_INPUT_INVALID"
    command = getattr(planner, entry)
    with pytest.raises(RunError, match=expected):
        if entry == "create":
            command(payload, command_key="invalid", principal="owner")
        else:
            command("not-needed", payload, command_key="invalid", principal="owner")
    assert planner.list(principal="owner") == []


@pytest.mark.parametrize("revision", [1_000_000_001, 10**40])
def test_policy_reference_bounds_match_registered_policy_bounds(
    tmp_path: Path,
    project: tuple,
    revision: int,
) -> None:
    registry, configured, _ = project
    fixed = registry.register_execution_policy(
        configured["id"], policy_request(configured), command_key="policy", principal="owner"
    )
    with pytest.raises(ProjectError, match="EXECUTION_POLICY_REFERENCE_INVALID"):
        registry.get_execution_policy(configured["id"], fixed["id"], revision, principal="owner")
    request = request_v2(configured, fixed)
    request["execution_policy"]["revision"] = revision
    planner = RunPlanner(tmp_path / "runs.sqlite", registry)
    with pytest.raises(RunError, match="RUN_INPUT_INVALID"):
        planner.create(request, command_key="out-of-range", principal="owner")
    assert planner.list(principal="owner") == []
