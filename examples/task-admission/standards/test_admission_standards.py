"""Independent Standards checks at public admission/HTTP/source boundaries.

Preparation uses a labelled qualification double, real persisted approved Run,
owner estimate and capacity stores. Fault injection wraps a public Capacity port
after its real transaction commits; it does not fabricate a receipt.
"""

import json
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import Event

import pytest
from karajan.capacity import CapacityStore
from karajan.orchestration.admission import ApprovedTaskAdmission
from karajan.runs import RunError
from test_task_admission import prepared, project
from test_v2_approval_workbench import run_client, v2_plan

__all__ = ["prepared", "project", "run_client", "v2_plan"]


class LostResponseCapacity(CapacityStore):
    def __init__(self, path, fail_on):
        super().__init__(path, clock=lambda: 1000.0)
        self.fail_on = fail_on
        self.calls = {"admit": 0, "cancel_unactivated": 0}

    def admit(self, *args, **kwargs):
        self.calls["admit"] += 1
        result = super().admit(*args, **kwargs)
        if self.fail_on == "admit":
            raise ConnectionError("real capacity admit committed; response lost")
        return result

    def cancel_unactivated(self, *args, **kwargs):
        self.calls["cancel_unactivated"] += 1
        result = super().cancel_unactivated(*args, **kwargs)
        if self.fail_on == "cancel_unactivated":
            raise ConnectionError("real capacity cancel committed; response lost")
        return result


@pytest.mark.parametrize("phase", ["admit", "cancel_unactivated"])
@pytest.mark.parametrize("after_expiry", [False, True])
def test_recovery_reads_real_committed_receipt_even_after_the_prediction_is_revoked(
    prepared, phase, after_expiry
):
    service, routing, run = prepared
    operation = service.enqueue(run["id"], "implement", principal="owner", command_key="queue")
    port = LostResponseCapacity(routing.capacity.path, phase)
    routing.capacity = port
    if phase == "cancel_unactivated":
        service.advance(run["id"], operation["id"], principal="owner")
    action = service.advance if phase == "admit" else service.cancel
    with pytest.raises(ConnectionError, match="committed; response lost"):
        action(run["id"], operation["id"], principal="owner")
    persisted = service.get(run["id"], operation["id"], principal="owner")
    assert persisted["state"] == ("queued" if phase == "admit" else "cancellation_pending")
    routing.estimates.revoke(
        run["project_id"], "prediction", 1, principal="owner", reason="withdrawn-after-commit"
    )
    if after_expiry:
        port.clock = lambda: 1030.0
    before = port.snapshot()
    reopened = ApprovedTaskAdmission(service.database, routing)
    recover = reopened.advance if phase == "admit" else reopened.cancel
    result = recover(run["id"], operation["id"], principal="owner")
    expected = ("expired" if after_expiry else "reserved") if phase == "admit" else "cancelled"
    assert result["state"] == expected
    assert port.calls[phase] == 1
    assert port.snapshot() == before
    assert result["activation_allowed"] is result["dispatch_enabled"] is False
    assert reopened.get(run["id"], operation["id"], principal="owner") == result


@pytest.mark.parametrize("principal", ["lead", "someone-else"])
@pytest.mark.parametrize("method", ["enqueue", "get", "advance", "cancel"])
def test_planning_participant_is_not_an_admission_owner(prepared, principal, method):
    service, routing, run = prepared
    operation = service.enqueue(run["id"], "implement", principal="owner", command_key="queue")
    before = routing.capacity.snapshot()
    with pytest.raises(RunError, match="^RUN_NOT_FOUND$"):
        if method == "enqueue":
            service.enqueue(run["id"], "implement", principal=principal, command_key="other")
        else:
            getattr(service, method)(run["id"], operation["id"], principal=principal)
    assert routing.capacity.snapshot() == before
    assert service.get(run["id"], operation["id"], principal="owner") == operation


def test_project_guard_fences_a_real_estimate_revocation_through_the_consumer(prepared):
    _, routing, run = prepared
    started = Event()

    def revoke():
        started.set()
        return routing.estimates.revoke(
            run["project_id"], "prediction", 1, principal="owner", reason="concurrent-with-guard"
        )

    with ThreadPoolExecutor(max_workers=1) as pool:
        with routing.admission_guard(
            run["id"],
            "implement",
            principal="owner",
            attempt_id="stored-attempt",
            context_id="stored-context",
        ) as current:
            assert current["state"] == "selected"
            future = pool.submit(revoke)
            assert started.wait(2)
            with pytest.raises(TimeoutError):
                future.result(timeout=0.15)
            assert current["activation_allowed"] is False
        future.result(timeout=5)
    with routing.admission_guard(
        run["id"],
        "implement",
        principal="owner",
        attempt_id="stored-attempt",
        context_id="stored-context",
    ) as later:
        assert later["state"] == "blocked"
        assert later["sources"]["estimates"][0]["reason_codes"] == ["RESOURCE_ESTIMATE_REVOKED"]
    assert routing.capacity.snapshot()["reservations"] == []


def test_new_operation_after_cancel_gets_new_identity_but_old_key_is_historical(prepared):
    service, routing, run = prepared
    first = service.enqueue(run["id"], "implement", principal="owner", command_key="original")
    cancelled = service.cancel(run["id"], first["id"], principal="owner")
    assert (
        service.enqueue(run["id"], "implement", principal="owner", command_key="original") == first
    )
    fresh = service.enqueue(run["id"], "implement", principal="owner", command_key="new-operation")
    for field in ("id", "planned_attempt_id", "planned_context_id"):
        assert fresh[field] != first[field]
    assert service.advance(run["id"], first["id"], principal="owner") == cancelled
    assert routing.capacity.snapshot()["reservations"] == []


@pytest.mark.parametrize("boundary", ["get", "advance", "enqueue"])
def test_elapsed_reservation_no_longer_blocks_a_new_operation(prepared, boundary):
    service, routing, run = prepared
    first = service.enqueue(run["id"], "implement", principal="owner", command_key="first")
    service.advance(run["id"], first["id"], principal="owner")
    routing.capacity.clock = lambda: 1030.0
    before = routing.capacity.snapshot()
    if boundary != "enqueue":
        expired = getattr(service, boundary)(run["id"], first["id"], principal="owner")
        assert expired["state"] == "expired"
        assert (
            expired["capacity_status"]["admission"]["exclusion_reason"]
            == "RESERVATION_EXPIRED_UNSENT"
        )
    fresh = service.enqueue(run["id"], "implement", principal="owner", command_key="new")
    assert fresh["state"] == "queued"
    assert fresh["planned_attempt_id"] != first["planned_attempt_id"]
    assert service.get(run["id"], first["id"], principal="owner")["state"] == "expired"
    assert routing.capacity.snapshot() == before


@pytest.mark.parametrize("state", ["released", "active", "unknown"])
def test_current_capacity_state_is_shown_without_enabling_execution(prepared, state):
    service, routing, run = prepared
    operation = service.enqueue(run["id"], "implement", principal="owner", command_key="queue")
    reserved = service.advance(run["id"], operation["id"], principal="owner")
    admission_id = reserved["capacity_receipt"]["admission_id"]
    if state == "released":
        routing.capacity.cancel_unactivated(
            admission_id, evidence_ref="external:cancel", command_key="external"
        )
    else:
        routing.capacity.activate(admission_id, command_key="external")
        if state == "unknown":
            routing.capacity.reconcile(
                admission_id,
                local_ended=True,
                remote_ended=False,
                usage_complete=False,
                not_sent=False,
                evidence_ref="external:unknown",
                command_key="unknown",
            )
    before = routing.capacity.snapshot()
    current = service.get(run["id"], operation["id"], principal="owner")
    assert current["state"] == ("released" if state == "released" else "reconciliation_required")
    assert current["capacity_status"]["admission"]["stored_state"] == state
    assert current["activation_allowed"] is current["dispatch_enabled"] is False
    if state != "released":
        with pytest.raises(RunError, match="^TASK_ADMISSION_PENDING$"):
            service.enqueue(run["id"], "implement", principal="owner", command_key="new")
    assert routing.capacity.snapshot() == before


@pytest.mark.parametrize("database", ["run", "project", "capacity"])
def test_admission_journal_cannot_alias_a_source_database(prepared, database):
    _, routing, _ = prepared
    path = {
        "run": routing.planner.database,
        "project": routing.planner.projects.database,
        "capacity": routing.capacity.path,
    }[database]
    with pytest.raises(RunError, match="^ADMISSION_DATABASE_MUST_BE_SEPARATE$"):
        ApprovedTaskAdmission(path, routing)


@pytest.mark.parametrize("phase", ["enqueue", "advance", "cancel"])
@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        {"activation_allowed": True},
        {"not_sent": True},
        {"capacity_receipt": {"decision": "admitted"}},
    ],
)
def test_each_http_mutation_rejects_caller_supplied_authority(v2_plan, phase, body):
    client, headers, _, run, _, _ = v2_plan
    collection = f"/v1/runs/{run['id']}/tasks/feature/admissions"
    operation = client.post(collection, json={}, headers=headers).json()
    assert operation["state"] == "blocked"
    item = f"/v1/runs/{run['id']}/task-admissions/{operation['id']}"
    target = collection if phase == "enqueue" else item + "/" + phase
    result = client.post(
        target, content=json.dumps(body), headers={**headers, "Content-Type": "application/json"}
    )
    assert result.status_code == 422
    assert client.get(item).json() == operation


def test_http_get_and_cancel_require_the_current_session(v2_plan):
    client, headers, _, run, _, _ = v2_plan
    operation = client.post(
        f"/v1/runs/{run['id']}/tasks/feature/admissions", json={}, headers=headers
    ).json()
    item = f"/v1/runs/{run['id']}/task-admissions/{operation['id']}"
    assert (
        client.post(item + "/cancel", json={}, headers={"Origin": headers["Origin"]}).status_code
        == 403
    )
    client.cookies.clear()
    assert client.get(item).status_code == 401
    assert client.post(item + "/cancel", json={}, headers=headers).status_code == 401
