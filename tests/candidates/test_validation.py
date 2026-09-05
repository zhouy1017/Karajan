"""Public candidate boundary exercised with real temporary Git repositories."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from karajan.candidates import CandidateError, CandidateStore


def git(directory: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(directory), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def case(tmp_path: Path) -> dict[str, Any]:
    repository = tmp_path / "trusted"
    repository.mkdir()
    git(repository, "init", "-q")
    git(repository, "config", "user.name", "Fixture")
    git(repository, "config", "user.email", "fixture@example.invalid")
    git(repository, "config", "core.autocrlf", "false")
    (repository / "app.py").write_bytes(b"print('base')\n")
    git(repository, "add", "app.py")
    git(repository, "commit", "-qm", "fixture base")
    store = CandidateStore(tmp_path / "state")
    baseline = store.register_baseline(
        repository,
        repository_identity="fixture-repository",
        base_sha=git(repository, "rev-parse", "HEAD"),
    )
    workspace = tmp_path / "worker"
    shutil.copytree(repository, workspace)
    (workspace / "app.py").write_bytes(b"print('candidate')\n")
    request = {
        "series_id": "run-1/task-1",
        "baseline_id": baseline["id"],
        "input_sha256": "a" * 64,
        "allowed_paths": ["app.py"],
        "task_class": "T2",
        "writer": {
            "attempt_id": "author-1",
            "fence": 1,
            "stopped": True,
            "observation_ref": "fixture:stop",
        },
        "authors": [
            {
                "attempt_id": "author-1",
                "fence": 1,
                "profile_id": "fast",
                "profile_revision": 1,
                "model_family": "family-a",
                "context_id": "author-context",
                "provenance_ref": "fixture:author",
            }
        ],
        "policy": {
            "id": "checks",
            "revision": 1,
            "checks": [
                {
                    "id": "tests",
                    "revision": 1,
                    "argv": ["python", "-m", "pytest"],
                    "environment_sha256": "b" * 64,
                }
            ],
            "review": {
                "revision": 1,
                "environment_sha256": "c" * 64,
                "approved_reviewers": [
                    {
                        "profile_id": "fast",
                        "profile_revision": 1,
                        "model_family": "family-a",
                        "qualification_ref": "fixture:reviewer",
                    }
                ],
            },
        },
    }
    return {
        "store": store,
        "repository": repository,
        "workspace": workspace,
        "request": request,
        "baseline": baseline,
    }


def test_freeze_preserves_exact_git_tree_and_survives_restart(
    case: dict[str, Any], tmp_path: Path
) -> None:
    git(case["workspace"], "add", "app.py")
    expected_tree = git(case["workspace"], "write-tree")
    result = case["store"].freeze(case["workspace"], case["request"])
    assert result["tree_sha"] == expected_tree
    assert result["base_sha"] == case["baseline"]["base_sha"]
    assert result["manifest"][0]["path"] == "app.py"
    reopened = CandidateStore(tmp_path / "state")
    assert reopened.get(result["id"]) == result


@pytest.mark.parametrize("change", ["added", "changed", "deleted"])
def test_freeze_rejects_changes_outside_approved_paths(case: dict[str, Any], change: str) -> None:
    case["request"]["allowed_paths"] = ["other/"]
    if change == "deleted":
        (case["workspace"] / "app.py").unlink()
    elif change == "added":
        (case["workspace"] / "app.py").write_bytes(b"print('base')\n")
        (case["workspace"] / "extra.txt").write_text("unauthorized")
    with pytest.raises(CandidateError, match="PATH_OUTSIDE_AUTHORIZATION"):
        case["store"].freeze(case["workspace"], case["request"])


@pytest.mark.parametrize("variant", ["running", "different_attempt", "stale_fence"])
def test_freeze_requires_current_writer_stop_observation(
    case: dict[str, Any], variant: str
) -> None:
    writer = case["request"]["writer"]
    if variant == "running":
        writer["stopped"] = False
    elif variant == "different_attempt":
        writer["attempt_id"] = "old-author"
    else:
        writer["fence"] = 2
    with pytest.raises(CandidateError, match="WRITER_STOP_NOT_CONFIRMED"):
        case["store"].freeze(case["workspace"], case["request"])


def test_collector_rejects_directory_links_without_reading_target(
    case: dict[str, Any], tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("FAKE-OUTSIDE-CANARY")
    link = case["workspace"] / "linked"
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(outside), str(link))
    else:
        link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(CandidateError, match="WORKSPACE_LINK_UNSUPPORTED"):
        case["store"].freeze(case["workspace"], case["request"])


def test_gate_waits_for_all_required_check_and_review_evidence(case: dict[str, Any]) -> None:
    candidate = case["store"].freeze(case["workspace"], case["request"])
    result = case["store"].gate(candidate["id"], current=context(candidate))
    assert result["local_gate_passed"] is False
    assert result["delivery_eligible"] is False
    assert result["reasons"] == ["CHECK_EVIDENCE_MISSING:tests", "REVIEW_EVIDENCE_MISSING"]


def context(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "repository_identity": candidate["repository_identity"],
        "base_sha": candidate["base_sha"],
        "input_sha256": candidate["input_sha256"],
        "policy_sha256": candidate["policy_sha256"],
    }


def check_record(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "evidence_key": "check-1",
        "candidate_id": candidate["id"],
        "policy_sha256": candidate["policy_sha256"],
        "input_sha256": candidate["input_sha256"],
        "environment_sha256": "b" * 64,
        "check_id": "tests",
        "check_revision": 1,
        "executor_ref": "fixture:process-1",
        "exit_code": 0,
        "outcome": "completed",
        "observation_ref": "fixture:exit-1",
        "provenance": "fixture",
    }


def test_successful_process_exit_with_log_satisfies_only_named_check(case: dict[str, Any]) -> None:
    candidate = case["store"].freeze(case["workspace"], case["request"])
    evidence = case["store"].record_check(check_record(candidate), log=b"1 passed\n")
    assert evidence["status"] == "passed"
    gate = case["store"].gate(candidate["id"], current=context(candidate))
    assert gate["reasons"] == ["REVIEW_EVIDENCE_MISSING"]
    assert gate["evidence"][0]["id"] == evidence["id"]


@pytest.mark.parametrize(
    ("exit_code", "outcome", "log", "expected"),
    [
        (1, "completed", b"actual test failure", "failed"),
        (None, "unknown", b"lost process", "inconclusive"),
        (None, "timed_out", b"timeout", "inconclusive"),
        (0, "completed", None, "unavailable"),
        (0, "completed", b"", "unavailable"),
    ],
)
def test_process_failure_or_missing_observation_cannot_pass(
    case: dict[str, Any], exit_code: int | None, outcome: str, log: bytes | None, expected: str
) -> None:
    candidate = case["store"].freeze(case["workspace"], case["request"])
    request = check_record(candidate)
    request.update(exit_code=exit_code, outcome=outcome)
    evidence = case["store"].record_check(request, log=log)
    assert evidence["status"] == expected
    assert (
        "CHECK_NOT_PASSED:tests"
        in case["store"].gate(candidate["id"], current=context(candidate))["reasons"]
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("policy_sha256", "d" * 64),
        ("input_sha256", "d" * 64),
        ("environment_sha256", "d" * 64),
        ("check_revision", 2),
        ("check_id", "removed-tests"),
    ],
)
def test_check_evidence_with_different_frozen_inputs_is_invalidated(
    case: dict[str, Any], field: str, value: str | int
) -> None:
    candidate = case["store"].freeze(case["workspace"], case["request"])
    request = check_record(candidate)
    request[field] = value
    result = case["store"].record_check(request, log=b"apparently passed")
    assert result["status"] == "invalidated"
    assert result["reasons"] == ["EVIDENCE_BINDING_MISMATCH"]


def review_record(candidate: dict[str, Any], checks: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "evidence_key": "review-1",
        "candidate_id": candidate["id"],
        "policy_sha256": candidate["policy_sha256"],
        "input_sha256": candidate["input_sha256"],
        "environment_sha256": "c" * 64,
        "review_revision": 1,
        "check_evidence_ids": [check["id"] for check in checks],
        "actor": {
            "attempt_id": "review-1",
            "fence": 1,
            "profile_id": "fast",
            "profile_revision": 1,
            "model_family": "family-a",
            "context_id": "fresh-context",
            "provenance_ref": "fixture:fresh-runtime",
        },
        "author_reasoning_included": False,
        "verdict": "passed",
        "findings": [],
        "observation_ref": "fixture:review-result",
        "provenance": "fixture",
    }


def validated(case: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    candidate = case["store"].freeze(case["workspace"], case["request"])
    check = case["store"].record_check(check_record(candidate), log=b"1 passed\n")
    review = case["store"].record_review(
        review_record(candidate, [check]), log=b"fixture structured review\n"
    )
    return candidate, check, review


def test_same_profile_with_independent_attempt_and_context_can_review_t2(
    case: dict[str, Any],
) -> None:
    candidate, _, review = validated(case)
    assert review["status"] == "passed"
    result = case["store"].gate(candidate["id"], current=context(candidate))
    assert result["local_gate_passed"] is True
    assert result["delivery_eligible"] is False
    assert result["live_qualification"] == "not_run"


@pytest.mark.parametrize(
    "variant",
    [
        "author_attempt",
        "author_context",
        "author_reasoning",
        "unapproved_profile",
        "unapproved_revision",
        "family_conflict",
    ],
)
def test_reviewer_independence_uses_fixed_qualified_identity(
    case: dict[str, Any], variant: str
) -> None:
    candidate = case["store"].freeze(case["workspace"], case["request"])
    check = case["store"].record_check(check_record(candidate), log=b"passed")
    request = review_record(candidate, [check])
    if variant == "author_attempt":
        request["actor"]["attempt_id"] = "author-1"
    elif variant == "author_context":
        request["actor"]["context_id"] = "author-context"
    elif variant == "author_reasoning":
        request["author_reasoning_included"] = True
    elif variant == "unapproved_profile":
        request["actor"]["profile_id"] = "invented"
    elif variant == "unapproved_revision":
        request["actor"]["profile_revision"] = 2
    else:
        request["actor"]["model_family"] = "self-reported-new-family"
    evidence = case["store"].record_review(request, log=b"claims pass")
    assert evidence["status"] == "invalidated"
    assert "REVIEWER_NOT_INDEPENDENT_OR_QUALIFIED" in evidence["reasons"]


@pytest.mark.parametrize("family", ["family-a", None])
def test_t3_requires_known_reviewer_family_different_from_every_author(
    case: dict[str, Any], family: str | None
) -> None:
    case["request"]["task_class"] = "T3"
    case["request"]["policy"]["review"]["approved_reviewers"][0]["model_family"] = family
    candidate = case["store"].freeze(case["workspace"], case["request"])
    check = case["store"].record_check(check_record(candidate), log=b"passed")
    request = review_record(candidate, [check])
    request["actor"]["model_family"] = family
    evidence = case["store"].record_review(request, log=b"claims pass")
    assert evidence["status"] == "invalidated"
    assert "T3_FAMILY_INDEPENDENCE_UNPROVEN" in evidence["reasons"]


@pytest.mark.parametrize("variant", ["failed", "inconclusive", "blocking_finding", "missing_log"])
def test_review_conclusion_cannot_override_failure_or_missing_evidence(
    case: dict[str, Any], variant: str
) -> None:
    candidate = case["store"].freeze(case["workspace"], case["request"])
    check = case["store"].record_check(check_record(candidate), log=b"passed")
    request = review_record(candidate, [check])
    expected = "failed"
    log: bytes | None = b"review result"
    if variant in {"failed", "inconclusive"}:
        request["verdict"] = variant
        expected = variant
    elif variant == "missing_log":
        log = None
        expected = "unavailable"
    else:
        request["findings"] = [
            {
                "severity": "high",
                "file": "app.py",
                "line": 1,
                "behavior": "wrong result",
                "trigger": "normal input",
                "acceptance_ref": "AC1",
                "blocking": True,
            }
        ]
    result = case["store"].record_review(request, log=log)
    assert result["status"] == expected
    assert (
        "REVIEW_NOT_PASSED"
        in case["store"].gate(candidate["id"], current=context(candidate))["reasons"]
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("base_sha", "d" * 40),
        ("input_sha256", "d" * 64),
        ("policy_sha256", "d" * 64),
        ("repository_identity", "other-repo"),
    ],
)
def test_current_validation_context_changes_invalidate_all_old_evidence(
    case: dict[str, Any], field: str, value: str
) -> None:
    candidate, _, _ = validated(case)
    current = context(candidate)
    current[field] = value
    result = case["store"].gate(candidate["id"], current=current)
    assert result["local_gate_passed"] is False
    assert "CURRENT_CONTEXT_CHANGED" in result["reasons"]
    assert {item["effective_status"] for item in result["evidence"]} == {"invalidated"}


def test_changing_workspace_creates_new_candidate_and_invalidates_old_gate(
    case: dict[str, Any],
) -> None:
    old, _, _ = validated(case)
    (case["workspace"] / "app.py").write_bytes(b"print('new candidate')\n")
    new = case["store"].freeze(case["workspace"], case["request"])
    assert new["revision"] == old["revision"] + 1
    assert new["content_sha256"] != old["content_sha256"]
    assert case["store"].get(old["id"]) == old
    previous_gate = case["store"].gate(old["id"], current=context(old))
    assert "CANDIDATE_SUPERSEDED" in previous_gate["reasons"]
    assert previous_gate["local_gate_passed"] is False
    assert case["store"].gate(new["id"], current=context(new))["reasons"] == [
        "CHECK_EVIDENCE_MISSING:tests",
        "REVIEW_EVIDENCE_MISSING",
    ]


@pytest.mark.parametrize("artifact", ["candidate", "check", "review"])
@pytest.mark.parametrize("failure", ["missing", "corrupt"])
def test_gate_rechecks_saved_artifact_integrity(
    case: dict[str, Any], artifact: str, failure: str
) -> None:
    candidate, check, review = validated(case)
    ref = (
        candidate["manifest"][0]["artifact"]
        if artifact == "candidate"
        else (check if artifact == "check" else review)["log"]
    )
    path = Path(ref["path"])
    if failure == "missing":
        path.unlink()
    else:
        path.write_bytes(b"corrupted evidence")
    result = case["store"].gate(candidate["id"], current=context(candidate))
    assert result["local_gate_passed"] is False
    assert "ARTIFACT_UNAVAILABLE" in result["reasons"]


def test_new_check_execution_invalidates_review_of_previous_check_results(
    case: dict[str, Any],
) -> None:
    candidate, _, _ = validated(case)
    another = check_record(candidate)
    another["evidence_key"] = "check-2"
    case["store"].record_check(another, log=b"second test execution passed")
    result = case["store"].gate(candidate["id"], current=context(candidate))
    assert result["local_gate_passed"] is False
    assert "REVIEW_CHECK_SET_CHANGED" in result["reasons"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("policy_sha256", "d" * 64),
        ("input_sha256", "d" * 64),
        ("environment_sha256", "d" * 64),
        ("review_revision", 2),
        ("check_evidence_ids", []),
    ],
)
def test_review_requires_exact_policy_environment_and_prior_passing_checks(
    case: dict[str, Any], field: str, value: Any
) -> None:
    candidate = case["store"].freeze(case["workspace"], case["request"])
    check = case["store"].record_check(check_record(candidate), log=b"passed")
    request = review_record(candidate, [check])
    request[field] = value
    result = case["store"].record_review(request, log=b"claims pass")
    assert result["status"] == "invalidated"
    assert "EVIDENCE_BINDING_MISMATCH" in result["reasons"]


@pytest.mark.parametrize("kind", ["check", "review"])
def test_repeated_evidence_receipt_replays_exact_result_and_rejects_changed_payload(
    case: dict[str, Any], kind: str
) -> None:
    candidate, check, review = validated(case)
    if kind == "check":
        method = case["store"].record_check
        request, log, expected = check_record(candidate), b"1 passed\n", check
    else:
        method = case["store"].record_review
        request, log, expected = (
            review_record(candidate, [check]),
            b"fixture structured review\n",
            review,
        )
    assert method(request, log=log) == expected
    with pytest.raises(CandidateError, match="EVIDENCE_KEY_CONFLICT"):
        method(request, log=b"different result under same receipt")


def test_repeated_freeze_of_unchanged_input_does_not_invalidate_validated_candidate(
    case: dict[str, Any],
) -> None:
    candidate, _, _ = validated(case)
    assert case["store"].freeze(case["workspace"], case["request"]) == candidate
    assert (
        case["store"].gate(candidate["id"], current=context(candidate))["local_gate_passed"] is True
    )


def test_content_and_manifest_hashes_are_portable_between_control_directories(
    case: dict[str, Any], tmp_path: Path
) -> None:
    candidate = case["store"].freeze(case["workspace"], case["request"])
    other = CandidateStore(tmp_path / "other-state")
    baseline = other.register_baseline(
        case["repository"], repository_identity="fixture-repository", base_sha=candidate["base_sha"]
    )
    second_request = dict(case["request"], baseline_id=baseline["id"])
    second = other.freeze(case["workspace"], second_request)
    assert second["content_sha256"] == candidate["content_sha256"]
    assert second["manifest_sha256"] == candidate["manifest_sha256"]
    assert baseline["id"] == case["baseline"]["id"]


def test_collector_does_not_load_worker_git_config_or_run_hooks(
    case: dict[str, Any], tmp_path: Path
) -> None:
    canary = tmp_path / "hook-executed"
    hook = case["workspace"] / ".git" / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\necho bad > '{canary.as_posix()}'\n")
    hook.chmod(0o755)
    (case["workspace"] / ".git" / "config").write_bytes(
        b"INVALID GIT CONFIG WITH FILTER COMMANDS\x00"
    )
    candidate = case["store"].freeze(case["workspace"], case["request"])
    assert candidate["changed_paths"] == ["app.py"]
    assert not canary.exists()


def test_workspace_containing_control_storage_is_rejected(
    case: dict[str, Any], tmp_path: Path
) -> None:
    with pytest.raises(CandidateError, match="CONTROL_STORAGE_OVERLAP"):
        case["store"].freeze(tmp_path, case["request"])


@pytest.mark.parametrize(
    "path", ["../app.py", "/app.py", "app.py\x00", "a//b", "./", ".git/", "x/../app.py"]
)
def test_authorized_paths_require_unambiguous_repository_relative_form(
    case: dict[str, Any], path: str
) -> None:
    case["request"]["allowed_paths"] = ["app.py", path]
    with pytest.raises(CandidateError, match="PATH_INVALID"):
        case["store"].freeze(case["workspace"], case["request"])


def test_duplicate_check_names_cannot_satisfy_two_policy_obligations(case: dict[str, Any]) -> None:
    case["request"]["policy"]["checks"].append(dict(case["request"]["policy"]["checks"][0]))
    with pytest.raises(CandidateError, match="POLICY_IDENTITY_AMBIGUOUS"):
        case["store"].freeze(case["workspace"], case["request"])


def test_collector_enforces_bounded_file_size_before_acceptance(case: dict[str, Any]) -> None:
    (case["workspace"] / "app.py").write_bytes(b"x" * (8 * 1024 * 1024 + 1))
    with pytest.raises(CandidateError, match="SNAPSHOT_LIMIT_EXCEEDED"):
        case["store"].freeze(case["workspace"], case["request"])


def test_materialize_exports_exact_frozen_bytes_for_independent_checks(
    case: dict[str, Any], tmp_path: Path
) -> None:
    candidate = case["store"].freeze(case["workspace"], case["request"])
    (case["workspace"] / "app.py").write_bytes(b"changed after freeze")
    destination = tmp_path / "check-workspace"
    result = case["store"].materialize(candidate["id"], destination)
    assert result["content_sha256"] == candidate["content_sha256"]
    assert (destination / "app.py").read_bytes() == b"print('candidate')\n"
    assert not (destination / ".git").exists()
    (destination / "scratch.txt").write_text("check scratch")
    assert case["store"].get(candidate["id"]) == candidate


def test_missing_workspace_returns_bounded_domain_error(
    case: dict[str, Any], tmp_path: Path
) -> None:
    with pytest.raises(CandidateError, match="WORKSPACE_UNAVAILABLE"):
        case["store"].freeze(tmp_path / "does-not-exist", case["request"])


def test_workspace_root_link_is_not_followed(case: dict[str, Any], tmp_path: Path) -> None:
    link = tmp_path / "linked-workspace"
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(case["workspace"]), str(link))
    else:
        link.symlink_to(case["workspace"], target_is_directory=True)
    with pytest.raises(CandidateError, match="WORKSPACE_LINK_UNSUPPORTED"):
        case["store"].freeze(link, case["request"])


def test_freeze_does_not_publish_reference_to_already_corrupt_content(case: dict[str, Any]) -> None:
    candidate = case["store"].freeze(case["workspace"], case["request"])
    Path(candidate["manifest"][0]["artifact"]["path"]).write_bytes(b"corrupt")
    with pytest.raises(CandidateError, match="ARTIFACT_UNAVAILABLE"):
        case["store"].freeze(case["workspace"], case["request"])


def test_known_other_family_passes_t3_with_independent_context(case: dict[str, Any]) -> None:
    case["request"]["task_class"] = "T3"
    case["request"]["policy"]["review"]["approved_reviewers"][0]["model_family"] = "family-b"
    candidate = case["store"].freeze(case["workspace"], case["request"])
    check = case["store"].record_check(check_record(candidate), log=b"passed")
    request = review_record(candidate, [check])
    request["actor"]["model_family"] = "family-b"
    assert case["store"].record_review(request, log=b"fixture review")["status"] == "passed"
    assert (
        case["store"].gate(candidate["id"], current=context(candidate))["local_gate_passed"] is True
    )


def test_unqualified_reviewer_waits_even_with_successful_checks(case: dict[str, Any]) -> None:
    case["request"]["policy"]["review"]["approved_reviewers"] = []
    candidate, _, review = validated(case)
    assert review["status"] == "invalidated"
    assert (
        case["store"].gate(candidate["id"], current=context(candidate))["local_gate_passed"]
        is False
    )


def test_real_process_probe_reports_failure_and_fixture_boundaries(tmp_path: Path) -> None:
    script = Path(__file__).resolve().parents[2] / "examples/candidates/probe_validation.py"
    result = subprocess.run(
        [sys.executable, str(script), "--directory", str(tmp_path / "probe")],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert all(report["conditions"].values())
    assert report["passing_check"]["input"]["exit_code"] == 0
    assert report["failing_check"]["input"]["exit_code"] == 1
    assert report["model_calls"] == report["cash_api_calls"] == 0
    assert report["live_qualification"] == "not_run"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits require a real POSIX filesystem")
@pytest.mark.parametrize("variant", ["new_executable", "add_execute_bit", "remove_execute_bit"])
def test_posix_executable_modes_match_git_tree_and_materialized_snapshot(
    case: dict[str, Any], tmp_path: Path, variant: str
) -> None:
    workspace = case["workspace"]
    (workspace / "app.py").write_bytes(b"print('base')\n")
    changed_path = "app.py"
    expected_mode = "100755"
    if variant == "new_executable":
        changed_path = "script.sh"
        (workspace / changed_path).write_bytes(b"#!/bin/sh\nexit 0\n")
        (workspace / changed_path).chmod(0o755)
        case["request"]["allowed_paths"].append(changed_path)
    elif variant == "add_execute_bit":
        (workspace / changed_path).chmod(0o755)
    else:
        (case["repository"] / changed_path).chmod(0o755)
        git(case["repository"], "add", changed_path)
        git(case["repository"], "commit", "-qm", "executable fixture baseline")
        baseline = case["store"].register_baseline(
            case["repository"],
            repository_identity="fixture-repository",
            base_sha=git(case["repository"], "rev-parse", "HEAD"),
        )
        case["request"]["baseline_id"] = baseline["id"]
        (workspace / changed_path).chmod(0o644)
        expected_mode = "100644"
    git(workspace, "add", "-A")
    expected_tree = git(workspace, "write-tree")
    candidate = case["store"].freeze(workspace, case["request"])
    assert candidate["tree_sha"] == expected_tree
    assert candidate["changed_paths"] == [changed_path]
    assert (
        next(entry for entry in candidate["manifest"] if entry["path"] == changed_path)["mode"]
        == expected_mode
    )
    exported = tmp_path / "executable-export"
    case["store"].materialize(candidate["id"], exported)
    assert bool((exported / changed_path).stat().st_mode & 0o100) == (expected_mode == "100755")
