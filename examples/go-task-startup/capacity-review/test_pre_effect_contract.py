"""Independent public Capacity gate contract; synthetic observations, no Host or provider."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from copy import deepcopy
from threading import Event

import pytest
from karajan.capacity import CapacityError, CapacityStore
from test_admission_bindings import bound_request, ledger, observation
from test_admission_bindings import request as legacy_request

__all__ = ["ledger"]


def reserve(store, *, identity="first", activate=True):
    request = bound_request(store, identity)
    receipt = store.admit(request, command_key="admit-" + identity)
    assert receipt["decision"] == "admitted", receipt
    if activate:
        result = store.activate(receipt["admission_id"], command_key="activate-" + identity)
        assert result["decision"] == "capacity_revalidated", result
    return receipt["admission_id"], request


def configure_single_slot_exact_headroom(store, clock):
    policy = store.snapshot()["policies"][-1]["policy"]
    policy.update(max_active_attempts=1, lead_reserved_slots=0)
    store.activate_policy(policy, expected_revision=1, command_key="one-slot")
    clock[0] = 1001.0
    observation(store, "short", "3", at=clock[0])
    # Weekly demand 3 plus the owner's unchanged lead reserve 8.
    observation(store, "weekly", "11", at=clock[0])


def test_existing_activate_excludes_own_reservation_at_exact_slot_and_pool_limits(ledger):
    store, clock = ledger
    configure_single_slot_exact_headroom(store, clock)
    admission_id, _ = reserve(store)
    snapshot = store.snapshot()
    assert len(snapshot["reservations"]) == 1
    assert snapshot["reservations"][0]["id"] == admission_id
    assert snapshot["reservations"][0]["state"] == "active"


def test_pre_effect_guard_does_not_spend_own_reserved_slot_or_vector_twice(ledger):
    store, clock = ledger
    configure_single_slot_exact_headroom(store, clock)
    admission_id, request = reserve(store)
    before = store.snapshot()
    reached = []
    with store.pre_effect_guard(admission_id, expected_request=request) as checked:
        assert checked["admission_id"] == admission_id
        assert checked["activation_allowed"] is False
        assert checked["reason_codes"] == []
        assert checked["available_before"] == {"short": "3.000000", "weekly": "3.000000"}
        reached.append("capacity-guard-body")
    assert reached == ["capacity-guard-body"]
    assert store.snapshot() == before


@pytest.mark.parametrize("other_state", ["reserved", "active", "unknown"])
def test_other_run_hold_is_never_excluded_with_the_current_admission(ledger, other_state):
    store, clock = ledger
    admission_id, request = reserve(store)
    other, _ = reserve(store, identity="another-run", activate=other_state != "reserved")
    if other_state == "unknown":
        store.reconcile(
            other,
            local_ended=True,
            remote_ended=False,
            usage_complete=False,
            not_sent=False,
            evidence_ref="test:unknown-remote",
            command_key="other-unknown",
        )
    clock[0] = 1001.0
    observation(store, "weekly", "13", at=clock[0])
    # Excluding only own 3 leaves 13 - lead 8 - other 3 = 2, below own demand 3.
    with pytest.raises(CapacityError, match="QUOTA_INSUFFICIENT:weekly"):
        with store.pre_effect_guard(admission_id, expected_request=request):
            pytest.fail("Another Run's reservation was hidden from the effect gate")
    facts = store.routing_facts().as_dict()["accounts"][0]
    assert set(facts["held_admission_ids"]) == {admission_id, other}


def test_existing_activate_receipt_is_recovered_readonly_even_with_unavailable_clock(ledger):
    store, _ = ledger
    admission_id, _ = reserve(store)
    # Caller did not retain the response; the committed command remains authoritative history.
    before = store.snapshot()
    reopened = CapacityStore(store.path, clock=lambda: float("nan"))
    payload = {"admission_id": admission_id}
    recovered = reopened.command_receipt("activate", payload, command_key="activate-first")
    assert recovered["decision"] == "capacity_revalidated"
    assert recovered["admission_id"] == admission_id
    assert recovered["activation_allowed"] is False
    recovered["observations"]["short"]["amount"] = "999"
    retained = reopened.command_receipt("activate", payload, command_key="activate-first")
    assert retained["observations"]["short"]["amount"] == "20"
    assert reopened.snapshot() == before


def test_missing_activation_receipt_does_not_claim_command_key(ledger):
    store, _ = ledger
    admission_id, _ = reserve(store, activate=False)
    before = store.snapshot()
    reader = CapacityStore(store.path, clock=lambda: float("nan"))
    assert (
        reader.command_receipt(
            "activate", {"admission_id": admission_id}, command_key="future-activation"
        )
        is None
    )
    assert reader.snapshot() == before
    assert store.activate(admission_id, command_key="future-activation")["decision"] == (
        "capacity_revalidated"
    )


def test_activation_receipt_requires_original_command_payload(ledger):
    store, _ = ledger
    reserve(store)
    with pytest.raises(CapacityError, match="^IDEMPOTENCY_CONFLICT$"):
        store.command_receipt(
            "activate", {"admission_id": "different-admission"}, command_key="activate-first"
        )


@pytest.mark.parametrize(
    "change,reason",
    [
        ("policy", "CAPACITY_POLICY_REVISION_CHANGED"),
        ("window", "CAPACITY_WINDOW_CHANGED:short"),
        ("balance", "QUOTA_INSUFFICIENT:short"),
        ("stale", "OBSERVATION_STALE:short"),
        ("cooldown", "ACCOUNT_COOLDOWN"),
    ],
)
def test_changes_after_committed_activation_prevent_entering_effect_body(ledger, change, reason):
    store, clock = ledger
    admission_id, request = reserve(store)
    if change == "policy":
        policy = store.snapshot()["policies"][-1]["policy"]
        store.activate_policy(policy, expected_revision=1, command_key="changed-policy")
    elif change == "window":
        clock[0] = 1006.0
        observation(store, "short", "20", at=clock[0], window="window-2")
    elif change == "balance":
        clock[0] = 1001.0
        observation(store, "short", "2", at=clock[0])
    elif change == "stale":
        clock[0] = 1006.0
    else:
        store.record_failure(
            "account",
            reason="RATE_LIMIT_TRANSIENT",
            retry_after_seconds=5,
            evidence_ref="test:rate-limit",
            command_key="rate-limit",
        )
    before = store.snapshot()
    with pytest.raises(CapacityError, match=reason):
        with store.pre_effect_guard(admission_id, expected_request=request):
            pytest.fail("Stale activation history entered the effect body")
    assert store.snapshot() == before


def test_new_same_window_observation_is_rechecked_without_requiring_old_snapshot_digest(ledger):
    store, clock = ledger
    admission_id, request = reserve(store)
    clock[0] = 1001.0
    observation(store, "short", "7", at=clock[0])
    with store.pre_effect_guard(admission_id, expected_request=request) as checked:
        assert checked["observations"]["short"]["amount"] == "7"
        assert checked["available_before"]["short"] == "7.000000"


@pytest.mark.parametrize(
    "state", ["reserved", "expired-unsent", "expired-active", "released", "unknown", "ended"]
)
def test_only_unexpired_active_admission_can_enter_the_guard(ledger, state):
    store, clock = ledger
    admission_id, request = reserve(store, activate=state not in {"reserved", "expired-unsent"})
    if state == "expired-unsent":
        clock[0] = 1031.0
        assert store.activate(admission_id, command_key="expired")["reason_codes"] == [
            "RESERVATION_EXPIRED"
        ]
    elif state == "expired-active":
        clock[0] = 1031.0
    elif state == "released":
        store.reconcile(
            admission_id,
            local_ended=True,
            remote_ended=True,
            usage_complete=False,
            not_sent=True,
            evidence_ref="test:proven-unsent",
            command_key="release",
        )
    elif state == "unknown":
        store.reconcile(
            admission_id,
            local_ended=True,
            remote_ended=False,
            usage_complete=False,
            not_sent=False,
            evidence_ref="test:remote-unknown",
            command_key="unknown",
        )
    elif state == "ended":
        store.record_usage(
            {
                "id": "usage",
                "admission_id": admission_id,
                "amounts": {"short": "1", "weekly": "1"},
                "window_ids": {"short": "window-1", "weekly": "window-1"},
                "evidence_ref": "test:usage",
                "attribution_ref": "test:attribution",
            },
            command_key="usage",
        )
        store.reconcile(
            admission_id,
            local_ended=True,
            remote_ended=True,
            usage_complete=True,
            not_sent=False,
            evidence_ref="test:ended",
            command_key="ended",
        )
    before = store.snapshot()
    with pytest.raises(CapacityError):
        with store.pre_effect_guard(admission_id, expected_request=request):
            pytest.fail("Non-executable admission entered the effect body")
    assert store.snapshot() == before


@pytest.mark.parametrize(
    "axis", ["attempt", "run", "profile", "authorization", "demand", "window", "reserve-access"]
)
def test_caller_cannot_present_a_different_expected_request_for_own_active_admission(ledger, axis):
    store, _ = ledger
    admission_id, request = reserve(store)
    changed = deepcopy(request)
    if axis in {"attempt", "run", "profile"}:
        changed[axis + "_id"] = "different"
    elif axis == "authorization":
        changed["authorization_ref"] = "different"
    elif axis == "demand":
        changed["demand"]["short"] = "1"
    elif axis == "window":
        changed["expected_capacity"]["pool_windows"]["short"] = "next-window"
    else:
        changed["expected_capacity"]["lead_reserve_access"] = True
    before = store.snapshot()
    with pytest.raises(CapacityError):
        with store.pre_effect_guard(admission_id, expected_request=changed):
            pytest.fail("Different authority/demand was accepted for the original reservation")
    assert store.snapshot() == before


@pytest.mark.parametrize("change", ["policy", "observation"])
def test_capacity_writer_waits_for_guard_exit_then_invalidates_the_next_entry(ledger, change):
    store, clock = ledger
    admission_id, request = reserve(store)
    clock[0] = 1001.0
    writer = CapacityStore(store.path, clock=lambda: clock[0])
    policy = store.snapshot()["policies"][-1]["policy"]
    entered = Event()
    finished = Event()

    def update():
        entered.set()
        if change == "policy":
            result = writer.activate_policy(
                policy, expected_revision=1, command_key="writer-policy"
            )
        else:
            result = observation(writer, "short", "2", at=1001.0)
        finished.set()
        return result

    with ThreadPoolExecutor(max_workers=1) as executor:
        with store.pre_effect_guard(admission_id, expected_request=request):
            future = executor.submit(update)
            assert entered.wait(timeout=2)
            with pytest.raises(TimeoutError):
                future.result(timeout=0.2)
            assert not finished.is_set()
        future.result(timeout=5)
    assert finished.is_set()
    clock[0] = 1001.0
    with pytest.raises(CapacityError):
        with store.pre_effect_guard(admission_id, expected_request=request):
            pytest.fail("The next effect entry ignored the committed writer")


def test_guard_body_failure_does_not_undo_previously_committed_activation(ledger):
    store, _ = ledger
    admission_id, request = reserve(store)
    before = store.snapshot()
    with pytest.raises(RuntimeError, match="controlled-effect-failure"):
        with store.pre_effect_guard(admission_id, expected_request=request):
            raise RuntimeError("controlled-effect-failure")
    assert store.snapshot() == before
    reopened = CapacityStore(store.path, clock=lambda: 1000.0)
    assert (
        reopened.command_receipt(
            "activate", {"admission_id": admission_id}, command_key="activate-first"
        )["decision"]
        == "capacity_revalidated"
    )


def test_legacy_unbound_request_cannot_enter_new_trusted_effect_guard(ledger):
    store, _ = ledger
    request = legacy_request()
    admission_id = store.admit(request, command_key="legacy-reserve")["admission_id"]
    assert store.activate(admission_id, command_key="legacy-activate")["decision"] == (
        "capacity_revalidated"
    )
    with pytest.raises(CapacityError):
        with store.pre_effect_guard(admission_id, expected_request=request):
            pytest.fail("Legacy unbound windows/policy were promoted into execution clearance")
