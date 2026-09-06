"""Independent intent checks; qualification and Host identity are explicit doubles."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import asdict
from threading import Barrier, Event

import pytest
from karajan.capacity import CapacityError
from karajan.execution import ProcessIdentity
from karajan.orchestration.go_execution_intent import GoExecutionIntents, GoExecutionSource
from karajan.runs import RunError
from test_go_execution_intent import (
    activate,
    case,
    controller,
    host_prepare,
    launched_intent,
    prepared,
    projected,
    ready,
    reservation,
)

__all__ = ["case", "projected", "reservation", "ready", "prepared", "launched_intent"]


def test_concurrent_prepare_keeps_one_original_attempt_and_current_cancelled_replay(
    ready, tmp_path
):
    admissions, routing, run, operation, _ = ready
    service = controller(admissions, tmp_path)
    capacity_before = routing.capacity.path.read_bytes()
    barrier = Barrier(4)

    def prepare(index):
        barrier.wait(timeout=3)
        return service.prepare_intent(
            run["id"], operation["id"], principal="owner", command_key=f"parallel-{index}"
        )

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(prepare, range(4)))
    assert all(item == results[0] for item in results)
    assert results[0]["planned_attempt_id"] == operation["planned_attempt_id"]
    assert routing.capacity.path.read_bytes() == capacity_before
    cancelled = service.cancel_intent(run["id"], operation["id"], principal="owner")
    assert (
        service.prepare_intent(
            run["id"], operation["id"], principal="owner", command_key="parallel-0"
        )
        == cancelled
    )
    assert cancelled["state"] == "cancellation_pending"
    assert cancelled["execution"]["cancel_requested"] is True


def test_different_runner_claims_only_one_live_return_and_never_persist_that_permission(
    launched_intent,
):
    service, args, _ = launched_intent
    barrier = Barrier(3)
    identities = [
        ProcessIdentity(70000 + index, f"explicit-test-birth-{index}") for index in range(3)
    ]

    def claim(identity):
        barrier.wait(timeout=3)
        return service.effect_start_claim(*args, principal="owner", runner=identity)

    with ThreadPoolExecutor(max_workers=3) as executor:
        returned = list(executor.map(claim, identities))
    assert sum(item["claim_allowed"] for item in returned) == 1
    saved = service.read(*args, principal="owner")
    winner = next(
        identity
        for identity in identities
        if asdict(identity) == (saved["execution"]["effect_claim"]["runner"])
    )
    assert "claim_allowed" not in saved
    assert all(
        item["execution"]["effect_claim"] == saved["execution"]["effect_claim"] for item in returned
    )
    reopened = GoExecutionIntents(service.admissions, source=service.source, host=service.host)
    before = service.admissions.database.read_bytes()
    assert (
        reopened.effect_start_claim(*args, principal="owner", runner=winner)["claim_allowed"]
        is False
    )
    assert reopened.reconcile(*args, principal="owner") == saved
    assert service.admissions.database.read_bytes() == before


def test_historical_activation_can_be_adopted_but_expired_capacity_still_rejects_effect(
    prepared, tmp_path
):
    service, args = prepared
    recorded = activate(service, args)
    capacity = service.admissions.routing.capacity
    capacity.clock = lambda: recorded["expires_at"] + 1
    capacity_before = capacity.path.read_bytes()
    recovered = service.activation_recorded(*args, principal="owner")
    assert recovered["execution"]["capacity_activation"] == recorded
    assert capacity.path.read_bytes() == capacity_before
    host_prepare(service, args, tmp_path)
    service.record_host_prepared(*args, principal="owner")
    service.mark_start_unknown(*args, principal="owner")
    with service.startup_guard(*args, principal="owner") as history:
        assert history["activation_allowed"] is history["dispatch_enabled"] is False
        with pytest.raises(CapacityError, match="RESERVATION_EXPIRED"):
            with capacity.pre_effect_guard(
                recorded["admission_id"], expected_request=history["request"]
            ):
                pytest.fail("Historical receipt bypassed current Capacity expiry")
    assert capacity.path.read_bytes() == capacity_before


@pytest.mark.parametrize("missing", ["run", "admission"])
def test_reconstructed_intent_status_and_prepare_never_create_missing_ledger(prepared, missing):
    service, args = prepared
    path = (
        service.admissions.routing.planner.database
        if missing == "run"
        else service.admissions.database
    )
    retained = path.with_name(path.name + ".retained")
    path.rename(retained)
    before = retained.read_bytes()
    reopened = GoExecutionIntents(service.admissions, source=service.source, host=service.host)
    import sqlite3

    for operation in (
        lambda: reopened.read(*args, principal="owner"),
        lambda: reopened.reconcile(*args, principal="owner"),
        lambda: reopened.prepare_intent(*args, principal="owner", command_key="new"),
    ):
        with pytest.raises(sqlite3.Error):
            operation()
        assert not path.exists()
        assert retained.read_bytes() == before


def test_cancel_via_original_admission_blocks_all_later_claim_paths(launched_intent):
    service, args, identity = launched_intent
    service.effect_start_claim(*args, principal="owner", runner=identity)
    before = service.admissions.routing.capacity.path.read_bytes()
    cancelled = service.admissions.cancel(*args, principal="owner")
    assert cancelled["cancel_requested"] is cancelled["execution"]["cancel_requested"] is True
    assert cancelled["state"] == "cancellation_pending"
    for guard in (
        service.startup_guard,
        lambda *args, **kwargs: service.effect_claim_guard(*args, **kwargs, runner=identity),
    ):
        with pytest.raises(RunError, match="TASK_EXECUTION_CANCEL_REQUESTED"):
            with guard(*args, principal="owner"):
                pytest.fail("Cancelled original operation became executable")
    with pytest.raises(RunError, match="TASK_EXECUTION_CANCEL_REQUESTED"):
        service.effect_start_claim(*args, principal="owner", runner=identity)
    assert service.admissions.routing.capacity.path.read_bytes() == before
    assert service.host_started(*args, principal="owner")["cancel_requested"] is True
    assert (
        service.prepare_intent(*args, principal="owner", command_key="prepare")["cancel_requested"]
        is True
    )


def test_claim_guard_mutation_and_exception_leave_persisted_identity_unchanged(launched_intent):
    service, args, identity = launched_intent
    service.effect_start_claim(*args, principal="owner", runner=identity)
    original = service.read(*args, principal="owner")
    before = service.admissions.database.read_bytes()
    with pytest.raises(RuntimeError, match="fixture body failed"):
        with service.effect_claim_guard(*args, principal="owner", runner=identity) as held:
            held["execution"]["intent"]["attempt_id"] = "mutated-detached-value"
            raise RuntimeError("fixture body failed")
    assert service.admissions.database.read_bytes() == before
    assert service.read(*args, principal="owner") == original
    assert service.cancel_intent(*args, principal="owner")["cancel_requested"] is True


def test_operation_run_capacity_guards_release_before_cancellation_finishes(launched_intent):
    service, args, identity = launched_intent
    service.effect_start_claim(*args, principal="owner", runner=identity)
    started = Event()

    def cancel():
        started.set()
        return service.cancel_intent(*args, principal="owner")

    routing = service.admissions.routing
    with ThreadPoolExecutor(max_workers=1) as executor:
        with service.effect_claim_guard(*args, principal="owner", runner=identity) as current:
            with routing.reserved_execution_guard(
                args[0], current["assessment"]["id"], principal="owner"
            ) as route:
                assert route["state"] == "selected", route["reason_codes"]
                with routing.capacity.pre_effect_guard(
                    current["capacity_receipt"]["admission_id"], expected_request=current["request"]
                ):
                    pending = executor.submit(cancel)
                    assert started.wait(timeout=2)
                    with pytest.raises(TimeoutError):
                        pending.result(timeout=0.1)
        assert pending.result(timeout=4)["state"] == "cancellation_pending"


def test_new_source_can_read_and_cancel_old_history_but_cannot_reclaim(launched_intent):
    service, args, identity = launched_intent
    saved = service.effect_start_claim(*args, principal="owner", runner=identity)
    changed = GoExecutionIntents(
        service.admissions, host=service.host, source=GoExecutionSource("c" * 64, "d" * 64)
    )
    assert changed.read(*args, principal="owner")["execution"] == saved["execution"]
    with pytest.raises(RunError, match="TASK_EXECUTION_SOURCE_CHANGED"):
        changed.effect_start_claim(*args, principal="owner", runner=identity)
    assert changed.cancel_intent(*args, principal="owner")["cancel_requested"] is True
