"""Real durable stores; only qualification is explicitly synthetic in positive cases."""

from contextlib import contextmanager
from pathlib import Path

import pytest
from karajan.orchestration.admission import ApprovedTaskAdmission
from karajan.orchestration.routing import ApprovedRunRouting
from karajan.projects.demand import AttemptEstimateStore
from karajan.projects.qualification import ProfileQualificationStore
from karajan.runs import RunError
from test_approved_routing_capacity import SyntheticQualifiedSource, capacity_for_plan
from test_routing_authorization import admitted_v2, approve_request, project, submit_request

__all__ = ["project"]


@pytest.fixture
def prepared(tmp_path: Path, project: tuple):
    planner, run, intent = admitted_v2(tmp_path, project)
    plan = planner.submit_plan(
        run["id"], submit_request(run, intent), command_key="plan", principal="lead"
    )
    planner.approve_plan(run["id"], approve_request(plan), command_key="approve", principal="owner")
    configuration = run["configuration_snapshot"]["configuration"]
    capacity = capacity_for_plan(tmp_path, configuration)
    estimates = AttemptEstimateStore(planner, clock=lambda: 1000.0)
    estimates.register(
        run["id"],
        "implement",
        {"id": "fixture-profile", "revision": 1},
        {
            "id": "prediction",
            "revision": 1,
            "source_kind": "owner_conservative_estimate",
            "validity_seconds": 60,
            "measurement_semantics": "window_independent_attempt",
            "demand": [
                {"pool_id": p["id"], "unit": p["unit"], "window_kind": "fixed", "amount": "3"}
                for p in configuration["resources"]["quota_pools"]
            ],
            "completion_seconds": None,
            "basis": "Explicit synthetic test forecast.",
        },
        principal="owner",
        command_key="prediction",
    )
    routing = ApprovedRunRouting(
        planner, SyntheticQualifiedSource(planner.projects), capacity, estimates=estimates
    )
    service = ApprovedTaskAdmission(tmp_path / "admission.sqlite", routing)
    return service, routing, run


def test_enqueue_persists_identity_then_advance_reserves_once_without_activation(prepared):
    service, routing, run = prepared
    queued = service.enqueue(run["id"], "implement", principal="owner", command_key="enqueue")
    assert queued["state"] == "queued"
    assert routing.capacity.snapshot()["reservations"] == []
    reserved = service.advance(run["id"], queued["id"], principal="owner")
    assert reserved["state"] == "reserved"
    assert reserved["activation_allowed"] is False
    assert reserved["dispatch_enabled"] is False
    rows = routing.capacity.snapshot()["reservations"]
    assert len(rows) == 1
    assert rows[0]["state"] == "reserved"
    assert rows[0]["request"]["attempt_id"] == queued["planned_attempt_id"]
    assert rows[0]["request"]["demand"] == queued["request"]["demand"]
    assert service.advance(run["id"], queued["id"], principal="owner") == reserved
    assert (
        service.enqueue(run["id"], "implement", principal="owner", command_key="enqueue") == queued
    )


@pytest.mark.parametrize("reserve", [False, True])
def test_cancel_prevents_admission_or_releases_only_unactivated_reservation(prepared, reserve):
    service, routing, run = prepared
    queued = service.enqueue(run["id"], "implement", principal="owner", command_key="enqueue")
    if reserve:
        service.advance(run["id"], queued["id"], principal="owner")
    cancelled = service.cancel(run["id"], queued["id"], principal="owner")
    assert cancelled["state"] == "cancelled"
    assert cancelled["activation_allowed"] is False
    assert service.advance(run["id"], queued["id"], principal="owner") == cancelled
    assert service.cancel(run["id"], queued["id"], principal="owner") == cancelled
    rows = routing.capacity.snapshot()["reservations"]
    assert [r["state"] for r in rows] == (["released"] if reserve else [])


@pytest.mark.parametrize("phase", ["admit", "cancel_unactivated"])
def test_reopen_recovers_committed_receipt_after_lost_response(prepared, monkeypatch, phase):
    service, routing, run = prepared
    queued = service.enqueue(run["id"], "implement", principal="owner", command_key="enqueue")
    if phase == "cancel_unactivated":
        service.advance(run["id"], queued["id"], principal="owner")
    original = getattr(routing.capacity, phase)

    def lose_response(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("response lost after actual Capacity commit")

    monkeypatch.setattr(routing.capacity, phase, lose_response)
    action = service.advance if phase == "admit" else service.cancel
    with pytest.raises(RuntimeError, match="response lost"):
        action(run["id"], queued["id"], principal="owner")

    @contextmanager
    def no_new_authorization(*args, **kwargs):
        raise AssertionError("Recovery must read the receipt, not request new admission")
        yield

    monkeypatch.setattr(routing, "admission_guard", no_new_authorization)
    reopened = ApprovedTaskAdmission(service.database, routing)
    action = reopened.advance if phase == "admit" else reopened.cancel
    result = action(run["id"], queued["id"], principal="owner")
    assert result["state"] == ("reserved" if phase == "admit" else "cancelled")
    assert len(routing.capacity.snapshot()["reservations"]) == 1
    assert result["dispatch_enabled"] is False


def test_estimate_revoked_before_admission_blocks_without_reserving(prepared):
    service, routing, run = prepared
    queued = service.enqueue(run["id"], "implement", principal="owner", command_key="enqueue")
    routing.estimates.revoke(
        run["project_id"], "prediction", 1, principal="owner", reason="forecast-withdrawn"
    )
    result = service.advance(run["id"], queued["id"], principal="owner")
    assert result["state"] == "blocked"
    assert routing.capacity.snapshot()["reservations"] == []
    assert result["revalidation"]["sources"]["estimates"][0]["reason_codes"] == [
        "RESOURCE_ESTIMATE_REVOKED"
    ]


def test_unqualified_runtime_cannot_create_a_reservation(prepared):
    service, routing, run = prepared
    routing.qualifications = ProfileQualificationStore(routing.planner.projects)
    blocked = service.enqueue(run["id"], "implement", principal="owner", command_key="enqueue")
    assert blocked["state"] == "blocked"
    assert service.advance(run["id"], blocked["id"], principal="owner") == blocked
    assert routing.capacity.snapshot()["reservations"] == []


def test_task_has_only_one_pending_admission_and_owner_is_required(prepared):
    service, routing, run = prepared
    queued = service.enqueue(run["id"], "implement", principal="owner", command_key="enqueue")
    with pytest.raises(RunError, match="TASK_ADMISSION_PENDING"):
        service.enqueue(run["id"], "implement", principal="owner", command_key="another")
    for method in (service.get, service.advance, service.cancel):
        with pytest.raises(RunError, match="RUN_NOT_FOUND"):
            method(run["id"], queued["id"], principal="someone-else")
    with pytest.raises(RunError, match="IDEMPOTENCY_CONFLICT"):
        service.enqueue(run["id"], "review", principal="owner", command_key="enqueue")
    assert routing.capacity.snapshot()["reservations"] == []


def test_independently_activated_reservation_is_not_released_by_cancel(prepared):
    service, routing, run = prepared
    queued = service.enqueue(run["id"], "implement", principal="owner", command_key="enqueue")
    reserved = service.advance(run["id"], queued["id"], principal="owner")
    routing.capacity.activate(reserved["capacity_receipt"]["admission_id"], command_key="external")
    result = service.cancel(run["id"], queued["id"], principal="owner")
    assert result["state"] == "reconciliation_required"
    assert result["reason_codes"] == ["CANNOT_RELEASE_ACTIVATED_ADMISSION"]
    assert routing.capacity.snapshot()["reservations"][0]["state"] == "active"
