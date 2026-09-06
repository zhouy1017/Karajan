"""Independent transactional/compatibility checks against fixed capacity source."""

from copy import deepcopy
from pathlib import Path

import pytest
from karajan.capacity import CapacityError, CapacityStore
from legacy_capacity import CapacityStore as LegacyStore


def prepare(path: Path, cls=CapacityStore, *, unknown: bool = False):
    service = cls(path, clock=lambda: 1000.0)
    service.register_pool(
        {
            "id": "p",
            "account_id": "a",
            "kind": "service",
            "unit": "requests",
            "window_kind": "fixed",
        },
        command_key="pool",
    )
    service.register_profile(
        {"id": "profile", "revision": 1, "account_id": "a", "pool_ids": ["p"]},
        command_key="profile",
    )
    service.observe(
        {
            "pool_id": "p",
            "window_id": "w1",
            "observed_at": 1000.0,
            "reset_at": 2000.0,
            "source": "fixture",
            "source_ref": "fixture:only",
            "metric": "unknown" if unknown else "remaining",
            "amount": None if unknown else "10",
            "limit": "10",
            "covered_usage_ids": [],
        },
        command_key="observation",
    )
    policy = {
        "account_id": "a",
        "max_active_attempts": 1,
        "max_attempt_duration_seconds": 100,
        "observation_max_age_seconds": 60,
        "require_official_observation": False,
        "safety_margin": {},
        "lead_reserve": {"p": "8"},
        "lead_reserved_slots": 1,
        "conservative_mode": {
            "enabled": True,
            "max_local_active_attempts": 1,
            "max_attempt_duration_seconds": 100,
            "observation_max_age_seconds": 60,
            "cooldown_seconds": 10,
        }
        if unknown
        else None,
    }
    service.activate_policy(policy, expected_revision=0, command_key="policy")
    return service, policy


def payload(*, access=None, role="commander", purpose="lead") -> dict:
    value = {
        "attempt_id": "attempt",
        "run_id": "run",
        "profile_id": "profile",
        "profile_revision": 1,
        "role": role,
        "purpose": purpose,
        "authorization_ref": "fixture:approved",
        "rulebook_revision": "r1",
        "duration_seconds": 50,
        "demand": {"p": "3"},
    }
    if access is not None:
        value["expected_capacity"] = {
            "policy_revision": 1,
            "pool_windows": {"p": "w1"},
            "lead_reserve_access": access,
        }
    return value


@pytest.mark.parametrize("activate_before_upgrade", [True, False])
def test_actual_old_database_replays_under_new_implementation(tmp_path, activate_before_upgrade):
    legacy, _ = prepare(tmp_path / "old.sqlite", LegacyStore)
    request = payload()
    admitted = legacy.admit(request, command_key="admit-before-upgrade")
    assert admitted["decision"] == "admitted"
    if activate_before_upgrade:
        activated = legacy.activate(admitted["admission_id"], command_key="activate-before-upgrade")
    current = CapacityStore(legacy.path, clock=lambda: 1000.0)
    assert current.admit(request, command_key="admit-before-upgrade") == admitted
    assert (
        current.admit({**request, "expected_capacity": None}, command_key="admit-before-upgrade")
        == admitted
    )
    if activate_before_upgrade:
        assert (
            current.activate(admitted["admission_id"], command_key="activate-before-upgrade")
            == activated
        )
    else:
        assert (
            current.activate(admitted["admission_id"], command_key="activate-after-upgrade")[
                "decision"
            ]
            == "capacity_revalidated"
        )


@pytest.mark.parametrize(
    "role,purpose", [("worker", None), ("reviewer", None), ("commander", "advice"), ("check", None)]
)
@pytest.mark.parametrize("unknown", [True, False])
def test_true_flag_cannot_give_nonlead_reserved_slot(tmp_path, role, purpose, unknown):
    service, _ = prepare(tmp_path / "current.sqlite", unknown=unknown)
    result = service.admit(payload(access=True, role=role, purpose=purpose), command_key="admit")
    assert result["decision"] == "rejected"
    assert "ACCOUNT_CONCURRENCY_EXHAUSTED" in result["reason_codes"]
    assert service.snapshot()["reservations"] == []


def test_false_lead_access_remains_effective_for_unknown_conservative_pool(tmp_path):
    service, _ = prepare(tmp_path / "current.sqlite", unknown=True)
    result = service.admit(payload(access=False), command_key="restricted")
    assert "CONSERVATIVE_CONCURRENCY_EXHAUSTED:p" in result["reason_codes"]
    assert result["decision"] == "rejected"


def test_replaying_admit_receipt_does_not_bypass_new_policy_at_activation(tmp_path):
    service, policy = prepare(tmp_path / "current.sqlite")
    original = payload(access=True)
    admitted = service.admit(original, command_key="reserve")
    policy["max_active_attempts"] = 2
    service.activate_policy(policy, expected_revision=1, command_key="new-policy")
    assert service.admit(original, command_key="reserve") == admitted
    activated = service.activate(admitted["admission_id"], command_key="activate")
    assert activated["reason_codes"] == ["CAPACITY_POLICY_REVISION_CHANGED"]
    assert service.snapshot()["reservations"][0]["state"] == "reserved"
    assert service.snapshot()["lifecycle"] == []


@pytest.mark.parametrize("change", ["policy", "windows", "access"])
def test_same_command_cannot_change_stored_capacity_binding(tmp_path, change):
    service, _ = prepare(tmp_path / "current.sqlite")
    request = payload(access=True)
    admitted = service.admit(request, command_key="reserve")
    modified = deepcopy(request)
    if change == "policy":
        modified["expected_capacity"]["policy_revision"] = 2
    elif change == "windows":
        modified["expected_capacity"]["pool_windows"]["p"] = "w2"
    else:
        modified["expected_capacity"]["lead_reserve_access"] = False
    with pytest.raises(CapacityError, match="IDEMPOTENCY_CONFLICT"):
        service.admit(modified, command_key="reserve")
    assert service.snapshot()["reservations"][0]["request"] == admitted["request"]
