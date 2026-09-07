"""The shared, internal Reviewer-binding handoff in the original operation.

These records prove lineage, not qualification. Only the configured producer may
stage them; current Project facts are checked separately before new effects.
History uses exact Candidate receipts and never scans for a newer revision.
"""

from copy import deepcopy
from typing import Annotated, Any, Literal

from pydantic import Field, ValidationError

from karajan.candidates import CandidateStore
from karajan.candidates.models import (
    CandidateIdentity,
    Contract,
    Digest,
    Identifier,
    ReviewerBinding,
)
from karajan.routing.compiler import digest
from karajan.runs import RunError


class SubjectTransition(Contract):
    schema_version: Literal["karajan.candidate-subject-transition.v1"]
    revision: Annotated[int, Field(gt=0)]
    id: Identifier
    phase: Literal["prepared", "rebind_claimed", "ready", "installed"]
    expected_subject_digest: Digest
    binding: ReviewerBinding
    binding_sha256: Digest
    command_key: Identifier
    receipt: CandidateIdentity | None
    reason_codes: list[Identifier]
    semantic_digest: Digest


def candidate_identity(candidate: dict[str, Any]) -> dict[str, Any]:
    try:
        identity = {
            key: candidate[key]
            for key in CandidateIdentity.model_fields
            if key not in {"baseline_id", "request_sha256"}
        }
        identity.update(
            baseline_id=candidate["request"]["baseline_id"],
            request_sha256=digest(candidate["request"]),
        )
        return CandidateIdentity.model_validate(identity).model_dump()
    except (KeyError, TypeError, ValidationError):
        raise RunError("REVIEW_SUBJECT_IDENTITY_INVALID") from None


def _parsed(value: object) -> dict[str, Any]:
    try:
        result = SubjectTransition.model_validate(value).model_dump()
    except ValidationError:
        raise RunError("REVIEW_SUBJECT_TRANSITION_INVALID") from None
    if (
        result["binding_sha256"] != digest(result["binding"])
        or result["revision"] != result["binding"]["revision"]
        or (result["phase"] in {"ready", "installed"}) != (result["receipt"] is not None)
    ):
        raise RunError("REVIEW_SUBJECT_TRANSITION_INVALID")
    return result


def parse_transition(operation: dict[str, Any]) -> dict[str, Any] | None:
    value = operation.get("validation", {}).get("subject_transition")
    return None if value is None else _parsed(value)


def transition_pending(operation: dict[str, Any]) -> bool:
    transition = parse_transition(operation)
    return transition is not None and transition["phase"] != "installed"


def cycles(operation: dict[str, Any]) -> list[dict[str, Any]]:
    validation = operation.get("validation")
    if validation is None:
        return []
    return [*validation.get("history", []), validation]


def cycle_for_check(operation: dict[str, Any], check_run_id: str) -> dict[str, Any]:
    found = [
        cycle
        for cycle in cycles(operation)
        if any(row["check_run_id"] == check_run_id for row in cycle["checks"]["runs"])
    ]
    if len(found) != 1:
        raise RunError("CANDIDATE_CHECK_NOT_FOUND")
    return found[0]


def check_is_current(operation: dict[str, Any], check_run_id: str) -> bool:
    return cycle_for_check(operation, check_run_id) is operation["validation"]


def assert_cycle_quiescent(operation: dict[str, Any]) -> None:
    validation = operation.get("validation")
    if validation is None:
        raise RunError("REVIEW_SUBJECT_REQUIRED")
    for row in validation["checks"]["runs"]:
        if row.get("native_claim") is None and row["phase"] in {
            "prepared",
            "claimed",
            "host_prepared",
        }:
            continue
        observation = row.get("observation") or {}
        cleanup = row.get("cleanup") or {}
        if observation.get("local_stop") in {"confirmed", "not_started"}:
            continue
        if cleanup.get("native", {}).get("local_stop") in {"confirmed", "not_started"}:
            continue
        if row.get("native_claim") is None and cleanup.get("host", {}).get("status") == "confirmed":
            continue
        raise RunError("REVIEW_SUBJECT_CHECK_STOP_REQUIRED")


def _binding_matches(operation: dict[str, Any], binding: dict[str, Any]) -> None:
    source = operation["workspace"]["source_binding"]
    expected = {
        "run_id": operation["run_id"],
        "operation_id": operation["id"],
        "capture_digest": operation["execution"]["collection"]["capture_digest"],
        "approval_digest": digest(source["approval"]),
        "plan_digest": source["plan"]["plan_digest"],
        "execution_policy_digest": source["execution_policy"]["digest"],
    }
    tasks = [
        task
        for task in source["plan"]["plan"]["tasks"]
        if task["id"] == binding["reviewer_task_id"]
    ]
    if (
        any(binding[key] != value for key, value in expected.items())
        or len(tasks) != 1
        or tasks[0]["role"] != "reviewer"
        or operation["task_id"] not in tasks[0]["depends_on"]
        or digest(tasks[0]) != binding["reviewer_task_digest"]
    ):
        raise RunError("REVIEW_SUBJECT_BINDING_MISMATCH")


def current_subject(operation: dict[str, Any], candidates: CandidateStore) -> dict[str, Any]:
    validation = operation.get("validation")
    if validation is None:
        raise RunError("REVIEW_SUBJECT_REQUIRED")
    collection = operation["execution"]["collection"]
    capture = collection["capture"]
    if digest(capture) != collection["capture_digest"]:
        raise RunError("TASK_CAPTURE_IDENTITY_CONFLICT")
    original = candidates.lookup_projection_capture(
        capture["freeze_request"],
        projection=capture["projection"],
        captured_files=capture["captured_files"],
    )
    if original is None or any(
        original[key] != value for key, value in collection["candidate"].items()
    ):
        raise RunError("REVIEW_SUBJECT_CAPTURE_MISMATCH")
    subject = validation["subject"]
    anchor = candidate_identity(original)
    if any(anchor[key] != value for key, value in subject["source_candidate"].items()):
        raise RunError("REVIEW_SUBJECT_CAPTURE_MISMATCH")
    installed = validation.get("review_binding")
    candidate = original
    if installed is not None:
        transition = _parsed(installed)
        _binding_matches(operation, transition["binding"])
        if transition["phase"] != "installed":
            raise RunError("REVIEW_SUBJECT_TRANSITION_INVALID")
        rebound = candidates.lookup_review_rebind(
            transition["binding"], command_key=transition["command_key"]
        )
        if rebound is None or candidate_identity(rebound) != transition["receipt"]:
            raise RunError("REVIEW_SUBJECT_RECEIPT_MISMATCH")
        candidate = rebound
    identity = candidate_identity(candidate)
    if any(identity[key] != value for key, value in subject["candidate"].items()):
        raise RunError("REVIEW_SUBJECT_IDENTITY_INVALID")
    source = operation["workspace"]["source_binding"]
    if (
        subject["source_capture_digest"] != collection["capture_digest"]
        or subject["approval_digest"] != digest(source["approval"])
        or subject["plan_digest"] != source["plan"]["plan_digest"]
        or subject["execution_policy_digest"] != source["execution_policy"]["digest"]
    ):
        raise RunError("REVIEW_SUBJECT_BINDING_MISMATCH")
    return {"subject": deepcopy(subject), "candidate": candidate, "capture_candidate": original}


def stage_transition(
    operation: dict[str, Any],
    binding: dict[str, Any],
    *,
    transition_id: str,
    command_key: str,
    semantic_digest: str,
) -> dict[str, Any]:
    validation = operation.get("validation")
    if validation is None:
        raise RunError("REVIEW_SUBJECT_REQUIRED")
    if transition_pending(operation):
        raise RunError("REVIEW_SUBJECT_TRANSITION_PENDING")
    transition = _parsed(
        {
            "schema_version": "karajan.candidate-subject-transition.v1",
            "revision": binding["revision"],
            "id": transition_id,
            "phase": "prepared",
            "expected_subject_digest": digest(validation["subject"]),
            "binding": binding,
            "binding_sha256": digest(binding),
            "command_key": command_key,
            "receipt": None,
            "reason_codes": [],
            "semantic_digest": semantic_digest,
        }
    )
    _binding_matches(operation, transition["binding"])
    if (
        any(
            binding["source_candidate"][key] != value
            for key, value in validation["subject"]["candidate"].items()
        )
        or binding["revision"] != validation.get("review_binding", {}).get("revision", 0) + 1
    ):
        raise RunError("REVIEW_SUBJECT_BINDING_MISMATCH")
    validation["subject_transition"] = transition
    return transition


def mark_ready(operation: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    transition = parse_transition(operation)
    if transition is None or transition["phase"] not in {"rebind_claimed", "ready"}:
        raise RunError("REVIEW_SUBJECT_REBIND_CLAIM_REQUIRED")
    binding = transition["binding"]
    _binding_matches(operation, binding)
    command = {"binding": binding, "command_key": transition["command_key"]}
    expected = command | {
        "schema_version": "karajan.candidate-review-rebind.v1",
        "binding_sha256": digest(binding),
        "request_sha256": digest(command),
    }
    receipt = candidate_identity(candidate)
    if (
        candidate.get("review_rebind") != expected
        or receipt["revision"] != binding["source_candidate"]["revision"] + 1
        or receipt["series_id"] != binding["source_candidate"]["series_id"]
        or (transition["receipt"] is not None and transition["receipt"] != receipt)
    ):
        raise RunError("REVIEW_SUBJECT_RECEIPT_MISMATCH")
    transition.update(phase="ready", receipt=receipt)
    operation["validation"]["subject_transition"] = transition
    return transition


def replace_prepared_transition(
    operation: dict[str, Any],
    binding: dict[str, Any],
    *,
    transition_id: str,
    command_key: str,
    semantic_digest: str,
) -> dict[str, Any]:
    """Only an unclaimed intent may be replaced under the caller's operation lock."""
    old = parse_transition(operation)
    if old is None or old["phase"] != "prepared":
        raise RunError("REVIEW_SUBJECT_REPLACEMENT_FORBIDDEN")
    validation = operation["validation"]
    retained = [old, *validation.get("intent_history", [])]
    if any(row["id"] == transition_id or row["command_key"] == command_key for row in retained):
        raise RunError("REVIEW_SUBJECT_REPLACEMENT_IDENTITY_REUSED")
    prospective = deepcopy(operation)
    prospective["validation"].pop("subject_transition")
    new = stage_transition(
        prospective,
        binding,
        transition_id=transition_id,
        command_key=command_key,
        semantic_digest=semantic_digest,
    )
    validation.setdefault("intent_history", []).append(old)
    validation["subject_transition"] = new
    return new
