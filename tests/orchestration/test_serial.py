"""Public serial coordination behavior across real stores and local processes."""

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest
from karajan.orchestration import SerialCoordinator

from .conftest import approve


@pytest.mark.parametrize("approved", [False, True])
def test_unapproved_or_unqualified_plan_never_prepares_or_spawns_a_writer(
    case: dict[str, Any], approved: bool
) -> None:
    if approved:
        approve(case)
    coordinator = SerialCoordinator(
        case["root"] / "orchestration", case["planner"], case["host"], case["candidates"]
    )
    queued = coordinator.enqueue(
        case["run"]["id"],
        "implement",
        profile_ref=case["profile"],
        command_key="enqueue",
        principal="owner",
    )
    assert queued["state"] == "blocked"
    assert queued["reason_codes"] == [
        "LIVE_QUALIFICATION_NOT_RUN" if approved else "TASK_SCOPE_NOT_APPROVED"
    ]
    assert coordinator.advance(case["run"]["id"])["attempts"] == []
    assert case["host"].reconcile() == []
    assert (case["repository"] / "original.txt").read_text() == "trusted baseline\n"
    assert not (case["repository"] / "src").exists()
    assert queued["dispatch_eligible"] is False


def test_restarted_outbox_runs_one_fixed_writer_and_freezes_actual_stopped_bytes(
    case: dict[str, Any],
) -> None:
    from karajan.orchestration import LocalFixtureRunner

    approve(case)
    runner = LocalFixtureRunner(case["fixture_root"])
    coordinator = SerialCoordinator(
        case["root"] / "orchestration",
        case["planner"],
        case["host"],
        case["candidates"],
        fixture_runner=runner,
    )
    initial = coordinator.enqueue(
        case["run"]["id"],
        "implement",
        profile_ref=case["profile"],
        command_key="enqueue",
        principal="owner",
    )
    assert initial["state"] == "queued"
    assert initial["counters"]["total_attempts"] == 1
    coordinator.advance(case["run"]["id"])
    reopened = SerialCoordinator(
        case["root"] / "orchestration",
        case["planner"],
        case["host"],
        case["candidates"],
        fixture_runner=runner,
    )
    assert (
        reopened.enqueue(
            case["run"]["id"],
            "implement",
            profile_ref=case["profile"],
            command_key="enqueue",
            principal="owner",
        )
        == initial
    )
    for _ in range(200):
        observed = reopened.advance(case["run"]["id"])
        if observed["state"] == "candidate_ready":
            break
        time.sleep(0.03)
    assert observed["state"] == "candidate_ready"
    assert len(observed["attempts"]) == 1
    attempt = observed["attempts"][0]
    assert case["host"].inspect(attempt["id"]).state == "exited"
    assert (
        Path(attempt["workspace"]) / "src/report.py"
    ).read_bytes() == b"print('fixture candidate')\n"
    assert not (case["repository"] / "src").exists()
    candidate = case["candidates"].get(observed["tasks"]["implement"]["candidate_id"])
    assert candidate["changed_paths"] == ["src/report.py"]
    assert candidate["request"]["writer"]["stopped"] is True
    assert candidate["request"]["writer"]["attempt_id"] == attempt["id"]
    assert observed["delivery_eligible"] is False


def advance_until(coordinator: SerialCoordinator, run_id: str, terminal: str) -> dict[str, Any]:
    for _ in range(250):
        result = coordinator.advance(run_id)
        if result["state"] == terminal:
            return result
        time.sleep(0.03)
    pytest.fail(f"Expected {terminal}, got {result['state']}: {result['reason_codes']}")


def test_real_fixed_checks_and_a_separate_reviewer_process_are_required_for_the_local_gate(
    case: dict[str, Any],
) -> None:
    from karajan.orchestration import LocalFixtureRunner

    approve(case)
    coordinator = SerialCoordinator(
        case["root"] / "orchestration",
        case["planner"],
        case["host"],
        case["candidates"],
        fixture_runner=LocalFixtureRunner(case["fixture_root"]),
    )
    coordinator.enqueue(
        case["run"]["id"],
        "implement",
        profile_ref=case["profile"],
        command_key="worker",
        principal="owner",
    )
    checked = advance_until(coordinator, case["run"]["id"], "awaiting_review")
    assert checked["tasks"]["implement"]["check_evidence"]["status"] == "passed"
    assert checked["delivery_eligible"] is False
    coordinator.enqueue(
        case["run"]["id"],
        "review",
        profile_ref=case["profile"],
        command_key="reviewer",
        principal="owner",
    )
    completed = advance_until(coordinator, case["run"]["id"], "local_gate_passed")
    review = completed["tasks"]["review"]["review_evidence"]
    assert review["status"] == "passed"
    assert review["input"]["author_reasoning_included"] is False
    assert review["input"]["actor"]["attempt_id"] != completed["attempts"][0]["id"]
    assert review["input"]["check_evidence_ids"] == [
        checked["tasks"]["implement"]["check_evidence"]["id"]
    ]
    assert len({row["workspace"] for row in completed["attempts"]}) == 3
    assert completed["counters"]["total_attempts"] == 3
    assert completed["delivery_eligible"] is False
    assert completed["live_qualification"] == "not_run"
    refusal = coordinator.enqueue(
        case["run"]["id"],
        "does-not-exist",
        profile_ref=case["profile"],
        command_key="invalid-after-pass",
        principal="owner",
    )
    assert refusal["reason_codes"] == ["TASK_SCOPE_NOT_APPROVED"]
    assert coordinator.advance(case["run"]["id"])["state"] == "local_gate_passed"


@pytest.mark.parametrize("outcome, status", [("fail", "failed"), ("missing_log", "unavailable")])
def test_a_failed_check_or_missing_process_log_blocks_the_candidate(
    case: dict[str, Any], outcome: str, status: str
) -> None:
    from karajan.orchestration import LocalFixtureRunner

    approve(case)
    coordinator = SerialCoordinator(
        case["root"] / "orchestration",
        case["planner"],
        case["host"],
        case["candidates"],
        fixture_runner=LocalFixtureRunner(case["fixture_root"], check_outcome=outcome),
    )
    coordinator.enqueue(
        case["run"]["id"],
        "implement",
        profile_ref=case["profile"],
        command_key="worker",
        principal="owner",
    )
    failed = advance_until(coordinator, case["run"]["id"], "blocked")
    assert failed["reason_codes"] == ["CHECK_NOT_PASSED"]
    evidence = failed["tasks"]["implement"]["check_evidence"]
    assert evidence["status"] == status
    assert evidence["input"]["exit_code"] == (1 if outcome == "fail" else 0)
    assert failed["delivery_eligible"] is False


def test_a_zero_exit_with_inconclusive_reviewer_output_is_not_a_pass(case: dict[str, Any]) -> None:
    from karajan.orchestration import LocalFixtureRunner

    approve(case)
    coordinator = SerialCoordinator(
        case["root"] / "orchestration",
        case["planner"],
        case["host"],
        case["candidates"],
        fixture_runner=LocalFixtureRunner(case["fixture_root"], review_verdict="inconclusive"),
    )
    coordinator.enqueue(
        case["run"]["id"],
        "implement",
        profile_ref=case["profile"],
        command_key="worker",
        principal="owner",
    )
    advance_until(coordinator, case["run"]["id"], "awaiting_review")
    coordinator.enqueue(
        case["run"]["id"],
        "review",
        profile_ref=case["profile"],
        command_key="reviewer",
        principal="owner",
    )
    failed = advance_until(coordinator, case["run"]["id"], "blocked")
    assert failed["tasks"]["review"]["review_evidence"]["status"] == "inconclusive"
    assert failed["attempts"][-1]["physical"]["exit_code"] == 0
    assert "REVIEW_NOT_PASSED" in failed["reason_codes"]


def test_pause_before_activation_keeps_the_durable_start_unspawned_until_resume(
    case: dict[str, Any],
) -> None:
    from karajan.orchestration import LocalFixtureRunner

    approve(case)
    coordinator = SerialCoordinator(
        case["root"] / "orchestration",
        case["planner"],
        case["host"],
        case["candidates"],
        fixture_runner=LocalFixtureRunner(case["fixture_root"]),
    )
    queued = coordinator.enqueue(
        case["run"]["id"],
        "implement",
        profile_ref=case["profile"],
        command_key="worker",
        principal="owner",
    )
    paused = coordinator.control(case["run"]["id"], "pause", command_key="pause", principal="owner")
    assert paused["state"] == "paused"
    assert coordinator.advance(case["run"]["id"])["state"] == "paused"
    assert case["host"].reconcile() == []
    assert not Path(queued["attempts"][0]["workspace"]).exists()
    coordinator.control(case["run"]["id"], "resume", command_key="resume", principal="owner")
    completed = advance_until(coordinator, case["run"]["id"], "candidate_ready")
    assert completed["attempts"][0]["start_key"] == queued["attempts"][0]["start_key"]
    assert completed["counters"]["total_attempts"] == 1


def test_cancel_stops_the_actual_child_tree_and_keeps_late_usage_separate(
    case: dict[str, Any],
) -> None:
    from karajan.orchestration import LocalFixtureRunner

    approve(case)
    coordinator = SerialCoordinator(
        case["root"] / "orchestration",
        case["planner"],
        case["host"],
        case["candidates"],
        fixture_runner=LocalFixtureRunner(case["fixture_root"], worker_behavior="wait"),
    )
    queued = coordinator.enqueue(
        case["run"]["id"],
        "implement",
        profile_ref=case["profile"],
        command_key="worker",
        principal="owner",
    )
    coordinator.advance(case["run"]["id"])
    attempt = queued["attempts"][0]
    path = Path(attempt["workspace"]) / "src/report.py"
    for _ in range(200):
        if path.exists() and b"heartbeat" in path.read_bytes():
            break
        time.sleep(0.03)
    assert b"heartbeat" in path.read_bytes()
    coordinator.control(case["run"]["id"], "cancel", command_key="cancel", principal="owner")
    cancelled = advance_until(coordinator, case["run"]["id"], "cancelled")
    size = path.stat().st_size
    time.sleep(0.08)
    assert path.stat().st_size == size
    assert cancelled["tasks"]["implement"]["candidate_id"] is None
    assert case["host"].inspect(attempt["id"]).state == "exited"
    assert (
        not case["host"]
        .receive_result(attempt["id"], 1, "late-result", {"result": "late"})
        .accepted
    )
    case["host"].record_usage(attempt["id"], 1, "late-usage", {"observed_tokens": 7})
    updated = coordinator.advance(case["run"]["id"])
    assert updated["attempts"][0]["physical"]["usage_events"][0]["usage"] == {"observed_tokens": 7}
    assert updated["attempts"][0]["physical"]["usage_settled"] is False
    assert updated["delivery_eligible"] is False


def test_changing_the_fixed_fixture_recipe_after_enqueue_cannot_start_it(
    case: dict[str, Any],
) -> None:
    from karajan.orchestration import LocalFixtureRunner

    approve(case)
    first = SerialCoordinator(
        case["root"] / "orchestration",
        case["planner"],
        case["host"],
        case["candidates"],
        fixture_runner=LocalFixtureRunner(case["fixture_root"]),
    )
    first.enqueue(
        case["run"]["id"],
        "implement",
        profile_ref=case["profile"],
        command_key="worker",
        principal="owner",
    )
    changed = SerialCoordinator(
        case["root"] / "orchestration",
        case["planner"],
        case["host"],
        case["candidates"],
        fixture_runner=LocalFixtureRunner(case["fixture_root"], check_outcome="fail"),
    )
    denied = changed.advance(case["run"]["id"])
    assert denied["state"] == "blocked"
    assert denied["reason_codes"] == ["FIXTURE_RECIPE_CHANGED"]
    assert case["host"].reconcile() == []


@pytest.mark.parametrize("case", ["same_family_t3"], indirect=True)
def test_missing_independent_reviewer_cannot_be_replaced_by_the_writer_profile(
    case: dict[str, Any],
) -> None:
    from karajan.orchestration import LocalFixtureRunner

    approve(case)
    coordinator = SerialCoordinator(
        case["root"] / "orchestration",
        case["planner"],
        case["host"],
        case["candidates"],
        fixture_runner=LocalFixtureRunner(case["fixture_root"]),
    )
    blocked = coordinator.enqueue(
        case["run"]["id"],
        "implement",
        profile_ref=case["profile"],
        command_key="worker",
        principal="owner",
    )
    assert blocked["state"] == "blocked"
    assert blocked["reason_codes"] == ["QUALIFIED_REVIEWER_UNAVAILABLE"]
    assert blocked["attempts"] == []
    assert case["host"].reconcile() == []


def revise(case: dict[str, Any], task_id: str, *, rename_to: str | None = None) -> dict[str, Any]:
    planner = case["planner"]
    run = planner.get(case["run"]["id"])
    revision = run["latest_plan_revision"] + 1
    intent = planner.planning_intent(
        run["id"],
        term=run["commander"]["term"],
        command_key=f"intent-{revision}",
        principal=run["commander"]["principal"],
    )
    reference = f"fixture-planning-{revision}"
    case["authority"].receipts[reference] = {
        "receipt_ref": reference,
        "authority_revision": "fixture-v1",
        "run_id": run["id"],
        "intent_id": intent["id"],
        "term": run["commander"]["term"],
        "principal": run["commander"]["principal"],
        "profile": case["profile"],
        "budget_ref": "planning",
        "state": "admitted",
        "provenance": "fixture",
    }
    planner.attach_planning_receipt(
        run["id"],
        intent["id"],
        receipt_ref=reference,
        command_key=f"attach-{revision}",
        principal="owner",
    )
    document = json.loads(json.dumps(run["plans"][-1]["plan"]))
    task = next(task for task in document["tasks"] if task["id"] == task_id)
    task["revision"] += 1
    task["acceptance"].append("New approved criterion")
    if rename_to is not None:
        task["id"] = rename_to
        for item in document["tasks"]:
            if task_id in item["depends_on"]:
                item["revision"] += 1
            item["depends_on"] = [
                rename_to if key == task_id else key for key in item["depends_on"]
            ]
    plan = planner.submit_plan(
        run["id"],
        {
            "term": run["commander"]["term"],
            "intent_id": intent["id"],
            "expected_plan_revision": revision - 1,
            "plan": document,
        },
        command_key=f"plan-{revision}",
        principal=run["commander"]["principal"],
    )
    planner.approve_plan(
        run["id"],
        {
            key: plan[key]
            for key in (
                "term",
                "plan_revision",
                "plan_digest",
                "authorization_digest",
                "configuration_digest",
            )
        },
        command_key=f"approve-{revision}",
        principal="owner",
    )
    return plan


def test_an_approved_change_to_independent_review_does_not_discard_worker_input(
    case: dict[str, Any],
) -> None:
    from karajan.orchestration import LocalFixtureRunner

    approve(case)
    coordinator = SerialCoordinator(
        case["root"] / "orchestration",
        case["planner"],
        case["host"],
        case["candidates"],
        fixture_runner=LocalFixtureRunner(case["fixture_root"]),
    )
    original = coordinator.enqueue(
        case["run"]["id"],
        "implement",
        profile_ref=case["profile"],
        command_key="worker",
        principal="owner",
    )
    updated = revise(case, "review")
    assert updated["impact"]["reusable"] == ["implement"]
    ready = advance_until(coordinator, case["run"]["id"], "candidate_ready")
    assert (
        ready["tasks"]["implement"]["binding"]["input_sha256"]
        == original["tasks"]["implement"]["binding"]["input_sha256"]
    )
    assert len(ready["attempts"]) == 1


def test_changed_approved_worker_input_revokes_and_stops_the_actual_writer(
    case: dict[str, Any],
) -> None:
    from karajan.orchestration import LocalFixtureRunner

    approve(case)
    coordinator = SerialCoordinator(
        case["root"] / "orchestration",
        case["planner"],
        case["host"],
        case["candidates"],
        fixture_runner=LocalFixtureRunner(case["fixture_root"], worker_behavior="wait"),
    )
    coordinator.enqueue(
        case["run"]["id"],
        "implement",
        profile_ref=case["profile"],
        command_key="worker",
        principal="owner",
    )
    started = coordinator.advance(case["run"]["id"])
    attempt = started["attempts"][0]
    path = Path(attempt["workspace"]) / "src/report.py"
    for _ in range(200):
        if path.exists() and b"heartbeat" in path.read_bytes():
            break
        time.sleep(0.02)
    assert b"heartbeat" in path.read_bytes()
    revise(case, "implement")
    blocked = advance_until(coordinator, case["run"]["id"], "blocked")
    assert blocked["reason_codes"] == ["APPROVED_INPUT_CHANGED"]
    assert case["host"].inspect(attempt["id"]).state == "exited"
    assert blocked["tasks"]["implement"]["state"] == "invalidated"
    assert blocked["tasks"]["implement"]["candidate_id"] is None
    size = path.stat().st_size
    time.sleep(0.06)
    assert path.stat().st_size == size


@pytest.mark.parametrize("point", ["before_spawn", "after_spawn"])
def test_lost_start_observation_reopens_the_same_identity_without_a_second_writer(
    case: dict[str, Any], point: str
) -> None:
    from karajan.execution import ProbeCrash
    from karajan.orchestration import LocalFixtureRunner

    approve(case)
    runner = LocalFixtureRunner(case["fixture_root"])
    coordinator = SerialCoordinator(
        case["root"] / "orchestration",
        case["planner"],
        case["host"],
        case["candidates"],
        fixture_runner=runner,
    )
    queued = coordinator.enqueue(
        case["run"]["id"],
        "implement",
        profile_ref=case["profile"],
        command_key="worker",
        principal="owner",
    )
    with pytest.raises(ProbeCrash):
        coordinator.advance(case["run"]["id"], crash_at=point)
    reopened = SerialCoordinator(
        case["root"] / "orchestration",
        case["planner"],
        case["host"],
        case["candidates"],
        fixture_runner=runner,
    )
    if point == "before_spawn":
        for _ in range(3):
            result = reopened.advance(case["run"]["id"])
        assert result["state"] == "unknown"
        assert not (Path(queued["attempts"][0]["workspace"]) / "src/report.py").exists()
    else:
        result = advance_until(reopened, case["run"]["id"], "candidate_ready")
        assert (
            Path(result["attempts"][0]["workspace"]) / "src/report.py"
        ).read_bytes() == b"print('fixture candidate')\n"
    assert result["counters"]["total_attempts"] == 1
    assert len(case["host"].reconcile()) == 1
    assert result["attempts"][0]["start_key"] == queued["attempts"][0]["start_key"]


@pytest.mark.parametrize("case", ["short_run"], indirect=True)
def test_elapsed_run_deadline_cannot_be_reset_by_pause_resume_or_reopening(
    case: dict[str, Any],
) -> None:
    from karajan.orchestration import LocalFixtureRunner

    approve(case)
    coordinator = SerialCoordinator(
        case["root"] / "orchestration",
        case["planner"],
        case["host"],
        case["candidates"],
        fixture_runner=LocalFixtureRunner(case["fixture_root"]),
    )
    coordinator.enqueue(
        case["run"]["id"],
        "implement",
        profile_ref=case["profile"],
        command_key="worker",
        principal="owner",
    )
    coordinator.control(case["run"]["id"], "pause", command_key="pause", principal="owner")
    time.sleep(1.05)
    coordinator = SerialCoordinator(
        case["root"] / "orchestration",
        case["planner"],
        case["host"],
        case["candidates"],
        fixture_runner=LocalFixtureRunner(case["fixture_root"]),
    )
    coordinator.control(case["run"]["id"], "resume", command_key="resume", principal="owner")
    blocked = advance_until(coordinator, case["run"]["id"], "blocked")
    assert blocked["reason_codes"] == ["RUN_DURATION_LIMIT"]
    assert case["host"].reconcile() == []
    assert blocked["counters"]["total_attempts"] == 1


def test_confirmed_infrastructure_failures_share_a_bounded_root_across_new_attempts(
    case: dict[str, Any],
) -> None:
    from karajan.orchestration import CoordinationError, LocalFixtureRunner

    approve(case)
    runner = LocalFixtureRunner(case["fixture_root"], worker_behavior="infrastructure_failure")
    coordinator = SerialCoordinator(
        case["root"] / "orchestration",
        case["planner"],
        case["host"],
        case["candidates"],
        fixture_runner=runner,
    )
    queued = coordinator.enqueue(
        case["run"]["id"],
        "implement",
        profile_ref=case["profile"],
        command_key="worker",
        principal="owner",
    )
    root = queued["attempts"][0]["root_task_id"]
    for number in (1, 2):
        failed = advance_until(coordinator, case["run"]["id"], "blocked")
        assert failed["attempts"][-1]["physical"]["exit_code"] == 75
        receipt = coordinator.retry(
            case["run"]["id"], "implement", command_key=f"retry-{number}", principal="owner"
        )
        assert receipt["counters"]["roots"][root]["infrastructure_retries"] == number
        assert (
            coordinator.retry(
                case["run"]["id"], "implement", command_key=f"retry-{number}", principal="owner"
            )
            == receipt
        )
    advance_until(coordinator, case["run"]["id"], "blocked")
    reopened = SerialCoordinator(
        case["root"] / "orchestration",
        case["planner"],
        case["host"],
        case["candidates"],
        fixture_runner=runner,
    )
    with pytest.raises(CoordinationError, match="ROOT_RETRY_LIMIT"):
        reopened.retry(case["run"]["id"], "implement", command_key="retry-3", principal="owner")
    final = reopened.snapshot(case["run"]["id"], principal="owner")
    assert final["counters"]["total_attempts"] == 3
    assert {row["root_task_id"] for row in final["attempts"]} == {root}
    assert len({row["workspace"] for row in final["attempts"]}) == 3
    assert final["counters"]["quality_repair_rounds"] == 0
    assert final["counters"]["max_quality_repair_rounds"] == 2


def test_an_unowned_preexisting_workspace_is_not_reused_for_a_new_start(
    case: dict[str, Any],
) -> None:
    from karajan.orchestration import LocalFixtureRunner

    approve(case)
    coordinator = SerialCoordinator(
        case["root"] / "orchestration",
        case["planner"],
        case["host"],
        case["candidates"],
        fixture_runner=LocalFixtureRunner(case["fixture_root"]),
    )
    queued = coordinator.enqueue(
        case["run"]["id"],
        "implement",
        profile_ref=case["profile"],
        command_key="worker",
        principal="owner",
    )
    workspace = Path(queued["attempts"][0]["workspace"])
    workspace.mkdir(parents=True)
    (workspace / "original.txt").write_text("unowned canary", encoding="utf-8")
    blocked = coordinator.advance(case["run"]["id"])
    assert blocked["state"] == "blocked"
    assert blocked["reason_codes"] == ["WORKSPACE_NOT_NEW"]
    assert case["host"].reconcile() == []
    assert (workspace / "original.txt").read_text() == "unowned canary"
    assert not (workspace / "src").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink canary is exercised on WSL/ext4")
def test_workspace_parent_link_cannot_redirect_a_fixture_writer_outside_its_root(
    case: dict[str, Any],
) -> None:
    from karajan.orchestration import LocalFixtureRunner

    approve(case)
    coordinator = SerialCoordinator(
        case["root"] / "orchestration",
        case["planner"],
        case["host"],
        case["candidates"],
        fixture_runner=LocalFixtureRunner(case["fixture_root"]),
    )
    coordinator.enqueue(
        case["run"]["id"],
        "implement",
        profile_ref=case["profile"],
        command_key="worker",
        principal="owner",
    )
    outside = case["root"] / "outside-canary"
    outside.mkdir()
    (case["fixture_root"] / "workspaces").symlink_to(outside, target_is_directory=True)
    blocked = coordinator.advance(case["run"]["id"])
    assert blocked["state"] == "blocked"
    assert blocked["reason_codes"] == ["FIXTURE_PATH_UNSAFE"]
    assert list(outside.iterdir()) == []
    assert case["host"].reconcile() == []


@pytest.mark.parametrize("case", ["one_attempt"], indirect=True)
def test_checks_and_review_cannot_bypass_the_run_total_attempt_limit(case: dict[str, Any]) -> None:
    from karajan.orchestration import LocalFixtureRunner

    approve(case)
    coordinator = SerialCoordinator(
        case["root"] / "orchestration",
        case["planner"],
        case["host"],
        case["candidates"],
        fixture_runner=LocalFixtureRunner(case["fixture_root"]),
    )
    coordinator.enqueue(
        case["run"]["id"],
        "implement",
        profile_ref=case["profile"],
        command_key="worker",
        principal="owner",
    )
    advance_until(coordinator, case["run"]["id"], "candidate_ready")
    blocked = coordinator.advance(case["run"]["id"])
    assert blocked["state"] == "blocked"
    assert blocked["reason_codes"] == ["RUN_ATTEMPT_LIMIT"]
    assert len(case["host"].reconcile()) == 1
    assert blocked["counters"]["total_attempts"] == 1
    assert blocked["delivery_eligible"] is False


def test_new_worker_identity_cannot_reset_a_failed_root_without_explicit_lineage(
    case: dict[str, Any],
) -> None:
    from karajan.orchestration import LocalFixtureRunner

    approve(case)
    coordinator = SerialCoordinator(
        case["root"] / "orchestration",
        case["planner"],
        case["host"],
        case["candidates"],
        fixture_runner=LocalFixtureRunner(
            case["fixture_root"], worker_behavior="infrastructure_failure"
        ),
    )
    original = coordinator.enqueue(
        case["run"]["id"],
        "implement",
        profile_ref=case["profile"],
        command_key="worker",
        principal="owner",
    )
    advance_until(coordinator, case["run"]["id"], "blocked")
    revise(case, "implement", rename_to="replacement-worker")
    blocked = coordinator.enqueue(
        case["run"]["id"],
        "replacement-worker",
        profile_ref=case["profile"],
        command_key="replace-root",
        principal="owner",
    )
    assert blocked["state"] == "blocked"
    assert blocked["reason_codes"] == ["TASK_LINEAGE_REQUIRED"]
    assert blocked["counters"]["total_attempts"] == 1
    assert set(blocked["counters"]["roots"]) == {original["attempts"][0]["root_task_id"]}


@pytest.mark.parametrize("case", ["unfinished_worker"], indirect=True)
def test_one_checked_candidate_does_not_complete_other_required_plan_tasks(
    case: dict[str, Any],
) -> None:
    from karajan.orchestration import LocalFixtureRunner

    approve(case)
    coordinator = SerialCoordinator(
        case["root"] / "orchestration",
        case["planner"],
        case["host"],
        case["candidates"],
        fixture_runner=LocalFixtureRunner(case["fixture_root"]),
    )
    coordinator.enqueue(
        case["run"]["id"],
        "implement",
        profile_ref=case["profile"],
        command_key="worker",
        principal="owner",
    )
    advance_until(coordinator, case["run"]["id"], "awaiting_review")
    coordinator.enqueue(
        case["run"]["id"],
        "review",
        profile_ref=case["profile"],
        command_key="reviewer",
        principal="owner",
    )
    for _ in range(250):
        state = coordinator.advance(case["run"]["id"])
        if state["tasks"]["review"]["state"] == "completed":
            break
        time.sleep(0.03)
    assert state["state"] == "awaiting_tasks"
    assert state["remaining_required_task_ids"] == ["second-worker"]
    assert state["delivery_eligible"] is False


@pytest.mark.parametrize("variant", ["later_check", "candidate_bytes", "superseded_candidate"])
def test_changed_evidence_or_candidate_invalidates_the_cached_local_gate_after_reopen(
    case: dict[str, Any], variant: str
) -> None:
    from karajan.orchestration import LocalFixtureRunner

    approve(case)
    runner = LocalFixtureRunner(case["fixture_root"])
    coordinator = SerialCoordinator(
        case["root"] / "orchestration",
        case["planner"],
        case["host"],
        case["candidates"],
        fixture_runner=runner,
    )
    coordinator.enqueue(
        case["run"]["id"],
        "implement",
        profile_ref=case["profile"],
        command_key="worker",
        principal="owner",
    )
    advance_until(coordinator, case["run"]["id"], "awaiting_review")
    coordinator.enqueue(
        case["run"]["id"],
        "review",
        profile_ref=case["profile"],
        command_key="review",
        principal="owner",
    )
    completed = advance_until(coordinator, case["run"]["id"], "local_gate_passed")
    candidate = case["candidates"].get(completed["tasks"]["implement"]["candidate_id"])
    if variant == "later_check":
        original = completed["tasks"]["implement"]["check_evidence"]["input"]
        case["candidates"].record_check(
            original | {"evidence_key": "later-failed-check", "exit_code": 1},
            log=b"Actual later fixture check failed\n",
        )
    elif variant == "candidate_bytes":
        Path(candidate["manifest"][-1]["artifact"]["path"]).write_bytes(
            b"corrupted fixture artifact"
        )
    else:
        workspace = Path(completed["attempts"][0]["workspace"])
        (workspace / "src/report.py").write_bytes(b"print('later fixture revision')\n")
        case["candidates"].freeze(workspace, candidate["request"])
    reopened = SerialCoordinator(
        case["root"] / "orchestration",
        case["planner"],
        case["host"],
        case["candidates"],
        fixture_runner=runner,
    )
    blocked = reopened.advance(case["run"]["id"])
    assert blocked["state"] == "blocked"
    reason = {
        "later_check": "REVIEW_CHECK_SET_CHANGED",
        "candidate_bytes": "ARTIFACT_UNAVAILABLE",
        "superseded_candidate": "CANDIDATE_SUPERSEDED",
    }[variant]
    assert reason in blocked["reason_codes"]
    assert len(case["host"].reconcile()) == 3
    assert blocked["delivery_eligible"] is False


def test_expired_queued_activation_is_a_durable_refusal_without_a_prepared_process(
    case: dict[str, Any],
) -> None:
    from karajan.orchestration import LocalFixtureRunner

    approve(case)
    coordinator = SerialCoordinator(
        case["root"] / "orchestration",
        case["planner"],
        case["host"],
        case["candidates"],
        fixture_runner=LocalFixtureRunner(case["fixture_root"]),
    )
    queued = coordinator.enqueue(
        case["run"]["id"],
        "implement",
        profile_ref=case["profile"],
        command_key="worker",
        principal="owner",
    )
    # WSL's wall clock can be corrected while monotonic sleep is in progress.
    # Observe the actual UTC deadline; a requested sleep is not expiry evidence.
    wait_limit = time.monotonic() + 60
    while (
        time.time() <= queued["attempts"][0]["activation"]["expires_at"]
        and time.monotonic() < wait_limit
    ):
        time.sleep(0.1)
    observed_time = time.time()
    assert observed_time > queued["attempts"][0]["activation"]["expires_at"], {
        "actual_wall_time": observed_time,
        "expires_at": queued["attempts"][0]["activation"]["expires_at"],
    }
    blocked = coordinator.advance(case["run"]["id"])
    assert blocked["state"] == "blocked"
    assert blocked["reason_codes"] == ["ACTIVATION_NOT_CURRENT"]
    assert case["host"].reconcile() == []
    assert not Path(queued["attempts"][0]["workspace"]).exists()
    assert coordinator.advance(case["run"]["id"])["attempts"] == blocked["attempts"]


def test_changed_fixture_recipe_stops_an_already_running_writer_before_blocking(
    case: dict[str, Any],
) -> None:
    from karajan.orchestration import LocalFixtureRunner

    approve(case)
    coordinator = SerialCoordinator(
        case["root"] / "orchestration",
        case["planner"],
        case["host"],
        case["candidates"],
        fixture_runner=LocalFixtureRunner(case["fixture_root"], worker_behavior="wait"),
    )
    queued = coordinator.enqueue(
        case["run"]["id"],
        "implement",
        profile_ref=case["profile"],
        command_key="worker",
        principal="owner",
    )
    coordinator.advance(case["run"]["id"])
    attempt = queued["attempts"][0]
    path = Path(attempt["workspace"]) / "src/report.py"
    for _ in range(200):
        if path.exists() and b"heartbeat" in path.read_bytes():
            break
        time.sleep(0.02)
    assert b"heartbeat" in path.read_bytes()
    changed = SerialCoordinator(
        case["root"] / "orchestration",
        case["planner"],
        case["host"],
        case["candidates"],
        fixture_runner=LocalFixtureRunner(case["fixture_root"]),
    )
    blocked = advance_until(changed, case["run"]["id"], "blocked")
    assert blocked["reason_codes"] == ["FIXTURE_RECIPE_CHANGED"]
    assert case["host"].inspect(attempt["id"]).state == "exited"
    assert blocked["tasks"]["implement"]["state"] == "invalidated"
    assert blocked["tasks"]["implement"]["candidate_id"] is None
    size = path.stat().st_size
    time.sleep(0.06)
    assert path.stat().st_size == size


def test_user_approved_commander_handoff_retains_an_unaffected_worker_start(
    case: dict[str, Any],
) -> None:
    from karajan.orchestration import LocalFixtureRunner

    approve(case)
    coordinator = SerialCoordinator(
        case["root"] / "orchestration",
        case["planner"],
        case["host"],
        case["candidates"],
        fixture_runner=LocalFixtureRunner(case["fixture_root"]),
    )
    queued = coordinator.enqueue(
        case["run"]["id"],
        "implement",
        profile_ref=case["profile"],
        command_key="worker",
        principal="owner",
    )
    proposal = case["planner"].propose_handoff(
        case["run"]["id"],
        {
            "term": 1,
            "expected_plan_revision": 1,
            "candidate": "replacement",
            "checkpoint": {"summary": "Existing approved Worker remains valid", "artifacts": []},
            "resource_impact": {
                "budget_ref": "planning",
                "summary": "No new cash or authorization",
            },
            "expires_at": time.time() + 60,
        },
        command_key="handoff",
        principal="owner",
    )
    assert case["planner"].get(case["run"]["id"])["commander"]["term"] == 1
    case["planner"].decide_handoff(
        case["run"]["id"],
        {
            "handoff_id": proposal["id"],
            "handoff_digest": proposal["digest"],
            "term": 1,
            "decision": "approve",
        },
        command_key="accept-handoff",
        principal="owner",
    )
    assert case["planner"].get(case["run"]["id"])["commander"]["term"] == 2
    ready = advance_until(coordinator, case["run"]["id"], "candidate_ready")
    assert ready["attempts"][0]["start_key"] == queued["attempts"][0]["start_key"]
    assert ready["tasks"]["implement"]["binding"]["planning_term"] == 1
    assert len(case["host"].reconcile()) == 1


@pytest.mark.parametrize("case", ["review_subset"], indirect=True)
def test_fixture_review_must_cover_its_whole_fixed_candidate_scope_before_start(
    case: dict[str, Any],
) -> None:
    from karajan.orchestration import LocalFixtureRunner

    approve(case)
    coordinator = SerialCoordinator(
        case["root"] / "orchestration",
        case["planner"],
        case["host"],
        case["candidates"],
        fixture_runner=LocalFixtureRunner(case["fixture_root"]),
    )
    coordinator.enqueue(
        case["run"]["id"],
        "implement",
        profile_ref=case["profile"],
        command_key="worker",
        principal="owner",
    )
    advance_until(coordinator, case["run"]["id"], "awaiting_review")
    blocked = coordinator.enqueue(
        case["run"]["id"],
        "review",
        profile_ref=case["profile"],
        command_key="review",
        principal="owner",
    )
    assert blocked["state"] == "blocked"
    assert blocked["reason_codes"] == ["REVIEW_SCOPE_UNSUPPORTED"]
    assert len(blocked["attempts"]) == 2
    assert len(case["host"].reconcile()) == 2


def test_a_premature_review_refusal_does_not_hide_the_pending_candidate_check(
    case: dict[str, Any],
) -> None:
    from karajan.orchestration import LocalFixtureRunner

    approve(case)
    coordinator = SerialCoordinator(
        case["root"] / "orchestration",
        case["planner"],
        case["host"],
        case["candidates"],
        fixture_runner=LocalFixtureRunner(case["fixture_root"]),
    )
    coordinator.enqueue(
        case["run"]["id"],
        "implement",
        profile_ref=case["profile"],
        command_key="worker",
        principal="owner",
    )
    ready = advance_until(coordinator, case["run"]["id"], "candidate_ready")
    refused = coordinator.enqueue(
        case["run"]["id"],
        "review",
        profile_ref=case["profile"],
        command_key="too-early",
        principal="owner",
    )
    assert refused["reason_codes"] == ["DEPENDENCIES_NOT_READY"]
    observed = coordinator.snapshot(case["run"]["id"])
    assert observed["state"] == "candidate_ready"
    assert observed["reason_codes"] == []
    assert (
        observed["tasks"]["implement"]["candidate_id"]
        == ready["tasks"]["implement"]["candidate_id"]
    )
    checked = advance_until(coordinator, case["run"]["id"], "awaiting_review")
    assert checked["tasks"]["implement"]["check_evidence"]["status"] == "passed"
    assert checked["counters"]["total_attempts"] == 2


@pytest.mark.parametrize("case", ["unfinished_worker"], indirect=True)
def test_another_valid_enqueue_cannot_hide_an_existing_tasks_pending_check(
    case: dict[str, Any],
) -> None:
    from karajan.orchestration import LocalFixtureRunner

    approve(case)
    coordinator = SerialCoordinator(
        case["root"] / "orchestration",
        case["planner"],
        case["host"],
        case["candidates"],
        fixture_runner=LocalFixtureRunner(case["fixture_root"]),
    )
    coordinator.enqueue(
        case["run"]["id"],
        "implement",
        profile_ref=case["profile"],
        command_key="worker",
        principal="owner",
    )
    advance_until(coordinator, case["run"]["id"], "candidate_ready")
    coordinator.enqueue(
        case["run"]["id"],
        "second-worker",
        profile_ref=case["profile"],
        command_key="second",
        principal="owner",
    )
    checked = advance_until(coordinator, case["run"]["id"], "awaiting_review")
    assert checked["tasks"]["implement"].get("check_evidence", {}).get("status") == "passed"


def test_resume_preserves_an_input_invalidation_that_happened_while_paused(
    case: dict[str, Any],
) -> None:
    from karajan.orchestration import LocalFixtureRunner

    approve(case)
    coordinator = SerialCoordinator(
        case["root"] / "orchestration",
        case["planner"],
        case["host"],
        case["candidates"],
        fixture_runner=LocalFixtureRunner(case["fixture_root"]),
    )
    coordinator.enqueue(
        case["run"]["id"],
        "implement",
        profile_ref=case["profile"],
        command_key="worker",
        principal="owner",
    )
    coordinator.control(case["run"]["id"], "pause", command_key="pause", principal="owner")
    revise(case, "implement")
    invalidated = advance_until(coordinator, case["run"]["id"], "blocked")
    assert invalidated["tasks"]["implement"]["state"] == "invalidated"
    resumed = coordinator.control(
        case["run"]["id"], "resume", command_key="resume", principal="owner"
    )
    assert resumed["paused"] is False
    assert resumed["state"] == "blocked"
    assert resumed["reason_codes"] == ["APPROVED_INPUT_CHANGED"]
    assert coordinator.advance(case["run"]["id"])["state"] == "blocked"
    assert case["host"].reconcile() == []
