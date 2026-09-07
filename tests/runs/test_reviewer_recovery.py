"""Focused durable Reviewer Capacity recovery regression coverage."""

import json
import sqlite3

import pytest
from karajan.runs import RunError
from test_reviewer_binding import (
    _activate_reviewer_reservation,
    _passed_reviewer_subject,
)

pytest_plugins = ["test_reviewer_binding", "test_task_admission"]


def _reviewer_with_activation(binding_case, command_key):
    intents, (run_id, _), _, _, _ = _passed_reviewer_subject(binding_case)
    queued = intents.admissions.enqueue(
        run_id, "review", principal="owner", command_key=command_key
    )
    reviewer = intents.admissions.advance(run_id, queued["id"], principal="owner")
    activated = _activate_reviewer_reservation(intents, run_id, reviewer)
    return intents, run_id, reviewer, activated


def test_reviewer_unknown_then_released_refreshes_exact_original_admission(binding_case):
    intents, run_id, reviewer, activated = _reviewer_with_activation(
        binding_case, "refresh-unknown-released"
    )
    capacity = intents.admissions.routing.capacity
    admission_id = reviewer["capacity_receipt"]["admission_id"]

    capacity.reconcile(
        admission_id,
        local_ended=True,
        remote_ended=False,
        usage_complete=False,
        not_sent=False,
        evidence_ref="fixture:refresh-unknown",
        command_key="refresh-unknown",
    )
    unknown = intents.admissions.get(run_id, reviewer["id"], principal="owner")
    assert unknown["state"] == "reconciliation_required"
    assert unknown["reason_codes"] == ["EXECUTION_RECONCILIATION_REQUIRED"]
    assert unknown["capacity_status"]["admission"]["stored_state"] == "unknown"
    assert unknown["reviewer_activation"]["receipt"] == activated["reviewer_activation"]["receipt"]
    with pytest.raises(RunError, match="TASK_ADMISSION_PENDING"):
        intents.admissions.enqueue(
            run_id, "review", principal="owner", command_key="before-release"
        )

    released_receipt = capacity.reconcile(
        admission_id,
        local_ended=True,
        remote_ended=True,
        usage_complete=False,
        not_sent=True,
        evidence_ref="fixture:refresh-released",
        command_key="refresh-released",
    )
    assert released_receipt["state"] == "released"
    released = intents.admissions.reconcile_reviewer(run_id, reviewer["id"], principal="owner")
    assert released["state"] == "released"
    assert released["reason_codes"] == ["RESERVATION_RELEASED"]
    assert released["capacity_status"]["admission"]["stored_state"] == "released"
    assert released["reviewer_activation"]["receipt"] == activated["reviewer_activation"]["receipt"]
    assert intents.admissions.get(run_id, reviewer["id"], principal="owner") == released
    assert (
        intents.admissions.reconcile_reviewer(run_id, reviewer["id"], principal="owner")
        == released
    )
    queued_again = intents.admissions.enqueue(
        run_id, "review", principal="owner", command_key="after-release"
    )
    assert queued_again["state"] == "queued"
    assert queued_again["id"] != reviewer["id"]

def test_reviewer_unknown_remains_pending_and_refresh_is_idempotent(binding_case):
    intents, run_id, reviewer, activated = _reviewer_with_activation(
        binding_case, "refresh-always-unknown"
    )
    capacity = intents.admissions.routing.capacity
    admission_id = reviewer["capacity_receipt"]["admission_id"]
    unknown = capacity.reconcile(
        admission_id,
        local_ended=True,
        remote_ended=False,
        usage_complete=False,
        not_sent=False,
        evidence_ref="fixture:always-unknown",
        command_key="always-unknown",
    )
    assert unknown["state"] == "unknown"
    first = intents.admissions.get(run_id, reviewer["id"], principal="owner")
    second = intents.admissions.reconcile_reviewer(run_id, reviewer["id"], principal="owner")
    assert first["state"] == second["state"] == "reconciliation_required"
    assert first["capacity_status"]["admission"]["stored_state"] == "unknown"
    assert second == first
    assert first["reviewer_activation"]["receipt"] == activated["reviewer_activation"]["receipt"]
    with pytest.raises(RunError, match="TASK_ADMISSION_PENDING"):
        intents.admissions.enqueue(run_id, "review", principal="owner", command_key="still-pending")
    assert capacity.reconcile(
        admission_id,
        local_ended=True,
        remote_ended=False,
        usage_complete=False,
        not_sent=False,
        evidence_ref="fixture:always-unknown",
        command_key="always-unknown",
    ) == unknown


def test_generic_worker_reconciliation_does_not_use_reviewer_refresh(prepared):
    service, routing, run = prepared
    queued = service.enqueue(
        run["id"], "implement", principal="owner", command_key="worker-refresh"
    )
    reserved = service.advance(run["id"], queued["id"], principal="owner")
    admission_id = reserved["capacity_receipt"]["admission_id"]
    routing.capacity.activate(admission_id, command_key="worker-activate")
    pending = service.cancel(run["id"], queued["id"], principal="owner")
    assert pending["state"] == "reconciliation_required"
    assert pending["reason_codes"] == ["CANNOT_RELEASE_ACTIVATED_ADMISSION"]
    routing.capacity.reconcile(
        admission_id,
        local_ended=True,
        remote_ended=True,
        usage_complete=False,
        not_sent=True,
        evidence_ref="fixture:worker-released",
        command_key="worker-released",
    )
    assert service.get(run["id"], queued["id"], principal="owner") == pending



def test_cancel_after_lost_reviewer_activation_reply_recovers_exact_receipt(
    binding_case, monkeypatch
):
    intents, (run_id, _), _, _, _ = _passed_reviewer_subject(binding_case)
    queued = intents.admissions.enqueue(
        run_id, "review", principal="owner", command_key="cancel-lost-activation"
    )
    reserved = intents.admissions.advance(run_id, queued["id"], principal="owner")
    capacity = intents.admissions.routing.capacity
    actual = capacity.activate
    calls = []

    def activate_then_lose(admission_id, *, command_key):
        calls.append((admission_id, command_key))
        actual(admission_id, command_key=command_key)
        raise ConnectionResetError("fixture activate response lost after Capacity commit")

    monkeypatch.setattr(capacity, "activate", activate_then_lose)
    with pytest.raises(ConnectionResetError, match="activate response lost"):
        intents.admissions.activate_reviewer(run_id, queued["id"], principal="owner")

    with sqlite3.connect(intents.admissions.database) as db:
        operation = json.loads(
            db.execute("SELECT data FROM operations WHERE id=?", (queued["id"],)).fetchone()[0]
        )
    assert operation["reviewer_activation"]["receipt"] is None

    cancelled = intents.admissions.cancel(run_id, queued["id"], principal="owner")
    assert cancelled["state"] == "reconciliation_required"
    assert cancelled["cancel_requested"] is True
    assert cancelled["reviewer_activation"]["receipt"] is None
    assert capacity.snapshot()["reservations"][0]["state"] == "active"

    recovered = intents.admissions.get(run_id, queued["id"], principal="owner")
    command_key = "reviewer-activate:" + queued["id"]
    exact = capacity.command_receipt(
        "activate",
        {"admission_id": reserved["capacity_receipt"]["admission_id"]},
        command_key=command_key,
    )
    assert exact is not None
    assert recovered["reviewer_activation"]["receipt"] == exact
    assert recovered["cancel_requested"] is True
    assert calls == [(reserved["capacity_receipt"]["admission_id"], command_key)]

    capacity.reconcile(
        reserved["capacity_receipt"]["admission_id"],
        local_ended=True,
        remote_ended=True,
        usage_complete=False,
        not_sent=True,
        evidence_ref="fixture:cancel-lost-activation-released",
        command_key="cancel-lost-activation-released",
    )
    released = intents.admissions.reconcile_reviewer(run_id, queued["id"], principal="owner")
    assert released["state"] == "released"
    assert released["reason_codes"] == ["RESERVATION_RELEASED"]
    assert released["cancel_requested"] is True
    assert released["reviewer_activation"]["receipt"] == exact
    with pytest.raises(RunError, match="REVIEWER_OPERATION_CANCELLED"):
        intents.admissions.activate_reviewer(run_id, queued["id"], principal="owner")

