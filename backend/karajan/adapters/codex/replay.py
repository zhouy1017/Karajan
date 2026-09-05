"""Pure offline replay; it never starts Codex or sends a protocol message."""

import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .models import (
    CancelStep,
    DecisionStep,
    NativeStep,
    RateSnapshot,
    ReplayDocument,
    ResolvedNotification,
    UsageNotification,
)
from .permissions import PermissionGate

PROTOCOL_SCHEMA_SHA256 = "d3eace08be5dca386bfd1f1e8df650058b4113f1e10870a284d775d75517576a"


def _report() -> dict[str, Any]:
    return {
        "schema_version": "karajan.codex-replay-report.v1",
        "status": "passed",
        "responses": [],
        "reason_codes": [],
        "qualification": {
            "live_status": "not_run",
            "dispatch_eligible": False,
            "capabilities": {
                "hidden_fallback_excluded": "not_run",
                "extra_delegation_disabled": "not_run",
                "native_sandbox_enforced": "not_run",
                "billing_path_confirmed": "not_run",
                "provider_model_confirmed": "not_run",
            },
            "remaining_live_cases": ["official_login", "inference", "file_tools", "cancel"],
        },
        "usage": {"state": "unknown", "coverage": "attempt", "model_call_count": None},
        "quota": {"state": "unknown"},
        "native_errors": [],
        "estimates": {"state": "not_provided"},
    }


def replay_file(path: Path) -> dict[str, Any]:
    report = _report()
    try:
        input_bytes = path.read_bytes()
        document = ReplayDocument.model_validate_json(input_bytes.decode("utf-8-sig"))
    except ValidationError:
        report["status"] = "failed"
        report["reason_codes"] = ["INPUT_INVALID"]
        return report
    except (OSError, UnicodeError) as error:
        report["status"] = "failed"
        report["reason_codes"] = [
            "INPUT_UNREADABLE" if isinstance(error, OSError) else "INPUT_ENCODING_INVALID"
        ]
        return report
    report["case_id"] = document.case_id
    report["input_sha256"] = hashlib.sha256(input_bytes).hexdigest()
    report["protocol"] = {
        "runtime_version": document.runtime_version,
        "schema_sha256": document.schema_sha256,
        "schema_file": "codex_app_server_protocol.v2.schemas.json",
        "config_source_sha256": document.config_source_sha256,
    }
    report["attempt"] = document.attempt.model_dump()
    report["authorization_hash"] = document.authorization.hash
    report["provenance"] = document.provenance.model_dump(mode="json")
    report["event_order"] = [
        {"index": index, "kind": step.kind, "at": step.at.isoformat()}
        for index, step in enumerate(document.steps)
    ]
    if document.schema_sha256 != PROTOCOL_SCHEMA_SHA256:
        report["status"] = "failed"
        report["reason_codes"] = ["PROTOCOL_SCHEMA_MISMATCH"]
        return report
    gate = PermissionGate(document.attempt, document.authorization)
    binding_confirmed = False
    turn_active = False
    turn_closed = False
    expected = {
        "model": document.requested.model,
        "modelProvider": document.requested.model_provider,
        "cwd": document.requested.cwd,
        "approvalPolicy": document.requested.approval_policy,
        "approvalsReviewer": document.requested.approvals_reviewer,
        "sandbox": document.requested.sandbox.model_dump(),
    }
    report["bindings"] = {"requested": expected, "accepted": None, "provider_reported": None}
    report["permission_outcomes"] = []
    previous_at = None
    for step in document.steps:
        if previous_at is not None and step.at < previous_at:
            report["status"] = "failed"
            report["reason_codes"].append("EVENT_TIME_REVERSED")
            report["responses"].extend(gate.invalidate())
            break
        previous_at = step.at
        outcome = None
        if isinstance(step, NativeStep) and step.message.get("method") == "thread/settings/updated":
            params = step.message.get("params")
            settings = params.get("threadSettings") if isinstance(params, dict) else None
            observed = (
                {
                    key: settings.get("sandboxPolicy" if key == "sandbox" else key)
                    for key in expected
                }
                if isinstance(settings, dict)
                else None
            )
            if (
                not isinstance(params, dict)
                or params.get("threadId") != document.attempt.thread_id
                or json.dumps(observed, sort_keys=True) != json.dumps(expected, sort_keys=True)
            ):
                outcome = {"status": "rejected", "reason": "CONFIGURATION_CHANGED"}
                report["responses"].extend(gate.invalidate())
        if isinstance(step, NativeStep) and step.message.get("method") == "error":
            params = step.message.get("params")
            report["observed_internal_retry"] = (
                params.get("willRetry") is True if isinstance(params, dict) else None
            )
            outcome = {"status": "rejected", "reason": "NATIVE_TURN_ERROR"}
            report["responses"].extend(gate.invalidate())
        if isinstance(step, NativeStep) and step.message.get("method") == "serverRequest/resolved":
            params = step.message.get("params")
            try:
                resolved = ResolvedNotification.model_validate(params)
                outcome = gate.resolve(thread_id=resolved.threadId, request_id=resolved.requestId)
            except ValidationError:
                outcome = {"status": "rejected", "reason": "NATIVE_REQUEST_INVALID"}
        if (
            isinstance(step, NativeStep)
            and step.message.get("method") == "thread/tokenUsage/updated"
        ):
            try:
                usage = UsageNotification.model_validate(step.message.get("params"))
                if (usage.threadId, usage.turnId) != (
                    document.attempt.thread_id,
                    document.attempt.turn_id,
                ):
                    raise ValueError("identity")
                report["usage"] = {
                    "state": "observed",
                    "coverage": "thread_and_turn_reported",
                    "model_call_count": None,
                    "at": step.at.isoformat(),
                    "token_usage": usage.tokenUsage.model_dump(),
                }
            except (ValidationError, ValueError):
                outcome = {"status": "rejected", "reason": "USAGE_OBSERVATION_INVALID"}
        if (
            isinstance(step, NativeStep)
            and step.message.get("method") == "account/rateLimits/updated"
        ):
            params = step.message.get("params")
            try:
                snapshot = RateSnapshot.model_validate(
                    params.get("rateLimits") if isinstance(params, dict) else None
                )
                report["quota"] = {
                    "state": "observed",
                    "at": step.at.isoformat(),
                    "account_identity": "unknown",
                    **snapshot.model_dump(),
                }
            except ValidationError:
                outcome = {"status": "rejected", "reason": "QUOTA_OBSERVATION_INVALID"}
        if isinstance(step, NativeStep) and "error" in step.message:
            native_error = step.message["error"]
            code = native_error.get("code") if isinstance(native_error, dict) else None
            code = code if type(code) is int else None
            category = (
                {
                    -32700: "parse_error",
                    -32600: "invalid_request",
                    -32601: "method_not_found",
                    -32602: "invalid_params",
                    -32603: "internal_error",
                }.get(code, "unknown")
                if type(code) is int
                else "unknown"
            )
            report["native_errors"].append(
                {"request_id": step.message.get("id"), "code": code, "category": category}
            )
            outcome = {"status": "rejected", "reason": "NATIVE_RPC_ERROR"}
            report["responses"].extend(gate.invalidate())
        if isinstance(step, CancelStep):
            report["responses"].extend(gate.invalidate())
            report["status"] = "failed"
            report["reason_codes"].append("ATTEMPT_INACTIVE")
        if isinstance(step, NativeStep) and step.message.get("method") == "model/rerouted":
            outcome = {"status": "rejected", "reason": "MODEL_REROUTED"}
            report["responses"].extend(gate.invalidate())
        if isinstance(step, NativeStep) and step.message.get("method") == "account/updated":
            params = step.message.get("params")
            auth_mode = params.get("authMode") if isinstance(params, dict) else None
            report["bindings"]["observed_auth_mode"] = auth_mode
            if auth_mode != "chatgpt":
                outcome = {"status": "rejected", "reason": "AUTH_MODE_MISMATCH"}
                report["responses"].extend(gate.invalidate())
        if (
            isinstance(step, NativeStep)
            and step.message.get("id") == document.thread_start_request_id
        ):
            result = step.message.get("result")
            if isinstance(result, dict):
                observed = {key: result.get(key) for key in expected}
                report["bindings"]["accepted"] = observed
                thread = result.get("thread")
                binding_confirmed = (
                    json.dumps(observed, sort_keys=True) == json.dumps(expected, sort_keys=True)
                    and isinstance(thread, dict)
                    and thread.get("id") == document.attempt.thread_id
                )
                if not binding_confirmed:
                    report["status"] = "failed"
                    report["reason_codes"].append("ACCEPTED_BINDING_MISMATCH")
                    report["responses"].extend(gate.invalidate())
        if isinstance(step, NativeStep) and step.message.get("method") in (
            "turn/started",
            "turn/completed",
        ):
            params = step.message.get("params")
            turn = params.get("turn") if isinstance(params, dict) else None
            if (
                not isinstance(params, dict)
                or not isinstance(turn, dict)
                or (
                    params.get("threadId") != document.attempt.thread_id
                    or turn.get("id") != document.attempt.turn_id
                )
            ):
                outcome = {"status": "rejected", "reason": "TURN_BINDING_MISMATCH"}
                report["responses"].extend(gate.invalidate())
            elif step.message["method"] == "turn/completed":
                if not turn_active or turn_closed:
                    outcome = {"status": "rejected", "reason": "EVENT_ORDER_INVALID"}
                elif turn.get("status") == "failed":
                    outcome = {"status": "rejected", "reason": "NATIVE_TURN_FAILED"}
                elif turn.get("status") == "interrupted":
                    outcome = {"status": "rejected", "reason": "NATIVE_TURN_INTERRUPTED"}
                elif turn.get("status") != "completed" or turn.get("error") is not None:
                    outcome = {"status": "rejected", "reason": "TURN_STATUS_INVALID"}
                turn_active = False
                turn_closed = True
                report["responses"].extend(gate.invalidate())
            elif turn.get("status") != "inProgress" or turn.get("error") is not None:
                outcome = {"status": "rejected", "reason": "TURN_STATUS_INVALID"}
                turn_active = False
                turn_closed = True
                report["responses"].extend(gate.invalidate())
            elif binding_confirmed and not turn_active and not turn_closed:
                turn_active = True
            else:
                outcome = {"status": "rejected", "reason": "EVENT_ORDER_INVALID"}
                report["responses"].extend(gate.invalidate())
        if isinstance(step, NativeStep) and "id" in step.message and "method" in step.message:
            if not binding_confirmed:
                outcome = {"status": "blocked", "reason": "BINDING_UNCONFIRMED"}
                report["responses"].extend(gate.invalidate())
            elif not turn_active:
                outcome = {"status": "blocked", "reason": "TURN_NOT_ACTIVE"}
                report["responses"].extend(gate.invalidate())
            else:
                outcome = gate.register(step.message, expires_at=step.expires_at, now=step.at)
        if isinstance(step, DecisionStep):
            outcome = gate.decide(step.decision, now=step.at)
        if outcome is not None:
            report["permission_outcomes"].append(outcome)
            if "response" in outcome:
                report["responses"].append(outcome["response"])
            report["responses"].extend(outcome.get("additional_responses", []))
            if "reason" in outcome:
                report["status"] = "failed"
                report["reason_codes"].append(outcome["reason"])
    if gate.pending_count:
        if report["status"] == "passed":
            report["status"] = "not_run"
        report["reason_codes"].append("PERMISSION_DECISION_MISSING")
    if not binding_confirmed:
        if report["status"] == "passed":
            report["status"] = "not_run"
        report["reason_codes"].append("BINDING_UNCONFIRMED")
    report["reason_codes"] = sorted(set(report["reason_codes"]))
    return report
