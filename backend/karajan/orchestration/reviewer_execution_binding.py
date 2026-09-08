"""Trusted, content-free bindings for the Reviewer Host preparation leaf."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from karajan.contracts.probe import AttemptManifest
from karajan.execution import ProcessSpec
from karajan.routing.compiler import digest
from karajan.runs import RunError

from .reviewer_input import ReviewerInput


def compiler_binding(
    held: dict[str, Any], reviewer_input: ReviewerInput, *, project_id: str
) -> dict[str, Any]:
    """Copy only controller facts while the Reviewer admission guard is held."""
    operation = held["operation"]
    worker_id = operation.get("depends_on_operation_id")
    assessment = operation.get("assessment")
    request = operation.get("request")
    if (
        not isinstance(worker_id, str)
        or not isinstance(assessment, dict)
        or not isinstance(request, dict)
    ):
        raise RunError("REVIEWER_EXECUTION_BINDING_INVALID")
    profile_ref = assessment.get("route", {}).get("selected_profile")
    registration = next(
        (
            row
            for row in assessment["route"]["snapshots"]["policy"]["resources"]["profiles"]
            if {key: row[key] for key in ("id", "revision")} == profile_ref
        ),
        None,
    )
    profile = registration.get("profile") if isinstance(registration, dict) else None
    try:
        candidate = json.loads(reviewer_input.content)["candidate"]
    except (UnicodeDecodeError, ValueError, KeyError, TypeError):
        raise RunError("REVIEWER_EXECUTION_BINDING_INVALID") from None
    if not isinstance(profile, dict) or not isinstance(candidate, dict):
        raise RunError("REVIEWER_EXECUTION_BINDING_INVALID")
    return {
        "run_id": operation["run_id"],
        "reviewer_operation_id": operation["id"],
        "worker_operation_id": worker_id,
        "project_id": project_id,
        "task_id": operation["task_id"],
        "planned_attempt_id": operation["planned_attempt_id"],
        "planned_context_id": operation["planned_context_id"],
        "admission_id": held["capacity"]["admission_id"],
        "assessment_digest": assessment["digest"],
        "authorization_ref": request["authorization_ref"],
        "budget_ref": assessment["route"]["snapshots"]["task"]["authorization"]["budget_ref"],
        "profile": deepcopy(profile),
        "candidate": candidate,
        "reviewer_input": {
            "schema_version": "karajan.reviewer-input.v2",
            "sha256": reviewer_input.content_sha256,
            "size": reviewer_input.size,
            "allowed_files": list(reviewer_input.allowed_files),
            "check_evidence_ids": list(reviewer_input.check_evidence_ids),
        },
    }


def host_manifest(intent: dict[str, Any]) -> AttemptManifest:
    """Produce the only Host manifest this leaf can prepare: Reviewer/read."""
    profile = intent["profile"]
    return AttemptManifest(
        id=intent["planned_attempt_id"],
        fence=intent["fence"],
        role="reviewer",
        profile_id=profile["id"],
        profile_revision=profile["revision"],
        authorization_ref=intent["authorization_ref"],
        budget_ref=intent["budget_ref"],
        permissions=["read"],
        requested_binding=profile["binding"],
    )


def launch_document(
    intent: dict[str, Any], spec: ProcessSpec, bootstrap_digest: str
) -> dict[str, Any]:
    if not isinstance(bootstrap_digest, str) or len(bootstrap_digest) != 64:
        raise RunError("REVIEWER_EXECUTION_LAUNCH_INVALID")
    try:
        process = spec.document()
    except ValueError:
        raise RunError("REVIEWER_EXECUTION_LAUNCH_INVALID") from None
    value = {
        "schema_version": "karajan.reviewer-host-launch.v1",
        "intent_digest": intent["binding_digest"],
        "bootstrap_digest": bootstrap_digest,
        "process_spec": process,
    }
    return value | {"digest": digest(value)}
