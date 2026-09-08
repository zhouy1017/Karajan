"""Compile a bounded, read-only Reviewer input from the trusted Candidate CAS."""

from __future__ import annotations

import difflib
import hashlib
import json
import tempfile
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from karajan.candidates import CandidateError, CandidateStore
from karajan.candidates.models import CandidateIdentity, Freeze
from karajan.candidates.store import digest, manifest_digest
from karajan.runs import RunError
from karajan.runs.models import Requirement
from karajan.runs.validation import covered

from .admission import ApprovedTaskAdmission
from .candidate_subjects import current_subject
from .go_execution_intent import GoExecutionIntents
from .go_task_collector import _validation_policy
from .workspace import _approved_task

MAX_INPUT_BYTES = 256 * 1024
_CANDIDATE_IDENTITY_FIELDS = (
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
)
_SUBJECT_FIELDS = {
    "schema_version",
    "revision",
    "source_capture_digest",
    "source_candidate",
    "candidate",
    "approval_digest",
    "plan_digest",
    "execution_policy_digest",
}


@dataclass(frozen=True)
class ReviewerInput:
    """The small immutable handoff consumed by a future Reviewer consumer."""

    content: bytes
    content_sha256: str
    size: int
    allowed_files: tuple[str, ...]
    candidate_id: str
    candidate_revision: int
    check_evidence_ids: tuple[str, ...]


def compile_reviewer_input(
    admissions: ApprovedTaskAdmission,
    candidates: CandidateStore,
    *,
    run_id: str,
    operation_id: str,
    principal: str,
    final_check_evidence_ids: Collection[str],
) -> ReviewerInput:
    """Build deterministic Reviewer content from current controller records.

    ``run_id`` and ``operation_id`` identify the controller-owned records. The
    current requirement is read from the Run, while the current candidate and
    validation subject are read from the original operation and Candidate CAS.
    No caller mapping can substitute for those records. The compiler never
    reads a caller path, mutable workspace, model output, or ledger effect. CAS
    materialization uses a temporary sibling directory and is removed before
    this function returns.
    """

    try:
        run = admissions.routing.planner.get(run_id, principal=principal)
        operation = GoExecutionIntents.read_operation(
            admissions, run_id, operation_id, principal=principal
        )
        return compile_reviewer_input_from_records(
            candidates,
            run=run,
            operation=operation,
            principal=principal,
            final_check_evidence_ids=final_check_evidence_ids,
        )
    except RunError:
        raise
    except CandidateError as error:
        raise RunError(error.code) from None
    except (
        AttributeError,
        KeyError,
        TypeError,
        ValueError,
        ValidationError,
        OSError,
        RecursionError,
    ):
        raise RunError("REVIEWER_INPUT_INVALID") from None


def compile_reviewer_input_from_records(
    candidates: CandidateStore,
    *,
    run: dict[str, Any],
    operation: dict[str, Any],
    principal: str,
    final_check_evidence_ids: Collection[str],
) -> ReviewerInput:
    """Compile from producer-held controller records without reopening their DBs.

    The admission effect guard already holds these exact Run and Worker records.
    A receiving writer can therefore recompile the full Candidate CAS and Check
    input without inverting the established controller lock order.
    """
    try:
        if (
            operation.get("run_id") != run.get("id")
            or operation.get("state") not in {"reserved", "execution_pending", "executing"}
        ):
            raise RunError("REVIEWER_INPUT_OPERATION_INVALID")
        plan, worker = _approved_task(run, operation, principal)
        workspace = operation.get("workspace", {})
        source = workspace.get("source_binding", {})
        approved = _approved_context(run, operation, source, plan, worker)
        if source.get("requirement") != run.get("requirement"):
            raise RunError("REVIEWER_INPUT_APPROVAL_CHANGED")
        try:
            current_policy = _validation_policy(source)
            installed = operation.get("validation", {}).get("review_binding")
            if installed is not None:
                current_policy["review"]["approved_reviewers"] = [
                    row["reviewer"] for row in installed["binding"]["reviewer_sources"]
                ]
            current = {
                "input_sha256": workspace["input_sha256"],
                "policy_sha256": digest(current_policy),
            }
        except (KeyError, TypeError, ValueError, ValidationError):
            raise RunError("REVIEWER_INPUT_APPROVAL_CHANGED") from None
        resolved = current_subject(operation, candidates)
        return _compile(
            candidates,
            resolved["subject"],
            run["requirement"],
            final_check_evidence_ids,
            current=current,
            read_paths=workspace["read_paths"],
            approved=approved,
        )
    except RunError:
        raise
    except CandidateError as error:
        raise RunError(error.code) from None
    except (
        AttributeError,
        KeyError,
        TypeError,
        ValueError,
        ValidationError,
        OSError,
        RecursionError,
    ):
        raise RunError("REVIEWER_INPUT_INVALID") from None


def _compile(
    candidates: CandidateStore,
    subject: Mapping[str, Any],
    requirement: Mapping[str, Any],
    final_check_evidence_ids: Collection[str],
    *,
    current: Mapping[str, str] | None = None,
    read_paths: Collection[str] | None = None,
    approved: Mapping[str, Any] | None = None,
) -> ReviewerInput:
    if not isinstance(subject, Mapping) or set(subject) != _SUBJECT_FIELDS:
        raise RunError("REVIEWER_INPUT_SUBJECT_INVALID")
    if subject["schema_version"] != "karajan.candidate-validation-subject.v1":
        raise RunError("REVIEWER_INPUT_SUBJECT_INVALID")
    if type(subject["revision"]) is not int or subject["revision"] <= 0:
        raise RunError("REVIEWER_INPUT_SUBJECT_INVALID")

    subject_candidate = _subject_identity(subject["candidate"])
    _subject_identity(subject["source_candidate"])
    try:
        approved_requirement = Requirement.model_validate(requirement).model_dump()
    except (ValidationError, TypeError, ValueError):
        raise RunError("REVIEWER_INPUT_REQUIREMENT_INVALID") from None
    if not isinstance(final_check_evidence_ids, Collection) or isinstance(
        final_check_evidence_ids, (str, bytes, bytearray)
    ):
        raise RunError("REVIEWER_INPUT_CHECKS_INVALID")
    evidence_ids = tuple(final_check_evidence_ids)
    if any(type(value) is not str or not value for value in evidence_ids):
        raise RunError("REVIEWER_INPUT_CHECKS_INVALID")
    if len(set(evidence_ids)) != len(evidence_ids):
        raise RunError("REVIEWER_INPUT_CHECKS_INVALID")

    candidate = candidates.get(subject_candidate["id"])
    actual_identity = _candidate_record_identity(candidate)
    if any(actual_identity[key] != value for key, value in subject_candidate.items()):
        raise RunError("REVIEWER_INPUT_SUBJECT_INVALID")
    if candidate["request"]["task_class"] != "T1":
        raise RunError("REVIEWER_INPUT_SCOPE_UNSUPPORTED")
    if current is not None and (
        current.get("input_sha256") != candidate["input_sha256"]
        or current.get("policy_sha256") != candidate["policy_sha256"]
    ):
        raise RunError("REVIEWER_INPUT_CANDIDATE_STALE")
    checks = _final_checks(candidates, candidate, evidence_ids, current=current)
    files, diff = _materialize_content(candidates, candidate, read_paths=read_paths)
    payload = {
        "schema_version": "karajan.reviewer-input.v2",
        "candidate": actual_identity,
        "requirement": approved_requirement,
        **({"approved": approved} if approved is not None else {}),
        "diff": diff,
        "files": files,
        "checks": checks,
    }
    try:
        content = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise RunError("REVIEWER_INPUT_INVALID") from None
    if len(content) > MAX_INPUT_BYTES:
        raise RunError("REVIEWER_INPUT_LIMIT_EXCEEDED")
    return ReviewerInput(
        content=content,
        content_sha256=hashlib.sha256(content).hexdigest(),
        size=len(content),
        allowed_files=tuple(row["path"] for row in files),
        candidate_id=actual_identity["id"],
        candidate_revision=actual_identity["revision"],
        check_evidence_ids=tuple(row["evidence_id"] for row in checks),
    )


def _approved_context(
    run: Mapping[str, Any],
    operation: Mapping[str, Any],
    source: object,
    plan: Mapping[str, Any],
    worker: Mapping[str, Any],
) -> dict[str, Any]:
    """Extract the only task semantics a Reviewer may receive from an approval."""
    try:
        if not isinstance(source, Mapping) or source["plan"] != plan:
            raise RunError("REVIEWER_INPUT_APPROVAL_CHANGED")
        approval = operation["assessment"]["sources"]["approval"]
        if source["approval"] != approval:
            raise RunError("REVIEWER_INPUT_APPROVAL_CHANGED")
        if source["requirement"] != run["requirement"]:
            raise RunError("REVIEWER_INPUT_APPROVAL_CHANGED")
        reviewers = [
            task
            for task in plan["plan"]["tasks"]
            if task["role"] == "reviewer" and worker["id"] in task["depends_on"]
        ]
        if len(reviewers) != 1 or reviewers[0]["depends_on"] != [worker["id"]]:
            raise RunError("UNIQUE_APPROVED_REVIEWER_DEPENDENCY_REQUIRED")
        reviewer = reviewers[0]
        return {
            "plan": {
                key: plan[key]
                for key in (
                    "plan_revision",
                    "term",
                    "plan_digest",
                    "authorization_digest",
                    "configuration_digest",
                    "routing_digest",
                )
            }
            | {"summary": plan["plan"]["summary"]},
            "approval": {
                key: approval[key]
                for key in (
                    "plan_revision",
                    "term",
                    "plan_digest",
                    "authorization_digest",
                    "configuration_digest",
                    "routing_digest",
                )
            },
            "worker": _task_context(worker),
            "reviewer": _task_context(reviewer),
        }
    except RunError:
        raise
    except (KeyError, TypeError):
        raise RunError("REVIEWER_INPUT_APPROVAL_CHANGED") from None


def _task_context(task: Mapping[str, Any]) -> dict[str, Any]:
    try:
        context = {
            key: task[key] for key in ("id", "revision", "role", "paths", "acceptance")
        }
        if (
            not isinstance(context["id"], str)
            or type(context["revision"]) is not int
            or not isinstance(context["role"], str)
            or not isinstance(context["paths"], list)
            or not isinstance(context["acceptance"], list)
        ):
            raise RunError("REVIEWER_INPUT_APPROVAL_CHANGED")
        return context
    except KeyError:
        raise RunError("REVIEWER_INPUT_APPROVAL_CHANGED") from None


def _subject_identity(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RunError("REVIEWER_INPUT_SUBJECT_INVALID")
    keys = set(value)
    full_keys = set(_CANDIDATE_IDENTITY_FIELDS)
    initial_keys = full_keys - {"request_sha256"}
    if keys != full_keys and keys != initial_keys:
        raise RunError("REVIEWER_INPUT_SUBJECT_INVALID")
    try:
        identity = {key: value[key] for key in _CANDIDATE_IDENTITY_FIELDS if key in value}
        if any(
            not isinstance(identity[key], str) or not identity[key]
            for key in identity
            if key != "revision"
        ):
            raise RunError("REVIEWER_INPUT_SUBJECT_INVALID")
        if type(identity["revision"]) is not int or identity["revision"] <= 0:
            raise RunError("REVIEWER_INPUT_SUBJECT_INVALID")
        if "request_sha256" in value:
            CandidateIdentity.model_validate(identity)
        else:
            identity.pop("request_sha256", None)
        return identity
    except KeyError:
        raise RunError("REVIEWER_INPUT_SUBJECT_INVALID") from None


def _candidate_record_identity(candidate: Mapping[str, Any]) -> dict[str, Any]:
    try:
        if candidate["schema_version"] != "karajan.candidate.v1":
            raise RunError("REVIEWER_INPUT_CANDIDATE_INVALID")
        request = candidate["request"]
        Freeze.model_validate(request)
        identity = {
            key: candidate[key]
            for key in _CANDIDATE_IDENTITY_FIELDS
            if key not in {"baseline_id", "request_sha256"}
        }
        identity["baseline_id"] = request["baseline_id"]
        identity["request_sha256"] = digest(request)
        identity = CandidateIdentity.model_validate(identity).model_dump()
        if candidate["content_sha256"] != digest(
            {
                key: candidate[key]
                for key in ("repository_identity", "base_sha", "tree_sha", "input_sha256")
            }
        ):
            raise RunError("REVIEWER_INPUT_CANDIDATE_INVALID")
        if candidate["manifest_sha256"] != manifest_digest(candidate["manifest"]):
            raise RunError("REVIEWER_INPUT_CANDIDATE_INVALID")
        if candidate["policy_sha256"] != digest(request["policy"]):
            raise RunError("REVIEWER_INPUT_CANDIDATE_INVALID")
        return identity
    except RunError:
        raise
    except (KeyError, TypeError, ValueError, ValidationError):
        raise RunError("REVIEWER_INPUT_CANDIDATE_INVALID") from None


def _final_checks(
    candidates: CandidateStore,
    candidate: Mapping[str, Any],
    evidence_ids: tuple[str, ...],
    *,
    current: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    gate_current = {
        key: candidate[key]
        for key in ("repository_identity", "base_sha", "input_sha256", "policy_sha256")
    }
    if current is not None:
        gate_current["input_sha256"] = current["input_sha256"]
        gate_current["policy_sha256"] = current["policy_sha256"]
    gate = candidates.gate(candidate["id"], current=gate_current)
    records = {
        record["id"]: record
        for record in gate["evidence"]
        if record.get("kind") == "check" and isinstance(record.get("id"), str)
    }
    if set(records) != set(
        record["id"] for record in gate["evidence"] if record.get("kind") == "check"
    ):
        raise RunError("REVIEWER_INPUT_CHECKS_INVALID")
    requested = set(evidence_ids)
    if not requested <= records.keys():
        raise RunError("REVIEWER_INPUT_CHECKS_INCOMPLETE")
    latest: dict[str, dict[str, Any]] = {}
    for record in gate["evidence"]:
        if record.get("kind") == "check":
            check_id = record.get("input", {}).get("check_id")
            if isinstance(check_id, str):
                latest[check_id] = record
    result = []
    for definition in candidate["request"]["policy"]["checks"]:
        record = latest.get(definition["id"])
        if record is None or record["id"] not in requested:
            raise RunError("REVIEWER_INPUT_CHECKS_INCOMPLETE")
        input_data = record.get("input")
        log = record.get("log")
        if (
            record.get("effective_status") != "passed"
            or not isinstance(input_data, Mapping)
            or input_data.get("candidate_id") != candidate["id"]
            or input_data.get("check_revision") != definition["revision"]
            or input_data.get("policy_sha256") != candidate["policy_sha256"]
            or input_data.get("input_sha256") != candidate["input_sha256"]
            or input_data.get("environment_sha256") != definition["environment_sha256"]
            or not isinstance(log, Mapping)
            or type(log.get("size")) is not int
            or not isinstance(log.get("sha256"), str)
        ):
            raise RunError("REVIEWER_INPUT_CHECKS_INCOMPLETE")
        result.append(
            {
                "evidence_id": record["id"],
                "evidence_key": input_data["evidence_key"],
                "check_id": input_data["check_id"],
                "check_revision": input_data["check_revision"],
                "argv": list(definition["argv"]),
                "environment_sha256": definition["environment_sha256"],
                "outcome": input_data["outcome"],
                "exit_code": input_data["exit_code"],
                "status": record["status"],
                "reasons": record["reasons"],
                "log": {"sha256": log["sha256"], "size": log["size"]},
            }
        )
    if requested != {record["evidence_id"] for record in result}:
        raise RunError("REVIEWER_INPUT_CHECKS_INVALID")
    return result


def _materialize_content(
    candidates: CandidateStore,
    candidate: Mapping[str, Any],
    *,
    read_paths: Collection[str] | None = None,
) -> tuple[list[dict[str, str]], str]:
    baseline = candidates.get_baseline(candidate["request"]["baseline_id"])
    if any(candidate[key] != baseline[key] for key in ("repository_identity", "base_sha")):
        raise RunError("REVIEWER_INPUT_CAS_INVALID")
    baseline_manifest = {entry["path"]: entry for entry in baseline["manifest"]}
    candidate_manifest = {entry["path"]: entry for entry in candidate["manifest"]}
    if set(baseline_manifest) != set(candidate_manifest):
        raise RunError("REVIEWER_INPUT_UNSUPPORTED_FILE")
    if any(
        baseline_manifest[path]["mode"] != candidate_manifest[path]["mode"]
        or baseline_manifest[path]["mode"] not in {"100644", "100755"}
        for path in baseline_manifest
    ):
        raise RunError("REVIEWER_INPUT_UNSUPPORTED_FILE")
    if read_paths is None:
        selected_paths = set(candidate_manifest)
    else:
        if (
            not isinstance(read_paths, Collection)
            or isinstance(read_paths, (str, bytes, bytearray))
            or any(type(path) is not str or not path for path in read_paths)
        ):
            raise RunError("REVIEWER_INPUT_SCOPE_UNSUPPORTED")
        roots = list(read_paths)
        selected_paths = {path for path in candidate_manifest if covered(path, roots)}
        for path in set(candidate_manifest) - selected_paths:
            old, new = baseline_manifest[path], candidate_manifest[path]
            if (
                old["mode"],
                old["artifact"]["sha256"],
                old["artifact"]["size"],
            ) != (
                new["mode"],
                new["artifact"]["sha256"],
                new["artifact"]["size"],
            ):
                raise RunError("REVIEWER_INPUT_SCOPE_UNSUPPORTED")
    with tempfile.TemporaryDirectory(
        prefix="reviewer-input-", dir=candidates.directory.parent
    ) as directory:
        root = Path(directory)
        baseline_root = root / "baseline"
        candidate_root = root / "candidate"
        candidates.materialize_baseline(candidate["request"]["baseline_id"], baseline_root)
        candidates.materialize(candidate["id"], candidate_root)
        files: list[dict[str, str]] = []
        diff_parts: list[str] = []
        for path in sorted(selected_paths):
            old = (baseline_root / path).read_bytes()
            new = (candidate_root / path).read_bytes()
            try:
                old_text = old.decode("utf-8")
                new_text = new.decode("utf-8")
            except UnicodeDecodeError:
                raise RunError("REVIEWER_INPUT_UNSUPPORTED_FILE") from None
            if "\x00" in old_text or "\x00" in new_text:
                raise RunError("REVIEWER_INPUT_UNSUPPORTED_FILE")
            files.append(
                {
                    "path": path,
                    "mode": candidate_manifest[path]["mode"],
                    "content": new_text,
                }
            )
            diff_parts.append(
                "".join(
                    difflib.unified_diff(
                        old_text.splitlines(keepends=True),
                        new_text.splitlines(keepends=True),
                        fromfile=f"baseline/{path}",
                        tofile=f"candidate/{path}",
                        lineterm="\n",
                    )
                )
            )
        diff = "".join(diff_parts)
    return files, diff
