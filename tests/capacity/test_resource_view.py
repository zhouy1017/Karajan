"""Resource read models expose conservative values without making reservations."""

from pathlib import Path

from karajan.capacity import CapacityStore
from test_shared_capacity import consume, end, pool, refresh, request, setup


def test_reading_resource_view_keeps_reported_consumption_future_and_protection_separate(
    tmp_path: Path,
) -> None:
    store = setup(tmp_path)
    first = store.admit(request("first"), command_key="first")
    store.activate(first["admission_id"], command_key="start")
    consume(store, first["admission_id"], "call")
    before = store.snapshot()
    view = store.resource_view()
    assert store.snapshot() == before
    account = view["accounts"][0]
    assert account["id"] == "shared-account"
    assert account["active_attempts"] == 1
    weekly = next(p for p in account["pools"] if p["id"] == "weekly")
    assert weekly["reported_remaining"] == "5.000000"
    assert weekly["local_uncovered"] == "1.000000"
    assert weekly["future_reserved"] == "1.000000"
    assert weekly["lead_reserve"] == "2"
    assert weekly["available_for_worker"] == "1.000000"
    assert weekly["available_for_lead"] == "3.000000"
    assert weekly["coverage_status"] == "uncertain"
    assert view["activation_allowed"] is False


def test_stale_or_unknown_observations_never_display_exact_available_values(tmp_path: Path) -> None:
    store = setup(tmp_path)
    first = store.admit(request("first"), command_key="first")
    store.activate(first["admission_id"], command_key="start")
    store.clock = lambda: 1031.0
    refresh(store, "weekly", None, metric="unknown", at=1031.0)
    account = store.resource_view()["accounts"][0]
    pools = {p["id"]: p for p in account["pools"]}
    assert pools["short"]["status"] == "stale"
    assert pools["weekly"]["status"] == "unknown"
    assert pools["short"]["available_for_worker"] is None
    assert pools["weekly"]["available_for_lead"] is None
    assert account["active_attempts"] == 1
    assert pools["weekly"]["future_reserved"] == "2.000000"


def test_view_identifies_local_allowance_and_explicit_coverage_after_reconciliation(
    tmp_path: Path,
) -> None:
    store = setup(tmp_path)
    first = store.admit(request("first"), command_key="first")
    store.activate(first["admission_id"], command_key="start")
    consume(store, first["admission_id"], "call")
    end(store, first["admission_id"])
    store.clock = lambda: 1001.0
    refresh(store, "weekly", "4", covered=["call"])
    account = store.resource_view()["accounts"][0]
    pools = {p["id"]: p for p in account["pools"]}
    assert account["active_attempts"] == 0
    assert pools["allowance"]["kind"] == "platform_allowance"
    assert pools["weekly"]["coverage_status"] == "explicit_coverage"
    assert pools["weekly"]["covered_usage_count"] == 1
    assert pools["weekly"]["available_for_worker"] == "2.000000"


def test_exhaustion_and_cooldown_are_visible_without_displaying_old_headroom(
    tmp_path: Path,
) -> None:
    store = setup(tmp_path)
    store.record_failure(
        "shared-account",
        reason="QUOTA_EXHAUSTED",
        retry_after_seconds=20,
        evidence_ref="fixture-error",
        command_key="exhausted",
    )
    account = store.resource_view()["accounts"][0]
    assert account["blockers"] == [
        {"reason_code": "ACCOUNT_COOLDOWN", "until": 1020.0},
        {"reason_code": "EXHAUSTION_REQUIRES_NEW_OBSERVATION", "until": None},
    ]
    weekly = next(p for p in account["pools"] if p["id"] == "weekly")
    assert weekly["reported_remaining"] == "5.000000"
    assert weekly["available_for_worker"] is None
    assert weekly["status"] == "unknown"


def test_exhaustion_is_visible_before_first_report_and_after_cooldown(tmp_path: Path) -> None:
    source = setup(tmp_path)
    policy = source.snapshot()["policies"][-1]["policy"]
    store = CapacityStore(tmp_path / "unobserved.sqlite", clock=lambda: 1000.0)
    store.register_pool(pool("weekly"), command_key="pool")
    store.activate_policy(policy, expected_revision=0, command_key="policy")
    store.record_failure(
        "shared-account",
        reason="QUOTA_EXHAUSTED",
        retry_after_seconds=20,
        evidence_ref="fixture-known-exhaustion",
        command_key="failure",
    )
    assert len(store.resource_view()["accounts"][0]["blockers"]) == 2
    store.clock = lambda: 1021.0
    before = store.snapshot()
    assert store.resource_view()["accounts"][0]["blockers"] == [
        {"reason_code": "EXHAUSTION_REQUIRES_NEW_OBSERVATION", "until": None},
    ]
    assert store.snapshot() == before
    refresh(store, "weekly", "5", at=1021.0)
    assert store.resource_view()["accounts"][0]["blockers"] == []
