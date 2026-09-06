"""Exact historical capture recovery from real Git/CAS, without new effects."""

import copy
import hashlib
import sqlite3

import pytest
from karajan.candidates import CandidateError, CandidateStore
from test_baseline_materialization import registered as registered
from test_projected_capture import inputs


def descriptors(contents):
    return [
        {"path": path, "sha256": hashlib.sha256(body).hexdigest(), "size": len(body)}
        for path, body in sorted(contents.items())
    ]


def test_existing_only_missing_empty_and_reconnected_ledger_do_not_initialize(tmp_path):
    missing = tmp_path / "missing"
    with pytest.raises(CandidateError, match="CANDIDATE_STORAGE_UNAVAILABLE"):
        CandidateStore(missing, existing_only=True)
    assert not missing.exists()
    store = CandidateStore(tmp_path / "state")
    database = store.directory / "candidates.sqlite"
    reopened = CandidateStore(store.directory, existing_only=True)
    database.rename(store.directory / "retained.sqlite")
    with pytest.raises(CandidateError, match="CANDIDATE_STORAGE_UNAVAILABLE"):
        reopened.get("absent")
    assert not database.exists()
    with sqlite3.connect(database):
        pass
    before = database.read_bytes()
    with pytest.raises(CandidateError, match="CANDIDATE_STORAGE_UNAVAILABLE"):
        CandidateStore(store.directory, existing_only=True)
    assert database.read_bytes() == before


@pytest.mark.parametrize("change", ["author", "writer", "policy", "input", "content"])
def test_similar_candidate_is_not_a_receipt_for_another_full_identity(registered, change):
    store, baseline, files = registered
    projection, contents, request = inputs(baseline, files)
    store.freeze_projection(projection, contents, request)
    captured = descriptors(contents)
    if change == "author":
        request["authors"][0]["context_id"] = "different-context"
    elif change == "writer":
        request["writer"]["observation_ref"] = "different-stop"
    elif change == "policy":
        request["policy"]["checks"][0]["argv"] = ["python", "different.py"]
    elif change == "input":
        request["input_sha256"] = "d" * 64
    else:
        next(row for row in captured if row["path"] == "src/task.py")["sha256"] = "d" * 64
    assert (
        store.lookup_projection_capture(request, projection=projection, captured_files=captured)
        is None
    )


@pytest.mark.parametrize(
    "fault", ["missing", "duplicate", "readonly", "extra", "wrong_type", "projection"]
)
def test_incomplete_capture_identity_is_rejected(registered, fault):
    store, baseline, files = registered
    projection, contents, request = inputs(baseline, files)
    store.freeze_projection(projection, contents, request)
    captured = descriptors(contents)
    if fault == "missing":
        captured.pop()
    elif fault == "duplicate":
        captured.append(copy.deepcopy(captured[0]))
    elif fault == "readonly":
        next(row for row in captured if row["path"] == "docs/untouched.md")["sha256"] = "d" * 64
    elif fault == "extra":
        captured[0]["content"] = "not accepted"
    elif fault == "wrong_type":
        captured[0]["size"] = True
    else:
        projection[0]["sha256"] = "d" * 64
    with pytest.raises(CandidateError, match="CAPTURE_IDENTITY_INVALID"):
        store.lookup_projection_capture(request, projection=projection, captured_files=captured)


def test_multiple_exact_commits_are_ambiguous_not_latest_wins(registered):
    store, baseline, files = registered
    projection, contents, request = inputs(baseline, files)
    captured = descriptors(contents)
    first = store.freeze_projection(projection, contents, request)
    other = contents | {"src/task.py": b"other\n"}
    store.freeze_projection(projection, other, request)
    last = store.freeze_projection(projection, contents, request)
    assert first["id"] != last["id"]
    with pytest.raises(CandidateError, match="CAPTURE_IDENTITY_AMBIGUOUS"):
        store.lookup_projection_capture(request, projection=projection, captured_files=captured)


@pytest.mark.parametrize("missing", ["artifacts", "git", "both"])
def test_historical_reopen_separates_commit_facts_from_current_storage_availability(
    registered, tmp_path, missing
):
    store, baseline, files = registered
    projection, contents, request = inputs(baseline, files)
    candidate = store.freeze_projection(projection, contents, request)
    unavailable = (
        [store.objects]
        if missing == "artifacts"
        else [store.git_directory]
        if missing == "git"
        else [store.objects, store.git_directory]
    )
    for path in unavailable:
        path.rename(path.with_name("retained-" + path.name))
    before = (store.directory / "candidates.sqlite").read_bytes()
    reopened = CandidateStore(store.directory, existing_only=True)
    assert (
        reopened.lookup_projection_capture(
            request, projection=projection, captured_files=descriptors(contents)
        )
        == candidate
    )
    if missing == "git":
        reopened.materialize(candidate["id"], tmp_path / "materialized")
        assert (tmp_path / "materialized/src/task.py").read_bytes() == contents["src/task.py"]
    else:
        with pytest.raises(CandidateError, match="ARTIFACT_UNAVAILABLE"):
            reopened.materialize(candidate["id"], tmp_path / "materialized")
    with pytest.raises(CandidateError, match="ARTIFACT_UNAVAILABLE|GIT_OPERATION_FAILED"):
        reopened.freeze_projection(projection, contents, request)
    assert all(not path.exists() for path in unavailable)
    assert (store.directory / "candidates.sqlite").read_bytes() == before


def test_duplicate_projection_allowlist_is_rejected_before_any_candidate_write(registered):
    store, baseline, files = registered
    projection, contents, request = inputs(baseline, files)
    request["allowed_paths"].append(request["allowed_paths"][0])
    before = {
        p.relative_to(store.directory): p.read_bytes()
        for p in store.directory.rglob("*")
        if p.is_file()
    }
    with pytest.raises(CandidateError, match="PROJECTION_WRITE_SCOPE_MISMATCH"):
        store.freeze_projection(projection, contents, request)
    assert {
        p.relative_to(store.directory): p.read_bytes()
        for p in store.directory.rglob("*")
        if p.is_file()
    } == before


def test_deferred_existing_handle_does_not_provision_and_checks_every_use(tmp_path):
    missing = tmp_path / "absent" / "candidate-state"
    with pytest.raises(CandidateError, match="CANDIDATE_EXISTING_MODE_REQUIRED"):
        CandidateStore(missing, defer_validation=True)
    handle = CandidateStore(missing, existing_only=True, defer_validation=True)
    assert not missing.parent.exists()
    with pytest.raises(CandidateError, match="CANDIDATE_STORAGE_UNAVAILABLE"):
        handle.get("absent")
    assert not missing.parent.exists()
    provisioned = CandidateStore(missing)
    assert handle.directory == provisioned.directory
    with pytest.raises(CandidateError, match="CANDIDATE_NOT_FOUND"):
        handle.get("absent")
    database = missing / "candidates.sqlite"
    database.rename(missing / "retained.sqlite")
    database.write_bytes(b"")
    with pytest.raises(CandidateError, match="CANDIDATE_STORAGE_UNAVAILABLE"):
        handle.get("absent")
    assert database.read_bytes() == b""


def test_exact_capture_lookup_recovers_original_not_latest_without_tree_or_writes(
    registered, tmp_path
):
    store, baseline, files = registered
    projection, contents, request = inputs(baseline, files)
    captured = descriptors(contents)
    original = store.freeze_projection(projection, contents, request)
    contents["src/task.py"] = b"another result\n"
    later = store.freeze_projection(projection, contents, request)
    assert later["id"] != original["id"]
    (tmp_path / "trusted").rename(tmp_path / "old-source")
    store.git_directory.rename(store.directory / "unavailable.git")
    store.objects.rename(store.directory / "unavailable-artifacts")
    database = store.directory / "candidates.sqlite"
    before = database.read_bytes()
    found = store.lookup_projection_capture(request, projection=projection, captured_files=captured)
    assert found == original
    found["request"]["authors"].clear()
    assert (
        store.lookup_projection_capture(request, projection=projection, captured_files=captured)
        == original
    )
    assert database.read_bytes() == before
