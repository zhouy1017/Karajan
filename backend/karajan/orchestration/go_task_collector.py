"""Internal native-to-Candidate handoff, never a public report/JSON ingress.

Only the fixed Host child hands its own execute_go_task return to this module.
Python dataclass identity and digests check consistency, not provenance by
themselves. A public facade accepts operation IDs, never these input objects.
"""

import hashlib
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from karajan.adapters.opencode.go_journal import GoCallJournal
from karajan.candidates import CandidateError, CandidateStore
from karajan.candidates._projection import prepare_projection
from karajan.candidates.models import Freeze
from karajan.execution import ProcessIdentity
from karajan.isolation._opencode_capture import StoppedProjection
from karajan.isolation.go_task import GoTaskResult, _validate_final
from karajan.routing.compiler import digest
from karajan.runs import RunError

from .go_execution_intent import GoExecutionIntents, GoTaskCaptureReceipt
from .go_task_binding import _parts, task_grant_binding
from .go_task_input import build_task_input


class ApprovedGoCollector:
    """The trusted child persists capture identity before its one Candidate freeze.

    Native termination is observed by the producer; the Host child itself stays
    alive through collection. Recovery links history and never calls a live
    source gate, rereads native files, starts an agent or retries a missing freeze.
    """

    def __init__(
        self,
        intents: GoExecutionIntents,
        candidates: CandidateStore,
        journal: GoCallJournal,
        *,
        source_check: Callable[[], None],
    ) -> None:
        if (
            not callable(source_check)
            or intents.candidates is None
            or (intents.candidates.directory != candidates.directory)
        ):
            raise RunError("TASK_COLLECTOR_CONFIGURATION_INVALID")
        self.intents, self.candidates, self.journal = intents, candidates, journal
        self.source_check = source_check

    def recover(self, run_id: str, operation_id: str, *, principal: str) -> dict[str, Any] | None:
        operation = self.intents.read(run_id, operation_id, principal=principal)
        collection = operation.get("execution", {}).get("collection")
        if collection is None:
            return None
        capture = GoTaskCaptureReceipt(collection["capture"]).as_dict()
        if digest(capture) != collection["capture_digest"]:
            raise RunError("TASK_CAPTURE_IDENTITY_CONFLICT")
        candidate = self.candidates.lookup_projection_capture(
            capture["freeze_request"],
            projection=capture["projection"],
            captured_files=capture["captured_files"],
        )
        if candidate is None:
            return None
        linked = self.intents.candidate_recorded(
            run_id,
            operation_id,
            principal=principal,
            capture_digest=collection["capture_digest"],
            candidate_id=candidate["id"],
        )
        return dict(linked["execution"]["collection"]["candidate"])

    def collect(
        self,
        run_id: str,
        operation_id: str,
        *,
        principal: str,
        runner: ProcessIdentity,
        result: GoTaskResult,
    ) -> dict[str, Any]:
        operation = self.intents.read(run_id, operation_id, principal=principal)
        if operation.get("execution", {}).get("collection") is not None:
            prior = self.recover(run_id, operation_id, principal=principal)
            if prior is None:
                raise RunError("TASK_CAPTURE_RECONCILIATION_REQUIRED")
            return prior
        capture = compile_go_capture(operation, result, self.candidates, self.journal)
        # This short Host observation is released before committing operation
        # metadata. The exact child and all business guards are reacquired for freeze.
        intent = operation["execution"]["intent"]
        with self.intents.host.current_runner_guard(
            intent["attempt_id"],
            fence=intent["fence"],
            authorization_ref=intent["authorization_ref"],
        ) as current:
            if current != runner:
                raise RunError("TASK_EXECUTION_CLAIM_NOT_CURRENT")
        self.intents.capture_recorded(
            run_id, operation_id, principal=principal, runner=runner, capture=capture
        )
        document = capture.as_dict()
        capture_digest = digest(document)
        routing = self.intents.admissions.routing
        with self.intents.collection_guard(
            run_id,
            operation_id,
            principal=principal,
            runner=runner,
            capture_digest=capture_digest,
        ) as locked:
            self.source_check()
            with routing.reserved_execution_guard(
                run_id, locked["assessment"]["id"], principal=principal
            ) as route:
                if route["state"] != "selected":
                    raise RunError("TASK_COLLECTION_APPROVAL_NOT_CURRENT")
                # The immutable Host manifest's Profile is checked without nesting
                # two Host writer transactions. The subsequent runner guard also
                # rechecks cancellation/fence and holds them through Candidate commit.
                with self.intents.host.current_fence_guard(
                    intent["attempt_id"],
                    fence=intent["fence"],
                    authorization_ref=intent["authorization_ref"],
                ) as writer:
                    if (
                        writer["profile"]
                        != locked["workspace"]["source_binding"]["selected_profile"]
                    ):
                        raise RunError("TASK_CAPTURE_HOST_PROFILE_MISMATCH")
                with self.intents.host.current_runner_guard(
                    intent["attempt_id"],
                    fence=intent["fence"],
                    authorization_ref=intent["authorization_ref"],
                ) as current:
                    if current != runner:
                        raise RunError("TASK_EXECUTION_CLAIM_NOT_CURRENT")
                    self.source_check()
                    assert result.capture is not None
                    candidate = self.candidates.freeze_projection(
                        document["projection"],
                        dict(result.capture.files),
                        document["freeze_request"],
                    )
        linked = self.intents.candidate_recorded(
            run_id,
            operation_id,
            principal=principal,
            capture_digest=capture_digest,
            candidate_id=candidate["id"],
        )
        return dict(linked["execution"]["collection"]["candidate"])


def compile_go_capture(
    operation: dict[str, Any],
    result: GoTaskResult,
    candidates: CandidateStore,
    journal: GoCallJournal,
) -> GoTaskCaptureReceipt:
    """Compile the exact immutable handoff; grant no current writer authority."""
    try:
        return _compile(operation, result, candidates, journal)
    except RunError:
        raise
    except CandidateError as error:
        raise RunError(error.code) from None
    except (ValueError, KeyError, TypeError, AttributeError):
        raise RunError("TASK_CAPTURE_INVALID") from None


def _compile(
    operation: dict[str, Any],
    result: GoTaskResult,
    candidates: CandidateStore,
    journal: GoCallJournal,
) -> GoTaskCaptureReceipt:
    intent, source, profile = _parts(operation)
    if type(result) is not GoTaskResult or type(result.capture) is not StoppedProjection:
        raise RunError("TASK_STOPPED_CAPTURE_REQUIRED")
    capture, report = result.capture, result.report
    task = build_task_input(
        operation["workspace"],
        candidates,
        native_source_sha256=intent["native_source_sha256"],
        runner_source_digest=intent["runner_source_sha256"],
    )
    binding = task_grant_binding(operation)
    expected = {
        "schema_version": "karajan.go-native-task-observation.v1",
        "status": "completed",
        "scope": "native_task_execution",
        "subject": binding["subject"],
        "attempt_id": intent["attempt_id"],
        "fence": intent["fence"],
        "grant_id": intent["grant_id"],
        "grant_binding": binding,
        "input": task.descriptor(),
        "input_digest": digest(task.descriptor()),
        "native_source_sha256": intent["native_source_sha256"],
        "runner_source_digest": intent["runner_source_sha256"],
        "candidate_validation": "not_run",
        "dispatch_eligible": False,
        "provider_remote_stop": "unknown",
        "real_credential_passed_to_runtime": False,
        "reason_codes": [],
    }
    projection = [row.projection() for row in task.files]
    if (
        any(report.get(key) != value for key, value in expected.items())
        or digest(report["native_source"]) != intent["native_source_sha256"]
        or capture.runtime_sha256
        != report["native_source"]["qualified_mechanism_descriptor"]["artifact_sha256"]
        or report["native_cleanup"] != capture.stop_evidence
        or report["journal"] != journal.snapshot(intent["grant_id"])
        or report["journal"]["binding"] != binding
        or report["observation_origin"] not in {"official_go", "http_fixture"}
        or [asdict(row) for row in capture.projection] != projection
    ):
        raise RunError("TASK_CAPTURE_BINDING_MISMATCH")
    reasons: list[str] = []
    _validate_final(report, capture, reasons)
    if reasons:
        raise RunError(reasons[0])
    limits = intent["execution_context"]["context"]
    if any(
        any(call["request_context"].get(key) != value for key, value in limits.items())
        for call in report["journal"]["calls"]
    ):
        raise RunError("TASK_CAPTURE_CONTEXT_MISMATCH")
    contents = dict(capture.files)
    if len(contents) != len(capture.files):
        raise RunError("TASK_CAPTURE_DUPLICATE_FILE")
    workspace = operation["workspace"]
    prepare_projection(projection, contents, workspace["baseline"], workspace["write_paths"])
    evidence = {
        "intent_digest": operation["execution"]["intent_digest"],
        "workspace_digest": workspace["digest"],
        "input_sha256": workspace["input_sha256"],
        "projection": projection,
        "captured_files": [
            {"path": path, "sha256": hashlib.sha256(body).hexdigest(), "size": len(body)}
            for path, body in sorted(contents.items())
        ],
        "report": report,
    }
    evidence_digest = digest(evidence)
    author = {
        "attempt_id": intent["attempt_id"],
        "fence": intent["fence"],
        "profile_id": profile.id,
        "profile_revision": profile.revision,
        "model_family": source["profile_registration"]["model_family"],
        "context_id": intent["context_id"],
        "provenance_ref": "go-task-author:" + evidence_digest,
    }
    request = Freeze.model_validate(
        {
            "series_id": "go-task-candidate:" + operation["id"],
            "baseline_id": workspace["baseline"]["id"],
            "input_sha256": workspace["input_sha256"],
            "allowed_paths": workspace["write_paths"],
            "task_class": operation["assessment"]["route"]["effective_class"],
            "writer": {
                "attempt_id": intent["attempt_id"],
                "fence": intent["fence"],
                "stopped": True,
                "observation_ref": "go-task-stop:" + evidence_digest,
            },
            "authors": [author],
            "policy": _validation_policy(source),
        }
    ).model_dump()
    if request["task_class"] != "T1":
        raise RunError("TASK_CAPTURE_SCOPE_UNSUPPORTED")
    return GoTaskCaptureReceipt(
        {
            "schema_version": "karajan.go-task-capture.v1",
            **evidence,
            "evidence_digest": evidence_digest,
            "freeze_request": request,
        }
    )


def _validation_policy(source: dict[str, Any]) -> dict[str, Any]:
    policy = source["execution_policy"]
    if policy["schema_version"] != "karajan.execution-policy.v2":
        raise RunError("TASK_VALIDATION_POLICY_REQUIRED")
    validation = policy["validation"]
    approved = source["plan"]["plan"]["authorization"]["checks"]
    ordinary = [check for check in approved if check != "independent_review"]
    if not ordinary:
        raise RunError("TASK_VALIDATION_CHECKS_REQUIRED")
    checks = {row["id"]: row for row in validation["checks"]}
    environments = {(row["id"], row["revision"]): row for row in validation["environments"]}

    def environment(consumer: dict[str, Any]) -> str:
        ref = consumer["environment_ref"]
        return str(environments[(ref["id"], ref["revision"])]["source_sha256"])

    return {
        "id": validation["id"],
        "revision": validation["revision"],
        "checks": [
            {
                "id": name,
                "revision": checks[name]["revision"],
                "argv": checks[name]["argv"],
                "environment_sha256": environment(checks[name]),
            }
            for name in ordinary
        ],
        "review": {
            "revision": validation["review"]["revision"],
            "environment_sha256": environment(validation["review"]),
            "approved_reviewers": [],
        },
    }
