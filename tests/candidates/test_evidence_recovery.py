"""Lost result recovery reads exact historical Evidence without replaying effects."""

import hashlib
import sqlite3

import pytest
from karajan.candidates import CandidateError, CandidateStore
from test_validation import case, check_record, context, review_record

__all__ = ["case"]


def lookup(store, request, kind, log):
    return store.lookup_evidence(
        request,
        kind=kind,
        log_sha256=None if log is None else hashlib.sha256(log).hexdigest(),
        log_size=None if log is None else len(log),
    )


@pytest.mark.parametrize("kind", ["check", "review"])
def test_exact_committed_result_recovers_after_reopen_without_log_or_writes(case, kind):
    store = case["store"]
    candidate = store.freeze(case["workspace"], case["request"])
    check = store.record_check(check_record(candidate), log=b"check output")
    request = check_record(candidate) if kind == "check" else review_record(candidate, [check])
    log = b"check output" if kind == "check" else b"review output"
    saved = getattr(store, "record_" + kind)(request, log=log)
    # Simulate lost submission response and later loss of the log artifact. The
    # commit can still be identified; current gate must separately stay blocked.
    (store.objects / saved["log"]["sha256"]).unlink()
    database = store.directory / "candidates.sqlite"
    before = database.read_bytes()
    reopened = CandidateStore(store.directory, existing_only=True)
    assert lookup(reopened, request, kind, log) == saved
    assert database.read_bytes() == before
    gate = reopened.gate(candidate["id"], current=context(candidate))
    assert gate["local_gate_passed"] is False
    assert "ARTIFACT_UNAVAILABLE" in gate["reasons"]


def test_lookup_preserves_original_result_when_latest_check_failed(case):
    store = case["store"]
    candidate = store.freeze(case["workspace"], case["request"])
    original = check_record(candidate)
    saved = store.record_check(original, log=b"first pass")
    later = original | {"evidence_key": "check-2", "exit_code": 1}
    store.record_check(later, log=b"latest failure")
    assert lookup(store, original, "check", b"first pass") == saved
    gate = store.gate(candidate["id"], current=context(candidate))
    assert "CHECK_NOT_PASSED:tests" in gate["reasons"]


@pytest.mark.parametrize("changed", ["request", "log", "size", "kind"])
def test_same_key_with_different_full_identity_never_matches(case, changed):
    store = case["store"]
    candidate = store.freeze(case["workspace"], case["request"])
    request = check_record(candidate)
    record = store.record_check(request, log=b"log")
    expected = request
    kind, log, size = "check", hashlib.sha256(b"log").hexdigest(), 3
    if changed == "request":
        expected = request | {"executor_ref": "another-process"}
    elif changed == "log":
        log = "f" * 64
    elif changed == "size":
        size = 4
    else:
        expected = review_record(candidate, [record]) | {"evidence_key": request["evidence_key"]}
        kind = "review"
    with pytest.raises(CandidateError, match="EVIDENCE_KEY_CONFLICT"):
        store.lookup_evidence(expected, kind=kind, log_sha256=log, log_size=size)


@pytest.mark.parametrize("log", [None, b""])
def test_missing_and_empty_logs_are_distinct_historical_identities(case, log):
    store = case["store"]
    candidate = store.freeze(case["workspace"], case["request"])
    request = check_record(candidate)
    saved = store.record_check(request, log=log)
    assert lookup(store, request, "check", log) == saved
    with pytest.raises(CandidateError, match="EVIDENCE_KEY_CONFLICT"):
        lookup(store, request, "check", b"" if log is None else None)


def test_no_record_is_read_only_and_missing_ledger_is_not_recreated(case):
    store = case["store"]
    candidate = store.freeze(case["workspace"], case["request"])
    request = check_record(candidate)
    database = store.directory / "candidates.sqlite"
    before = database.read_bytes()
    assert lookup(store, request, "check", b"log") is None
    assert database.read_bytes() == before
    database.unlink()
    with pytest.raises(CandidateError, match="CANDIDATE_STORAGE_UNAVAILABLE"):
        lookup(store, request, "check", b"log")
    assert not database.exists()


def test_inconsistent_stored_identity_does_not_recover(case):
    store = case["store"]
    candidate = store.freeze(case["workspace"], case["request"])
    request = check_record(candidate)
    store.record_check(request, log=b"log")
    with sqlite3.connect(store.directory / "candidates.sqlite") as connection:
        connection.execute("UPDATE evidence SET candidate_id='different-candidate'")
    with pytest.raises(CandidateError, match="EVIDENCE_IDENTITY_INVALID"):
        lookup(store, request, "check", b"log")
