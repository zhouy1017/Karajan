"""Independent Spec checks for explicit v2 planning; no execution is granted."""

import copy
import hashlib
import importlib.util
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import karajan.projects.registry as registry_module
import karajan.runs.planning as planning_module
import pytest
from fastapi.testclient import TestClient
from karajan.projects import ProjectError, ProjectRegistry
from karajan.runs import RunError, RunPlanner
from karajan.web import create_app

WORKTREE = Path.cwd().resolve()
v1_spec = importlib.util.spec_from_file_location(
    "v1_spec_inputs", WORKTREE / "tests/runs/test_planning.py"
)
assert v1_spec is not None and v1_spec.loader is not None
v1_inputs = importlib.util.module_from_spec(v1_spec)
sys.modules[v1_spec.name] = v1_inputs
v1_spec.loader.exec_module(v1_inputs)
ScriptedAdmissionReader = v1_inputs.ScriptedAdmissionReader
approval = v1_inputs.approval
create_request = v1_inputs.create_request
handoff_request = v1_inputs.handoff_request
project = v1_inputs.project
proposal = v1_inputs.proposal

__all__ = ["project"]
ARTIFACTS = WORKTREE / ".cache/v2-approval-spec/evidence"


def hash_value(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def rows(path):
    with closing(sqlite3.connect(path)) as db:
        return {
            name: list(db.execute('SELECT * FROM "' + name.replace('"', '""') + '" ORDER BY rowid'))
            for (name,) in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        }


def record(name, data):
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    (ARTIFACTS / (name + ".json")).write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def policy_input(configured):
    return {
        "schema_version": "karajan.execution-policy.v1",
        "id": "spec-execution",
        "revision": 1,
        "configuration_digest": configured["configuration"]["digest"],
        "constraints": {
            "profile_refs": [{"id": "fixture-profile", "revision": 1}],
            "channel_ids": ["fixture-channel"],
            "tools": ["repo-read", "repo-edit"],
            "data_destinations": ["local-fixture", "local-evidence"],
            "required_capabilities": ["controlled_tools"],
            "min_isolation": "tool_sandboxed",
        },
        "risk_policy": {
            "id": "spec-risk",
            "revision": 1,
            "mapping": {"standard": "T1", "critical": "T3"},
            "path_floors": [{"prefix": "src/auth", "minimum_class": "T3"}],
        },
        "channel_destinations": {"fixture-channel": "local-fixture"},
        "tool_policy": {
            "id": "spec-tools",
            "revision": 1,
            "tool_permissions": {"repo-read": ["fixture-read"], "repo-edit": ["fixture-write"]},
        },
        "context_policy": {
            "id": "spec-context",
            "revision": 1,
            "input_accounting": "explicit_approved_upper_bound",
            "reserved_output_tokens": 2000,
        },
        "max_context_tokens": 12000,
    }


def creation_input(configured, policy):
    request = create_request(configured)
    request.update(
        schema_version="karajan.create-run.v2",
        execution_policy={k: policy[k] for k in ("id", "revision", "digest")},
    )
    request["authorization"].update(
        channel_ids=["fixture-channel"],
        tools=["repo-read"],
        data_destinations=["local-fixture"],
        required_capabilities=["controlled_tools", "code_review"],
        min_isolation="tool_sandboxed",
        currency_limits={"USD": "2", "CNY": "3"},
        max_attempt_duration_seconds=28,
        max_quality_repair_rounds=1,
        stage_permissions={
            "bounded-worker": {"normal": True, "quality_indices": []},
            "standard-review": {"normal": True, "quality_indices": []},
        },
    )
    return request


def submitted_input(run, intent):
    request = copy.deepcopy(proposal(run, intent))
    request["schema_version"] = "karajan.submit-plan.v2"
    for task in request["plan"]["tasks"]:
        task.update(
            purpose=None,
            domains=["code"],
            required_capabilities=[],
            tools=["repo-read"],
            context_tokens=9000,
            duration_seconds=26,
        )
    return request


def approval_input(plan):
    return {
        "schema_version": "karajan.approve-plan.v2",
        **{
            k: plan[k]
            for k in (
                "term",
                "plan_revision",
                "plan_digest",
                "authorization_digest",
                "configuration_digest",
                "routing_digest",
            )
        },
    }


def arrange(tmp_path, project):
    registry, configured, repository = project
    config = registry.get_configuration(configured["id"])["configuration"]
    next(b for b in config["resources"]["budgets"] if b["id"] == "run")["currency_limits"] = {
        "USD": "10",
        "CNY": "20",
    }
    preview = registry.preview_configuration(
        configured["id"], config, command_key="spec-budget-preview", principal="owner"
    )
    configured = registry.apply_configuration(
        configured["id"],
        preview["preview_id"],
        expected_revision=configured["revision"],
        command_key="spec-budget-apply",
        principal="owner",
    )
    fixed = registry.register_execution_policy(
        configured["id"], policy_input(configured), command_key="spec-policy", principal="owner"
    )
    authority = ScriptedAdmissionReader()
    planner = RunPlanner(tmp_path / "runs.sqlite", registry, admissions=authority)
    run = planner.create(
        creation_input(configured, fixed), command_key="spec-create", principal="owner"
    )
    intent = planner.planning_intent(run["id"], term=1, command_key="spec-intent", principal="lead")
    planner.attach_planning_receipt(
        run["id"],
        intent["id"],
        receipt_ref=authority.grant(intent),
        command_key="spec-receipt",
        principal="owner",
    )
    return registry, configured, fixed, planner, run, intent


def test_imports_and_complete_public_round_trip(tmp_path, project):
    assert Path(planning_module.__file__).is_relative_to(WORKTREE / "backend")
    assert Path(registry_module.__file__).is_relative_to(WORKTREE / "backend")
    registry, configured, fixed, planner, run, intent = arrange(tmp_path, project)
    submitted = submitted_input(run, intent)
    plan = planner.submit_plan(run["id"], submitted, command_key="spec-plan", principal="lead")
    result = planner.approve_plan(
        run["id"], approval_input(plan), command_key="spec-approve", principal="owner"
    )
    reopened = RunPlanner(planner.database, ProjectRegistry(registry.database, [tmp_path]))
    assert reopened.get(run["id"])["approvals"] == [result]
    assert reopened.get(run["id"])["execution_policy_snapshot"] == fixed
    assert result["routing_digest"] == plan["routing_digest"]
    assert plan["routing_binding"]["execution_policy"]["project_id"] == configured["id"]
    assert plan["routing_binding"]["stage_grants"]["bounded-worker"]["quality"] == []
    assert (
        reopened.create(
            creation_input(configured, fixed), command_key="spec-create", principal="owner"
        )
        == run
    )
    assert (
        reopened.submit_plan(run["id"], submitted, command_key="spec-plan", principal="lead")
        == plan
    )
    assert (
        reopened.approve_plan(
            run["id"], approval_input(plan), command_key="spec-approve", principal="owner"
        )
        == result
    )
    assert reopened.get(run["id"])["dispatch_enabled"] is False
    record(
        "round-trip",
        {
            "policy_input": policy_input(configured),
            "create_input": creation_input(configured, fixed),
            "submit_input": submitted,
            "plan": plan,
            "approval": result,
            "imports": [planning_module.__file__, registry_module.__file__],
        },
    )


@pytest.mark.parametrize(
    "dimension",
    [
        "tools",
        "destination",
        "cash",
        "currency",
        "duration",
        "quality",
        "stage",
        "requirements",
        "checks",
        "context",
        "missing_task_field",
        "missing_auth_field",
    ],
)
def test_original_ceiling_applies_even_when_policy_or_configuration_is_broader(
    tmp_path, project, dimension
):
    _, _, _, planner, run, intent = arrange(tmp_path, project)
    request = submitted_input(run, intent)
    auth, task = request["plan"]["authorization"], request["plan"]["tasks"][0]
    if dimension == "tools":
        auth["tools"].append("repo-edit")
    elif dimension == "destination":
        auth["data_destinations"].append("local-evidence")
    elif dimension == "cash":
        auth["currency_limits"]["USD"] = "3"
    elif dimension == "currency":
        auth["currency_limits"]["EUR"] = "0"
    elif dimension == "duration":
        auth["max_attempt_duration_seconds"] = 29
    elif dimension == "quality":
        auth["max_quality_repair_rounds"] = 2
    elif dimension == "stage":
        auth["stage_permissions"]["bounded-worker"]["quality_indices"] = [0]
    elif dimension == "requirements":
        auth["required_capabilities"].remove("code_review")
    elif dimension == "checks":
        auth["checks"].remove("independent_review")
    elif dimension == "context":
        task["context_tokens"] = 10001
    elif dimension == "missing_task_field":
        del task["duration_seconds"]
    else:
        del auth["stage_permissions"]
    before = planner.get(run["id"])
    with pytest.raises(RunError) as failure:
        planner.submit_plan(run["id"], request, command_key="spec-denied", principal="lead")
    assert planner.get(run["id"]) == before
    record(
        "ceiling-" + dimension,
        {"input": request, "reason_code": failure.value.code, "run_unchanged": True},
    )


def test_narrowing_allows_explicit_plan_only_and_old_or_wrong_hashes_stay_inactive(
    tmp_path, project
):
    _, _, _, planner, run, intent = arrange(tmp_path, project)
    proposal_one = submitted_input(run, intent)
    first = planner.submit_plan(run["id"], proposal_one, command_key="spec-first", principal="lead")
    proposal_two = copy.deepcopy(proposal_one)
    proposal_two["expected_plan_revision"] = 1
    proposal_two["plan"]["authorization"]["currency_limits"] = {"USD": "1"}
    proposal_two["plan"]["authorization"]["max_attempt_duration_seconds"] = 26
    proposal_two["plan"]["authorization"]["max_quality_repair_rounds"] = 0
    proposal_two["plan"]["authorization"]["stage_permissions"]["standard-review"]["normal"] = False
    second = planner.submit_plan(
        run["id"], proposal_two, command_key="spec-second", principal="lead"
    )
    assert first["authorization_digest"] != second["authorization_digest"]
    assert first["routing_digest"] != second["routing_digest"]
    for field in ("plan_digest", "authorization_digest", "configuration_digest", "routing_digest"):
        wrong = approval_input(second)
        wrong[field] = "f" * 64
        with pytest.raises(RunError, match="APPROVAL_BINDING_MISMATCH"):
            planner.approve_plan(
                run["id"], wrong, command_key="spec-wrong-" + field, principal="owner"
            )
    with pytest.raises(RunError, match="PLAN_REVISION_STALE"):
        planner.approve_plan(
            run["id"], approval_input(first), command_key="spec-stale", principal="owner"
        )
    with pytest.raises(RunError, match="USER_DECISION_REQUIRED"):
        planner.approve_plan(
            run["id"], approval_input(second), command_key="spec-rogue-approve", principal="lead"
        )
    assert planner.get(run["id"])["approvals"] == []
    approved = planner.approve_plan(
        run["id"], approval_input(second), command_key="spec-final-approve", principal="owner"
    )
    assert approved["plan_revision"] == 2


def test_execution_policy_is_owned_scoped_and_cannot_reuse_component_body(tmp_path, project):
    registry, configured, fixed, planner, run, _ = arrange(tmp_path, project)
    for actor in ("lead", "another-owner"):
        with pytest.raises(ProjectError, match="USER_DECISION_REQUIRED"):
            registry.get_execution_policy(configured["id"], fixed["id"], 1, principal=actor)
    altered = policy_input(configured)
    altered["revision"] = 2
    altered["tool_policy"]["tool_permissions"]["repo-read"] = ["fixture-write"]
    with pytest.raises(ProjectError, match="EXECUTION_POLICY_COMPONENT_REVISION_CONFLICT"):
        registry.register_execution_policy(
            configured["id"], altered, command_key="spec-change-definition", principal="owner"
        )
    altered["tool_policy"]["revision"] = 2
    new = registry.register_execution_policy(
        configured["id"], altered, command_key="spec-new-definition", principal="owner"
    )
    assert new["digest"] != fixed["digest"]
    assert planner.get(run["id"])["execution_policy_snapshot"] == fixed
    wrong = creation_input(configured, fixed)
    wrong["execution_policy"]["digest"] = new["digest"]
    with pytest.raises(RunError, match="EXECUTION_POLICY_BINDING_MISMATCH"):
        planner.create(wrong, command_key="spec-mixed-version", principal="owner")
    wrong["execution_policy"]["body"] = altered
    with pytest.raises(RunError, match="RUN_INPUT_INVALID"):
        planner.create(wrong, command_key="spec-supplied-body", principal="owner")


def test_changed_task_requires_revision_and_handoff_expires_old_commander_authority(
    tmp_path, project
):
    _, _, _, planner, run, intent = arrange(tmp_path, project)
    request = submitted_input(run, intent)
    plan = planner.submit_plan(run["id"], request, command_key="spec-plan", principal="lead")
    request["expected_plan_revision"] = 1
    request["plan"]["tasks"][0]["duration_seconds"] = 25
    with pytest.raises(RunError, match="TASK_REVISION_REUSED"):
        planner.submit_plan(run["id"], request, command_key="spec-reuse", principal="lead")
    request["plan"]["tasks"][0]["revision"] = 2
    changed = planner.submit_plan(run["id"], request, command_key="spec-revision", principal="lead")
    assert changed["routing_digest"] != plan["routing_digest"]
    handoff = planner.propose_handoff(
        run["id"], handoff_request(2), command_key="spec-handoff", principal="owner"
    )
    planner.decide_handoff(
        run["id"],
        {
            "handoff_id": handoff["id"],
            "handoff_digest": handoff["digest"],
            "term": 1,
            "decision": "approve",
        },
        command_key="spec-handoff-approve",
        principal="owner",
    )
    with pytest.raises(RunError, match="COMMANDER_TERM_STALE"):
        planner.approve_plan(
            run["id"], approval_input(changed), command_key="spec-old-term", principal="owner"
        )
    assert planner.get(run["id"])["active_plan_revision"] is None
    assert planner.get(run["id"])["commander"]["term"] == 2


def test_old_http_approval_cannot_approve_v2_and_v1_receipt_stays_exact(tmp_path, project):
    registry, configured, _, planner, run, intent = arrange(tmp_path, project)
    plan = planner.submit_plan(
        run["id"], submitted_input(run, intent), command_key="spec-v2-plan", principal="lead"
    )
    legacy_input = create_request(configured)
    legacy = planner.create(legacy_input, command_key="spec-v1-create", principal="owner")
    before = planner.get(run["id"])
    app = create_app(
        tmp_path,
        origin="http://127.0.0.1:8765",
        bootstrap_token="spec-bootstrap",
        allowed_roots=[tmp_path],
    )
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        session = client.post(
            "/v1/session/bootstrap",
            json={"token": "spec-bootstrap"},
            headers={"Origin": "http://127.0.0.1:8765"},
        )
        headers = {
            "Origin": "http://127.0.0.1:8765",
            "X-CSRF-Token": session.json()["csrf_token"],
            "Idempotency-Key": "spec-old-ui",
        }
        old_ui_input = {
            k: plan[k]
            for k in (
                "term",
                "plan_revision",
                "plan_digest",
                "authorization_digest",
                "configuration_digest",
            )
        }
        response = client.post(
            f"/v1/runs/{run['id']}/plan-approval", json=old_ui_input, headers=headers
        )
        assert response.status_code == 409
        assert response.json()["reason_code"] == "RUN_PROTOCOL_VERSION_MISMATCH"
        assert planner.get(run["id"]) == before
        record(
            "old-http-ui",
            {
                "request": old_ui_input,
                "response": response.json(),
                "status_code": response.status_code,
                "run_unchanged": True,
            },
        )
    reopened = RunPlanner(planner.database, registry)
    assert reopened.create(legacy_input, command_key="spec-v1-create", principal="owner") == legacy
    assert reopened.get(legacy["id"]) == legacy
    assert "execution_policy_snapshot" not in legacy
    assert "tools" not in legacy["authorization_ceiling"]


def test_policy_lookup_is_project_scoped_even_with_identical_configuration(tmp_path, project):
    registry, configured, fixed, planner, run, _ = arrange(tmp_path, project)
    new_project = registry.create(
        {
            "name": "Second project",
            "repository_path": str(project[2]),
            "base_ref": "HEAD",
            "target_branch": "main",
            "allowed_target_branches": ["main"],
        },
        command_key="spec-second-project",
        principal="owner",
    )
    config = registry.get_configuration(configured["id"])["configuration"]
    preview = registry.preview_configuration(
        new_project["id"], config, command_key="spec-second-preview", principal="owner"
    )
    new_project = registry.apply_configuration(
        new_project["id"],
        preview["preview_id"],
        expected_revision=1,
        command_key="spec-second-apply",
        principal="owner",
    )
    assert new_project["configuration"]["digest"] == configured["configuration"]["digest"]
    cross_project = creation_input(new_project, fixed)
    with pytest.raises(RunError, match="EXECUTION_POLICY_NOT_FOUND"):
        planner.create(cross_project, command_key="spec-cross-project", principal="owner")
    assert len(planner.list(principal="owner")) == 1
    registered = registry.register_execution_policy(
        new_project["id"],
        policy_input(new_project),
        command_key="spec-second-policy",
        principal="owner",
    )
    assert registered["digest"] == fixed["digest"]
    second = planner.create(
        creation_input(new_project, registered), command_key="spec-second-run", principal="owner"
    )
    assert second["execution_policy_snapshot"]["project_id"] == new_project["id"]
    assert run["execution_policy_snapshot"]["project_id"] == configured["id"]


def test_current_configuration_cannot_be_combined_with_old_policy_and_original_run_is_frozen(
    tmp_path, project
):
    registry, configured, fixed, planner, run, _ = arrange(tmp_path, project)
    config = registry.get_configuration(configured["id"])["configuration"]
    next(b for b in config["resources"]["budgets"] if b["id"] == "run")["currency_limits"][
        "USD"
    ] = "12"
    preview = registry.preview_configuration(
        configured["id"], config, command_key="spec-changed-preview", principal="owner"
    )
    changed = registry.apply_configuration(
        configured["id"],
        preview["preview_id"],
        expected_revision=configured["revision"],
        command_key="spec-changed-apply",
        principal="owner",
    )
    assert changed["configuration"]["digest"] != configured["configuration"]["digest"]
    with pytest.raises(RunError, match="EXECUTION_POLICY_BINDING_MISMATCH"):
        planner.create(
            creation_input(changed, fixed), command_key="spec-stale-policy", principal="owner"
        )
    old = planner.get(run["id"])
    assert old["execution_policy_snapshot"] == fixed
    assert old["configuration_snapshot"] == run["configuration_snapshot"]
    assert old["authorization_ceiling"]["currency_limits"] == {"USD": "2", "CNY": "3"}
    record(
        "configuration-and-policy",
        {
            "old_configuration_digest": configured["configuration"]["digest"],
            "new_configuration_digest": changed["configuration"]["digest"],
            "preserved_policy_digest": old["execution_policy_snapshot"]["digest"],
            "rejected_reason": "EXECUTION_POLICY_BINDING_MISMATCH",
        },
    )


def test_legacy_database_migration_preserves_old_rows_and_command_receipts(tmp_path, project):
    registry, configured, _ = project
    authority = ScriptedAdmissionReader()
    planner = RunPlanner(tmp_path / "legacy-runs.sqlite", registry, admissions=authority)
    creation = create_request(configured)
    legacy = planner.create(creation, command_key="spec-legacy-create", principal="owner")
    intent = planner.planning_intent(
        legacy["id"], term=1, command_key="spec-legacy-intent", principal="lead"
    )
    planner.attach_planning_receipt(
        legacy["id"],
        intent["id"],
        receipt_ref=authority.grant(intent),
        command_key="spec-legacy-receipt",
        principal="owner",
    )
    submission = proposal(legacy, intent)
    plan = planner.submit_plan(
        legacy["id"], submission, command_key="spec-legacy-plan", principal="lead"
    )
    approved = planner.approve_plan(
        legacy["id"], approval(plan), command_key="spec-legacy-approve", principal="owner"
    )
    with closing(sqlite3.connect(registry.database)) as db:
        assert db.execute("SELECT COUNT(*) FROM execution_policies").fetchone()[0] == 0
        # Arrange the legacy project schema; no policy or operational record is deleted.
        db.execute("DROP TABLE execution_policies")
        db.commit()
    before_projects, before_runs = rows(registry.database), rows(planner.database)
    reopened_registry = ProjectRegistry(registry.database, [tmp_path])
    reopened = RunPlanner(planner.database, reopened_registry)
    assert reopened.create(creation, command_key="spec-legacy-create", principal="owner") == legacy
    assert (
        reopened.submit_plan(
            legacy["id"], submission, command_key="spec-legacy-plan", principal="lead"
        )
        == plan
    )
    assert (
        reopened.approve_plan(
            legacy["id"], approval(plan), command_key="spec-legacy-approve", principal="owner"
        )
        == approved
    )
    after_projects = rows(registry.database)
    assert after_projects.pop("execution_policies") == []
    assert after_projects == before_projects
    assert rows(planner.database) == before_runs
    assert "routing_binding" not in reopened.get(legacy["id"])["plans"][0]
    record(
        "legacy-migration",
        {
            "legacy_project_tables": list(before_projects),
            "existing_project_rows_preserved": True,
            "all_run_rows_preserved": True,
            "create_replay_sha256": hash_value(legacy),
            "plan_replay_sha256": hash_value(plan),
            "approval_replay_sha256": hash_value(approved),
        },
    )


@pytest.mark.parametrize("operation", ["create", "submit_plan", "approve_plan"])
def test_non_object_command_is_a_stable_public_error(tmp_path, project, operation):
    planner = RunPlanner(tmp_path / "invalid-runs.sqlite", project[0])
    before = rows(planner.database)
    method = getattr(planner, operation)
    args = ([],) if operation == "create" else ("nonexistent-run", [])
    expected = "RUN_INPUT_INVALID" if operation == "create" else "PLANNING_INPUT_INVALID"
    with pytest.raises(RunError, match=expected):
        method(*args, command_key="spec-invalid-json", principal="owner")
    assert rows(planner.database) == before
