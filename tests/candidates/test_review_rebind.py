"""Controlled reviewer rebinding uses real Git/CAS; authority records are synthetic."""

import copy
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest
from karajan.candidates import CandidateError, CandidateStore
from karajan.candidates.store import canonical, digest
from test_baseline_materialization import directory_link
from test_baseline_materialization import registered as registered
from test_projected_capture import inputs
from test_validation import check_record, context


def identity(candidate):
    return {
        key: candidate[key]
        for key in (
            "id",
            "series_id",
            "revision",
            "repository_identity",
            "base_sha",
            "tree_sha",
            "content_sha256",
            "manifest_sha256",
            "input_sha256",
            "policy_sha256",
        )
    } | {
        "baseline_id": candidate["request"]["baseline_id"],
        "request_sha256": digest(candidate["request"]),
    }


def binding_for(candidate):
    return {
        "schema_version": "karajan.reviewer-binding.v1",
        "revision": 1,
        "source_candidate": identity(candidate),
        "run_id": "run-1",
        "operation_id": "worker-operation-1",
        "reviewer_task_id": "review-task-1",
        "capture_digest": "1" * 64,
        "approval_digest": "2" * 64,
        "plan_digest": "3" * 64,
        "execution_policy_digest": "4" * 64,
        "reviewer_task_digest": "5" * 64,
        "rulebook_digest": "6" * 64,
        "reviewer_sources": [
            {
                "reviewer": {
                    "profile_id": "reviewer-profile",
                    "profile_revision": 2,
                    "model_family": "fixture-reviewer-family",
                    "qualification_ref": "fixture:reviewer-qualified",
                },
                "qualification_source_digest": "7" * 64,
                "authentication_source_digest": "8" * 64,
            }
        ],
    }


def test_rebind_preserves_complete_candidate_and_requires_new_check_evidence(registered, tmp_path):
    store, baseline, files = registered
    projection, contents, request = inputs(baseline, files)
    original = store.freeze_projection(projection, contents, request)
    evidence = store.record_check(check_record(original), log=b"synthetic check observation\n")
    binding = binding_for(original)
    rebound = store.rebind_reviewers(binding, command_key="review-binding-1")
    expected_request = copy.deepcopy(original["request"])
    expected_request["policy"]["review"]["approved_reviewers"] = [
        row["reviewer"] for row in binding["reviewer_sources"]
    ]
    assert rebound["request"] == expected_request
    assert rebound["policy_sha256"] == digest(expected_request["policy"])
    assert rebound["id"] != original["id"]
    assert rebound["revision"] == original["revision"] + 1
    for key in original.keys() - {"id", "revision", "policy_sha256", "request", "frozen_at"}:
        assert rebound[key] == original[key]
    assert rebound["review_rebind"]["binding"] == binding
    assert rebound["review_rebind"]["binding_sha256"] == digest(binding)
    assert store.get(original["id"]) == original
    assert (
        store.gate(original["id"], current=context(original))["evidence"][0]["id"] == evidence["id"]
    )
    gate = store.gate(rebound["id"], current=context(rebound))
    assert gate["evidence"] == []
    assert gate["reasons"] == ["CHECK_EVIDENCE_MISSING:tests", "REVIEW_EVIDENCE_MISSING"]
    assert not gate["delivery_eligible"]
    reopened = CandidateStore(store.directory, existing_only=True)
    assert reopened.lookup_review_rebind(binding, command_key="review-binding-1") == rebound
    assert reopened.rebind_reviewers(binding, command_key="review-binding-1") == rebound
    target = tmp_path / "rebound"
    reopened.materialize(rebound["id"], target)
    assert {
        p.relative_to(target).as_posix(): p.read_bytes() for p in target.rglob("*") if p.is_file()
    } == files | contents
    if os.name != "nt":
        assert (target / "bin/run").stat().st_mode & 0o777 == 0o755


def source(registered):
    store, baseline, files = registered
    projection, contents, request = inputs(baseline, files)
    original = store.freeze_projection(projection, contents, request)
    return store, original, binding_for(original)


def test_exact_lost_reply_recovery_and_conflict_remain_readonly_without_assets_or_clock(
    registered, monkeypatch, tmp_path
):
    from karajan.candidates import _review_binding

    store, original, binding = source(registered)
    committed = store.rebind_reviewers(binding, command_key="lost-reply")
    next_binding = binding_for(committed)
    next_binding["revision"] = 2
    newest = store.rebind_reviewers(next_binding, command_key="next-binding")
    assert newest["revision"] == 3
    store.objects.rename(store.objects.with_name("retained-artifacts"))
    store.git_directory.rename(store.git_directory.with_name("retained-git"))
    (tmp_path / "trusted").rename(tmp_path / "retained-repository")
    database = store.directory / "candidates.sqlite"
    before = database.read_bytes()

    def forbidden(*args, **kwargs):
        raise AssertionError("history must not observe mutable resources or clocks")

    monkeypatch.setattr(CandidateStore, "_git", forbidden)
    monkeypatch.setattr(CandidateStore, "_materialization_content", forbidden)
    monkeypatch.setattr(_review_binding, "timestamp", forbidden)
    reopened = CandidateStore(store.directory, existing_only=True)
    assert reopened.lookup_review_rebind(binding, command_key="lost-reply") == committed
    assert reopened.rebind_reviewers(binding, command_key="lost-reply") == committed
    assert reopened.lookup_review_rebind(binding, command_key="no-command") is None
    conflicting = copy.deepcopy(binding)
    conflicting["approval_digest"] = "f" * 64
    for operation in (reopened.lookup_review_rebind, reopened.rebind_reviewers):
        with pytest.raises(CandidateError, match="REVIEW_REBIND_IDEMPOTENCY_CONFLICT"):
            operation(conflicting, command_key="lost-reply")
    with pytest.raises(CandidateError, match="CANDIDATE_SUPERSEDED"):
        reopened.rebind_reviewers(binding, command_key="different-command")
    assert not store.objects.exists() and not store.git_directory.exists()
    assert reopened.get(original["id"])["request"]["policy"]["review"]["approved_reviewers"] == []
    assert database.read_bytes() == before


@pytest.mark.parametrize(
    "field",
    [
        "id",
        "series_id",
        "revision",
        "repository_identity",
        "base_sha",
        "tree_sha",
        "content_sha256",
        "manifest_sha256",
        "input_sha256",
        "policy_sha256",
        "baseline_id",
        "request_sha256",
    ],
)
def test_every_source_identity_component_is_required(registered, field):
    store, _, binding = source(registered)
    original = binding["source_candidate"][field]
    binding["source_candidate"][field] = (
        original + 1
        if field == "revision"
        else "f" * 40
        if field.endswith("_sha")
        else "f" * 64
        if field.endswith("_sha256") or field == "baseline_id"
        else "other"
    )
    database = store.directory / "candidates.sqlite"
    before = database.read_bytes()
    with pytest.raises(CandidateError, match="REVIEW_SOURCE_BINDING_MISMATCH|CANDIDATE_NOT_FOUND"):
        store.rebind_reviewers(binding, command_key="different-source")
    assert store.lookup_review_rebind(binding, command_key="different-source") is None
    assert database.read_bytes() == before


@pytest.mark.parametrize(
    "fault",
    [
        "empty",
        "duplicate",
        "policy",
        "writer",
        "checks",
        "environment",
        "class",
        "raw_qualification",
        "missing_auth",
        "invalid_source",
        "key",
    ],
)
def test_binding_cannot_replace_other_policy_or_authority_fields(registered, fault):
    store, _, binding = source(registered)
    command_key = "binding"
    if fault == "empty":
        binding["reviewer_sources"] = []
    elif fault == "duplicate":
        other = copy.deepcopy(binding["reviewer_sources"][0])
        other["reviewer"]["model_family"] = "different-family"
        binding["reviewer_sources"].append(other)
    elif fault == "raw_qualification":
        binding["reviewer_sources"][0]["status"] = "passed"
    elif fault == "missing_auth":
        del binding["reviewer_sources"][0]["authentication_source_digest"]
    elif fault == "invalid_source":
        binding["reviewer_sources"][0]["qualification_source_digest"] = "unknown"
    elif fault == "key":
        command_key = "not a key"
    else:
        binding[fault] = {"replacement": True}
    database = store.directory / "candidates.sqlite"
    before = database.read_bytes()
    for operation in (store.rebind_reviewers, store.lookup_review_rebind):
        with pytest.raises(
            CandidateError, match="REVIEW_BINDING_INVALID|POLICY_IDENTITY_AMBIGUOUS"
        ):
            operation(binding, command_key=command_key)
    assert database.read_bytes() == before


@pytest.mark.parametrize(
    "fault", ["changed", "missing", "hardlink", "directory_link", "old_baseline"]
)
def test_new_effect_verifies_full_candidate_and_baseline_cas(registered, tmp_path, fault):
    store, original, binding = source(registered)
    entry = next(row for row in original["manifest"] if row["path"] == "data/bytes.bin")
    if fault == "old_baseline":
        baseline = store.get_baseline(original["request"]["baseline_id"])
        entry = next(row for row in baseline["manifest"] if row["path"] == "src/task.py")
    artifact = store.objects / entry["artifact"]["sha256"]
    if fault in {"changed", "old_baseline"}:
        artifact.write_bytes(b"synthetic tamper")
    elif fault == "missing":
        artifact.unlink()
    elif fault == "hardlink":
        os.link(artifact, tmp_path / "shared-artifact")
    else:
        relocated = tmp_path / "relocated-artifacts"
        store.objects.rename(relocated)
        directory_link(store.objects, relocated)
    database = store.directory / "candidates.sqlite"
    before = database.read_bytes()
    with pytest.raises(CandidateError, match="ARTIFACT_UNAVAILABLE"):
        store.rebind_reviewers(binding, command_key="bad-cas")
    assert store.lookup_review_rebind(binding, command_key="bad-cas") is None
    assert database.read_bytes() == before


def test_new_content_supersedes_old_source_but_retains_exact_commit(registered):
    store, original, binding = source(registered)
    rebound = store.rebind_reviewers(binding, command_key="first")
    _, baseline, files = registered
    projection, contents, request = inputs(baseline, files)
    contents["src/task.py"] = b"print('later content')\n"
    later = store.freeze_projection(projection, contents, request)
    assert later["content_sha256"] != original["content_sha256"]
    before = (store.directory / "candidates.sqlite").read_bytes()
    with pytest.raises(CandidateError, match="CANDIDATE_SUPERSEDED"):
        store.rebind_reviewers(binding_for(rebound), command_key="too-late")
    assert store.lookup_review_rebind(binding, command_key="first") == rebound
    assert store.rebind_reviewers(binding, command_key="first") == rebound
    assert (store.directory / "candidates.sqlite").read_bytes() == before


def test_readonly_missing_ledger_never_initializes(registered, tmp_path):
    _, _, binding = source(registered)
    absent = tmp_path / "absent" / "state"
    handle = CandidateStore(absent, existing_only=True, defer_validation=True)
    for operation in (handle.lookup_review_rebind, handle.rebind_reviewers):
        with pytest.raises(CandidateError, match="CANDIDATE_STORAGE_UNAVAILABLE"):
            operation(binding, command_key="missing")
    assert not absent.parent.exists()


@pytest.mark.parametrize("field", ["id", "series_id", "revision", "manifest", "binding_digest"])
def test_inconsistent_committed_metadata_is_never_an_exact_receipt(registered, field):
    store, _, binding = source(registered)
    result = store.rebind_reviewers(binding, command_key="committed")
    corrupt = copy.deepcopy(result)
    if field == "manifest":
        corrupt["manifest"][0]["mode"] = "100755"
    elif field == "binding_digest":
        corrupt["review_rebind"]["binding_sha256"] = "f" * 64
    elif field == "revision":
        corrupt[field] = 9
    else:
        corrupt[field] = "different"
    with sqlite3.connect(store.directory / "candidates.sqlite") as connection:
        connection.execute(
            "UPDATE candidates SET data=? WHERE id=?", (canonical(corrupt).decode(), result["id"])
        )
    before = (store.directory / "candidates.sqlite").read_bytes()
    for operation in (store.lookup_review_rebind, store.rebind_reviewers):
        with pytest.raises(
            CandidateError, match="CANDIDATE_IDENTITY_INVALID|REVIEW_REBIND_RECEIPT_INVALID"
        ):
            operation(binding, command_key="committed")
    assert (store.directory / "candidates.sqlite").read_bytes() == before


def test_concurrent_same_command_commits_exactly_once(registered):
    store, original, binding = source(registered)

    def call(_):
        return CandidateStore(store.directory, existing_only=True).rebind_reviewers(
            binding, command_key="shared"
        )

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(call, range(4)))
    assert all(result == results[0] for result in results)
    assert results[0]["revision"] == original["revision"] + 1
    with sqlite3.connect(store.directory / "candidates.sqlite") as connection:
        assert connection.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 2


def test_concurrent_different_commands_cannot_branch_a_superseded_source(registered):
    store, _, binding = source(registered)

    def call(key):
        try:
            return CandidateStore(store.directory, existing_only=True).rebind_reviewers(
                binding, command_key=key
            )
        except CandidateError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(call, ["branch-a", "branch-b"]))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert "CANDIDATE_SUPERSEDED" in results


def test_commit_succeeds_then_reply_is_lost_exact_lookup_recovers_after_reopen(
    registered, monkeypatch
):
    store, original, binding = source(registered)
    original_connection = store._connection

    @contextmanager
    def lose_reply(*, readonly=False):
        with original_connection(readonly=readonly) as connection:
            yield connection
        if not readonly:
            raise ConnectionResetError("synthetic lost commit reply")

    with monkeypatch.context() as patch:
        patch.setattr(store, "_connection", lose_reply)
        with pytest.raises(ConnectionResetError, match="synthetic lost commit reply"):
            store.rebind_reviewers(binding, command_key="lost-after-commit")
    reopened = CandidateStore(store.directory, existing_only=True)
    recovered = reopened.lookup_review_rebind(binding, command_key="lost-after-commit")
    assert recovered is not None
    assert recovered["revision"] == original["revision"] + 1
    assert reopened.rebind_reviewers(binding, command_key="lost-after-commit") == recovered
    with sqlite3.connect(store.directory / "candidates.sqlite") as connection:
        assert connection.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 2


def test_command_key_cannot_be_reused_in_another_series(registered):
    store, _, binding = source(registered)
    store.rebind_reviewers(binding, command_key="global-key")
    _, baseline, files = registered
    projection, contents, request = inputs(baseline, files)
    request["series_id"] = "other-run/other-task"
    other = store.freeze_projection(projection, contents, request)
    before = (store.directory / "candidates.sqlite").read_bytes()
    for operation in (store.rebind_reviewers, store.lookup_review_rebind):
        with pytest.raises(CandidateError, match="REVIEW_REBIND_IDEMPOTENCY_CONFLICT"):
            operation(binding_for(other), command_key="global-key")
    assert (store.directory / "candidates.sqlite").read_bytes() == before


def test_persisted_binding_is_detached_and_unknown_family_is_not_filled(registered):
    store, _, binding = source(registered)
    binding["reviewer_sources"][0]["reviewer"]["model_family"] = None
    request = copy.deepcopy(binding)
    result = store.rebind_reviewers(binding, command_key="immutable")
    expected = copy.deepcopy(result)
    assert result["request"]["policy"]["review"]["approved_reviewers"][0]["model_family"] is None
    binding["reviewer_sources"][0]["reviewer"]["model_family"] = "invented-later"
    result["review_rebind"]["binding"]["approval_digest"] = "f" * 64
    result["request"]["writer"]["stopped"] = False
    assert store.lookup_review_rebind(request, command_key="immutable") == expected
