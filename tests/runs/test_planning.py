"""Run planning behavior through public services and durable local SQLite."""

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from karajan.projects import ProjectRegistry
from karajan.runs import RunError, RunPlanner


@pytest.fixture
def project(tmp_path: Path) -> tuple[ProjectRegistry, dict, Path]:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", str(repository)], check=True, capture_output=True)
    (repository / "original.txt").write_text("untouched\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            "fixture",
        ],
        check=True,
        capture_output=True,
    )
    registry = ProjectRegistry(tmp_path / "projects.sqlite", [tmp_path])
    created = registry.create(
        {
            "name": "Offline",
            "repository_path": str(repository),
            "base_ref": "HEAD",
            "target_branch": "main",
            "allowed_target_branches": ["main"],
        },
        command_key="project",
        principal="owner",
    )
    configuration = json.loads(
        (Path(__file__).parents[2] / "examples/projects/offline-configuration.json").read_text()
    )
    preview = registry.preview_configuration(
        created["id"], configuration, command_key="preview", principal="owner"
    )
    configured = registry.apply_configuration(
        created["id"],
        preview["preview_id"],
        expected_revision=1,
        command_key="apply",
        principal="owner",
    )
    return registry, configured, repository


def create_request(project: dict) -> dict:
    reference = {"id": "fixture-profile", "revision": 1}
    return {
        "project_id": project["id"],
        "project_revision": project["revision"],
        "configuration_digest": project["configuration"]["digest"],
        "requirement": {"goal": "Add an offline report", "acceptance": ["Report is repeatable"]},
        "participants": [
            {"principal": "lead", "profile": reference, "purpose": "lead"},
            {"principal": "adviser", "profile": reference, "purpose": "advice"},
            {"principal": "replacement", "profile": reference, "purpose": "candidate"},
        ],
        "authorization": {
            "profile_refs": [reference],
            "read_paths": ["src", "tests"],
            "write_paths": ["src", "tests"],
            "budget_ref": "run",
            "checks": ["tests", "independent_review"],
            "delivery": "pull_request",
            "target_branch": "main",
        },
    }


def test_create_freezes_the_applied_project_and_survives_restart_without_repository_write(
    tmp_path: Path, project: tuple[ProjectRegistry, dict, Path]
) -> None:
    registry, configured, repository = project
    planner = RunPlanner(tmp_path / "runs.sqlite", registry)
    created = planner.create(create_request(configured), command_key="run", principal="owner")

    assert created["state"] == "planning"
    assert created["commander"]["term"] == 1
    assert created["commander"]["principal"] == "lead"
    assert created["configuration_snapshot"]["digest"] == configured["configuration"]["digest"]
    assert created["configuration_snapshot"]["configuration"]["resources"]["budgets"][0][
        "currency_limits"
    ] == {"USD": "0", "CNY": "0"}
    assert created["dispatch_enabled"] is False
    assert created["live_qualification"] == "not_run"
    assert RunPlanner(tmp_path / "runs.sqlite", registry).get(created["id"]) == created
    assert (repository / "original.txt").read_text() == "untouched\n"
    assert sorted(item.name for item in repository.iterdir()) == [".git", "original.txt"]


def test_repeated_concurrent_create_is_one_command_and_changed_payload_is_rejected(
    tmp_path: Path, project: tuple[ProjectRegistry, dict, Path]
) -> None:
    registry, configured, _ = project
    planner = RunPlanner(tmp_path / "runs.sqlite", registry)
    request = create_request(configured)
    with ThreadPoolExecutor(max_workers=4) as workers:
        results = list(
            workers.map(
                lambda _: planner.create(request, command_key="same", principal="owner"), range(4)
            )
        )
    assert results == [results[0]] * 4
    assert (
        RunPlanner(tmp_path / "runs.sqlite", registry).create(
            request, command_key="same", principal="owner"
        )
        == results[0]
    )
    changed = {**request, "requirement": {"goal": "different", "acceptance": ["new"]}}
    with pytest.raises(RunError, match="IDEMPOTENCY_CONFLICT"):
        planner.create(changed, command_key="same", principal="owner")


class ScriptedAdmissionReader:
    """Test-only authority responses, never evidence of real quota or pricing."""

    def __init__(self) -> None:
        self.receipts: dict[str, dict] = {}

    def __call__(self, reference: str) -> dict:
        return self.receipts[reference]

    def grant(self, intent: dict, reference: str = "receipt-1") -> str:
        self.receipts[reference] = {
            "receipt_ref": reference,
            "authority_revision": "test-authority-v1",
            "run_id": intent["run_id"],
            "intent_id": intent["id"],
            "term": intent["term"],
            "principal": intent["principal"],
            "profile": intent["profile"],
            "budget_ref": intent["budget_ref"],
            "state": "admitted",
            "provenance": "fixture",
        }
        return reference


def test_planning_intent_needs_a_matching_authority_receipt_and_never_grants_execution(
    tmp_path: Path, project: tuple[ProjectRegistry, dict, Path]
) -> None:
    registry, configured, _ = project
    authority = ScriptedAdmissionReader()
    planner = RunPlanner(tmp_path / "runs.sqlite", registry, admissions=authority)
    run = planner.create(create_request(configured), command_key="run", principal="owner")
    intent = planner.planning_intent(run["id"], term=1, command_key="lead-call", principal="lead")
    assert intent["budget_ref"] == "planning"
    assert intent["permissions"] == ["read"]
    assert intent["state"] == "awaiting_receipt"
    assert intent["dispatch_enabled"] is False
    reference = authority.grant(intent)
    attached = planner.attach_planning_receipt(
        run["id"],
        intent["id"],
        receipt_ref=reference,
        command_key="attach",
        principal="owner",
    )
    assert attached["receipt"]["state"] == "admitted"
    assert attached["receipt"]["provenance"] == "fixture"
    assert attached["dispatch_enabled"] is False
    assert planner.get(run["id"])["planning_intents"][0] == attached
    with pytest.raises(RunError, match="PLANNING_ACTOR_NOT_ACTIVE"):
        planner.planning_intent(
            run["id"], term=1, command_key="unauthorized-replacement", principal="replacement"
        )


def admitted_run(tmp_path: Path, project: tuple[ProjectRegistry, dict, Path]) -> tuple:
    registry, configured, _ = project
    authority = ScriptedAdmissionReader()
    planner = RunPlanner(tmp_path / "runs.sqlite", registry, admissions=authority)
    run = planner.create(create_request(configured), command_key="run", principal="owner")
    intent = planner.planning_intent(run["id"], term=1, command_key="intent", principal="lead")
    planner.attach_planning_receipt(
        run["id"],
        intent["id"],
        receipt_ref=authority.grant(intent),
        command_key="receipt",
        principal="owner",
    )
    return planner, run, intent, authority


def proposal(run: dict, intent: dict) -> dict:
    return {
        "term": 1,
        "intent_id": intent["id"],
        "expected_plan_revision": 0,
        "plan": {
            "summary": "Add and independently verify the report",
            "authorization": run["authorization_ceiling"],
            "tasks": [
                {
                    "id": "implement",
                    "revision": 1,
                    "role": "worker",
                    "readiness": "ready",
                    "complexity": "T2",
                    "risk": "standard",
                    "paths": ["src/report.py"],
                    "depends_on": [],
                    "acceptance": ["Report is repeatable"],
                    "required": True,
                },
                {
                    "id": "review",
                    "revision": 1,
                    "role": "reviewer",
                    "readiness": "ready",
                    "complexity": "T2",
                    "risk": "standard",
                    "paths": ["src/report.py"],
                    "depends_on": ["implement"],
                    "acceptance": ["Independent review passes"],
                    "required": True,
                },
            ],
        },
    }


def approval(plan: dict) -> dict:
    return {
        key: plan[key]
        for key in (
            "plan_revision",
            "plan_digest",
            "authorization_digest",
            "configuration_digest",
            "term",
        )
    }


def test_only_the_exact_user_approved_plan_becomes_the_active_immutable_revision(
    tmp_path: Path, project: tuple[ProjectRegistry, dict, Path]
) -> None:
    planner, run, intent, _ = admitted_run(tmp_path, project)
    submitted = planner.submit_plan(
        run["id"], proposal(run, intent), command_key="plan", principal="lead"
    )
    assert submitted["plan_revision"] == 1
    assert len(submitted["plan_digest"]) == 64
    assert planner.get(run["id"])["active_plan_revision"] is None
    receipt = planner.approve_plan(
        run["id"], approval(submitted), command_key="approve", principal="owner"
    )
    current = planner.get(run["id"])
    assert receipt["approved_by"] == "owner"
    assert current["active_plan_revision"] == 1
    assert current["plans"] == [submitted]
    assert current["approvals"] == [receipt]
    assert current["dispatch_enabled"] is False
    assert planner.task_gate(run["id"], "implement")["scope_approved"] is True
    assert planner.task_gate(run["id"], "implement")["dispatch_enabled"] is False


@pytest.mark.parametrize(
    "variant, reason",
    [
        ("cycle", "PLAN_GRAPH_INVALID"),
        ("missing_dependency", "PLAN_GRAPH_INVALID"),
        ("duplicate_task", "PLAN_GRAPH_INVALID"),
        ("path_escape", "PLAN_SCOPE_EXCEEDED"),
        ("absolute_path", "PLAN_PATH_INVALID"),
        ("cash_budget", "PLAN_SCOPE_EXCEEDED"),
        ("profile", "PLAN_SCOPE_EXCEEDED"),
        ("remove_review", "REQUIRED_CHECKS_REMOVED"),
    ],
)
def test_untrusted_plans_cannot_expand_scope_or_remove_required_checks(
    tmp_path: Path, project: tuple[ProjectRegistry, dict, Path], variant: str, reason: str
) -> None:
    planner, run, intent, _ = admitted_run(tmp_path, project)
    request = proposal(run, intent)
    plan = request["plan"]
    if variant == "cycle":
        plan["tasks"][0]["depends_on"] = ["review"]
    elif variant == "missing_dependency":
        plan["tasks"][0]["depends_on"] = ["missing"]
    elif variant == "duplicate_task":
        plan["tasks"].append(plan["tasks"][0])
    elif variant in {"path_escape", "absolute_path"}:
        plan["tasks"][0]["paths"] = ["outside/file" if variant == "path_escape" else "C:/secret"]
    elif variant == "cash_budget":
        plan["authorization"]["budget_ref"] = "unapproved-cash"
    elif variant == "profile":
        plan["authorization"]["profile_refs"] = [{"id": "unapproved", "revision": 1}]
    else:
        plan["authorization"]["checks"] = ["tests"]
    with pytest.raises(RunError, match=reason):
        planner.submit_plan(run["id"], request, command_key="invalid", principal="lead")
    assert planner.get(run["id"])["plans"] == []


def handoff_request(plan_revision: int = 1) -> dict:
    return {
        "term": 1,
        "expected_plan_revision": plan_revision,
        "candidate": "replacement",
        "checkpoint": {"summary": "Approved report work can continue", "artifacts": []},
        "resource_impact": {
            "budget_ref": "planning",
            "summary": "Same approved budget; no new cash",
        },
        "expires_at": time.time() + 60,
    }


@pytest.mark.parametrize("decision", ["none", "reject", "approve"])
def test_each_commander_handoff_requires_an_explicit_user_decision_without_pausing_approved_work(
    tmp_path: Path, project: tuple[ProjectRegistry, dict, Path], decision: str
) -> None:
    planner, run, intent, _ = admitted_run(tmp_path, project)
    plan = planner.submit_plan(
        run["id"], proposal(run, intent), command_key="plan", principal="lead"
    )
    planner.approve_plan(run["id"], approval(plan), command_key="approve", principal="owner")
    handoff = planner.propose_handoff(
        run["id"], handoff_request(), command_key="handoff", principal="owner"
    )
    assert handoff["state"] == "pending"
    assert handoff["candidate"]["principal"] == "replacement"
    assert handoff["binding"]["plan_digest"] == plan["plan_digest"]
    assert planner.get(run["id"])["commander"]["principal"] == "lead"
    if decision != "none":
        planner.decide_handoff(
            run["id"],
            {
                "handoff_id": handoff["id"],
                "handoff_digest": handoff["digest"],
                "term": 1,
                "decision": decision,
            },
            command_key="decide",
            principal="owner",
        )
    current = planner.get(run["id"])
    assert current["commander"]["term"] == (2 if decision == "approve" else 1)
    assert current["commander"]["principal"] == ("replacement" if decision == "approve" else "lead")
    assert planner.task_gate(run["id"], "implement")["scope_approved"] is True
    assert current["dispatch_enabled"] is False
    if decision == "approve":
        with pytest.raises(RunError, match="COMMANDER_TERM_STALE"):
            planner.submit_plan(
                run["id"], proposal(run, intent), command_key="old-term", principal="lead"
            )
    else:
        with pytest.raises(RunError, match="PLANNING_ACTOR_NOT_ACTIVE"):
            planner.planning_intent(
                run["id"], term=1, command_key="too-early", principal="replacement"
            )


def test_revising_a_task_requires_a_new_task_revision_and_reports_dependent_impact(
    tmp_path: Path, project: tuple[ProjectRegistry, dict, Path]
) -> None:
    planner, run, intent, _ = admitted_run(tmp_path, project)
    first = planner.submit_plan(
        run["id"], proposal(run, intent), command_key="p1", principal="lead"
    )
    planner.approve_plan(run["id"], approval(first), command_key="a1", principal="owner")
    changed = proposal(run, intent)
    changed["expected_plan_revision"] = 1
    changed["plan"]["tasks"][0]["acceptance"] = ["Report preserves ordering"]
    with pytest.raises(RunError, match="TASK_REVISION_REUSED"):
        planner.submit_plan(run["id"], changed, command_key="overwrite", principal="lead")
    changed["plan"]["tasks"][0]["revision"] = 2
    second = planner.submit_plan(run["id"], changed, command_key="p2", principal="lead")
    assert second["impact"]["changed"] == ["implement"]
    assert second["impact"]["affected"] == ["implement", "review"]
    assert planner.get(run["id"])["plans"][0] == first
    assert planner.task_gate(run["id"], "implement")["plan_revision"] == 1
    with pytest.raises(RunError, match="PLAN_REVISION_STALE"):
        planner.approve_plan(
            run["id"], approval(first), command_key="late-approve", principal="owner"
        )
    planner.approve_plan(run["id"], approval(second), command_key="a2", principal="owner")
    assert planner.task_gate(run["id"], "implement")["plan_revision"] == 2


@pytest.mark.parametrize(
    "variant", ["profile", "candidate", "owner_is_model", "review", "path", "budget"]
)
def test_user_run_configuration_cannot_invent_unregistered_profiles_or_weaken_fixed_policy(
    tmp_path: Path, project: tuple[ProjectRegistry, dict, Path], variant: str
) -> None:
    registry, configured, _ = project
    planner = RunPlanner(tmp_path / "runs.sqlite", registry)
    request = create_request(configured)
    if variant == "profile":
        request["authorization"]["profile_refs"] = [{"id": "unknown", "revision": 1}]
    elif variant == "candidate":
        request["participants"][2]["profile"] = {"id": "unknown", "revision": 1}
    elif variant == "owner_is_model":
        request["participants"][0]["principal"] = "owner"
    elif variant == "review":
        request["authorization"]["checks"] = ["tests"]
    elif variant == "path":
        request["authorization"]["write_paths"] = ["../outside"]
    else:
        request["authorization"]["budget_ref"] = "unknown"
    with pytest.raises(RunError):
        planner.create(request, command_key="invalid-create", principal="owner")


def test_run_listing_filters_owner_and_project_and_audit_survives_rejected_commands(
    tmp_path: Path, project: tuple[ProjectRegistry, dict, Path]
) -> None:
    planner, run, intent, _ = admitted_run(tmp_path, project)
    plan = planner.submit_plan(
        run["id"], proposal(run, intent), command_key="plan", principal="lead"
    )
    for _ in range(2):
        with pytest.raises(RunError, match="USER_DECISION_REQUIRED"):
            planner.approve_plan(
                run["id"], approval(plan), command_key="self-approve", principal="lead"
            )
    assert [item["id"] for item in planner.list(principal="owner")] == [run["id"]]
    assert planner.list(principal="other-owner") == []
    assert planner.list(principal="owner", project_id="other-project") == []
    assert len(planner.list(principal="owner", project_id=run["project_id"])) == 1
    events = planner.events(run["id"], principal="owner")
    rejected = [item for item in events if item["result"]["status"] == "rejected"]
    assert len(rejected) == 1
    assert rejected[0]["result"]["reason"] == "USER_DECISION_REQUIRED"
    assert len(rejected[0]["request_digest"]) == 64
    with pytest.raises(RunError, match="RUN_NOT_FOUND"):
        planner.get(run["id"], principal="other-owner")


@pytest.mark.parametrize("state", ["denied", "unknown"])
def test_a_denied_or_unknown_authority_receipt_cannot_authorize_plan_submission(
    tmp_path: Path, project: tuple[ProjectRegistry, dict, Path], state: str
) -> None:
    registry, configured, _ = project
    authority = ScriptedAdmissionReader()
    planner = RunPlanner(tmp_path / "runs.sqlite", registry, admissions=authority)
    run = planner.create(create_request(configured), command_key="run", principal="owner")
    intent = planner.planning_intent(run["id"], term=1, command_key="intent", principal="lead")
    ref = authority.grant(intent)
    authority.receipts[ref]["state"] = state
    planner.attach_planning_receipt(
        run["id"], intent["id"], receipt_ref=ref, command_key="receipt", principal="owner"
    )
    with pytest.raises(RunError, match="PLANNING_ADMISSION_REQUIRED"):
        planner.submit_plan(run["id"], proposal(run, intent), command_key="plan", principal="lead")
    assert planner.get(run["id"])["planning_intents"][0]["receipt"]["state"] == state


def test_adviser_can_record_budgeted_advice_but_never_submit_or_approve_a_plan(
    tmp_path: Path, project: tuple[ProjectRegistry, dict, Path]
) -> None:
    planner, run, lead_intent, authority = admitted_run(tmp_path, project)
    adviser = planner.planning_intent(run["id"], term=1, command_key="adviser", principal="adviser")
    planner.attach_planning_receipt(
        run["id"],
        adviser["id"],
        receipt_ref=authority.grant(adviser, "advice-receipt"),
        command_key="advice-attach",
        principal="owner",
    )
    with pytest.raises(RunError, match="ONLY_CURRENT_COMMANDER_CAN_SUBMIT"):
        planner.submit_plan(
            run["id"], proposal(run, adviser), command_key="advice-plan", principal="adviser"
        )
    plan = planner.submit_plan(
        run["id"], proposal(run, lead_intent), command_key="plan", principal="lead"
    )
    with pytest.raises(RunError, match="USER_DECISION_REQUIRED"):
        planner.approve_plan(
            run["id"], approval(plan), command_key="advice-approve", principal="adviser"
        )


@pytest.mark.parametrize("field", ["plan_digest", "authorization_digest", "configuration_digest"])
def test_a_plan_approval_with_mismatched_material_is_rejected_without_state_change(
    tmp_path: Path, project: tuple[ProjectRegistry, dict, Path], field: str
) -> None:
    planner, run, intent, _ = admitted_run(tmp_path, project)
    plan = planner.submit_plan(
        run["id"], proposal(run, intent), command_key="plan", principal="lead"
    )
    bad = {**approval(plan), field: "0" * 64}
    before = planner.get(run["id"])
    with pytest.raises(RunError, match="APPROVAL_BINDING_MISMATCH"):
        planner.approve_plan(run["id"], bad, command_key="bad-approve", principal="owner")
    assert planner.get(run["id"]) == before


@pytest.mark.parametrize(
    "variant, reason",
    [
        ("expired", "HANDOFF_STALE"),
        ("superseded", "HANDOFF_STALE"),
        ("new_plan", "HANDOFF_CHECKPOINT_STALE"),
        ("wrong_hash", "HANDOFF_DIGEST_MISMATCH"),
    ],
)
def test_late_or_changed_handoff_confirmation_never_activates_a_replacement(
    tmp_path: Path, project: tuple[ProjectRegistry, dict, Path], variant: str, reason: str
) -> None:
    planner, run, intent, _ = admitted_run(tmp_path, project)
    handoff = planner.propose_handoff(
        run["id"], handoff_request(0), command_key="handoff", principal="owner"
    )
    decision = {
        "handoff_id": handoff["id"],
        "handoff_digest": handoff["digest"],
        "term": 1,
        "decision": "approve",
    }
    if variant == "expired":
        planner = RunPlanner(
            tmp_path / "runs.sqlite", project[0], clock=lambda: handoff["expires_at"] + 1
        )
    elif variant == "superseded":
        planner.propose_handoff(
            run["id"], handoff_request(0), command_key="new-handoff", principal="owner"
        )
    elif variant == "new_plan":
        planner.submit_plan(
            run["id"], proposal(run, intent), command_key="new-plan", principal="lead"
        )
    else:
        decision["handoff_digest"] = "0" * 64
    with pytest.raises(RunError, match=reason):
        planner.decide_handoff(run["id"], decision, command_key="late-decision", principal="owner")
    assert planner.get(run["id"])["commander"]["term"] == 1


def test_concurrent_confirmation_creates_one_new_term_and_only_that_actor_can_submit(
    tmp_path: Path, project: tuple[ProjectRegistry, dict, Path]
) -> None:
    planner, run, _, authority = admitted_run(tmp_path, project)
    handoff = planner.propose_handoff(
        run["id"], handoff_request(0), command_key="handoff", principal="owner"
    )
    decision = {
        "handoff_id": handoff["id"],
        "handoff_digest": handoff["digest"],
        "term": 1,
        "decision": "approve",
    }

    def confirm(key: str) -> str:
        try:
            planner.decide_handoff(run["id"], decision, command_key=key, principal="owner")
            return "accepted"
        except RunError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(confirm, ["first", "second"]))
    assert sorted(results) == ["COMMANDER_TERM_STALE", "accepted"]
    assert planner.get(run["id"])["commander"]["term"] == 2
    intent = planner.planning_intent(
        run["id"], term=2, command_key="new-lead", principal="replacement"
    )
    planner.attach_planning_receipt(
        run["id"],
        intent["id"],
        receipt_ref=authority.grant(intent, "new-term-receipt"),
        command_key="new-receipt",
        principal="owner",
    )
    request = proposal(run, intent)
    request["term"] = 2
    assert (
        planner.submit_plan(run["id"], request, command_key="new-plan", principal="replacement")[
            "term"
        ]
        == 2
    )


def test_t0_and_unknown_authority_do_not_become_implementation_permission(
    tmp_path: Path, project: tuple[ProjectRegistry, dict, Path]
) -> None:
    planner, run, intent, _ = admitted_run(tmp_path, project)
    request = proposal(run, intent)
    request["plan"]["tasks"][0]["readiness"] = "T0"
    plan = planner.submit_plan(run["id"], request, command_key="plan", principal="lead")
    planner.approve_plan(run["id"], approval(plan), command_key="approve", principal="owner")
    assert planner.task_gate(run["id"], "implement")["scope_approved"] is False
    unavailable = RunPlanner(tmp_path / "runs.sqlite", project[0])
    with pytest.raises(RunError, match="ADMISSION_AUTHORITY_UNAVAILABLE"):
        unavailable.attach_planning_receipt(
            run["id"],
            intent["id"],
            receipt_ref="new",
            command_key="not-configured",
            principal="owner",
        )


def test_public_cli_creates_only_a_requirement_and_returns_the_same_owned_run_after_restart(
    tmp_path: Path, project: tuple[ProjectRegistry, dict, Path]
) -> None:
    registry, configured, repository = project
    source = tmp_path / "create.json"
    source.write_text(json.dumps(create_request(configured)), encoding="utf-8")
    common = [
        sys.executable,
        "-m",
        "karajan.runs",
        "--database",
        str(tmp_path / "runs.sqlite"),
        "--projects",
        str(registry.database),
        "--allowed-root",
        str(tmp_path),
        "--principal",
        "owner",
    ]
    environment = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[2] / "backend")}
    created = subprocess.run(
        common + ["create", "--input", str(source), "--command-key", "cli"],
        text=True,
        capture_output=True,
        check=False,
        env=environment,
        timeout=10,
    )
    assert created.returncode == 0, created.stdout + created.stderr
    run = json.loads(created.stdout)
    listed = subprocess.run(
        common + ["list"], text=True, capture_output=True, check=False, env=environment, timeout=10
    )
    assert listed.returncode == 0
    assert json.loads(listed.stdout) == [run]
    assert run["planning_intents"] == []
    assert run["plans"] == []
    assert run["dispatch_enabled"] is False
    assert (repository / "original.txt").read_text() == "untouched\n"


def test_run_keeps_its_frozen_configuration_when_the_project_changes_and_replay_is_historical(
    tmp_path: Path, project: tuple[ProjectRegistry, dict, Path]
) -> None:
    planner, run, intent, _ = admitted_run(tmp_path, project)
    plan = planner.submit_plan(
        run["id"], proposal(run, intent), command_key="plan", principal="lead"
    )
    registry, configured, _ = project
    registry.update(
        configured["id"],
        {
            "name": "Changed project name",
            "base_ref": "HEAD",
            "target_branch": "main",
            "allowed_target_branches": ["main"],
        },
        expected_revision=configured["revision"],
        command_key="project-update",
        principal="owner",
    )
    assert planner.create(create_request(configured), command_key="run", principal="owner") == run
    with pytest.raises(RunError, match="PROJECT_SNAPSHOT_CHANGED"):
        planner.create(create_request(configured), command_key="new-create", principal="owner")
    planner.approve_plan(run["id"], approval(plan), command_key="approve", principal="owner")
    assert planner.get(run["id"])["configuration_snapshot"] == run["configuration_snapshot"]


@pytest.mark.parametrize("variant", ["term_bool", "unknown_role", "enable_flag", "credential"])
def test_unknown_proposal_fields_or_types_cannot_grant_authority(
    tmp_path: Path, project: tuple[ProjectRegistry, dict, Path], variant: str
) -> None:
    planner, run, intent, _ = admitted_run(tmp_path, project)
    request = proposal(run, intent)
    if variant == "term_bool":
        request["term"] = True
    elif variant == "unknown_role":
        request["plan"]["tasks"][0]["role"] = "admin"
    elif variant == "enable_flag":
        request["plan"]["dispatch_enabled"] = True
    else:
        request["plan"]["authorization"]["api_key"] = "synthetic-not-a-real-secret"
    with pytest.raises(RunError, match="PLANNING_INPUT_INVALID"):
        planner.submit_plan(run["id"], request, command_key="invalid", principal="lead")
    assert planner.get(run["id"])["plans"] == []


def test_a_receipt_for_another_intent_or_profile_cannot_be_rebound_by_the_caller(
    tmp_path: Path, project: tuple[ProjectRegistry, dict, Path]
) -> None:
    planner, run, _, authority = admitted_run(tmp_path, project)
    intent = planner.planning_intent(run["id"], term=1, command_key="second", principal="lead")
    before = planner.get(run["id"])
    with pytest.raises(RunError, match="ADMISSION_BINDING_MISMATCH"):
        planner.attach_planning_receipt(
            run["id"],
            intent["id"],
            receipt_ref="receipt-1",
            command_key="reuse-receipt",
            principal="owner",
        )
    ref = authority.grant(intent, "second-receipt")
    authority.receipts[ref]["profile"] = {"id": "different-model-profile", "revision": 2}
    with pytest.raises(RunError, match="ADMISSION_BINDING_MISMATCH"):
        planner.attach_planning_receipt(
            run["id"],
            intent["id"],
            receipt_ref=ref,
            command_key="different-profile",
            principal="owner",
        )
    assert planner.get(run["id"]) == before


@pytest.mark.parametrize(
    "path", [".GIT/config", ".git./config", "src./report.py", "NUL", "src/aux.py"]
)
def test_windows_metadata_and_device_aliases_cannot_enter_the_write_authorization(
    tmp_path: Path, project: tuple[ProjectRegistry, dict, Path], path: str
) -> None:
    registry, configured, _ = project
    planner = RunPlanner(tmp_path / "runs.sqlite", registry)
    request = create_request(configured)
    request["authorization"]["write_paths"] = [path]
    with pytest.raises(RunError):
        planner.create(request, command_key="alias", principal="owner")
    assert planner.list(principal="owner") == []


def test_broad_directory_scope_does_not_let_a_plan_write_nested_git_metadata(
    tmp_path: Path, project: tuple[ProjectRegistry, dict, Path]
) -> None:
    planner, run, intent, _ = admitted_run(tmp_path, project)
    request = proposal(run, intent)
    request["plan"]["tasks"][0]["paths"] = ["src/.GIT/config"]
    with pytest.raises(RunError, match="PLAN_SCOPE_EXCEEDED"):
        planner.submit_plan(run["id"], request, command_key="nested-git", principal="lead")
    assert planner.get(run["id"])["plans"] == []


def test_mutating_with_an_invalid_run_identity_has_a_domain_error_and_no_state_change(
    tmp_path: Path, project: tuple[ProjectRegistry, dict, Path]
) -> None:
    planner, run, _, _ = admitted_run(tmp_path, project)
    before = planner.get(run["id"])
    with pytest.raises(RunError, match="COMMAND_IDENTITY_INVALID"):
        planner.planning_intent("\ud800", term=1, command_key="bad-run", principal="lead")
    assert planner.get(run["id"]) == before
