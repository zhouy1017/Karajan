"""Bind real quota admission to the windows and protection used by trusted routing."""

import copy
import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from karajan.capacity import CapacityError, CapacityStore


@pytest.fixture
def ledger(tmp_path: Path):
    clock = [1000.0]
    store = CapacityStore(tmp_path / "capacity.sqlite", clock=lambda: clock[0])
    for identity in ("short", "weekly"):
        store.register_pool(
            {
                "id": identity,
                "account_id": "account",
                "kind": "service",
                "unit": "requests",
                "window_kind": "fixed",
            },
            command_key="pool-" + identity,
        )
        observation(store, identity, "20", at=clock[0])
    store.register_profile(
        {"id": "profile", "revision": 1, "account_id": "account", "pool_ids": ["short", "weekly"]},
        command_key="profile",
    )
    store.activate_policy(
        {
            "account_id": "account",
            "max_active_attempts": 4,
            "max_attempt_duration_seconds": 60,
            "observation_max_age_seconds": 30,
            "require_official_observation": False,
            "safety_margin": {},
            "lead_reserve": {"weekly": "8"},
            "lead_reserved_slots": 1,
            "conservative_mode": None,
        },
        expected_revision=0,
        command_key="policy",
    )
    return store, clock


def observation(store, identity, remaining, *, at, window="window-1"):
    return store.observe(
        {
            "pool_id": identity,
            "window_id": window,
            "observed_at": at,
            "reset_at": (1005.0 if window == "window-1" else 2000.0)
            if identity == "short"
            else 10000.0,
            "source": "fixture",
            "source_ref": "synthetic-observation",
            "metric": "remaining",
            "amount": remaining,
            "limit": "20",
            "covered_usage_ids": [],
        },
        command_key=f"observation-{identity}-{at}",
    )


def request(identity="attempt", *, role="worker", purpose=None):
    return {
        "attempt_id": identity,
        "run_id": "run-" + identity,
        "profile_id": "profile",
        "profile_revision": 1,
        "role": role,
        "purpose": purpose,
        "authorization_ref": "synthetic-approved-scope",
        "rulebook_revision": "synthetic-rules-1",
        "duration_seconds": 30,
        "demand": {"short": "3", "weekly": "3"},
    }


def bound_request(store, identity="attempt", *, access=False, **kwargs):
    facts = store.routing_facts().as_dict()["accounts"][0]
    return {
        **request(identity, **kwargs),
        "expected_capacity": {
            "policy_revision": facts["policy_revision"],
            "pool_windows": {
                p["id"]: p["observation"]["observation"]["window_id"] for p in facts["pools"]
            },
            "lead_reserve_access": access,
        },
    }


@pytest.mark.parametrize("phase", ["admit", "activate"])
def test_policy_revision_changes_are_rejected_at_each_transaction_boundary(ledger, phase):
    store, _ = ledger
    value = bound_request(store)
    admitted = store.admit(value, command_key="reserve") if phase == "activate" else None
    policy = store.snapshot()["policies"][-1]["policy"]
    store.activate_policy(policy, expected_revision=1, command_key="new-policy")
    result = (
        store.admit(value, command_key="stale")
        if phase == "admit"
        else store.activate(admitted["admission_id"], command_key="stale")
    )
    assert result["decision"] == "rejected"
    assert result["reason_codes"] == ["CAPACITY_POLICY_REVISION_CHANGED"]
    assert result["policy_revision"] == 2
    assert len(store.snapshot()["reservations"]) == (1 if admitted else 0)
    assert not store.snapshot()["lifecycle"]


@pytest.mark.parametrize("phase", ["admit", "activate"])
def test_window_rollover_rejects_estimate_bound_to_old_window(ledger, phase):
    store, clock = ledger
    value = bound_request(store)
    admitted = store.admit(value, command_key="reserve") if phase == "activate" else None
    clock[0] = 1006.0
    observation(store, "short", "20", at=clock[0], window="window-2")
    result = (
        store.admit(value, command_key="stale")
        if phase == "admit"
        else store.activate(admitted["admission_id"], command_key="stale")
    )
    assert result["decision"] == "rejected"
    assert result["reason_codes"] == ["CAPACITY_WINDOW_CHANGED:short"]
    assert result["observations"]["short"]["window_id"] == "window-2"
    assert len(store.snapshot()["reservations"]) == (1 if admitted else 0)
    assert not store.snapshot()["lifecycle"]
    if admitted is None:
        rebuilt = bound_request(store, "fresh")
        assert store.admit(rebuilt, command_key="fresh")["decision"] == "admitted"


@pytest.mark.parametrize("phase", ["admit", "activate"])
@pytest.mark.parametrize("remaining, decision", [("10", "accepted"), ("2", "rejected")])
def test_same_window_uses_latest_balance_without_requiring_identical_observation(
    ledger, phase, remaining, decision
):
    store, clock = ledger
    value = bound_request(store)
    admitted = store.admit(value, command_key="reserve") if phase == "activate" else None
    clock[0] = 1001.0
    observation(store, "short", remaining, at=clock[0])
    result = (
        store.admit(value, command_key="latest")
        if phase == "admit"
        else store.activate(admitted["admission_id"], command_key="latest")
    )
    expected = "admitted" if phase == "admit" else "capacity_revalidated"
    assert result["decision"] == (expected if decision == "accepted" else "rejected")
    assert result["available_before"]["short"] == f"{remaining}.000000"
    assert result["reason_codes"] == (
        [] if decision == "accepted" else ["QUOTA_INSUFFICIENT:short"]
    )


@pytest.mark.parametrize("missing", ["short", "weekly", "extra"])
def test_expected_windows_must_cover_exact_registered_profile_pool_vector(ledger, missing):
    store, _ = ledger
    value = bound_request(store)
    if missing == "extra":
        value["expected_capacity"]["pool_windows"]["unregistered"] = "window-1"
    else:
        del value["expected_capacity"]["pool_windows"][missing]
    result = store.admit(value, command_key="incomplete")
    assert result["decision"] == "rejected"
    assert result["reason_codes"] == ["CAPACITY_WINDOW_VECTOR_MISMATCH"]
    assert not store.snapshot()["reservations"]


@pytest.mark.parametrize(
    "role,purpose,access,accepted",
    [
        ("commander", "lead", False, False),
        ("commander", "lead", True, True),
        ("commander", "advice", True, False),
        ("worker", None, True, False),
    ],
)
def test_reserve_access_can_only_narrow_existing_lead_role(ledger, role, purpose, access, accepted):
    store, clock = ledger
    clock[0] = 1001.0
    observation(store, "weekly", "10", at=clock[0])
    value = bound_request(store, access=access, role=role, purpose=purpose)
    result = store.admit(value, command_key="reserve-access")
    assert result["decision"] == ("admitted" if accepted else "rejected")
    assert result["available_before"]["weekly"] == ("10.000000" if accepted else "2.000000")


def test_reserve_access_restriction_is_retained_at_activation(ledger):
    store, clock = ledger
    value = bound_request(store, role="commander", purpose="lead", access=False)
    admitted = store.admit(value, command_key="reserve")
    assert admitted["decision"] == "admitted"
    clock[0] = 1001.0
    observation(store, "weekly", "10", at=clock[0])
    result = store.activate(admitted["admission_id"], command_key="start")
    assert result["decision"] == "rejected"
    assert result["reason_codes"] == ["QUOTA_INSUFFICIENT:weekly"]
    assert (
        store.snapshot()["reservations"][0]["request"]["expected_capacity"]
        == value["expected_capacity"]
    )


def test_restricted_lead_cannot_use_reserved_concurrency_slot(ledger):
    store, _ = ledger
    policy = store.snapshot()["policies"][-1]["policy"]
    policy.update(max_active_attempts=1, lead_reserved_slots=1)
    store.activate_policy(policy, expected_revision=1, command_key="only-lead-slot")
    restricted = bound_request(store, "restricted", role="commander", purpose="lead")
    denied = store.admit(restricted, command_key="restricted")
    assert denied["reason_codes"] == ["ACCOUNT_CONCURRENCY_EXHAUSTED"]
    lead = bound_request(store, "lead", role="commander", purpose="lead", access=True)
    assert store.admit(lead, command_key="lead")["decision"] == "admitted"


@pytest.mark.parametrize(
    "binding",
    [
        {},
        {"policy_revision": 1},
        {"policy_revision": True, "pool_windows": {"short": "w"}, "lead_reserve_access": False},
        {"policy_revision": 1, "pool_windows": {}, "lead_reserve_access": False},
        {"policy_revision": 1, "pool_windows": {"short": "w"}, "lead_reserve_access": 1},
        {"policy_revision": 1, "pool_windows": {"short": "w"}, "lead_reserve_access": "false"},
    ],
)
def test_partial_or_non_strict_binding_cannot_be_admitted(ledger, binding):
    store, _ = ledger
    value = {**request(), "expected_capacity": binding}
    with pytest.raises(CapacityError, match="^CAPACITY_INPUT_INVALID$"):
        store.admit(value, command_key="invalid")
    assert not store.snapshot()["reservations"]


def test_legacy_payload_digest_and_replay_are_unchanged(ledger):
    store, _ = ledger
    value = request()
    admitted = store.admit(value, command_key="legacy")
    assert admitted["request"] == value
    canonical = json.dumps(["admit", value], sort_keys=True, separators=(",", ":"), allow_nan=False)
    with sqlite3.connect(store.path) as db:
        stored_digest = db.execute("SELECT digest FROM commands WHERE key='legacy'").fetchone()[0]
    assert stored_digest == hashlib.sha256(canonical.encode()).hexdigest()
    reopened = CapacityStore(store.path, clock=lambda: 1000.0)
    assert reopened.admit({**value, "expected_capacity": None}, command_key="legacy") == admitted
    assert reopened.activate(admitted["admission_id"], command_key="start")["decision"] == (
        "capacity_revalidated"
    )


def test_bound_snapshot_cannot_double_spend_last_slot_across_runs(ledger):
    store, _ = ledger
    policy = store.snapshot()["policies"][-1]["policy"]
    policy.update(max_active_attempts=1, lead_reserved_slots=0)
    store.activate_policy(policy, expected_revision=1, command_key="one-slot")
    values = [bound_request(store, "contender-" + str(index)) for index in range(2)]
    barrier = Barrier(2)

    def contend(value):
        connection = CapacityStore(store.path, clock=lambda: 1000.0)
        barrier.wait(timeout=5)
        return connection.admit(value, command_key=value["attempt_id"])

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(contend, values))
    assert sorted(result["decision"] for result in results) == ["admitted", "rejected"]
    assert len(store.snapshot()["reservations"]) == 1


def test_replaying_bound_reservation_does_not_upgrade_or_duplicate_it(ledger):
    store, _ = ledger
    value = bound_request(store, role="commander", purpose="lead", access=False)
    admitted = store.admit(value, command_key="bound")
    assert store.admit(copy.deepcopy(value), command_key="bound") == admitted
    expanded = copy.deepcopy(value)
    expanded["expected_capacity"]["lead_reserve_access"] = True
    with pytest.raises(CapacityError, match="^IDEMPOTENCY_CONFLICT$"):
        store.admit(expanded, command_key="bound")
    assert len(store.snapshot()["reservations"]) == 1
