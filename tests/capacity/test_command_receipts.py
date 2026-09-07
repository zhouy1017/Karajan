"""Recover committed capacity commands without replaying their actions."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier

import karajan.capacity.store as capacity_store
import pytest
from karajan.capacity import CapacityError, CapacityStore
from test_admission_bindings import bound_request, ledger, observation, request

__all__ = ["ledger"]


def test_lost_admit_response_is_read_after_reopen_without_expiring_or_mutating_it(ledger):
    store, clock = ledger
    value = request()
    store.admit(value, command_key="lost-response")
    before = store.snapshot()
    clock[0] = 5000.0
    recovered = CapacityStore(store.path, clock=lambda: clock[0])

    receipt = recovered.command_receipt(
        "admit", {**value, "expected_capacity": None}, command_key="lost-response"
    )

    assert receipt["decision"] == "admitted"
    assert receipt["admission_id"] == before["reservations"][0]["id"]
    assert receipt["request"] == value
    assert receipt["activation_allowed"] is False
    assert recovered.snapshot() == before
    receipt["request"]["demand"]["short"] = "999"
    assert (
        recovered.command_receipt("admit", value, command_key="lost-response")["request"] == value
    )


@pytest.mark.parametrize("activation_first", [True, False])
def test_cancellation_and_activation_cannot_both_win_after_a_reserved_snapshot(
    ledger, activation_first
):
    store, clock = ledger
    admission_id = store.admit(request(), command_key="reserve")["admission_id"]
    assert store.snapshot()["reservations"][0]["state"] == "reserved"
    other = CapacityStore(store.path, clock=lambda: clock[0])
    cancellation = {"admission_id": admission_id, "evidence_ref": "controller:cancel-before-start"}

    if activation_first:
        other.activate(admission_id, command_key="activate")
        with pytest.raises(CapacityError, match="^CANNOT_RELEASE_ACTIVATED_ADMISSION$"):
            store.cancel_unactivated(**cancellation, command_key="cancel")
        assert store.snapshot()["reservations"][0]["state"] == "active"
        assert (
            store.command_receipt("cancel_unactivated", cancellation, command_key="cancel") is None
        )
    else:
        result = store.cancel_unactivated(**cancellation, command_key="cancel")
        assert result == {
            "admission_id": admission_id,
            "state": "released",
            "activation_allowed": False,
        }
        with pytest.raises(CapacityError, match="^ACTIVATION_ALREADY_RECORDED$"):
            other.activate(admission_id, command_key="activate")
        assert (
            other.command_receipt("cancel_unactivated", cancellation, command_key="cancel")
            == result
        )
        assert other.snapshot()["reservations"][0]["state"] == "released"


def test_missing_receipt_does_not_consult_clock_expire_reservations_or_claim_the_key(ledger):
    store, _ = ledger
    store.admit(request(), command_key="existing")
    before = store.snapshot()
    reader = CapacityStore(store.path, clock=lambda: float("nan"))
    future = {**request("future"), "profile_id": "not-registered"}

    assert reader.command_receipt("admit", future, command_key="unused") is None
    assert (
        reader.command_receipt("admit", request(), command_key="existing")["decision"] == "admitted"
    )
    assert reader.snapshot() == before
    pool = {
        "id": "proof-key-was-not-claimed",
        "account_id": "account",
        "kind": "service",
        "unit": "requests",
        "window_kind": "fixed",
    }
    assert store.register_pool(pool, command_key="unused") == pool


def test_bound_admission_receipt_retains_the_exact_capacity_expectations(ledger):
    store, clock = ledger
    value = bound_request(store)
    original = store.admit(value, command_key="bound")
    reopened = CapacityStore(store.path, clock=lambda: clock[0])

    assert reopened.command_receipt("admit", value, command_key="bound") == original
    changed = deepcopy(value)
    changed["expected_capacity"]["pool_windows"]["short"] = "next-window"
    with pytest.raises(CapacityError, match="^IDEMPOTENCY_CONFLICT$"):
        reopened.command_receipt("admit", changed, command_key="bound")


@pytest.mark.parametrize("change", ["attempt", "amount", "capacity", "kind"])
def test_reading_a_different_command_payload_or_kind_cannot_reuse_its_key(ledger, change):
    store, _ = ledger
    value = request()
    original = store.admit(value, command_key="recorded")
    changed = deepcopy(value)
    kind = "admit"
    if change == "attempt":
        changed["attempt_id"] = "different-attempt"
    elif change == "amount":
        changed["demand"]["short"] = "4"
    elif change == "capacity":
        changed["expected_capacity"] = {
            "policy_revision": 1,
            "pool_windows": {"short": "window-1", "weekly": "window-1"},
            "lead_reserve_access": False,
        }
    else:
        kind = "reconcile"
        changed = reconciliation(original["admission_id"])
    before = store.snapshot()

    with pytest.raises(CapacityError, match="^IDEMPOTENCY_CONFLICT$"):
        store.command_receipt(kind, changed, command_key="recorded")

    assert store.snapshot() == before
    assert (
        store.command_receipt("admit", dict(reversed(list(value.items()))), command_key="recorded")
        == original
    )


def reconciliation(admission_id):
    return {
        "admission_id": admission_id,
        "local_ended": True,
        "remote_ended": False,
        "usage_complete": False,
        "not_sent": False,
        "evidence_ref": "controller:remote-stop-unknown",
    }


def test_reconciliation_receipt_survives_reopen_and_unknown_usage_cannot_be_released(ledger):
    store, clock = ledger
    admission_id = store.admit(request(), command_key="reserve")["admission_id"]
    store.activate(admission_id, command_key="activate")
    payload = reconciliation(admission_id)
    original = store.reconcile(**payload, command_key="reconcile")
    before = store.snapshot()
    reopened = CapacityStore(store.path, clock=lambda: clock[0])

    assert reopened.command_receipt("reconcile", payload, command_key="reconcile") == original
    with pytest.raises(CapacityError, match="^IDEMPOTENCY_CONFLICT$"):
        reopened.command_receipt(
            "reconcile", {**payload, "remote_ended": True}, command_key="reconcile"
        )
    with pytest.raises(CapacityError, match="^CANNOT_RELEASE_ACTIVATED_ADMISSION$"):
        reopened.cancel_unactivated(
            admission_id, evidence_ref="controller:late-cancel", command_key="cancel"
        )
    assert reopened.snapshot() == before
    assert original["state"] == "unknown"


def test_cancel_receipt_is_durable_and_a_new_key_cannot_release_again(ledger):
    store, clock = ledger
    admission_id = store.admit(request(), command_key="reserve")["admission_id"]
    payload = {"admission_id": admission_id, "evidence_ref": "controller:cancel"}
    original = store.cancel_unactivated(**payload, command_key="cancel")
    before = store.snapshot()
    reopened = CapacityStore(store.path, clock=lambda: clock[0])

    assert reopened.cancel_unactivated(**payload, command_key="cancel") == original
    assert reopened.command_receipt("cancel_unactivated", payload, command_key="cancel") == original
    with pytest.raises(CapacityError, match="^IDEMPOTENCY_CONFLICT$"):
        reopened.command_receipt(
            "cancel_unactivated",
            {**payload, "evidence_ref": "other:evidence"},
            command_key="cancel",
        )
    with pytest.raises(CapacityError, match="^ADMISSION_ALREADY_RECONCILED$"):
        reopened.cancel_unactivated(**payload, command_key="another-cancel")
    with pytest.raises(CapacityError, match="^CAPACITY_INPUT_INVALID$"):
        reopened.command_receipt(
            "cancel_unactivated", {**payload, "not_sent": True}, command_key="invented-unsent"
        )
    assert reopened.snapshot() == before


def test_cancel_keeps_elapsed_unsent_reservation_expired_and_cannot_activate_it(ledger):
    store, clock = ledger
    admission_id = store.admit(request(), command_key="reserve")["admission_id"]
    clock[0] = 1040.0

    result = store.cancel_unactivated(
        admission_id, evidence_ref="controller:cancel", command_key="cancel"
    )

    assert result["state"] == "expired"
    assert store.snapshot()["reservations"][0]["state"] == "expired"
    assert store.activate(admission_id, command_key="activate")["reason_codes"] == [
        "RESERVATION_EXPIRED"
    ]


def test_ended_admission_is_not_released_by_new_cancellation(ledger):
    store, _ = ledger
    admission_id = store.admit(request(), command_key="reserve")["admission_id"]
    store.activate(admission_id, command_key="activate")
    store.record_usage(
        {
            "id": "usage",
            "admission_id": admission_id,
            "amounts": {"short": "1", "weekly": "1"},
            "window_ids": {"short": "window-1", "weekly": "window-1"},
            "evidence_ref": "controller:usage",
            "attribution_ref": "controller:window-attribution",
        },
        command_key="usage",
    )
    store.reconcile(
        **{**reconciliation(admission_id), "remote_ended": True, "usage_complete": True},
        command_key="ended",
    )
    before = store.snapshot()
    assert before["reservations"][0]["state"] == "ended"

    with pytest.raises(CapacityError, match="^ADMISSION_ALREADY_RECONCILED$"):
        store.cancel_unactivated(
            admission_id, evidence_ref="controller:cancel", command_key="cancel"
        )
    assert store.snapshot() == before


def test_concurrent_activation_and_cancellation_serialize_in_the_capacity_transaction(ledger):
    store, clock = ledger
    admission_id = store.admit(request(), command_key="reserve")["admission_id"]
    activator = CapacityStore(store.path, clock=lambda: clock[0])
    canceller = CapacityStore(store.path, clock=lambda: clock[0])
    barrier = Barrier(2)

    def command(activate):
        barrier.wait(timeout=5)
        try:
            if activate:
                return activator.activate(admission_id, command_key="activate")
            return canceller.cancel_unactivated(
                admission_id, evidence_ref="controller:cancel", command_key="cancel"
            )
        except CapacityError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(command, [True, False]))

    state = store.snapshot()["reservations"][0]["state"]
    assert sum(isinstance(value, dict) for value in outcomes) == 1
    if state == "active":
        assert outcomes[1] == "CANNOT_RELEASE_ACTIVATED_ADMISSION"
    else:
        assert state == "released"
        assert outcomes[0] == "ACTIVATION_ALREADY_RECORDED"


@pytest.mark.parametrize("key", ["", "control\n", "\ud800", True, None])
def test_receipt_command_keys_have_the_same_boundary_as_mutating_commands(ledger, key):
    store, _ = ledger
    before = store.snapshot()
    with pytest.raises(CapacityError, match="^CAPACITY_INPUT_INVALID$"):
        store.command_receipt("admit", request(), command_key=key)
    with pytest.raises(CapacityError, match="^CAPACITY_INPUT_INVALID$"):
        store.admit(request(), command_key=key)
    assert store.snapshot() == before


def test_receipt_only_accepts_the_defined_recovery_kinds_and_strict_payloads(ledger):
    store, _ = ledger
    before = store.snapshot()
    with pytest.raises(CapacityError, match="^COMMAND_RECEIPT_KIND_UNSUPPORTED$"):
        store.command_receipt("start", {"admission_id": "unknown"}, command_key="unknown")
    with pytest.raises(CapacityError, match="^CAPACITY_INPUT_INVALID$"):
        store.command_receipt("reconcile", {"admission_id": "unknown"}, command_key="unknown")
    assert store.snapshot() == before


def test_admit_boundary_callback_skips_exact_historical_receipt(ledger):
    store, _ = ledger
    value = request()
    original = store.admit(value, command_key="historical-admit")
    callbacks: list[str] = []

    replayed = store.admit(
        value,
        command_key="historical-admit",
        before_reserve=lambda: callbacks.append("called"),
        after_capacity_facts=lambda _: callbacks.append("facts"),
        before_reservation_write=lambda: callbacks.append("write"),
    )

    assert replayed == original
    assert callbacks == []


def test_admit_boundary_callback_rolls_back_before_reservation_and_receipt(ledger):
    store, _ = ledger
    value = request()
    before = store.snapshot()
    callbacks: list[str] = []

    def deadline_expired() -> None:
        callbacks.append("called")
        raise RuntimeError("RUN_DURATION_LIMIT")

    with pytest.raises(RuntimeError, match="^RUN_DURATION_LIMIT$"):
        store.admit(value, command_key="deadline-expired", before_reserve=deadline_expired)

    assert callbacks == ["called"]
    assert store.snapshot() == before
    assert store.command_receipt("admit", value, command_key="deadline-expired") is None


def test_admit_facts_callback_is_full_and_has_no_owned_claim(ledger):
    store, _ = ledger
    seen = []

    admitted = store.admit(
        request(),
        command_key="boundary-facts",
        after_capacity_facts=seen.append,
    )

    assert admitted["decision"] == "admitted"
    assert len(seen) == 1
    assert seen[0].owned_admission_id is None
    assert seen[0].facts.as_dict()["account_ids"] == ["account"]


def test_admit_facts_callback_failure_rolls_back_reservation_and_receipt(ledger):
    store, _ = ledger
    value = request()
    before = store.snapshot()

    def forbidden_boundary_calculation(_) -> None:
        raise RuntimeError("ROUTE_CONSERVATIVE_LIMIT_EXCEEDED")

    with pytest.raises(RuntimeError, match="^ROUTE_CONSERVATIVE_LIMIT_EXCEEDED$"):
        store.admit(
            value,
            command_key="boundary-facts-failure",
            after_capacity_facts=forbidden_boundary_calculation,
        )

    assert store.snapshot() == before
    assert store.command_receipt("admit", value, command_key="boundary-facts-failure") is None


def test_admit_final_callback_runs_after_capacity_reads_and_rechecks_its_clock(ledger, monkeypatch):
    store, clock = ledger
    reads: list[str] = []
    original = store._observation

    def delayed_observation(*args):
        observed = original(*args)
        reads.append("observation")
        clock[0] = 1005.0
        return observed

    monkeypatch.setattr(store, "_observation", delayed_observation)
    callbacks: list[str] = []
    rejected = store.admit(
        request(),
        command_key="final-capacity-temporal",
        before_reservation_write=lambda: callbacks.append("write"),
    )

    assert reads
    assert callbacks == []
    assert rejected["decision"] == "rejected"
    assert "OBSERVATION_STALE:short" in rejected["reason_codes"]
    assert store.snapshot()["reservations"] == []


def test_admit_final_callback_failure_rolls_back_reservation_and_receipt(ledger):
    store, _ = ledger
    value = request()
    before = store.snapshot()

    def expired_run_deadline() -> None:
        raise RuntimeError("RUN_DURATION_LIMIT")

    with pytest.raises(RuntimeError, match="^RUN_DURATION_LIMIT$"):
        store.admit(
            value,
            command_key="final-capacity-deadline",
            before_reservation_write=expired_run_deadline,
        )

    assert store.snapshot() == before
    assert store.command_receipt("admit", value, command_key="final-capacity-deadline") is None


def test_admit_final_callback_can_defer_its_scalar_check_until_the_write_tail(ledger):
    store, clock = ledger
    calls: list[str] = []

    def prepare_final_check():
        calls.append("prepare")

        def final_check() -> None:
            calls.append("final")
            clock[0] = 1001.0

        return final_check

    admitted = store.admit(
        request(), command_key="deferred-final-check", before_reservation_write=prepare_final_check
    )

    assert admitted["decision"] == "admitted"
    assert calls == ["prepare", "final"]
    reservation = store.snapshot()["reservations"][0]
    assert reservation["created_at"] == 1000.0
    assert reservation["expires_at"] == 1030.0


def test_admit_rejects_when_reservation_encoding_consumes_its_lifetime(ledger, monkeypatch):
    store, clock = ledger
    policy = store.snapshot()["policies"][-1]["policy"]
    policy["observation_max_age_seconds"] = 1000
    store.activate_policy(policy, expected_revision=1, command_key="long-observation-window")
    clock[0] = 1006.0
    for pool in ("short", "weekly"):
        observation(store, pool, "20", at=clock[0], window="window-2")
    original = capacity_store.encoded

    def delayed_reservation_encoding(value):
        result = original(value)
        if isinstance(value, dict) and value.get("state") == "reserved":
            clock[0] = 1036.0
        return result

    monkeypatch.setattr(capacity_store, "encoded", delayed_reservation_encoding)
    rejected = store.admit(request(), command_key="encoding-crosses-reservation-expiry")

    assert rejected["decision"] == "rejected"
    assert rejected["reason_codes"] == ["RESERVATION_EXPIRED"]
    assert store.snapshot()["reservations"] == []


def test_admit_rejects_a_clock_that_regresses_after_its_creation_sample(ledger, monkeypatch):
    store, clock = ledger
    original = capacity_store.encoded

    def regress_after_encoding(value):
        result = original(value)
        if isinstance(value, dict) and value.get("state") == "reserved":
            clock[0] = 1000.0
        return result

    def advance_controller_clock() -> None:
        clock[0] = 1001.0

    monkeypatch.setattr(capacity_store, "encoded", regress_after_encoding)
    rejected = store.admit(
        request(),
        command_key="encoding-regresses-after-creation",
        before_reservation_write=advance_controller_clock,
    )

    assert rejected["decision"] == "rejected"
    assert rejected["reason_codes"] == ["CAPACITY_CLOCK_REGRESSED"]
    assert store.snapshot()["reservations"] == []


def test_admit_rolls_back_an_earlier_expiry_when_a_recheck_clock_regresses(ledger):
    store, clock = ledger
    held = store.admit(
        {**request("held"), "duration_seconds": 3}, command_key="short-lived-held"
    )
    clock[0] = 1004.0
    before = store.snapshot()

    def regress_before_recheck() -> None:
        clock[0] = 1002.0

    with pytest.raises(CapacityError, match="^CAPACITY_CLOCK_REGRESSED$"):
        store.admit(
            request("candidate"),
            command_key="regressed-recheck",
            before_reserve=regress_before_recheck,
        )

    assert store.snapshot() == before
    assert store.snapshot()["reservations"] == [
        {**before["reservations"][0], "id": held["admission_id"], "state": "reserved"}
    ]
    assert (
        store.command_receipt("admit", request("candidate"), command_key="regressed-recheck")
        is None
    )


def test_admit_rechecks_time_and_uses_the_post_callback_reservation_clock(ledger):
    store, clock = ledger
    calls: list[str] = []

    def delayed_controller_read() -> None:
        calls.append("called")
        clock[0] = 1001.0

    admitted = store.admit(
        request(), command_key="post-callback-clock", before_reserve=delayed_controller_read
    )
    reservation = store.snapshot()["reservations"][0]
    assert calls == ["called"]
    assert admitted["decision"] == "admitted"
    assert reservation["created_at"] == 1001.0
    assert reservation["expires_at"] == 1031.0


def test_admit_rechecks_observation_expiry_after_a_blocking_callback(ledger):
    store, clock = ledger
    calls: list[str] = []

    def delayed_controller_read() -> None:
        calls.append("called")
        clock[0] = 1005.0

    rejected = store.admit(
        request(), command_key="post-callback-stale", before_reserve=delayed_controller_read
    )
    assert calls == ["called"]
    assert rejected["decision"] == "rejected"
    assert "OBSERVATION_STALE:short" in rejected["reason_codes"]
    assert store.snapshot()["reservations"] == []
