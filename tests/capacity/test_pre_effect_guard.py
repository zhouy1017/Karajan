"""Fresh pre-effect capacity checks and historical activation recovery via real stores."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from copy import deepcopy
from threading import Event

import pytest
from karajan.capacity import CapacityError, CapacityStore
from test_admission_bindings import bound_request, ledger, observation, request

__all__ = ["ledger"]


def test_lost_activation_response_is_history_even_when_clock_is_unavailable(ledger):
    store, _ = ledger
    value = bound_request(store)
    admission_id = store.admit(value, command_key="reserve")["admission_id"]
    original = store.activate(admission_id, command_key="lost-activation-response")
    before = store.snapshot()

    def forbidden_clock():
        raise AssertionError("Historical receipt must not consult the clock")

    reopened = CapacityStore(store.path, clock=forbidden_clock)
    receipt = reopened.command_receipt(
        "activate", {"admission_id": admission_id}, command_key="lost-activation-response"
    )
    assert receipt == original
    assert receipt["activation_allowed"] is False
    assert reopened.snapshot() == before
    receipt["available_before"]["short"] = "999"
    assert (
        reopened.command_receipt(
            "activate", {"admission_id": admission_id}, command_key="lost-activation-response"
        )
        == original
    )
    with pytest.raises(CapacityError, match="^IDEMPOTENCY_CONFLICT$"):
        reopened.command_receipt(
            "activate", {"admission_id": "another"}, command_key="lost-activation-response"
        )


def test_exactly_one_slot_and_one_demand_are_not_charged_again_for_the_same_admission(ledger):
    store, clock = ledger
    policy = store.snapshot()["policies"][-1]["policy"]
    policy.update(max_active_attempts=1, lead_reserved_slots=0, lead_reserve={})
    store.activate_policy(policy, expected_revision=1, command_key="only-one-slot")
    clock[0] = 1001.0
    for pool in ("short", "weekly"):
        observation(store, pool, "3", at=clock[0])
    value = bound_request(store)
    admission_id = store.admit(value, command_key="reserve")["admission_id"]
    original = store.activate(admission_id, command_key="activate")
    before = store.snapshot()

    with store.pre_effect_guard(admission_id, expected_request=value) as current:
        assert current["decision"] == "capacity_revalidated"
        assert current["reason_codes"] == []
        assert current["available_before"] == {"short": "3.000000", "weekly": "3.000000"}
        assert current["state"] == "active"
        assert current["checked_at"] == clock[0]
        assert current["expires_at"] == original["expires_at"]
        assert current["request"] == value
        assert current["activation_allowed"] is False
    assert store.snapshot() == before
    assert len(before["lifecycle"]) == 1


@pytest.mark.parametrize("other_state", ["reserved", "active", "unknown"])
def test_other_runs_holds_remain_charged_when_excluding_only_the_original_admission(
    ledger, other_state
):
    store, clock = ledger
    value = bound_request(store, "own")
    admission_id = store.admit(value, command_key="own")["admission_id"]
    store.activate(admission_id, command_key="own-activation")
    second = bound_request(store, "other-run")
    other_id = store.admit(second, command_key="other")["admission_id"]
    if other_state != "reserved":
        store.activate(other_id, command_key="other-activation")
    if other_state == "unknown":
        store.reconcile(
            other_id,
            local_ended=True,
            remote_ended=False,
            usage_complete=False,
            not_sent=False,
            evidence_ref="fixture:unknown",
            command_key="unknown",
        )
    clock[0] = 1001.0
    observation(store, "short", "5", at=clock[0])
    before = store.snapshot()

    with pytest.raises(CapacityError, match="^QUOTA_INSUFFICIENT:short$"):
        with store.pre_effect_guard(admission_id, expected_request=value):
            pytest.fail("The other Run consumes the remaining quota")
    assert store.snapshot() == before


def test_recorded_usage_remains_charged_after_its_admission_ends(ledger):
    store, clock = ledger
    value = bound_request(store, "own")
    admission_id = store.admit(value, command_key="own")["admission_id"]
    store.activate(admission_id, command_key="own-activation")
    other_id = store.admit(bound_request(store, "other"), command_key="other")["admission_id"]
    store.activate(other_id, command_key="other-activation")
    store.record_usage(
        {
            "id": "actual-consumption",
            "admission_id": other_id,
            "amounts": {"short": "4", "weekly": "4"},
            "window_ids": {"short": "window-1", "weekly": "window-1"},
            "evidence_ref": "fixture:actual-usage",
            "attribution_ref": "fixture:window",
        },
        command_key="usage",
    )
    store.reconcile(
        other_id,
        local_ended=True,
        remote_ended=True,
        usage_complete=True,
        not_sent=False,
        evidence_ref="fixture:ended",
        command_key="ended",
    )
    clock[0] = 1001.0
    observation(store, "short", "5", at=clock[0])
    before = store.snapshot()
    with pytest.raises(CapacityError, match="^QUOTA_INSUFFICIENT:short$"):
        with store.pre_effect_guard(admission_id, expected_request=value):
            pytest.fail("Ending a Run does not refund its observed consumption")
    assert store.snapshot() == before


@pytest.mark.parametrize("state", ["reserved", "unknown", "released", "expired"])
def test_only_active_unexpired_reservations_can_enter_the_guard(ledger, state):
    store, clock = ledger
    value = bound_request(store)
    admission_id = store.admit(value, command_key="reserve")["admission_id"]
    if state == "unknown":
        store.activate(admission_id, command_key="activate")
        store.reconcile(
            admission_id,
            local_ended=True,
            remote_ended=False,
            usage_complete=False,
            not_sent=False,
            evidence_ref="fixture:unknown",
            command_key="unknown",
        )
    elif state == "released":
        store.cancel_unactivated(admission_id, evidence_ref="fixture:cancel", command_key="cancel")
    elif state == "expired":
        store.activate(admission_id, command_key="activate")
        clock[0] = 1030.0
    before = store.snapshot()
    reason = "RESERVATION_EXPIRED" if state == "expired" else "ADMISSION_NOT_ACTIVE"
    with pytest.raises(CapacityError, match=f"^{reason}$"):
        with store.pre_effect_guard(admission_id, expected_request=value):
            pytest.fail("No new effect allowed")
    assert store.snapshot() == before


@pytest.mark.parametrize("field", ["run_id", "authorization_ref", "demand", "expected_capacity"])
def test_complete_original_request_cannot_be_rebound_before_effect(ledger, field):
    store, _ = ledger
    value = bound_request(store)
    admission_id = store.admit(value, command_key="reserve")["admission_id"]
    store.activate(admission_id, command_key="activate")
    changed = deepcopy(value)
    if field == "demand":
        changed[field]["short"] = "3.0"
    elif field == "expected_capacity":
        changed[field]["lead_reserve_access"] = True
    else:
        changed[field] = "another-binding"
    before = store.snapshot()
    with pytest.raises(CapacityError, match="^ADMISSION_REQUEST_MISMATCH$"):
        with store.pre_effect_guard(admission_id, expected_request=changed):
            pytest.fail("A numerical synonym or changed binding is not the exact request")
    assert store.snapshot() == before


def test_legacy_activation_stays_supported_but_cannot_bypass_the_new_bound_effect_guard(ledger):
    store, _ = ledger
    legacy = request()
    admission_id = store.admit(legacy, command_key="reserve")["admission_id"]
    original = store.activate(admission_id, command_key="activate")
    assert original["decision"] == "capacity_revalidated"
    with pytest.raises(CapacityError, match="^CAPACITY_BINDING_REQUIRED$"):
        with store.pre_effect_guard(admission_id, expected_request=legacy):
            pytest.fail("The effect guard requires original policy and window bindings")


@pytest.mark.parametrize("effect_raises", [False, True])
def test_other_connection_updates_wait_until_guard_exit_and_activation_survives(
    effect_raises, ledger
):
    store, clock = ledger
    value = bound_request(store)
    admission_id = store.admit(value, command_key="reserve")["admission_id"]
    original = store.activate(admission_id, command_key="activate")
    updater = CapacityStore(store.path, clock=lambda: 1001.0)
    attempted, finished = Event(), Event()

    def update():
        attempted.set()
        result = observation(updater, "short", "2", at=1000.5)
        finished.set()
        return result

    context = (
        pytest.raises(RuntimeError, match="synthetic effect failure")
        if effect_raises
        else nullcontext()
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        with context:
            with store.pre_effect_guard(admission_id, expected_request=value) as current:
                future = pool.submit(update)
                assert attempted.wait(3)
                assert not finished.wait(0.15)
                assert current["observations"]["short"]["amount"] == "20"
                if effect_raises:
                    raise RuntimeError("synthetic effect failure")
        assert future.result(timeout=3)["applied"] is True
    assert finished.is_set()
    clock[0] = 1001.0
    with pytest.raises(CapacityError, match="^QUOTA_INSUFFICIENT:short$"):
        with store.pre_effect_guard(admission_id, expected_request=value):
            pytest.fail("The next guard must observe the newly committed balance")
    assert (
        store.command_receipt("activate", {"admission_id": admission_id}, command_key="activate")
        == original
    )
    snapshot = store.snapshot()
    assert snapshot["reservations"][0]["state"] == "active"
    assert len(snapshot["lifecycle"]) == 1
