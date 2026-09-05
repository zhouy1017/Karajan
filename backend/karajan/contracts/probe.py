"""Inspect a supplied probe without launching a runtime or making a request."""

import hashlib
import json
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, ValidationError

Identifier = Annotated[str, Field(min_length=1, max_length=256, pattern=r"^\S+$")]
PositiveInteger = Annotated[int, Field(gt=0)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Binding(Contract):
    model_id: Identifier
    channel_id: Identifier
    account_id: Identifier
    runtime_kind: Identifier
    runtime_version: Identifier
    auth_mode: Identifier
    billing_path: Literal["subscription_only", "api_cash"]
    native_settings: dict[str, JsonValue]


class Profile(Contract):
    id: Identifier
    revision: PositiveInteger
    binding: Binding
    auth_ref: Identifier
    required_permissions: list[Identifier]
    admission_granularity: Literal["attempt", "model_call"]
    usage_coverage: Literal["attempt", "model_call", "unknown"]


class AttemptManifest(Contract):
    id: Identifier
    fence: PositiveInteger
    role: Literal["commander", "worker", "reviewer"]
    profile_id: Identifier
    profile_revision: PositiveInteger
    authorization_ref: Identifier
    budget_ref: Identifier
    permissions: list[Identifier]
    requested_binding: Binding


class Provenance(Contract):
    kind: Literal["fixture", "imported_observation"]
    runtime_version: Identifier
    os: Identifier
    isolation: Identifier
    observed_at: AwareDatetime
    evidence_refs: list[Identifier]
    limitations: list[str]


class ScriptEvent(Contract):
    event_id: Identifier
    attempt_id: Identifier
    fence: PositiveInteger
    profile_id: Identifier
    profile_revision: PositiveInteger


class AcceptedBindingEvent(ScriptEvent):
    type: Literal["binding.accepted"]
    binding: Binding


class ReportedBinding(Contract):
    model_id: Identifier | None = None
    channel_id: Identifier | None = None
    account_id: Identifier | None = None
    runtime_kind: Identifier | None = None
    runtime_version: Identifier | None = None
    auth_mode: Identifier | None = None
    billing_path: Literal["subscription_only", "api_cash"] | None = None
    native_settings: dict[str, JsonValue] | None = None


class ProviderBindingEvent(ScriptEvent):
    type: Literal["binding.provider_reported"]
    binding: ReportedBinding


class CapabilityResultEvent(ScriptEvent):
    type: Literal["capability.result"]
    capability: Identifier
    status: Literal["passed", "failed", "not_run", "unsupported"]
    evidence_refs: list[Identifier]
    limitations: list[str]


Observation = Annotated[
    AcceptedBindingEvent | ProviderBindingEvent | CapabilityResultEvent, Field(discriminator="type")
]


class ProbeDocument(Contract):
    schema_version: Literal["karajan.probe.v1"]
    case_id: Identifier
    profile: Profile
    attempt: AttemptManifest
    required_capabilities: Annotated[list[Identifier], Field(min_length=1)]
    events: list[Observation]
    provenance: Provenance


def _base_report(case_id: str | None, status: str, reason_codes: list[str]) -> dict[str, Any]:
    return {
        "schema_version": "karajan.qualification.v1",
        "case_id": case_id,
        "status": status,
        "qualification_scope": "offline_contract",
        "live_qualified": False,
        "profile_enabled": False,
        "reason_codes": reason_codes,
        "validation_issues": [],
        "summary": f"Offline contract {status}; no runtime was executed or qualified.",
    }


def inspect_probe_file(path: Path) -> dict[str, Any]:
    try:
        input_bytes = path.read_bytes()
        document = ProbeDocument.model_validate_json(input_bytes.decode("utf-8-sig"))
    except (OSError, UnicodeError) as error:
        return _base_report(
            None,
            "failed",
            ["INPUT_UNREADABLE" if isinstance(error, OSError) else "INPUT_ENCODING_INVALID"],
        )
    except ValidationError as error:
        issues = [
            {
                "path": ".".join(str(part) for part in issue["loc"]),
                "code": (
                    "REQUIRED_FIELD"
                    if issue["type"] == "missing"
                    else "UNKNOWN_FIELD"
                    if issue["type"] == "extra_forbidden"
                    else "INVALID_VALUE"
                ),
            }
            for issue in error.errors(include_url=False, include_context=False, include_input=False)
        ]
        invalid_report = _base_report(None, "failed", ["INPUT_INVALID"])
        invalid_report["validation_issues"] = issues
        return invalid_report
    report: dict[str, Any] = {
        **_base_report(document.case_id, "passed", []),
        "input_sha256": hashlib.sha256(input_bytes).hexdigest(),
        "profile": {"id": document.profile.id, "revision": document.profile.revision},
        "attempt": {
            "id": document.attempt.id,
            "fence": document.attempt.fence,
            "role": document.attempt.role,
        },
        "coverage": {
            "source": "profile_declaration",
            "admission_granularity": document.profile.admission_granularity,
            "usage_coverage": document.profile.usage_coverage,
            "observed_model_call_count": None,
        },
        "provenance": document.provenance.model_dump(mode="json"),
        "capabilities": [],
        "binding_observations": {
            "requested": binding_observation(document.attempt.requested_binding, "requested"),
            "accepted": {"state": "unknown"},
            "provider_reported": [],
        },
    }
    if not set(document.profile.required_permissions).issubset(document.attempt.permissions):
        report["status"] = "failed"
        report["reason_codes"].append("REQUIRED_PERMISSION_MISSING")
    if (document.attempt.profile_id, document.attempt.profile_revision) != (
        document.profile.id,
        document.profile.revision,
    ):
        report["status"] = "failed"
        report["reason_codes"].append("PROFILE_IDENTITY_MISMATCH")
    if not same_json(document.attempt.requested_binding, document.profile.binding):
        report["status"] = "failed"
        report["reason_codes"].append("REQUESTED_BINDING_MISMATCH")
    if document.provenance.runtime_version != document.profile.binding.runtime_version:
        report["status"] = "failed"
        report["reason_codes"].append("PROVENANCE_RUNTIME_MISMATCH")
    unique_events: dict[
        str, AcceptedBindingEvent | ProviderBindingEvent | CapabilityResultEvent
    ] = {}
    for event in document.events:
        if event.event_id in unique_events and not same_json(unique_events[event.event_id], event):
            report["status"] = "failed"
            report["reason_codes"].append("EVENT_ID_CONFLICT")
        else:
            unique_events[event.event_id] = event
    report["event_summary"] = {
        "unique_count": len(unique_events),
        "duplicate_count": len(document.events) - len(unique_events),
    }
    capability_results: dict[str, str] = {}
    for event in unique_events.values():
        if (event.attempt_id, event.fence, event.profile_id, event.profile_revision) != (
            document.attempt.id,
            document.attempt.fence,
            document.profile.id,
            document.profile.revision,
        ):
            report["status"] = "failed"
            report["reason_codes"].append("EVENT_IDENTITY_MISMATCH")
            continue
        if isinstance(event, ProviderBindingEvent):
            reported_fields = event.binding.model_dump(exclude_none=True)
            observation = binding_observation(
                event.binding, "observed" if reported_fields else "unknown"
            )
            observation["fields"] = sorted(reported_fields)
            if event.binding.native_settings is not None:
                observation["native_settings_fields"] = sorted(event.binding.native_settings)
            report["binding_observations"]["provider_reported"].append(observation)
            requested_fields = document.attempt.requested_binding.model_dump()
            if not reported_matches(requested_fields, reported_fields):
                report["status"] = "failed"
                report["reason_codes"].append("PROVIDER_BINDING_MISMATCH")
        if isinstance(event, AcceptedBindingEvent):
            report["binding_observations"]["accepted"] = binding_observation(
                event.binding, "observed"
            )
            if not same_json(event.binding, document.attempt.requested_binding):
                report["status"] = "failed"
                report["reason_codes"].append("ACCEPTED_BINDING_MISMATCH")
        if isinstance(event, CapabilityResultEvent):
            if event.capability in capability_results:
                if capability_results[event.capability] != event.status:
                    report["status"] = "failed"
                    report["reason_codes"].append("CAPABILITY_RESULT_CONFLICT")
            capability_results[event.capability] = event.status
            status = event.status
            if status == "passed" and (
                not event.evidence_refs
                or not set(event.evidence_refs).issubset(document.provenance.evidence_refs)
            ):
                status = "not_run"
                report["reason_codes"].append("CAPABILITY_EVIDENCE_MISSING")
            report["capabilities"].append(
                {
                    "event_id": event.event_id,
                    "capability": event.capability,
                    "status": status,
                    "evidence_refs": event.evidence_refs,
                    "limitations": event.limitations,
                }
            )
            if status != "passed":
                priority = {"passed": 0, "not_run": 1, "unsupported": 2, "failed": 3}
                if priority[status] > priority[report["status"]]:
                    report["status"] = status
                report["reason_codes"].append("CAPABILITY_" + status.upper())
    if report["binding_observations"]["accepted"]["state"] == "unknown":
        if report["status"] == "passed":
            report["status"] = "not_run"
        report["reason_codes"].append("BINDING_UNCONFIRMED")
    observed_capabilities = {entry["capability"] for entry in report["capabilities"]}
    for capability in document.required_capabilities:
        if capability not in observed_capabilities:
            if report["status"] == "passed":
                report["status"] = "not_run"
            report["reason_codes"].append("CAPABILITY_MISSING")
            report["capabilities"].append({"capability": capability, "status": "not_run"})
    report["reason_codes"] = sorted(set(report["reason_codes"]))
    report["summary"] = (
        f"Offline contract {report['status']}; no runtime was executed or qualified."
    )
    return report


def binding_observation(binding: Binding | ReportedBinding, state: str) -> dict[str, Any]:
    values = binding.model_dump(exclude_none=True)
    if "native_settings" in values:
        settings = values.pop("native_settings")
        values["native_settings_sha256"] = hashlib.sha256(
            json.dumps(settings, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    return {"state": state, "values": values}


def same_json(left: BaseModel, right: BaseModel) -> bool:
    return json.dumps(left.model_dump(mode="json"), sort_keys=True) == json.dumps(
        right.model_dump(mode="json"), sort_keys=True
    )


def reported_matches(requested: JsonValue, reported: JsonValue) -> bool:
    if isinstance(reported, dict):
        return isinstance(requested, dict) and all(
            key in requested and reported_matches(requested[key], value)
            for key, value in reported.items()
        )
    return json.dumps(requested, sort_keys=True) == json.dumps(reported, sort_keys=True)
