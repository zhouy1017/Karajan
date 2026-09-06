"""Independent public admission invariants; no Host or model execution."""

import copy
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pathlib import Path
from threading import Event

import pytest
from admission_spec_fixture import approval, observe, prepare, queue, reopen
from karajan.capacity import CapacityError
from karajan.orchestration.admission import ApprovedTaskAdmission
from karajan.projects.qualification import ProfileQualificationStore
from karajan.runs import RunError


def test_public_identity_grants_and_repeated_commands_use_one_capacity_hold(tmp_path):
    case = prepare(tmp_path)
    service, capacity, run = case["admission"], case["capacity"], case["run"]["id"]
    queued = queue(case)
    assert capacity.snapshot()["reservations"] == []
    result = service.advance(run, queued["id"], principal="owner")
    assert result["state"] == "reserved"
    assert result["planned_context_id"] == queued["planned_context_id"]
    held = capacity.snapshot()["reservations"]
    assert len(held) == 1
    request = held[0]["request"]
    assert request["attempt_id"] == queued["planned_attempt_id"]
    assert request["demand"] == {"service-fixture": "7.25"}
    assert request["expected_capacity"] == {
        "policy_revision": 1,
        "pool_windows": {"service-fixture": "fixed-current"},
        "lead_reserve_access": False,
    }
    assert held[0]["state"] == "reserved"
    assert capacity.snapshot()["lifecycle"] == []
    assert not result["activation_allowed"] and not result["dispatch_enabled"]
    assert service.advance(run, queued["id"], principal="owner") == result
    assert service.enqueue(run, "implement", principal="owner", command_key="queue") == queued
    with pytest.raises(RunError, match="TASK_ADMISSION_PENDING"):
        queue(case, "other")


@pytest.mark.parametrize("phase", ["admit", "cancel_unactivated"])
def test_process_crash_after_capacity_commit_recovers_without_reissuing(tmp_path, phase):
    case = prepare(tmp_path)
    queued = queue(case)
    run = case["run"]["id"]
    if phase == "cancel_unactivated":
        case["admission"].advance(run, queued["id"], principal="owner")
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "crash",
        str(tmp_path),
        run,
        queued["id"],
        phase,
    ]
    child = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert child.returncode == 91, child.stdout + child.stderr
    recovered, routing = reopen(tmp_path, qualification_double=False)

    def forbidden(*args, **kwargs):
        raise AssertionError("A committed Capacity effect must only be recovered by read receipt")

    setattr(routing.capacity, phase, forbidden)
    if phase == "admit":
        result = recovered.advance(run, queued["id"], principal="owner")
        assert result["state"] == "reserved"
    else:
        result = recovered.cancel(run, queued["id"], principal="owner")
        assert result["state"] == "cancelled"
    held = routing.capacity.snapshot()["reservations"]
    assert len(held) == 1
    assert held[0]["request"]["attempt_id"] == queued["planned_attempt_id"]
    assert result["planned_context_id"] == queued["planned_context_id"]
    assert not result["activation_allowed"] and not result["dispatch_enabled"]


@pytest.mark.parametrize("change", ["qualification", "plan", "balance", "window", "policy"])
def test_pending_admission_reads_current_authority_and_capacity(tmp_path, change):
    case = prepare(tmp_path)
    queued = queue(case)
    if change == "qualification":
        case["routing"].qualifications = ProfileQualificationStore(
            case["registry"], clock=lambda: case["now"][0]
        )
    elif change == "plan":
        proposal = copy.deepcopy(case["proposal"])
        proposal["expected_plan_revision"] = 1
        proposal["plan"]["tasks"][0]["revision"] = 2
        proposal["plan"]["tasks"][0]["acceptance"] = ["Different approved acceptance"]
        plan = case["planner"].submit_plan(
            case["run"]["id"], proposal, command_key="changed-plan", principal="lead"
        )
        case["planner"].approve_plan(
            case["run"]["id"], approval(plan), command_key="changed-approval", principal="owner"
        )
    elif change == "balance":
        case["now"][0] += 1
        observe(case, amount="10")
    elif change == "window":
        original = case["capacity"].snapshot()["observations"][-1]["observation"]
        observation = {
            **original,
            "window_id": "another-window",
            "observed_at": 1201.0,
            "reset_at": 1400.0,
            "source_ref": "next-observation",
        }
        case["now"][0] = 1201.0
        assert case["capacity"].observe(observation, command_key="next-window")["applied"]
    else:
        policy = case["capacity"].snapshot()["policies"][-1]["policy"]
        policy["lead_reserved_slots"] = 2
        case["capacity"].activate_policy(policy, expected_revision=1, command_key="next-policy")
    result = case["admission"].advance(case["run"]["id"], queued["id"], principal="owner")
    assert result["state"] == "blocked"
    assert result["reason_codes"]
    assert case["capacity"].snapshot()["reservations"] == []


def test_foreign_run_capacity_hold_competes_between_enqueue_and_admit(tmp_path):
    case = prepare(tmp_path)
    queued = queue(case)
    request = copy.deepcopy(queued["request"])
    request.update(
        attempt_id="different-run-attempt", run_id="different-run", demand={"service-fixture": "29"}
    )
    foreign = case["capacity"].admit(request, command_key="different-run-admit")
    assert foreign["decision"] == "admitted"
    result = case["admission"].advance(case["run"]["id"], queued["id"], principal="owner")
    assert result["state"] == "blocked"
    assert len(case["capacity"].snapshot()["reservations"]) == 1


def test_cancel_waits_for_in_flight_real_admit_then_releases_once(tmp_path, monkeypatch):
    case = prepare(tmp_path)
    queued = queue(case)
    service = case["admission"]
    run = case["run"]["id"]
    entered, finish = Event(), Event()
    original = case["capacity"].admit

    def paused_admit(*args, **kwargs):
        entered.set()
        assert finish.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(case["capacity"], "admit", paused_admit)
    with ThreadPoolExecutor(max_workers=2) as pool:
        advancing = pool.submit(service.advance, run, queued["id"], principal="owner")
        try:
            assert entered.wait(5)
            cancelling = pool.submit(service.cancel, run, queued["id"], principal="owner")
            with pytest.raises(TimeoutError):
                cancelling.result(timeout=0.15)
        finally:
            finish.set()
        assert advancing.result(timeout=5)["state"] == "reserved"
        assert cancelling.result(timeout=5)["state"] == "cancelled"
    held = case["capacity"].snapshot()["reservations"]
    assert len(held) == 1 and held[0]["state"] == "released"
    assert service.advance(run, queued["id"], principal="owner")["state"] == "cancelled"


def test_project_estimate_revocation_waits_until_real_admission_guard_exits(tmp_path, monkeypatch):
    case = prepare(tmp_path)
    queued = queue(case)
    entered, finish = Event(), Event()
    original = case["capacity"].admit

    def paused_admit(*args, **kwargs):
        entered.set()
        assert finish.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(case["capacity"], "admit", paused_admit)
    with ThreadPoolExecutor(max_workers=2) as pool:
        advancing = pool.submit(
            case["admission"].advance, case["run"]["id"], queued["id"], principal="owner"
        )
        try:
            assert entered.wait(5)
            revoking = pool.submit(
                case["estimates"].revoke,
                case["project"],
                "explicit-prediction",
                1,
                principal="owner",
                reason="withdrawn",
            )
            with pytest.raises(TimeoutError):
                revoking.result(timeout=0.15)
        finally:
            finish.set()
        assert advancing.result(timeout=5)["state"] == "reserved"
        revoking.result(timeout=5)
    assert (
        case["admission"].cancel(case["run"]["id"], queued["id"], principal="owner")["state"]
        == "cancelled"
    )


def test_readonly_recovery_key_cannot_bind_another_payload(tmp_path):
    case = prepare(tmp_path)
    queued = queue(case)
    case["admission"].advance(case["run"]["id"], queued["id"], principal="owner")
    before = case["capacity"].snapshot()
    changed = {**queued["request"], "run_id": "another-run"}
    with pytest.raises(CapacityError, match="IDEMPOTENCY_CONFLICT"):
        case["capacity"].command_receipt("admit", changed, command_key="task-admit:" + queued["id"])
    assert case["capacity"].snapshot() == before


def test_independent_activation_racing_cancellation_cannot_be_released(tmp_path, monkeypatch):
    case = prepare(tmp_path)
    queued = queue(case)
    service, run = case["admission"], case["run"]["id"]
    reserved = service.advance(run, queued["id"], principal="owner")
    entered, finish = Event(), Event()
    original = case["capacity"].cancel_unactivated

    def paused_cancel(*args, **kwargs):
        entered.set()
        assert finish.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(case["capacity"], "cancel_unactivated", paused_cancel)
    with ThreadPoolExecutor(max_workers=1) as pool:
        cancelling = pool.submit(service.cancel, run, queued["id"], principal="owner")
        try:
            assert entered.wait(5)
            activation = case["capacity"].activate(
                reserved["capacity_receipt"]["admission_id"],
                command_key="external-racing-activation",
            )
            assert activation["decision"] == "capacity_revalidated"
        finally:
            finish.set()
        result = cancelling.result(timeout=5)
    assert result["state"] == "reconciliation_required"
    assert result["reason_codes"] == ["CANNOT_RELEASE_ACTIVATED_ADMISSION"]
    assert case["capacity"].snapshot()["reservations"][0]["state"] == "active"


def test_cancelled_intent_fences_later_advance_after_reopen(tmp_path, monkeypatch):
    case = prepare(tmp_path)
    queued = queue(case)
    service, run = case["admission"], case["run"]["id"]
    cancelled = service.cancel(run, queued["id"], principal="owner")

    def forbidden(*args, **kwargs):
        raise AssertionError("Cancelled intent must not enter a capacity reservation path")

    monkeypatch.setattr(case["capacity"], "admit", forbidden)
    monkeypatch.setattr(case["routing"], "admission_guard", forbidden)
    reopened = ApprovedTaskAdmission(service.database, case["routing"])
    assert reopened.advance(run, queued["id"], principal="owner") == cancelled
    assert case["capacity"].snapshot()["reservations"] == []


def test_public_owner_and_task_identity_cannot_redirect_existing_operation(tmp_path):
    case = prepare(tmp_path)
    queued = queue(case)
    service, run = case["admission"], case["run"]["id"]
    for method in (service.get, service.advance, service.cancel):
        with pytest.raises(RunError, match="RUN_NOT_FOUND"):
            method(run, queued["id"], principal="not-owner")
        with pytest.raises(RunError, match="TASK_ADMISSION_NOT_FOUND"):
            method(run, "unknown-operation", principal="owner")
    with pytest.raises(RunError, match="IDEMPOTENCY_CONFLICT"):
        service.enqueue(run, "review", principal="owner", command_key="queue")
    reviewer = service.enqueue(run, "review", principal="owner", command_key="review")
    assert reviewer["state"] == "blocked"
    assert reviewer["reason_codes"] == ["EXECUTION_LINEAGE_REQUIRED"]
    assert case["capacity"].snapshot()["reservations"] == []


def test_real_runtime_qualification_never_upgrades_for_admission(tmp_path):
    case = prepare(tmp_path, qualification_double=False)
    result = case["admission"].enqueue(
        case["run"]["id"], "implement", principal="owner", command_key="blocked"
    )
    assert result["state"] == "blocked"
    assert case["capacity"].snapshot()["reservations"] == []


if __name__ == "__main__":
    assert sys.argv[1] == "crash"
    root, run, operation, phase = Path(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5]
    service, routing = reopen(root)
    original = getattr(routing.capacity, phase)

    def crash_after_commit(*args, **kwargs):
        original(*args, **kwargs)
        os._exit(91)

    setattr(routing.capacity, phase, crash_after_commit)
    action = service.advance if phase == "admit" else service.cancel
    action(run, operation, principal="owner")
    raise AssertionError("Requested crash was not reached")
