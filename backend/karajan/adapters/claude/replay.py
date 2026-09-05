"""Interpret supplied records; never transmit a request or grant a tool permission."""

import hashlib
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .models import RUNTIME_VERSION, ControllerStep, ReplayDocument, Step
from .native import Assistant, RateLimit, Result, Retry, Stream, ToolResult, ToolUse, User
from .usage import UsageEvidence

MAX_INPUT_BYTES = 4 * 1024 * 1024


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError("non-finite JSON number")


def _read(path: Path) -> tuple[ReplayDocument, str]:
    if not path.is_file():
        raise ValueError("regular input file required")
    with path.open("rb") as handle:
        content = handle.read(MAX_INPUT_BYTES + 1)
    if len(content) > MAX_INPUT_BYTES:
        raise ValueError("input too large")
    # The typed JSON decoder preserves strict types and rejects invalid Unicode.
    # A preliminary pass also rejects duplicate keys and non-standard JSON constants.
    json.loads(content, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    return ReplayDocument.model_validate_json(content), hashlib.sha256(content).hexdigest()


def _profile_error(document: ReplayDocument) -> str | None:
    profile = document.profile
    attempt = document.attempt
    digest = hashlib.sha256(
        json.dumps(profile.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if (
        digest != document.profile_digest
        or attempt.profile_id != profile.id
        or attempt.profile_revision != profile.revision
        or attempt.requested_binding != profile.binding
        or attempt.permissions != profile.required_permissions
        or document.provenance.runtime_version != RUNTIME_VERSION
    ):
        return "ATTEMPT_PROFILE_MISMATCH"
    supported_settings = {
        "permission_mode": "dontAsk",
        "permission_prompts": "none",
        "safe_mode": True,
        "restricted": True,
        "tools": ["Read"],
        "setting_sources": [],
        "mcp_servers": [],
        "fallback_model": None,
        "input_format": "text",
        "output_format": "stream-json",
        "verbose": True,
        "include_partial_messages": True,
    }
    binding = profile.binding
    if (
        binding.runtime_kind != "claude-code"
        or binding.runtime_version != RUNTIME_VERSION
        or binding.auth_mode != "claudeai"
        or binding.billing_path != "subscription_only"
        or profile.admission_granularity != "attempt"
        or profile.usage_coverage != "attempt"
        or profile.required_permissions != ["workspace_read"]
        or json.dumps(binding.native_settings, sort_keys=True)
        != json.dumps(supported_settings, sort_keys=True)
    ):
        return "PROFILE_UNSUPPORTED"
    return None


def replay_file(path: Path) -> dict[str, Any]:
    report = _base_report()
    try:
        document, input_digest = _read(path)
        state = _Replay(document, report)
    except (OSError, ValueError, RecursionError, OverflowError):
        report.update(status="failed", reason_codes=["INPUT_INVALID"])
        return report
    error = _profile_error(document)
    if error:
        report.update(status="failed", reason_codes=[error])
        return report
    report["identity"] = {
        "attempt_id": document.attempt.id,
        "fence": document.attempt.fence,
        "profile_id": document.profile.id,
        "profile_revision": document.profile.revision,
        "profile_digest": document.profile_digest,
        "session_id": document.session_id,
        "configuration_source_sha256": document.configuration_source_sha256,
    }
    report["provenance"] = {
        "kind": document.provenance.kind,
        "input_sha256": input_digest,
        "runtime_version": RUNTIME_VERSION,
        "native_stream_verified": False,
    }
    for step in document.steps:
        state.step(step)
    state.finish()
    return report


def _base_report() -> dict[str, Any]:
    return {
        "schema_version": "karajan.claude-replay-report.v1",
        "validation_scope": "offline_protocol_replay",
        "status": "passed",
        "reason_codes": [],
        "qualification": {"live_status": "not_run", "dispatch_eligible": False},
        "stopping": {"main_process": "unknown", "process_tree": "unknown", "remote": "unknown"},
        "result": {"state": "unknown"},
        "result_decisions": {"accepted": 0, "rejected": 0},
        "binding": {"native_reported": {}, "settings_confirmation": "unknown"},
        "events": {"duplicates": 0},
        "permissions": {
            "requires_new_attempt": False,
            "outbound_grants": 0,
            "denial_system_events": 0,
            "terminal_denial_observations": 0,
            "tool_requests_observed": 0,
            "tool_results_observed": 0,
        },
        "stream": {"events": 0},
        "retries": [],
        "native_errors": [],
        "quota": {"observations": [], "account_remaining": None, "basis": "native_advisory"},
        "capabilities": {
            "tool_isolation": "not_run",
            "subscription_authentication": "not_run",
            "billing_path": "not_run",
            "dynamic_permission_grant": "unsupported",
            "process_tree_stop": "not_run",
            "remote_stop": "not_run",
        },
    }


class _Replay:
    def __init__(self, document: ReplayDocument, report: dict[str, Any]):
        self.document = document
        self.report = report
        self.initialized = False
        self.result_gate_open = True
        self.seen_events: dict[str, str] = {}
        self.usage = UsageEvidence()
        self.last_at = document.started_at
        self.deadline = document.started_at + timedelta(
            seconds=document.max_attempt_duration_seconds
        )
        self.seen_controls: dict[str, str] = {}
        self.tool_requests: dict[str, str] = {}

    def step(self, step: Step) -> None:
        if step.at < self.last_at:
            self.fail("EVENT_TIME_REVERSED")
            return
        self.last_at = step.at
        if step.at >= self.deadline:
            self.invalidate("duration_exceeded")
        if isinstance(step, ControllerStep):
            if (
                step.attempt_id != self.document.attempt.id
                or step.fence != self.document.attempt.fence
            ):
                self.fail("CONTROLLER_IDENTITY_MISMATCH")
                return
            fingerprint = step.model_dump_json()
            if step.event_id in self.seen_controls:
                if self.seen_controls[step.event_id] != fingerprint:
                    self.fail("EVENT_ID_CONFLICT")
                return
            self.seen_controls[step.event_id] = fingerprint
            self.invalidate(step.action)
        else:
            self.native(step.message)

    def invalidate(self, reason: str) -> None:
        self.result_gate_open = False
        self.report.setdefault("invalidation", reason)
        if self.report["result"]["state"] == "unknown":
            self.report["result"] = {"state": "invalidated"}

    def finish(self) -> None:
        self.report["usage"] = self.usage.report()
        if self.report["status"] == "passed" and self.report["result"]["state"] == "unknown":
            self.report.update(status="not_run", reason_codes=["TERMINAL_RESULT_MISSING"])

    def fail(self, reason: str) -> None:
        self.report["status"] = "failed"
        if reason not in self.report["reason_codes"]:
            self.report["reason_codes"].append(reason)
        self.result_gate_open = False

    def native(self, message: dict[str, Any]) -> None:
        if message.get("session_id") != self.document.session_id:
            self.fail("NATIVE_SESSION_MISMATCH")
            return
        event_id = message.get("uuid")
        if not isinstance(event_id, str) or not event_id or not event_id.isprintable():
            self.fail("NATIVE_RECORD_INVALID")
            return
        fingerprint = json.dumps(message, sort_keys=True, separators=(",", ":"))
        if event_id in self.seen_events:
            if fingerprint != self.seen_events[event_id]:
                self.fail("EVENT_ID_CONFLICT")
            else:
                self.report["events"]["duplicates"] += 1
            return
        self.seen_events[event_id] = fingerprint
        if message.get("parent_tool_use_id") is not None:
            # The selected Profile cannot launch subagents. Continue parsing only to
            # retain observable usage; this closes business result acceptance.
            self.fail("NATIVE_SUBAGENT_UNSUPPORTED")
        kind = message.get("type")
        if kind == "system" and message.get("subtype") == "init":
            self.init(message)
        elif not self.initialized:
            self.fail("EVENT_SEQUENCE_INVALID")
        elif kind == "result":
            self.result(message)
        elif kind == "assistant":
            self.assistant(message)
        elif kind == "user":
            self.user(message)
        elif kind == "stream_event":
            self.stream(message)
        elif kind == "system" and message.get("subtype") == "permission_denied":
            self.report["permissions"]["denial_system_events"] += 1
            self.permission_denied()
        elif kind == "system" and message.get("subtype") == "api_retry":
            self.retry(message)
        elif kind == "rate_limit_event":
            self.rate_limit(message)
        else:
            self.fail("PROTOCOL_UNSUPPORTED")

    def retry(self, message: dict[str, Any]) -> None:
        if "no_response" in message:
            self.fail("PROTOCOL_VERSION_UNSUPPORTED")
            return
        try:
            record = Retry.model_validate(message)
        except ValidationError:
            self.fail("NATIVE_RECORD_INVALID")
            return
        categories = {
            "authentication_failed",
            "oauth_org_not_allowed",
            "billing_error",
            "rate_limit",
            "overloaded",
            "invalid_request",
            "model_not_found",
            "server_error",
            "max_output_tokens",
            "unknown",
        }
        self.report["retries"].append(
            {
                "attempt": record.attempt,
                "max_retries": record.max_retries,
                "retry_delay_ms": record.retry_delay_ms,
                "http_status": record.error_status,
                "category": record.error if record.error in categories else "unknown",
            }
        )

    def rate_limit(self, message: dict[str, Any]) -> None:
        try:
            record = RateLimit.model_validate(message).rate_limit_info
        except ValidationError:
            self.fail("NATIVE_RECORD_INVALID")
            return
        self.report["quota"]["observations"].append(
            {
                "status": record.status,
                "window": record.rateLimitType,
                "reset_epoch_seconds": record.resetsAt,
                "utilization": record.utilization,
                "overage_status": record.overageStatus,
                "overage_reset_epoch_seconds": record.overageResetsAt,
            }
        )

    def permission_denied(self) -> None:
        self.result_gate_open = False
        self.report["permissions"]["requires_new_attempt"] = True
        if self.report["result"]["state"] == "unknown":
            self.report["result"] = {"state": "blocked", "category": "permission_denied"}

    def assistant(self, message: dict[str, Any]) -> None:
        try:
            record = Assistant.model_validate(message)
        except ValidationError:
            self.fail("NATIVE_RECORD_INVALID")
            return
        if record.error is not None:
            category = {
                "authentication_failed": "authentication_error",
                "billing_error": "billing_error",
                "rate_limit": "rate_limited",
                "invalid_request": "invalid_request",
                "server_error": "server_error",
            }.get(record.error, "unknown")
            self.report["native_errors"].append(category)
        elif record.message.model != self.document.profile.binding.model_id:
            self.fail("NATIVE_BINDING_MISMATCH")
        if not self.usage.assistant(record):
            self.fail("MESSAGE_USAGE_CONFLICT")
        for block in record.message.content:
            self.block(block)

    def block(self, block: dict[str, Any]) -> None:
        kind = block.get("type")
        try:
            if kind == "tool_use":
                tool = ToolUse.model_validate(block)
                if tool.id not in self.tool_requests:
                    self.report["permissions"]["tool_requests_observed"] += 1
                elif self.tool_requests[tool.id] != tool.name:
                    self.fail("TOOL_ID_CONFLICT")
                self.tool_requests[tool.id] = tool.name
                if tool.name != "Read":
                    self.fail("TOOL_OUTSIDE_PROFILE")
            elif kind in ("text", "thinking"):
                if not isinstance(block.get(kind), str):
                    self.fail("NATIVE_RECORD_INVALID")
            else:
                self.fail("PROTOCOL_UNSUPPORTED")
        except ValidationError:
            self.fail("NATIVE_RECORD_INVALID")

    def user(self, message: dict[str, Any]) -> None:
        try:
            record = User.model_validate(message)
            if isinstance(record.message.content, str):
                return
            for block in record.message.content:
                result = ToolResult.model_validate(block)
                if result.tool_use_id not in self.tool_requests:
                    self.fail("TOOL_RESULT_UNBOUND")
                    continue
                self.report["permissions"]["tool_results_observed"] += 1
        except ValidationError:
            self.fail("NATIVE_RECORD_INVALID")

    def stream(self, message: dict[str, Any]) -> None:
        try:
            record = Stream.model_validate(message)
        except ValidationError:
            self.fail("NATIVE_RECORD_INVALID")
            return
        self.report["stream"]["events"] += 1
        payload = record.event
        if payload.message is not None:
            if payload.message.model != self.document.profile.binding.model_id:
                self.fail("NATIVE_BINDING_MISMATCH")
            snapshot = Assistant(
                type="assistant",
                uuid=record.uuid,
                session_id=record.session_id,
                parent_tool_use_id=record.parent_tool_use_id,
                message=payload.message,
            )
            if not self.usage.assistant(snapshot):
                self.fail("MESSAGE_USAGE_CONFLICT")
        if payload.content_block is not None:
            self.block(payload.content_block)

    def init(self, message: dict[str, Any]) -> None:
        if self.initialized:
            self.fail("EVENT_SEQUENCE_INVALID")
            return
        expected = {
            "model": self.document.profile.binding.model_id,
            "claude_code_version": RUNTIME_VERSION,
            "cwd": self.document.cwd,
            "permissionMode": "dontAsk",
            "tools": ["Read"],
            "mcp_servers": [],
            "plugins": [],
        }
        if any(message.get(key) != value for key, value in expected.items()):
            self.fail("NATIVE_BINDING_MISMATCH")
            return
        self.initialized = True
        self.report["binding"] = {
            "settings_confirmation": "partial",
            "native_reported": {
                "model_id": message["model"],
                "runtime_version": RUNTIME_VERSION,
                "tools": ["Read"],
                "permission_mode": "dontAsk",
                "auth_mode": None,
                "billing_path": None,
            },
        }

    def result(self, message: dict[str, Any]) -> None:
        try:
            result = Result.model_validate(message)
        except ValidationError:
            self.fail("NATIVE_RECORD_INVALID")
            return
        self.usage.result(result)
        if not result.is_error and self.report["native_errors"]:
            self.fail("NATIVE_OUTCOME_CONFLICT")
        if any(model != self.document.profile.binding.model_id for model in result.modelUsage):
            self.fail("NATIVE_BINDING_MISMATCH")
        if result.permission_denials:
            self.report["permissions"]["terminal_denial_observations"] += len(
                result.permission_denials
            )
            self.permission_denied()
        if self.result_gate_open:
            self.report["result_decisions"]["accepted"] += 1
            if result.is_error:
                category = result.category()
                if category == "execution_error" and self.report["native_errors"]:
                    category = self.report["native_errors"][-1]
                self.report["result"] = {"state": "failed", "category": category}
            else:
                text = result.result or ""
                self.report["result"] = {
                    "state": "completed",
                    "text_length": len(text),
                    "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                }
            self.result_gate_open = False
        else:
            self.report["result_decisions"]["rejected"] += 1
