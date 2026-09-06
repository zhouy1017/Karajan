"""Independent behavior checks; product source is never changed by these tests."""

from __future__ import annotations

import copy
import hashlib
import json
import socket
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Event

import pytest
from cases import (
    Arrangement,
    aggregate_beyond_scalar_limit,
    all_rows,
    exhausted_then_unknown_and_unapplied,
    mixed_lifecycle,
    partial_coverage,
    reset_with_and_without_attribution,
    row_digests,
    used_above_limit,
)
from karajan.capacity import CapacityError, CapacityStore

ARTIFACTS = Path(__file__).resolve().parents[3] / ".cache/capacity-facts-spec/evidence-final"


def capture(case: Arrangement, name: str) -> dict:
    before = all_rows(case.store.path)
    facts = case.store.routing_facts()
    same = case.store.routing_facts()
    after = all_rows(case.store.path)
    assert before == after
    assert len(before) == 9
    assert facts.canonical_json == same.canonical_json
    assert facts.sha256 == hashlib.sha256(facts.canonical_json.encode()).hexdigest()
    document = facts.as_dict()
    changed_copy = facts.as_dict()
    changed_copy["accounts"].clear()
    assert facts.as_dict() == document
    assert document["scope"] == "capacity_fragment"
    assert document["captured_at"] == case.now
    assert document["activation_allowed"] is False
    forbidden = {
        "cash_remaining",
        "budget_remaining",
        "estimates",
        "fx",
        "profile_facts",
        "authorization",
        "valid_until",
        "confidence",
    }

    def inspect(value: object) -> None:
        if isinstance(value, dict):
            assert not forbidden.intersection(value)
            for nested in value.values():
                inspect(nested)
        elif isinstance(value, list):
            for nested in value:
                inspect(nested)

    inspect(document)
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    (ARTIFACTS / (name + ".json")).write_text(
        json.dumps(
            {
                "arrangement_events": case.events,
                "before": row_digests(before),
                "after": row_digests(after),
                "facts_sha256": facts.sha256,
                "facts": document,
                "all_tables_unchanged": True,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return document


def test_independent_partial_coverage_preserves_future_slice(tmp_path: Path) -> None:
    case = partial_coverage(tmp_path)
    account = capture(case, "partial-coverage")["accounts"][0]
    pool = account["pools"][0]
    assert pool["reported_remaining"] == "80.000000"
    assert pool["local_uncovered"] == "2.000000"
    assert pool["future_reserved"] == "5.000000"
    observed = pool["observation"]["observation"]
    assert observed["covered_usage_ids"] == ["covered-first"]
    assert observed["coverage_ref"] == "spec-explicit-coverage"
    assert {u["receipt"]["id"] for u in account["usage"]} == {"covered-first", "uncovered-second"}


def test_independent_expiration_does_not_write_or_release_unknown(tmp_path: Path) -> None:
    case = mixed_lifecycle(tmp_path)
    account = capture(case, "mixed-lifecycle")["accounts"][0]
    assert account["held_attempts"] == 2
    assert account["unknown_attempts"] == 1
    admissions = {item["attempt_id"]: item for item in account["admissions"]}
    expired = admissions["attempt-expired-reserved"]
    assert expired["stored_state"] == "reserved"
    assert expired["effective_held"] is False
    assert expired["exclusion_reason"] == "RESERVATION_EXPIRED_UNSENT"
    for identity in ("attempt-active", "attempt-unknown"):
        assert admissions[identity]["effective_held"] is True
        assert admissions[identity]["reservation"]["expires_at"] < case.now
    assert {a["run_id"] for a in account["admissions"] if a["effective_held"]} == {"run-a", "run-b"}
    assert (
        admissions["attempt-unknown"]["lifecycle"][-1]["event"]["evidence"]["remote_ended"] is False
    )
    for pool in account["pools"]:
        assert pool["local_uncovered"] == "6.000000"
        assert pool["future_reserved"] == "12.000000"
        assert "WINDOW_EXPIRED" in pool["diagnostics"]


def test_independent_all_window_kinds_keep_unattributed_use(tmp_path: Path) -> None:
    case = reset_with_and_without_attribution(tmp_path)
    pools = {
        p["window_kind"]: p for p in capture(case, "window-attribution")["accounts"][0]["pools"]
    }
    assert pools["fixed"]["local_uncovered"] == "3.000000"
    for kind in ("rolling", "balance", "unknown"):
        assert pools[kind]["local_uncovered"] == "6.000000"
    assert all(p["future_reserved"] == "0.000000" for p in pools.values())


def test_independent_exhaustion_survives_current_unknown_and_later_unapplied(
    tmp_path: Path,
) -> None:
    case = exhausted_then_unknown_and_unapplied(tmp_path)
    account = capture(case, "exhausted-unknown-unapplied")["accounts"][0]
    pool = account["pools"][0]
    assert account["cooldown_until"] is None
    assert account["exhaustion_requires_new_observation"] is True
    assert pool["observation"]["observation"]["metric"] == "unknown"
    assert pool["observation"]["observation"]["observed_at"] == 1004.0
    assert pool["latest_numeric_observation"]["observation"]["amount"] == "0"
    assert pool["reported_remaining"] is None
    assert "EXHAUSTION_REQUIRES_NEW_OBSERVATION" in pool["diagnostics"]
    assert account["failures"][0]["failure"]["failure"]["evidence_ref"] == "spec-exhausted-error"


def test_independent_negative_remaining_is_a_fact_and_conversion_gap(tmp_path: Path) -> None:
    case = used_above_limit(tmp_path)
    pool = capture(case, "negative-remaining")["accounts"][0]["pools"][0]
    assert pool["observation"]["observation"]["amount"] == "101"
    assert pool["reported_remaining"] == "-1.000000"
    assert {"REPORTED_USAGE_EXCEEDS_LIMIT", "ROUTING_QUANTITY_OUT_OF_RANGE"}.issubset(
        pool["diagnostics"]
    )


def test_independent_aggregate_above_single_quantity_range_is_not_clamped(tmp_path: Path) -> None:
    case = aggregate_beyond_scalar_limit(tmp_path)
    pool = capture(case, "aggregate-above-scalar-limit")["accounts"][0]["pools"][0]
    assert pool["local_uncovered"] == "18446744073709.551614"
    assert pool["future_reserved"] == "0.000000"
    assert "ROUTING_QUANTITY_OUT_OF_RANGE" in pool["diagnostics"]


def test_independent_account_filter_and_current_policy(tmp_path: Path) -> None:
    case = Arrangement(tmp_path)
    second_policy = dict(case.policy, account_id="account-policy-only", lead_reserved_slots=2)
    case.call("activate_policy", second_policy, expected_revision=0)
    current_policy = dict(case.policy, lead_reserved_slots=3, safety_margin={"pool-fixed": "4"})
    case.call("activate_policy", current_policy, expected_revision=1)
    both = capture(case, "policy-and-filter")
    first = next(a for a in both["accounts"] if a["id"] == "account-a")
    assert first["policy_revision"] == 2
    assert first["policy"] == current_policy
    assert first["pools"][0]["safety_margin"] == "4"
    second = case.store.routing_facts(account_ids=("account-policy-only",)).as_dict()
    assert second["account_ids"] == ["account-policy-only"]
    assert second["accounts"][0]["policy_revision"] == 1
    assert second["accounts"][0]["pools"] == []
    assert case.store.routing_facts(account_ids=()).as_dict()["accounts"] == []
    before = all_rows(case.store.path)
    for selection in (("account-a", "account-a"), ("unknown",), ["account-a"], (True,)):
        with pytest.raises(CapacityError):
            case.store.routing_facts(account_ids=selection)
    assert all_rows(case.store.path) == before


@pytest.mark.parametrize("operation", ["policy", "usage"])
def test_independent_one_snapshot_survives_committed_concurrent_update(
    tmp_path: Path,
    operation: str,
) -> None:
    case = partial_coverage(tmp_path)
    with closing(sqlite3.connect(case.store.path)) as connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    writer = CapacityStore(case.store.path, clock=lambda: case.now)
    stable = case.store.routing_facts().as_dict()
    entered, release = Event(), Event()

    def paused_clock() -> float:
        entered.set()
        assert release.wait(10), "test did not release reader"
        return case.now

    def write() -> None:
        if operation == "policy":
            writer.activate_policy(
                dict(case.policy, lead_reserved_slots=2),
                expected_revision=1,
                command_key="spec-concurrent-policy",
            )
        else:
            original = copy.deepcopy(case.events[-2]["result"]["receipt"])
            original.update(id="concurrent-usage", amounts={"pool-fixed": "2"})
            writer.record_usage(original, command_key="spec-concurrent-usage")

    case.store.clock = paused_clock
    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            reader = workers.submit(case.store.routing_facts)
            assert entered.wait(10), "reader did not reach snapshot clock"
            update = workers.submit(write)
            update.result(timeout=10)  # Actual committed update while read is held.
            release.set()
            captured = reader.result(timeout=10).as_dict()
    finally:
        release.set()
        case.store.clock = lambda: case.now
    assert captured == stable
    later = case.store.routing_facts().as_dict()
    assert later["source_summary"] != stable["source_summary"]
    if operation == "policy":
        assert later["accounts"][0]["policy_revision"] == 2
        assert captured["accounts"][0]["policy_revision"] == 1
    else:
        assert later["accounts"][0]["pools"][0]["local_uncovered"] == "4.000000"
        assert later["accounts"][0]["pools"][0]["future_reserved"] == "3.000000"
    (ARTIFACTS / ("concurrent-" + operation + ".json")).write_text(
        json.dumps(
            {
                "operation": operation,
                "before": stable,
                "during": captured,
                "after": later,
                "update_committed_while_read_paused": True,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )


def test_independent_bad_clock_and_export_do_not_connect_or_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = Arrangement(tmp_path)
    attempts = []

    def network_attempt(*args: object, **kwargs: object) -> None:
        attempts.append((args, kwargs))
        raise AssertionError("capacity export attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", network_attempt)
    monkeypatch.setattr(socket.socket, "connect_ex", network_attempt)
    capture(case, "no-network")
    before = all_rows(case.store.path)
    for value in (True, None, float("inf"), float("nan"), 10**1000):
        case.store.clock = lambda value=value: value
        with pytest.raises(CapacityError, match="CLOCK_UNAVAILABLE"):
            case.store.routing_facts()
    assert attempts == []
    assert all_rows(case.store.path) == before
