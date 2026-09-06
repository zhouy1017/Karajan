"""Independently reproduce expired reservation visibility and renewal blockage."""

import subprocess
import sys
from pathlib import Path

import pytest
from admission_spec_fixture import prepare, queue, reopen
from karajan.runs import RunError


def test_expired_reservation_is_not_presented_as_current_reserved(tmp_path):
    case = prepare(tmp_path)
    queued = queue(case)
    service, run = case["admission"], case["run"]["id"]
    reserved = service.advance(run, queued["id"], principal="owner")
    assert reserved["state"] == "reserved"
    case["now"][0] += queued["request"]["duration_seconds"] + 1
    capacity = case["capacity"].routing_facts().as_dict()
    assert capacity["accounts"][0]["held_attempts"] == 0
    current = service.get(run, queued["id"], principal="owner")
    assert current["state"] == "expired"


def test_expiry_refresh_in_enqueue_releases_pending_identity_without_prior_get(tmp_path):
    case = prepare(tmp_path)
    queued = queue(case)
    service, run = case["admission"], case["run"]["id"]
    service.advance(run, queued["id"], principal="owner")
    case["now"][0] += queued["request"]["duration_seconds"] + 1
    replacement = queue(case, "next-generation")
    assert replacement["planned_attempt_id"] != queued["planned_attempt_id"]
    assert replacement["planned_context_id"] != queued["planned_context_id"]
    result = service.advance(run, replacement["id"], principal="owner")
    assert result["state"] == "reserved"
    facts = case["capacity"].routing_facts().as_dict()
    assert facts["accounts"][0]["held_attempts"] == 1
    assert len(case["capacity"].snapshot()["reservations"]) == 2
    assert service.enqueue(run, "implement", principal="owner", command_key="queue") == queued


def test_lost_admit_response_recovery_after_expiry_is_current_and_never_resends(tmp_path):
    case = prepare(tmp_path)
    queued = queue(case)
    run = case["run"]["id"]
    command = [
        sys.executable,
        str(Path(__file__).with_name("test_public_admission.py")),
        "crash",
        str(tmp_path),
        run,
        queued["id"],
        "admit",
    ]
    child = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert child.returncode == 91, child.stdout + child.stderr
    service, routing = reopen(
        tmp_path, now=1000 + queued["request"]["duration_seconds"] + 1, qualification_double=False
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("Expired response recovery must not issue an admission")

    routing.capacity.admit = forbidden
    result = service.advance(run, queued["id"], principal="owner")
    assert result["state"] == "expired"
    assert result["capacity_receipt"]["decision"] == "admitted"
    assert len(routing.capacity.snapshot()["reservations"]) == 1
    assert routing.capacity.routing_facts().as_dict()["accounts"][0]["held_attempts"] == 0


@pytest.mark.parametrize("state", ["active", "unknown"])
def test_activated_or_unknown_hold_does_not_expire_with_unsent_deadline(tmp_path, state):
    case = prepare(tmp_path)
    queued = queue(case)
    service, capacity, run = case["admission"], case["capacity"], case["run"]["id"]
    result = service.advance(run, queued["id"], principal="owner")
    admission = result["capacity_receipt"]["admission_id"]
    assert (
        capacity.activate(admission, command_key="external-activation")["decision"]
        == "capacity_revalidated"
    )
    if state == "unknown":
        assert (
            capacity.reconcile(
                admission,
                local_ended=False,
                remote_ended=False,
                usage_complete=False,
                not_sent=False,
                evidence_ref="spec-external-unknown",
                command_key="external-unknown",
            )["state"]
            == "unknown"
        )
    case["now"][0] += queued["request"]["duration_seconds"] + 1
    assert capacity.routing_facts().as_dict()["accounts"][0]["held_attempts"] == 1
    current = service.get(run, queued["id"], principal="owner")
    assert current["state"] == "reconciliation_required"
    assert capacity.snapshot()["reservations"][0]["state"] == state
    with pytest.raises(RunError, match="TASK_ADMISSION_PENDING"):
        queue(case, "blocked-new-operation")
