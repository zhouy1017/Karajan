"""Observe real shared SQLite admission across profiles, Runs and quota windows."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from karajan.capacity import CapacityError, CapacityStore


def pool(identity: str, *, kind: str = "service") -> dict:
    return {
        "id": identity,
        "account_id": "shared-account",
        "kind": kind,
        "unit": "requests",
        "window_kind": "fixed",
    }


def observe(
    store: CapacityStore, identity: str, remaining: str, *, window: str = "window-1"
) -> None:
    store.observe(
        {
            "pool_id": identity,
            "window_id": window,
            "observed_at": 1000.0,
            "reset_at": 2000.0 if identity == "short" else 10000.0,
            "source": "fixture",
            "source_ref": "local-fixture-observer",
            "metric": "remaining",
            "amount": remaining,
            "limit": "100",
            "covered_usage_ids": [],
        },
        command_key="observe-" + identity + "-" + window,
    )


def setup(tmp_path: Path) -> CapacityStore:
    store = CapacityStore(tmp_path / "capacity.sqlite", clock=lambda: 1000.0)
    for identity in ["short", "weekly", "allowance"]:
        store.register_pool(
            pool(identity, kind="platform_allowance" if identity == "allowance" else "service"),
            command_key="pool-" + identity,
        )
        observe(store, identity, "20" if identity != "weekly" else "5")
    for profile in ["fast-a", "fast-b"]:
        store.register_profile(
            {
                "id": profile,
                "revision": 1,
                "account_id": "shared-account",
                "pool_ids": ["short", "weekly", "allowance"],
            },
            command_key="profile-" + profile,
        )
    store.activate_policy(
        {
            "account_id": "shared-account",
            "max_active_attempts": 4,
            "max_attempt_duration_seconds": 60,
            "observation_max_age_seconds": 30,
            "require_official_observation": False,
            "safety_margin": {},
            "lead_reserve": {"weekly": "2"},
            "lead_reserved_slots": 0,
            "conservative_mode": None,
        },
        expected_revision=0,
        command_key="policy-1",
    )
    return store


def request(
    identity: str, *, profile: str = "fast-a", run: str = "run-a", lead: bool = False
) -> dict:
    return {
        "attempt_id": identity,
        "run_id": run,
        "profile_id": profile,
        "profile_revision": 1,
        "role": "commander" if lead else "worker",
        "purpose": "lead" if lead else None,
        "authorization_ref": "fixture-approved-scope",
        "rulebook_revision": "rules-" + run,
        "duration_seconds": 30,
        "demand": {"short": "2", "weekly": "2", "allowance": "2"},
    }


def test_two_profiles_and_runs_cannot_each_spend_the_same_weekly_headroom(tmp_path: Path) -> None:
    store = setup(tmp_path)
    barrier = Barrier(2)

    def contend(index: int) -> dict:
        reopened = CapacityStore(tmp_path / "capacity.sqlite", clock=lambda: 1000.0)
        barrier.wait(timeout=5)
        return reopened.admit(
            request(
                "attempt-" + str(index),
                profile="fast-a" if index == 0 else "fast-b",
                run="run-" + str(index),
            ),
            command_key="admit-" + str(index),
        )

    with ThreadPoolExecutor(max_workers=2) as workers:
        decisions = list(workers.map(contend, [0, 1]))
    assert sorted(item["decision"] for item in decisions) == ["admitted", "rejected"]
    assert len(store.snapshot()["reservations"]) == 1
    assert all(item["policy_revision"] == 1 for item in decisions)
    denied = next(item for item in decisions if item["decision"] == "rejected")
    assert denied["reason_codes"] == ["QUOTA_INSUFFICIENT:weekly"]
    leader = store.admit(request("lead", lead=True), command_key="lead-admit")
    assert leader["decision"] == "admitted"
    assert len(store.snapshot()["reservations"]) == 2


def test_failed_multi_window_admission_does_not_reserve_the_other_pools(tmp_path: Path) -> None:
    store = setup(tmp_path)
    oversized = request("oversized")
    oversized["demand"]["weekly"] = "4"
    rejected = store.admit(oversized, command_key="oversized")
    assert rejected["decision"] == "rejected"
    assert store.snapshot()["reservations"] == []
    assert store.admit(request("independent"), command_key="independent")["decision"] == "admitted"


def test_no_omission_of_a_required_pool_or_lead_privilege_for_an_adviser(tmp_path: Path) -> None:
    store = setup(tmp_path)
    worker = store.admit(request("worker"), command_key="worker")
    assert worker["decision"] == "admitted"
    advice = request("advice", lead=True)
    advice["purpose"] = "advice"
    assert store.admit(advice, command_key="advice")["decision"] == "rejected"
    omitted = request("omitted")
    del omitted["demand"]["weekly"]
    assert store.admit(omitted, command_key="omitted")["reason_codes"] == ["POOL_VECTOR_MISMATCH"]


def test_activation_rechecks_current_policy_without_releasing_old_inflight_hold(
    tmp_path: Path,
) -> None:
    store = setup(tmp_path)
    first = store.admit(request("first"), command_key="first")
    policy = store.snapshot()["policies"][-1]["policy"]
    policy["lead_reserve"]["weekly"] = "4"
    store.activate_policy(policy, expected_revision=1, command_key="policy-2")
    activated = store.activate(first["admission_id"], command_key="activate-first")
    assert activated["decision"] == "rejected"
    assert activated["policy_revision"] == 2
    assert activated["reason_codes"] == ["QUOTA_INSUFFICIENT:weekly"]
    assert store.snapshot()["reservations"][0]["state"] == "reserved"
    second = store.admit(request("second", run="new-run"), command_key="second")
    assert second["policy_revision"] == 2
    assert second["decision"] == "rejected"
    assert len(store.snapshot()["reservations"]) == 1


def test_unknown_activation_survives_restart_and_deadline_until_both_ends_known(
    tmp_path: Path,
) -> None:
    store = setup(tmp_path)
    admission = store.admit(request("first"), command_key="first")
    identity = admission["admission_id"]
    activated = store.activate(identity, command_key="start-first")
    assert activated["decision"] == "capacity_revalidated"
    assert activated["activation_allowed"] is False
    assert store.activate(identity, command_key="start-first") == activated
    with pytest.raises(CapacityError, match="ACTIVATION_ALREADY_RECORDED"):
        store.activate(identity, command_key="different-start")
    reopened = CapacityStore(store.path, clock=lambda: 1031.0)
    reopened.reconcile(
        identity,
        local_ended=True,
        remote_ended=False,
        usage_complete=False,
        not_sent=False,
        evidence_ref="local-process-exited",
        command_key="local-end",
    )
    assert reopened.snapshot()["reservations"][0]["state"] == "unknown"
    reopened.admit(request("blocked"), command_key="blocked")
    assert reopened.snapshot()["reservations"][0]["state"] == "unknown"


def test_unsent_expiration_can_release_capacity_but_cannot_reactivate_same_attempt(
    tmp_path: Path,
) -> None:
    store = setup(tmp_path)
    admission = store.admit(request("unsent"), command_key="unsent")
    reopened = CapacityStore(store.path, clock=lambda: 1031.0)
    result = reopened.activate(admission["admission_id"], command_key="expired-start")
    assert result["reason_codes"] == ["RESERVATION_EXPIRED"]
    assert reopened.snapshot()["reservations"][0]["state"] == "expired"
    with pytest.raises(CapacityError, match="ATTEMPT_ALREADY_RESERVED"):
        reopened.admit(request("unsent"), command_key="new-key")


def refresh(
    store: CapacityStore,
    identity: str,
    remaining: str | None,
    *,
    at: float = 1001.0,
    covered: list[str] | None = None,
    window: str = "window-1",
    reset: float | None = None,
    **changes: object,
) -> dict:
    observation = {
        "pool_id": identity,
        "window_id": window,
        "observed_at": at,
        "reset_at": reset if reset is not None else (2000.0 if identity == "short" else 10000.0),
        "source": "fixture",
        "source_ref": "report-" + str(at),
        "metric": "remaining",
        "amount": remaining,
        "limit": "100",
        "covered_usage_ids": covered or [],
        "coverage_ref": "verified-request-ids" if covered else None,
    }
    observation.update(changes)
    return store.observe(observation, command_key="report-" + identity + "-" + str(at))


def consume(
    store: CapacityStore,
    admission_id: str,
    usage_id: str,
    quantity: str = "1",
    window: str | None = "window-1",
) -> dict:
    return store.record_usage(
        {
            "id": usage_id,
            "admission_id": admission_id,
            "amounts": {p: quantity for p in ("short", "weekly", "allowance")},
            "window_ids": {p: window for p in ("short", "weekly", "allowance")},
            "evidence_ref": "fixture-call-receipt",
            "attribution_ref": "fixture-window-attribution" if window else None,
        },
        command_key="usage-" + usage_id,
    )


def end(store: CapacityStore, admission_id: str, key: str = "end") -> dict:
    return store.reconcile(
        admission_id,
        local_ended=True,
        remote_ended=True,
        usage_complete=True,
        not_sent=False,
        evidence_ref="both-ends-and-final-usage",
        command_key=key,
    )


def test_usage_and_future_slice_not_double_counted_and_only_explicit_coverage_removes_overlap(
    tmp_path: Path,
) -> None:
    store = setup(tmp_path)
    first = store.admit(request("first"), command_key="first")
    store.activate(first["admission_id"], command_key="start")
    usage = consume(store, first["admission_id"], "call-1")
    assert consume(store, first["admission_id"], "call-1") == usage
    # Known actual 1 + future slice 1 = 2; never parent 2 + actual 1.
    leader = store.admit(request("leader", lead=True), command_key="leader")
    assert leader["available_before"]["weekly"] == "3.000000"
    end(store, first["admission_id"])
    store.clock = lambda: 1001.0
    refresh(store, "weekly", "4")  # Report time alone cannot cover our receipt.
    blocked = store.admit(request("blocked"), command_key="blocked")
    assert blocked["available_before"]["weekly"] == "-1.000000"  # 4 - 1 - 2 - reserve 2
    store.clock = lambda: 1002.0
    refresh(store, "weekly", "4", at=1002.0, covered=["call-1"])
    covered = store.admit(request("covered"), command_key="covered")
    assert covered["available_before"]["weekly"] == "0.000000"
    assert len(store.snapshot()["usage"]) == 1


def test_actual_over_estimate_is_recorded_without_clamping_and_no_false_unsent_release(
    tmp_path: Path,
) -> None:
    store = setup(tmp_path)
    first = store.admit(request("first"), command_key="first")
    store.activate(first["admission_id"], command_key="start")
    result = consume(store, first["admission_id"], "overrun", "6")
    assert result["over_estimate_pools"] == ["allowance", "short", "weekly"]
    with pytest.raises(CapacityError, match="UNSENT_CONFLICTS_WITH_USAGE"):
        store.reconcile(
            first["admission_id"],
            local_ended=True,
            remote_ended=True,
            usage_complete=True,
            not_sent=True,
            evidence_ref="false-proof",
            command_key="false-release",
        )
    end(store, first["admission_id"])
    result = store.admit(request("next", lead=True), command_key="next")
    assert result["decision"] == "rejected"
    assert result["available_before"]["weekly"] == "-1.000000"
    assert store.snapshot()["usage"][0]["receipt"]["amounts"]["weekly"] == "6"


def test_finishing_without_final_usage_does_not_release_unknown_consumption(tmp_path: Path) -> None:
    store = setup(tmp_path)
    first = store.admit(request("first"), command_key="first")
    store.activate(first["admission_id"], command_key="start")
    with pytest.raises(CapacityError, match="FINAL_USAGE_REQUIRED"):
        end(store, first["admission_id"])
    assert store.snapshot()["reservations"][0]["state"] == "active"
    consume(store, first["admission_id"], "explicit-zero", "0")
    assert end(store, first["admission_id"])["state"] == "ended"


def test_service_external_usage_does_not_reduce_platform_allowance(tmp_path: Path) -> None:
    store = setup(tmp_path)
    store.clock = lambda: 1001.0
    refresh(store, "short", "50", metric="used")
    result = store.admit(request("first"), command_key="first")
    assert result["available_before"]["short"] == "50.000000"
    assert result["available_before"]["allowance"] == "20.000000"


@pytest.mark.parametrize("known_window,expected", [(True, "5.000000"), (False, "3.000000")])
def test_fixed_reset_only_clears_usage_with_explicit_window_attribution(
    tmp_path: Path,
    known_window: bool,
    expected: str,
) -> None:
    store = setup(tmp_path)
    first = store.admit(request("first"), command_key="first")
    store.activate(first["admission_id"], command_key="start")
    consume(store, first["admission_id"], "call", "2", "window-1" if known_window else None)
    end(store, first["admission_id"])
    store.clock = lambda: 2001.0
    # Only short resets. Weekly and allowance reports remain in the same window.
    refresh(store, "short", "5", at=2001.0, window="window-2", reset=3000.0)
    refresh(store, "weekly", "5", at=2001.0)
    refresh(store, "allowance", "20", at=2001.0)
    result = store.admit(request("next", lead=True), command_key="next")
    assert result["available_before"]["short"] == expected
    assert result["available_before"]["weekly"] == "3.000000"


def unknown_policy(
    store: CapacityStore, *, missing: str | None = None, official: bool = False
) -> dict:
    policy = store.snapshot()["policies"][-1]["policy"]
    policy["require_official_observation"] = official
    policy["conservative_mode"] = {
        "enabled": True,
        "max_local_active_attempts": 1,
        "max_attempt_duration_seconds": 30,
        "observation_max_age_seconds": 10,
        "cooldown_seconds": 20,
    }
    if missing:
        policy["conservative_mode"][missing] = None
    store.activate_policy(policy, expected_revision=1, command_key="policy-unknown")
    store.clock = lambda: 1001.0
    refresh(store, "weekly", None, metric="unknown", source="official" if official else "fixture")
    return policy


@pytest.mark.parametrize(
    "missing",
    [
        "max_local_active_attempts",
        "max_attempt_duration_seconds",
        "observation_max_age_seconds",
        "cooldown_seconds",
    ],
)
def test_unknown_mode_requires_each_explicit_finite_limit(tmp_path: Path, missing: str) -> None:
    store = setup(tmp_path)
    unknown_policy(store, missing=missing)
    result = store.admit(request("first"), command_key="first")
    assert result["decision"] == "rejected"
    assert "CONSERVATIVE_MODE_INCOMPLETE:weekly" in result["reason_codes"]
    assert store.snapshot()["reservations"] == []


def test_unknown_mode_applies_concurrency_duration_freshness_and_persistent_cooldown(
    tmp_path: Path,
) -> None:
    store = setup(tmp_path)
    unknown_policy(store)
    long = request("long")
    long["duration_seconds"] = 31
    assert (
        "CONSERVATIVE_DURATION_EXCEEDED:weekly"
        in store.admit(long, command_key="long")["reason_codes"]
    )
    first = store.admit(request("first"), command_key="first")
    assert first["decision"] == "admitted"
    assert first["available_before"]["weekly"] == "unknown"
    second = store.admit(request("second"), command_key="second")
    assert "CONSERVATIVE_CONCURRENCY_EXHAUSTED:weekly" in second["reason_codes"]
    store.reconcile(
        first["admission_id"],
        local_ended=True,
        remote_ended=True,
        not_sent=True,
        usage_complete=False,
        evidence_ref="cancelled-before-activation",
        command_key="cancel",
    )
    failed = store.record_failure(
        "shared-account",
        reason="RATE_LIMIT_TRANSIENT",
        retry_after_seconds=5,
        evidence_ref="fixture-429",
        command_key="failure",
    )
    assert failed["until"] == 1021.0  # Cannot shorten the approved cooldown of 20.
    reopened = CapacityStore(store.path, clock=lambda: 1002.0)
    assert "ACCOUNT_COOLDOWN" in reopened.admit(request("cool"), command_key="cool")["reason_codes"]
    reopened.clock = lambda: 1012.0
    stale = reopened.admit(request("stale"), command_key="stale")
    assert "CONSERVATIVE_OBSERVATION_STALE:weekly" in stale["reason_codes"]
    reopened.clock = lambda: 1022.0
    refresh(reopened, "weekly", None, at=1022.0, metric="unknown")
    assert reopened.admit(request("ready"), command_key="ready")["decision"] == "admitted"


def test_official_numeric_requirement_cannot_fall_back_to_unknown_estimation(
    tmp_path: Path,
) -> None:
    store = setup(tmp_path)
    unknown_policy(store, official=True)
    result = store.admit(request("first"), command_key="first")
    assert "OFFICIAL_QUOTA_REQUIRED:weekly" in result["reason_codes"]
    assert result["decision"] == "rejected"


def test_known_exhaustion_is_not_reclassified_as_unknown(tmp_path: Path) -> None:
    store = setup(tmp_path)
    unknown_policy(store)
    store.clock = lambda: 1002.0
    refresh(store, "weekly", "0", at=1002.0)
    result = store.admit(request("first"), command_key="first")
    assert result["reason_codes"] == ["QUOTA_INSUFFICIENT:weekly"]


def test_duplicate_late_and_premature_window_reports_are_audited_without_replacing_current(
    tmp_path: Path,
) -> None:
    store = setup(tmp_path)
    store.clock = lambda: 1003.0
    fresh = refresh(store, "weekly", "1", at=1002.0)
    duplicate = store.observe(fresh["observation"], command_key="duplicate-source-delivery")
    late = refresh(store, "weekly", "100", at=1001.0)
    premature = refresh(store, "weekly", "100", at=1003.0, window="window-future", reset=20000.0)
    assert [item["applied"] for item in (fresh, duplicate, late, premature)] == [
        True,
        False,
        False,
        False,
    ]
    assert all(item["current"]["amount"] == "1" for item in (duplicate, late, premature))
    assert len(store.snapshot()["observations"]) == 7
    assert store.admit(request("blocked"), command_key="blocked")["decision"] == "rejected"


def test_fixed_window_reset_identity_cannot_be_rewritten(tmp_path: Path) -> None:
    store = setup(tmp_path)
    store.clock = lambda: 1001.0
    with pytest.raises(CapacityError, match="WINDOW_IDENTITY_CONFLICT"):
        refresh(store, "short", "100", reset=9000.0)
    assert len(store.snapshot()["observations"]) == 3


def test_manual_calibration_keeps_reason_before_after_and_all_usage(tmp_path: Path) -> None:
    store = setup(tmp_path)
    first = store.admit(request("first"), command_key="first")
    store.activate(first["admission_id"], command_key="start")
    consume(store, first["admission_id"], "call")
    end(store, first["admission_id"])
    store.clock = lambda: 1001.0
    with pytest.raises(CapacityError, match="ADJUSTMENT_REASON_REQUIRED"):
        refresh(store, "weekly", "4", source="manual")
    adjusted = refresh(
        store,
        "weekly",
        "4",
        source="manual",
        covered=["call"],
        adjustment_reason="已对照本机凭据与服务显示核对这次调用",
    )
    assert adjusted["previous"]["amount"] == "5"
    assert adjusted["current"]["amount"] == "4"
    assert len(store.snapshot()["usage"]) == 1
    assert (
        store.admit(request("next"), command_key="next")["available_before"]["weekly"] == "2.000000"
    )


@pytest.mark.parametrize("bad", [True, -1, 0, float("nan"), "30", 1000001])
def test_malformed_finite_bounds_are_rejected_without_writes(tmp_path: Path, bad: object) -> None:
    store = setup(tmp_path)
    value = request("invalid")
    value["duration_seconds"] = bad
    with pytest.raises(CapacityError, match="CAPACITY_INPUT_INVALID"):
        store.admit(value, command_key="bad")
    assert store.snapshot()["reservations"] == []


@pytest.mark.parametrize("identity", ["\ud800", "line\nfeed", "", " "])
def test_invalid_identifiers_raise_stable_errors_before_persistence(
    tmp_path: Path, identity: str
) -> None:
    store = setup(tmp_path)
    value = request(identity)
    with pytest.raises(CapacityError, match="CAPACITY_INPUT_INVALID"):
        store.admit(value, command_key="bad-id")
    assert store.snapshot()["reservations"] == []


def test_clock_overflow_is_a_stable_failure_without_admission(tmp_path: Path) -> None:
    store = setup(tmp_path)
    store.clock = lambda: 10**400
    with pytest.raises(CapacityError, match="CLOCK_UNAVAILABLE"):
        store.admit(request("first"), command_key="first")
    assert store.snapshot()["reservations"] == []


def test_running_hold_persists_across_policy_revisions_and_old_run_rules(tmp_path: Path) -> None:
    store = setup(tmp_path)
    first = store.admit(request("first"), command_key="first")
    store.activate(first["admission_id"], command_key="activate")
    consume(store, first["admission_id"], "call")
    policy = store.snapshot()["policies"][-1]["policy"]
    policy["lead_reserve"]["weekly"] = "4"
    store.activate_policy(policy, expected_revision=1, command_key="raise-protection")
    blocked = store.admit(request("old-run"), command_key="old-run")
    assert blocked["policy_revision"] == 2
    assert blocked["available_before"]["weekly"] == "-1.000000"
    assert store.snapshot()["reservations"][0]["state"] == "active"
    with pytest.raises(CapacityError, match="CAPACITY_POLICY_STALE"):
        store.activate_policy(policy, expected_revision=1, command_key="stale-write")
    policy["lead_reserve"]["weekly"] = "0"
    store.activate_policy(policy, expected_revision=2, command_key="lower-protection")
    new = store.admit(request("new-run", run="new-run"), command_key="new-run")
    assert new["decision"] == "admitted"
    assert new["available_before"]["weekly"] == "3.000000"
    assert new["policy_revision"] == 3
    assert len(store.snapshot()["usage"]) == 1


def test_external_consumption_between_admission_and_activation_blocks_the_effect(
    tmp_path: Path,
) -> None:
    store = setup(tmp_path)
    first = store.admit(request("first"), command_key="first")
    store.clock = lambda: 1001.0
    refresh(store, "weekly", "1")
    activated = store.activate(first["admission_id"], command_key="activate")
    assert activated["decision"] == "rejected"
    assert activated["reason_codes"] == ["QUOTA_INSUFFICIENT:weekly"]
    assert store.snapshot()["lifecycle"] == []


def test_duplicate_command_with_different_payload_cannot_reserve_twice(tmp_path: Path) -> None:
    store = setup(tmp_path)
    first = store.admit(request("first"), command_key="shared-command")
    reopened = CapacityStore(store.path, clock=lambda: 1000.0)
    assert reopened.admit(request("first"), command_key="shared-command") == first
    with pytest.raises(CapacityError, match="IDEMPOTENCY_CONFLICT"):
        reopened.admit(request("second"), command_key="shared-command")
    assert len(reopened.snapshot()["reservations"]) == 1


def test_fake_source_cannot_satisfy_official_policy_but_local_allowance_remains_local(
    tmp_path: Path,
) -> None:
    store = setup(tmp_path)
    policy = store.snapshot()["policies"][-1]["policy"]
    policy["require_official_observation"] = True
    store.activate_policy(policy, expected_revision=1, command_key="official-policy")
    result = store.admit(request("first"), command_key="first")
    assert result["reason_codes"] == [
        "OFFICIAL_OBSERVATION_REQUIRED:short",
        "OFFICIAL_OBSERVATION_REQUIRED:weekly",
    ]
    store.clock = lambda: 1001.0
    for identity in ("short", "weekly"):
        refresh(store, identity, "5", source="official")  # Contract test data only.
    assert store.admit(request("second"), command_key="second")["decision"] == "admitted"
    with pytest.raises(CapacityError, match="ALLOWANCE_REQUIRES_LOCAL_OBSERVATION"):
        refresh(store, "allowance", "20", source="official")


def test_numeric_zero_expired_short_window_does_not_erase_long_window_consumption(
    tmp_path: Path,
) -> None:
    store = setup(tmp_path)
    unknown_policy(store)
    store.clock = lambda: 1002.0
    refresh(store, "short", "0", at=1002.0)
    store.clock = lambda: 2001.0
    refresh(store, "short", None, metric="unknown", at=2001.0, window="window-2", reset=3000.0)
    refresh(store, "weekly", "2", at=2001.0)
    refresh(store, "allowance", "20", at=2001.0)
    result = store.admit(request("next"), command_key="next")
    assert result["reason_codes"] == ["QUOTA_INSUFFICIENT:weekly"]
    assert result["available_before"]["short"] == "unknown"
