"""Contract tests for the read-only Reviewer input compiler."""

from __future__ import annotations

import hashlib
import importlib
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from karajan.candidates import CandidateError, CandidateStore
from karajan.orchestration.admission import ApprovedTaskAdmission
from karajan.orchestration.reviewer_input import (
    MAX_INPUT_BYTES,
    ReviewerInput,
    _compile,
    compile_reviewer_input,
)
from karajan.routing.compiler import digest
from karajan.runs import RunError


def git(directory: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(directory), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _request(
    baseline_id: str, *, checks: int = 1, allowed_paths: list[str] | None = None
) -> dict[str, Any]:
    check_rows = [
        {
            "id": f"check-{index}",
            "revision": 1,
            "argv": ["python", "-m", "pytest"],
            "environment_sha256": str(index) * 64,
        }
        for index in range(1, checks + 1)
    ]
    return {
        "series_id": "run-1/task-1",
        "baseline_id": baseline_id,
        "input_sha256": "a" * 64,
        "allowed_paths": allowed_paths or ["app.py"],
        "task_class": "T1",
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
                "profile_id": "worker",
                "profile_revision": 1,
                "model_family": "family-a",
                "context_id": "author-context",
                "provenance_ref": "fixture:author",
            }
        ],
        "policy": {
            "id": "checks",
            "revision": 1,
            "checks": check_rows,
            "review": {
                "revision": 1,
                "environment_sha256": "b" * 64,
                "approved_reviewers": [],
            },
        },
    }


def _identity(candidate: dict[str, Any]) -> dict[str, Any]:
    fields = (
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
        "request_sha256",
    )
    return {
        **{key: candidate[key] for key in fields if key != "request_sha256"},
        "baseline_id": candidate["request"]["baseline_id"],
        "request_sha256": digest(candidate["request"]),
    }


def _subject(candidate: dict[str, Any]) -> dict[str, Any]:
    identity = _identity(candidate)
    return {
        "schema_version": "karajan.candidate-validation-subject.v1",
        "revision": 1,
        "source_capture_digest": "c" * 64,
        "source_candidate": identity,
        "candidate": identity,
        "approval_digest": "d" * 64,
        "plan_digest": "e" * 64,
        "execution_policy_digest": "f" * 64,
    }


def _check(candidate: dict[str, Any], index: int) -> dict[str, Any]:
    check_id = f"check-{index}"
    return {
        "evidence_key": f"evidence-{index}-{candidate['id']}",
        "candidate_id": candidate["id"],
        "policy_sha256": candidate["policy_sha256"],
        "input_sha256": candidate["input_sha256"],
        "environment_sha256": str(index) * 64,
        "check_id": check_id,
        "check_revision": 1,
        "executor_ref": f"fixture:executor-{index}",
        "exit_code": 0,
        "outcome": "completed",
        "observation_ref": f"fixture:observation-{index}",
        "provenance": "fixture",
    }


@pytest.fixture
def case(tmp_path: Path) -> dict[str, Any]:
    repository = tmp_path / "trusted"
    repository.mkdir()
    git(repository, "init", "-q")
    git(repository, "config", "user.name", "Fixture")
    git(repository, "config", "user.email", "fixture@example.invalid")
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
    request = _request(baseline["id"])
    candidate = store.freeze(workspace, request)
    evidence = store.record_check(_check(candidate, 1), log=b"1 passed\n")
    return {
        "store": store,
        "candidate": candidate,
        "evidence": evidence,
        "request": request,
        "workspace": workspace,
    }


def compile_case(case: dict[str, Any], *, evidence_ids: list[str] | None = None) -> ReviewerInput:
    return _compile(
        case["store"],
        subject=_subject(case["candidate"]),
        requirement={"goal": "Review the candidate", "acceptance": ["all checks pass"]},
        final_check_evidence_ids=(
            [case["evidence"]["id"]] if evidence_ids is None else evidence_ids
        ),
    )


def _trusted_admission(
    case: dict[str, Any], monkeypatch: pytest.MonkeyPatch, *, source_requirement: dict[str, Any]
) -> tuple[Any, dict[str, Any]]:
    compiler = importlib.import_module("karajan.orchestration.reviewer_input")

    run = {
        "id": "run-1",
        "owner": "principal",
        "schema_version": "karajan.run-planning.v2",
        "state": "executing",
        "requirement": {"goal": "Review", "acceptance": ["checks"]},
    }
    execution_policy = {
        "schema_version": "karajan.execution-policy.v2",
        "validation": {
            "id": "checks",
            "revision": 1,
            "checks": [
                {
                    "id": "check-1",
                    "revision": 1,
                    "argv": ["python", "-m", "pytest"],
                    "environment_ref": {"id": "env-check", "revision": 1},
                }
            ],
            "environments": [
                {"id": "env-check", "revision": 1, "source_sha256": "1" * 64},
                {"id": "env-review", "revision": 1, "source_sha256": "b" * 64},
            ],
            "review": {
                "revision": 1,
                "environment_ref": {"id": "env-review", "revision": 1},
            },
        },
    }
    plan = {"plan": {"authorization": {"checks": ["check-1"]}}}
    operation: dict[str, Any] = {
        "id": "operation-1",
        "run_id": "run-1",
        "task_id": "task-1",
        "state": "reserved",
        "workspace": {
            "input_sha256": case["candidate"]["input_sha256"],
            "read_paths": ["app.py"],
            "source_binding": {
                "requirement": source_requirement,
                "execution_policy": execution_policy,
                "plan": plan,
            },
        },
    }
    planner = SimpleNamespace(get=lambda run_id, *, principal: run)
    admissions = SimpleNamespace(routing=SimpleNamespace(planner=planner))
    monkeypatch.setattr(
        compiler.GoExecutionIntents,
        "read_operation",
        staticmethod(lambda *_args, **_kwargs: operation),
    )
    monkeypatch.setattr(compiler, "_approved_task", lambda *_args: ({}, {}))
    monkeypatch.setattr(
        compiler,
        "current_subject",
        lambda *_args: {
            "subject": _subject(case["candidate"]),
            "candidate": case["candidate"],
            "capture_candidate": case["candidate"],
        },
    )
    return admissions, run


def test_public_compiler_reads_current_trusted_records(
    case: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    run_requirement = {"goal": "Review", "acceptance": ["checks"]}
    admissions, run = _trusted_admission(case, monkeypatch, source_requirement=run_requirement)
    result = compile_reviewer_input(
        admissions,
        case["store"],
        run_id=run["id"],
        operation_id="operation-1",
        principal="principal",
        final_check_evidence_ids=[case["evidence"]["id"]],
    )
    assert json.loads(result.content)["requirement"] == run_requirement


def test_public_compiler_rejects_stale_operation_requirement(
    case: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    admissions, run = _trusted_admission(
        case,
        monkeypatch,
        source_requirement={"goal": "old", "acceptance": ["checks"]},
    )
    with pytest.raises(RunError, match="REVIEWER_INPUT_APPROVAL_CHANGED"):
        compile_reviewer_input(
            admissions,
            case["store"],
            run_id=run["id"],
            operation_id="operation-1",
            principal="principal",
            final_check_evidence_ids=[case["evidence"]["id"]],
        )


def test_public_compiler_rejects_evidence_with_foreign_candidate_id(
    case: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    admissions, run = _trusted_admission(
        case,
        monkeypatch,
        source_requirement={"goal": "Review", "acceptance": ["checks"]},
    )
    database = case["store"].directory / "candidates.sqlite"
    with sqlite3.connect(database) as db:
        row = db.execute(
            "SELECT data FROM evidence WHERE id=?", (case["evidence"]["id"],)
        ).fetchone()
        assert row is not None
        evidence = json.loads(row[0])
        evidence["input"]["candidate_id"] = "foreign-candidate"
        db.execute(
            "UPDATE evidence SET data=? WHERE id=?",
            (json.dumps(evidence), case["evidence"]["id"]),
        )
    with pytest.raises(RunError, match="REVIEWER_INPUT_CHECKS_INCOMPLETE"):
        compile_reviewer_input(
            admissions,
            case["store"],
            run_id=run["id"],
            operation_id="operation-1",
            principal="principal",
            final_check_evidence_ids=[case["evidence"]["id"]],
        )


def test_public_compiler_rejects_persisted_subject_revision_drift(
    case: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The public path reloads the operation and rejects an old persisted subject."""
    requirement = {"goal": "Review", "acceptance": ["checks"]}
    approval = {"id": "approval-1", "revision": 1}
    plan = {
        "plan_digest": "e" * 64,
        "plan": {"authorization": {"checks": ["check-1"]}},
    }
    execution_policy = {
        "schema_version": "karajan.execution-policy.v2",
        "digest": "f" * 64,
        "validation": {
            "id": "checks",
            "revision": 1,
            "checks": [
                {
                    "id": "check-1",
                    "revision": 1,
                    "argv": ["python", "-m", "pytest"],
                    "environment_ref": {"id": "env-check", "revision": 1},
                }
            ],
            "environments": [
                {"id": "env-check", "revision": 1, "source_sha256": "1" * 64},
                {"id": "env-review", "revision": 1, "source_sha256": "b" * 64},
            ],
            "review": {
                "revision": 1,
                "environment_ref": {"id": "env-review", "revision": 1},
            },
        },
    }
    baseline_entry = case["store"].get_baseline(case["candidate"]["request"]["baseline_id"])[
        "manifest"
    ][0]
    candidate_entry = case["candidate"]["manifest"][0]
    capture = {
        "freeze_request": case["candidate"]["request"],
        "projection": [
            {
                "path": "app.py",
                "sha256": baseline_entry["artifact"]["sha256"],
                "writable": True,
            }
        ],
        "captured_files": [
            {
                "path": "app.py",
                "sha256": candidate_entry["artifact"]["sha256"],
                "size": candidate_entry["artifact"]["size"],
            }
        ],
    }
    source = {
        "requirement": requirement,
        "approval": approval,
        "plan": plan,
        "execution_policy": execution_policy,
    }
    subject = {
        "schema_version": "karajan.candidate-validation-subject.v1",
        "revision": 1,
        "source_capture_digest": digest(capture),
        "source_candidate": _identity(case["candidate"]),
        "candidate": _identity(case["candidate"]),
        "approval_digest": digest(approval),
        "plan_digest": plan["plan_digest"],
        "execution_policy_digest": execution_policy["digest"],
    }
    operation = {
        "schema_version": "karajan.approved-task-admission.v1",
        "id": "operation-1",
        "run_id": "run-1",
        "task_id": "task-1",
        "state": "reserved",
        "assessment": {"sources": {"approval": approval}},
        "workspace": {
            "input_sha256": case["candidate"]["input_sha256"],
            "read_paths": ["app.py"],
            "source_binding": source,
        },
        "execution": {
            "collection": {
                "capture": capture,
                "capture_digest": digest(capture),
                "candidate": {
                    key: case["candidate"][key]
                    for key in (
                        "id",
                        "series_id",
                        "revision",
                        "content_sha256",
                        "manifest_sha256",
                        "input_sha256",
                        "policy_sha256",
                    )
                },
            }
        },
        "validation": {"subject": subject},
    }
    run = {
        "schema_version": "karajan.run-planning.v2",
        "id": "run-1",
        "owner": "principal",
        "state": "executing",
        "requirement": requirement,
    }
    runs_db = tmp_path / "runs.sqlite"
    with sqlite3.connect(runs_db) as db:
        db.execute("CREATE TABLE runs (id TEXT PRIMARY KEY, snapshot TEXT NOT NULL)")
        db.execute("INSERT INTO runs VALUES (?, ?)", (run["id"], json.dumps(run)))

    class PersistedPlanner:
        database = runs_db
        projects = SimpleNamespace(database=tmp_path / "projects.sqlite")

        @staticmethod
        def _get(db: sqlite3.Connection, run_id: str) -> dict[str, Any]:
            row = db.execute("SELECT snapshot FROM runs WHERE id=?", (run_id,)).fetchone()
            assert row is not None
            snapshot = json.loads(row[0])
            assert isinstance(snapshot, dict)
            return snapshot

        @staticmethod
        def _owner(snapshot: dict[str, Any], principal: str) -> None:
            if snapshot["owner"] != principal:
                raise RunError("RUN_NOT_FOUND")

        def get(self, run_id: str, *, principal: str) -> dict[str, Any]:
            with sqlite3.connect(self.database) as db:
                snapshot = self._get(db, run_id)
            self._owner(snapshot, principal)
            return snapshot

    routing = SimpleNamespace(
        planner=PersistedPlanner(), capacity=SimpleNamespace(path=tmp_path / "capacity.sqlite")
    )
    admissions = ApprovedTaskAdmission(tmp_path / "admission.sqlite", cast(Any, routing))
    with admissions._transaction() as db:
        admissions._save(db, operation)
    compiler = importlib.import_module("karajan.orchestration.reviewer_input")
    monkeypatch.setattr(compiler, "_approved_task", lambda *_args: ({}, {}))

    result = compile_reviewer_input(
        admissions,
        case["store"],
        run_id="run-1",
        operation_id="operation-1",
        principal="principal",
        final_check_evidence_ids=[case["evidence"]["id"]],
    )
    assert result.candidate_id == case["candidate"]["id"]

    validation = cast(dict[str, Any], operation["validation"])
    persisted_subject = cast(dict[str, Any], validation["subject"])
    persisted_candidate = cast(dict[str, Any], persisted_subject["candidate"])
    persisted_candidate["revision"] = 99
    with admissions._transaction() as db:
        admissions._save(db, operation)
    with pytest.raises(RunError, match="REVIEW_SUBJECT_IDENTITY_INVALID"):
        compile_reviewer_input(
            admissions,
            case["store"],
            run_id="run-1",
            operation_id="operation-1",
            principal="principal",
            final_check_evidence_ids=[case["evidence"]["id"]],
        )


def test_compiles_verified_cas_and_does_not_require_overall_review_gate(
    case: dict[str, Any],
) -> None:
    result = compile_case(case)
    document = json.loads(result.content)

    assert result.content_sha256 == hashlib.sha256(result.content).hexdigest()
    assert result.size == len(result.content)
    assert result.allowed_files == ("app.py",)
    assert document["schema_version"] == "karajan.reviewer-input.v1"
    assert document["candidate"] == _identity(case["candidate"])
    assert document["files"] == [
        {"path": "app.py", "mode": "100644", "content": "print('candidate')\n"}
    ]
    assert "baseline/app.py" in document["diff"]
    assert document["checks"][0]["evidence_id"] == case["evidence"]["id"]
    assert document["checks"][0]["log"] == {
        "sha256": hashlib.sha256(b"1 passed\n").hexdigest(),
        "size": len(b"1 passed\n"),
    }
    assert document["checks"][0]["argv"] == ["python", "-m", "pytest"]
    assert document["checks"][0]["environment_sha256"] == "1" * 64


def test_caller_workspace_is_ignored_after_freeze(case: dict[str, Any]) -> None:
    (case["workspace"] / "app.py").write_text("caller mutation\n", encoding="utf-8")
    result = compile_case(case)
    document = json.loads(result.content)

    assert document["diff"]
    assert document["files"][0]["content"] == "print('candidate')\n"


def test_empty_diff_is_supported(case: dict[str, Any]) -> None:
    case["workspace"].joinpath("app.py").write_bytes(b"print('base')\n")
    unchanged = case["store"].freeze(case["workspace"], case["request"])
    evidence = case["store"].record_check(
        {**_check(unchanged, 1), "evidence_key": f"same-{unchanged['id']}"}, log=b"same\n"
    )
    result = _compile(
        case["store"],
        subject=_subject(unchanged),
        requirement={"goal": "Review", "acceptance": ["checks"]},
        final_check_evidence_ids=[evidence["id"]],
    )
    assert json.loads(result.content)["diff"] == ""


def test_check_ids_are_set_identity_but_output_follows_policy_order(case: dict[str, Any]) -> None:
    request = _request(case["candidate"]["request"]["baseline_id"], checks=2)
    workspace = case["workspace"]
    candidate = case["store"].freeze(workspace, request)
    first = case["store"].record_check(_check(candidate, 1), log=b"first\n")
    second = case["store"].record_check(_check(candidate, 2), log=b"second\n")
    result = _compile(
        case["store"],
        subject=_subject(candidate),
        requirement={"goal": "Review", "acceptance": ["checks"]},
        final_check_evidence_ids=frozenset({second["id"], first["id"]}),
    )

    document = json.loads(result.content)
    assert [row["check_id"] for row in document["checks"]] == ["check-1", "check-2"]
    assert result.check_evidence_ids == (first["id"], second["id"])


@pytest.mark.parametrize("bad_requirement", [{"goal": ""}, {"goal": "x", "acceptance": []}])
def test_rejects_invalid_approved_requirement(
    case: dict[str, Any], bad_requirement: dict[str, Any]
) -> None:
    with pytest.raises(RunError, match="REVIEWER_INPUT_REQUIREMENT_INVALID"):
        _compile(
            case["store"],
            subject=_subject(case["candidate"]),
            requirement=bad_requirement,
            final_check_evidence_ids=[case["evidence"]["id"]],
        )


@pytest.mark.parametrize("variant", ["missing", "extra"])
def test_rejects_incomplete_or_extra_final_checks(case: dict[str, Any], variant: str) -> None:
    evidence_ids = [] if variant == "missing" else [case["evidence"]["id"], "unknown"]
    with pytest.raises(RunError, match="REVIEWER_INPUT_CHECKS_"):
        compile_case(case, evidence_ids=evidence_ids)


def test_rejects_failed_final_check(case: dict[str, Any]) -> None:
    failed = case["store"].record_check(
        {**_check(case["candidate"], 1), "evidence_key": "failed-evidence", "exit_code": 1},
        log=b"failure\n",
    )
    with pytest.raises(RunError, match="REVIEWER_INPUT_CHECKS_INCOMPLETE"):
        compile_case(case, evidence_ids=[failed["id"]])


def test_rejects_non_t1_candidate_scope(case: dict[str, Any]) -> None:
    request = _request(case["candidate"]["request"]["baseline_id"])
    request["task_class"] = "T2"
    candidate = case["store"].freeze(case["workspace"], request)
    evidence = case["store"].record_check(_check(candidate, 1), log=b"ok\n")
    with pytest.raises(RunError, match="REVIEWER_INPUT_SCOPE_UNSUPPORTED"):
        _compile(
            case["store"],
            subject=_subject(candidate),
            requirement={"goal": "Review", "acceptance": ["checks"]},
            final_check_evidence_ids=[evidence["id"]],
        )


def test_rejects_added_file_and_binary_file(case: dict[str, Any], tmp_path: Path) -> None:
    baseline_id = case["candidate"]["request"]["baseline_id"]
    added_workspace = tmp_path / "added"
    shutil.copytree(case["workspace"], added_workspace)
    (added_workspace / "new.txt").write_text("new", encoding="utf-8")
    added = case["store"].freeze(
        added_workspace, _request(baseline_id, allowed_paths=["app.py", "new.txt"])
    )
    added_check = case["store"].record_check(_check(added, 1), log=b"ok\n")
    with pytest.raises(RunError, match="REVIEWER_INPUT_UNSUPPORTED_FILE"):
        _compile(
            case["store"],
            subject=_subject(added),
            requirement={"goal": "Review", "acceptance": ["checks"]},
            final_check_evidence_ids=[added_check["id"]],
        )

    binary_workspace = tmp_path / "binary"
    shutil.copytree(case["workspace"], binary_workspace)
    (binary_workspace / "app.py").write_bytes(b"\x00binary")
    binary = case["store"].freeze(binary_workspace, _request(baseline_id))
    binary_check = case["store"].record_check(_check(binary, 1), log=b"ok\n")
    with pytest.raises(RunError, match="REVIEWER_INPUT_UNSUPPORTED_FILE"):
        _compile(
            case["store"],
            subject=_subject(binary),
            requirement={"goal": "Review", "acceptance": ["checks"]},
            final_check_evidence_ids=[binary_check["id"]],
        )


def test_rejects_input_over_budget_without_truncation(case: dict[str, Any], tmp_path: Path) -> None:
    oversized = tmp_path / "oversized"
    shutil.copytree(case["workspace"], oversized)
    (oversized / "app.py").write_text("x" * MAX_INPUT_BYTES, encoding="utf-8")
    candidate = case["store"].freeze(
        oversized, _request(case["candidate"]["request"]["baseline_id"])
    )
    evidence = case["store"].record_check(_check(candidate, 1), log=b"ok\n")

    with pytest.raises(RunError, match="REVIEWER_INPUT_LIMIT_EXCEEDED"):
        _compile(
            case["store"],
            subject=_subject(candidate),
            requirement={"goal": "Review", "acceptance": ["checks"]},
            final_check_evidence_ids=[evidence["id"]],
        )


def test_rejects_subject_that_does_not_match_exact_cas_candidate(case: dict[str, Any]) -> None:
    subject = _subject(case["candidate"])
    subject["candidate"] = {**subject["candidate"], "revision": 99}
    with pytest.raises(RunError, match="REVIEWER_INPUT_SUBJECT_INVALID"):
        _compile(
            case["store"],
            subject=subject,
            requirement={"goal": "Review", "acceptance": ["checks"]},
            final_check_evidence_ids=[case["evidence"]["id"]],
        )


def test_candidate_store_errors_are_bounded(
    case: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        case["store"], "get", lambda _: (_ for _ in ()).throw(CandidateError("CAS"))
    )
    admissions, run = _trusted_admission(
        case,
        monkeypatch,
        source_requirement={"goal": "Review", "acceptance": ["checks"]},
    )
    with pytest.raises(RunError, match="CAS"):
        compile_reviewer_input(
            admissions,
            case["store"],
            run_id=run["id"],
            operation_id="operation-1",
            principal="principal",
            final_check_evidence_ids=[case["evidence"]["id"]],
        )
