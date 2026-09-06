"""Read trustworthy capacity fragments through the public ledger interface."""

import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from pathlib import Path
from threading import Event

import pytest
from karajan.capacity import CapacityError, CapacityStore
from test_shared_capacity import consume, end, pool, refresh, request, setup


def rows(path: Path) -> dict[str, list[tuple]]:
    with sqlite3.connect(path) as db:
        tables = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        return {
            table: db.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
            for (table,) in tables
        }


def test_fixed_capture_is_immutable_repeatable_and_leaves_every_table_unchanged(
    tmp_path: Path,
) -> None:
    store = setup(tmp_path)
    before = rows(store.path)
    first = store.routing_facts()
    second = store.routing_facts()
    assert first.canonical_json == second.canonical_json
    assert first.sha256 == hashlib.sha256(first.canonical_json.encode()).hexdigest()
    assert first.as_dict()["scope"] == "capacity_fragment"
    assert first.as_dict()["activation_allowed"] is False
    decoded = first.as_dict()
    decoded["accounts"][0]["policy"]["lead_reserve"]["weekly"] = "999"
    assert first.as_dict()["accounts"][0]["policy"]["lead_reserve"]["weekly"] == "2"
    with pytest.raises(FrozenInstanceError):
        first.canonical_json = "{}"
    assert rows(store.path) == before


def generous(store: CapacityStore) -> None:
    store.clock = lambda: 1001.0
    for identity in ("short", "weekly", "allowance"):
        refresh(store, identity, "80")


def account(store: CapacityStore) -> dict:
    return store.routing_facts().as_dict()["accounts"][0]


def pools(store: CapacityStore) -> dict:
    return {p["id"]: p for p in account(store)["pools"]}


def active(store: CapacityStore, identity: str, demand: str = "2", **kwargs: str) -> str:
    value = request(identity, **kwargs)
    value["demand"] = dict.fromkeys(value["demand"], demand)
    admission = store.admit(value, command_key="admit-" + identity)
    assert admission["decision"] == "admitted"
    started = store.activate(admission["admission_id"], command_key="activate-" + identity)
    assert started["decision"] == "capacity_revalidated"
    return admission["admission_id"]


def test_raw_remaining_uncovered_and_future_are_separate_and_explicit_coverage_is_not_double_used(
    tmp_path: Path,
) -> None:
    store = setup(tmp_path)
    generous(store)
    identity = active(store, "first", "8")
    consume(store, identity, "first-call", "3")
    before = rows(store.path)
    weekly = pools(store)["weekly"]
    assert (weekly["reported_remaining"], weekly["local_uncovered"], weekly["future_reserved"]) == (
        "80.000000",
        "3.000000",
        "5.000000",
    )
    assert weekly["observation"]["observation"]["source_ref"] == "report-1001.0"
    assert rows(store.path) == before
    store.clock = lambda: 1002.0
    refresh(store, "weekly", "80", at=1002.0)
    assert pools(store)["weekly"]["local_uncovered"] == "3.000000"
    store.clock = lambda: 1003.0
    covered = refresh(store, "weekly", "80", at=1003.0, covered=["first-call"])
    weekly = pools(store)["weekly"]
    assert weekly["local_uncovered"] == "0.000000"
    assert weekly["future_reserved"] == "5.000000"
    assert weekly["observation"]["sequence"] == covered["sequence"]
    assert weekly["observation"]["received_at"] == 1003.0
    assert weekly["observation"]["observation"]["coverage_ref"] == "verified-request-ids"
    assert weekly["observation"]["observation"]["covered_usage_ids"] == ["first-call"]


def test_all_account_pools_and_runs_share_one_held_count(tmp_path: Path) -> None:
    store = setup(tmp_path)
    generous(store)
    first = active(store, "first", run="run-a", profile="fast-a")
    second = active(store, "second", run="run-b", profile="fast-b")
    facts = store.routing_facts(account_ids=("shared-account",)).as_dict()
    current = facts["accounts"][0]
    assert current["held_attempts"] == 2
    assert current["held_admission_ids"] == sorted([first, second])
    assert {r["run_id"] for r in current["admissions"]} == {"run-a", "run-b"}
    assert {p["id"] for p in current["pools"]} == {"short", "weekly", "allowance"}
    assert all(p["future_reserved"] == "4.000000" for p in current["pools"])
    assert len(current["profiles"]) == 2
    assert current["policy_revision"] == 1
    assert facts["source_summary"]["reservations"]["row_count"] == 2


def test_expired_unsent_is_excluded_without_write_while_active_and_unknown_still_hold(
    tmp_path: Path,
) -> None:
    store = setup(tmp_path)
    generous(store)
    running = active(store, "running")
    unknown = active(store, "unknown", run="run-b", profile="fast-b")
    unsent = store.admit(request("unsent"), command_key="admit-unsent")["admission_id"]
    store.reconcile(
        unknown,
        local_ended=True,
        remote_ended=False,
        usage_complete=False,
        not_sent=False,
        evidence_ref="local-exit-only",
        command_key="unknown",
    )
    store.clock = lambda: 1100.0
    before = rows(store.path)
    facts = account(store)
    by_id = {r["admission_id"]: r for r in facts["admissions"]}
    assert facts["held_attempts"] == 2
    assert facts["unknown_attempts"] == 1
    assert facts["held_admission_ids"] == sorted([running, unknown])
    assert by_id[unsent]["stored_state"] == "reserved"
    assert by_id[unsent]["effective_held"] is False
    assert by_id[unsent]["exclusion_reason"] == "RESERVATION_EXPIRED_UNSENT"
    assert by_id[unsent]["lifecycle"] == []
    assert by_id[unknown]["lifecycle"][-1]["event"]["evidence"]["evidence_ref"] == "local-exit-only"
    assert all(p["future_reserved"] == "4.000000" for p in facts["pools"])
    assert rows(store.path) == before


def test_ended_usage_survives_and_only_proved_fixed_window_reset_excludes_attributed_usage(
    tmp_path: Path,
) -> None:
    store = setup(tmp_path)
    generous(store)
    identity = active(store, "first", "8")
    consume(store, identity, "attributed", "3")
    consume(store, identity, "unattributed", "1", window=None)
    end(store, identity)
    store.clock = lambda: 2001.0
    refreshed = refresh(store, "short", "80", at=2001.0, window="window-2", reset=3000.0)
    assert refreshed["applied"] is True
    current = pools(store)
    assert current["short"]["local_uncovered"] == "1.000000"
    assert current["weekly"]["local_uncovered"] == "4.000000"
    assert current["short"]["future_reserved"] == "0.000000"
    assert len(account(store)["usage"]) == 2


@pytest.mark.parametrize("window_kind", ["rolling", "unknown", "balance"])
def test_nonfixed_windows_never_infer_coverage_from_elapsed_time(
    tmp_path: Path,
    window_kind: str,
) -> None:
    store = CapacityStore(tmp_path / "capacity.sqlite", clock=lambda: 1000.0)
    definition = pool("short")
    definition["window_kind"] = window_kind
    store.register_pool(definition, command_key="pool")
    refresh(store, "short", "80", at=1000.0)
    store.register_profile(
        {"id": "single", "revision": 1, "account_id": "shared-account", "pool_ids": ["short"]},
        command_key="profile",
    )
    policy = {
        "account_id": "shared-account",
        "max_active_attempts": 4,
        "max_attempt_duration_seconds": 60,
        "observation_max_age_seconds": 30,
        "require_official_observation": False,
        "safety_margin": {},
        "lead_reserve": {},
        "lead_reserved_slots": 0,
        "conservative_mode": None,
    }
    store.activate_policy(policy, expected_revision=0, command_key="policy")
    value = request("first", profile="single")
    value["demand"] = {"short": "8"}
    admitted = store.admit(value, command_key="admit")
    identity = admitted["admission_id"]
    store.activate(identity, command_key="activate")
    store.record_usage(
        {
            "id": "call",
            "admission_id": identity,
            "amounts": {"short": "3"},
            "window_ids": {"short": "window-1"},
            "evidence_ref": "usage-proof",
            "attribution_ref": "window-proof",
        },
        command_key="usage",
    )
    end(store, identity)
    store.clock = lambda: 2001.0
    refresh(store, "short", "80", at=2001.0, window="window-2", reset=3000.0)
    assert pools(store)["short"]["local_uncovered"] == "3.000000"


def test_unknown_keeps_latest_numeric_and_late_unapplied_report_cannot_replace_source(
    tmp_path: Path,
) -> None:
    store = setup(tmp_path)
    store.clock = lambda: 1001.0
    numeric = refresh(store, "weekly", "0")
    store.clock = lambda: 1002.0
    unknown = refresh(store, "weekly", None, at=1002.0, metric="unknown")
    before = store.routing_facts()
    rejected = refresh(store, "weekly", "99", at=1001.5)
    assert rejected["applied"] is False
    current = pools(store)["weekly"]
    assert current["observation"]["sequence"] == unknown["sequence"]
    assert current["observation"]["observation"]["metric"] == "unknown"
    assert current["latest_numeric_observation"]["sequence"] == numeric["sequence"]
    assert current["reported_remaining"] is None
    assert current["exhaustion_requires_new_observation"] is True
    assert "QUOTA_UNKNOWN" in current["diagnostics"]
    assert "OBSERVATION_CONFIDENCE_UNAVAILABLE" in current["diagnostics"]
    after = store.routing_facts()
    assert after.sha256 != before.sha256
    assert after.as_dict()["source_summary"]["observations"]["row_count"] == 6


def test_missing_report_policy_stale_and_exhaustion_after_cooldown_remain_distinct(
    tmp_path: Path,
) -> None:
    store = setup(tmp_path)
    store.register_pool(pool("unobserved"), command_key="unobserved")
    store.record_failure(
        "shared-account",
        reason="QUOTA_EXHAUSTED",
        retry_after_seconds=20,
        evidence_ref="provider-error-receipt",
        command_key="failure",
    )
    assert account(store)["cooldown_until"] == 1020.0
    store.clock = lambda: 1040.0
    current = account(store)
    assert current["cooldown_until"] is None
    assert current["exhaustion_requires_new_observation"] is True
    assert current["failures"][0]["sequence"] == 1
    assert current["failures"][0]["failure"]["failure"]["evidence_ref"] == "provider-error-receipt"
    entries = {p["id"]: p for p in current["pools"]}
    assert "OBSERVATION_STALE" in entries["weekly"]["diagnostics"]
    assert "EXHAUSTION_REQUIRES_NEW_OBSERVATION" in entries["weekly"]["diagnostics"]
    assert "OBSERVATION_REQUIRED" in entries["unobserved"]["diagnostics"]
    isolated = CapacityStore(tmp_path / "unconfigured.sqlite", clock=lambda: 1000.0)
    isolated.register_pool(pool("unconfigured"), command_key="pool")
    unconfigured = pools(isolated)["unconfigured"]
    assert "CAPACITY_POLICY_REQUIRED" in unconfigured["diagnostics"]
    assert unconfigured["safety_margin"] is None


def test_negative_remaining_and_oversized_aggregate_are_preserved_with_conversion_gap(
    tmp_path: Path,
) -> None:
    store = setup(tmp_path)
    generous(store)
    identity = active(store, "first", "8")
    consume(store, identity, "max-one", "9223372036854.775807")
    consume(store, identity, "max-two", "9223372036854.775807")
    store.clock = lambda: 1002.0
    refresh(store, "weekly", "105", at=1002.0, metric="used")
    current = pools(store)["weekly"]
    assert current["reported_remaining"] == "-5.000000"
    assert current["local_uncovered"] == "18446744073709.551614"
    assert current["future_reserved"] == "0.000000"
    assert current["observation"]["observation"]["amount"] == "105"
    assert "REPORTED_USAGE_EXCEEDS_LIMIT" in current["diagnostics"]
    assert "ROUTING_QUANTITY_OUT_OF_RANGE" in current["diagnostics"]


def test_capture_sees_one_sqlite_snapshot_during_real_policy_and_usage_updates(
    tmp_path: Path,
) -> None:
    store = setup(tmp_path)
    generous(store)
    identity = active(store, "first", "8")
    consume(store, identity, "before-call", "1")
    with sqlite3.connect(store.path) as db:
        assert db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    writer = CapacityStore(store.path, clock=lambda: 1001.0)
    anchored, updated = Event(), Event()

    def clock() -> float:
        anchored.set()
        assert updated.wait(timeout=5)
        return 1001.0

    store.clock = clock
    with ThreadPoolExecutor(max_workers=1) as executor:
        capture = executor.submit(store.routing_facts)
        assert anchored.wait(timeout=5)
        try:
            policy = writer.snapshot()["policies"][-1]["policy"]
            policy["lead_reserved_slots"] = 1
            writer.activate_policy(policy, expected_revision=1, command_key="policy-next")
            consume(writer, identity, "after-call", "2")
        finally:
            updated.set()
        first = capture.result(timeout=5).as_dict()
    second = store.routing_facts().as_dict()
    assert first["accounts"][0]["policy_revision"] == 1
    assert len(first["accounts"][0]["usage"]) == 1
    assert first["accounts"][0]["pools"][0]["local_uncovered"] == "1.000000"
    assert second["accounts"][0]["policy_revision"] == 2
    assert len(second["accounts"][0]["usage"]) == 2
    assert second["accounts"][0]["pools"][0]["local_uncovered"] == "3.000000"
    assert first["source_summary"]["policies"] != second["source_summary"]["policies"]
    assert first["source_summary"]["usage"] != second["source_summary"]["usage"]


@pytest.mark.parametrize(
    "selection",
    [["shared-account"], ("shared-account", "shared-account"), ("bad account",), ("\ud800",), (1,)],
)
def test_invalid_selection_fails_without_mutation(tmp_path: Path, selection: object) -> None:
    store = setup(tmp_path)
    before = rows(store.path)
    with pytest.raises(CapacityError, match="^CAPACITY_INPUT_INVALID$"):
        store.routing_facts(account_ids=selection)
    assert rows(store.path) == before


def test_unknown_account_and_empty_selection_are_explicit(tmp_path: Path) -> None:
    store = setup(tmp_path)
    before = rows(store.path)
    with pytest.raises(CapacityError, match="^CAPACITY_ACCOUNT_UNKNOWN$"):
        store.routing_facts(account_ids=("not-registered",))
    empty = store.routing_facts(account_ids=()).as_dict()
    assert empty["accounts"] == []
    assert all(summary["row_count"] == 0 for summary in empty["source_summary"].values())
    assert rows(store.path) == before


def test_account_filter_keeps_complete_pools_and_binds_only_selected_account_sources(
    tmp_path: Path,
) -> None:
    store = setup(tmp_path)
    before = store.routing_facts(account_ids=("shared-account",))
    other = pool("other-weekly")
    other["account_id"] = "other-account"
    store.register_pool(other, command_key="other-pool")
    policy = store.snapshot()["policies"][-1]["policy"]
    policy["account_id"] = "other-account"
    policy["lead_reserve"] = {}
    store.activate_policy(policy, expected_revision=0, command_key="other-policy")
    after = store.routing_facts(account_ids=("shared-account",))
    assert before == after
    all_facts = store.routing_facts().as_dict()
    assert all_facts["account_ids"] == ["other-account", "shared-account"]
    assert (
        store.routing_facts(account_ids=("shared-account", "other-account")).as_dict() == all_facts
    )
    selected = store.routing_facts(account_ids=("other-account",)).as_dict()
    assert selected["source_summary"]["pools"]["row_count"] == 1
    assert selected["source_summary"]["policies"]["row_count"] == 1
    assert selected["source_summary"]["observations"]["row_count"] == 0
    assert selected["accounts"][0]["pools"][0]["id"] == "other-weekly"


def test_official_source_does_not_invent_confidence_or_satisfy_an_admission_request(
    tmp_path: Path,
) -> None:
    store = setup(tmp_path)
    store.clock = lambda: 1001.0
    refresh(store, "weekly", "5", source="official", source_ref="recorded-service-report")
    value = store.routing_facts().as_dict()
    weekly = next(p for p in value["accounts"][0]["pools"] if p["id"] == "weekly")
    assert weekly["observation"]["observation"]["source"] == "official"
    assert "OBSERVATION_CONFIDENCE_UNAVAILABLE" in weekly["diagnostics"]
    for absent in (
        "cash_remaining",
        "budget_remaining",
        "estimates",
        "profile_facts",
        "authorization",
    ):
        assert absent not in value
        assert absent not in value["accounts"][0]
    assert "EXECUTION_QUALIFICATION" in value["missing_facts"]
    before = rows(store.path)
    with pytest.raises(CapacityError, match="^CAPACITY_INPUT_INVALID$"):
        store.admit(value, command_key="cannot-admit-fragment")
    assert rows(store.path) == before


@pytest.mark.parametrize("value", [True, float("inf"), float("nan"), "1000", 10**500])
def test_invalid_trusted_clock_fails_without_mutation(tmp_path: Path, value: object) -> None:
    store = setup(tmp_path)
    before = rows(store.path)
    store.clock = lambda: value
    with pytest.raises(CapacityError, match="^CLOCK_UNAVAILABLE$"):
        store.routing_facts()
    assert rows(store.path) == before


def test_capture_reads_once_and_never_recreates_a_missing_database(tmp_path: Path) -> None:
    location = tmp_path / "space # question ? unicode 容量.sqlite"
    # '?' is invalid in Windows filenames; URI escaping is still exercised by '#' and spaces.
    location = location.with_name(location.name.replace("?", "mark"))
    calls = []

    def clock() -> float:
        calls.append(1)
        return 1000.0

    store = CapacityStore(location, clock=clock)
    assert store.routing_facts().as_dict()["captured_at"] == 1000.0
    assert calls == [1]
    location.unlink()
    with pytest.raises(sqlite3.OperationalError):
        store.routing_facts()
    assert not location.exists()
