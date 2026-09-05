"""Synthetic profile identity and evidence; never enables a real account."""

import json
import platform
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from karajan.contracts.probe import (
    AcceptedBindingEvent,
    AttemptManifest,
    Binding,
    CapabilityResultEvent,
    Observation,
    ProbeDocument,
    Profile,
    Provenance,
)


def fixture_identity(identity: str, scenario: str) -> tuple[Profile, AttemptManifest]:
    binding = Binding(
        model_id="fixture-model",
        channel_id="local-fake-http",
        account_id="no-real-account",
        runtime_kind="opencode-server",
        runtime_version="1.18.29",
        auth_mode="synthetic-capability",
        billing_path="api_cash",
        native_settings={
            "protocol": "chat_completions",
            "max_tokens": 256,
            "tools": ["read"],
            "scenario": scenario,
            "network_destination": "local-fixture-only",
        },
    )
    profile = Profile(
        id="opencode-fixture",
        revision=1,
        binding=binding,
        auth_ref="synthetic-no-provider-key",
        required_permissions=["read"],
        admission_granularity="model_call",
        usage_coverage="unknown",
    )
    attempt = AttemptManifest(
        id=f"attempt-{identity}",
        fence=1,
        role="worker",
        profile_id=profile.id,
        profile_revision=profile.revision,
        authorization_ref="offline-fixture-only",
        budget_ref="no-cash-calls",
        permissions=["read"],
        requested_binding=binding,
    )
    return profile, attempt


def persist(
    directory: Path, report: dict[str, Any], profile: Profile, attempt: AttemptManifest
) -> None:
    files = {
        "report.json": report,
        "broker-receipts.json": report["receipts"],
        "provider-requests.json": report["provider_requests"],
        "server-events.json": report["events"],
    }
    for name, content in files.items():
        (directory / name).write_text(
            json.dumps(content, indent=2, ensure_ascii=True), encoding="utf-8"
        )
    common: dict[str, Any] = {
        "attempt_id": attempt.id,
        "fence": attempt.fence,
        "profile_id": profile.id,
        "profile_revision": profile.revision,
    }
    events: list[Observation] = []
    if report["configuration_accepted"]:
        events.append(
            AcceptedBindingEvent(
                type="binding.accepted",
                event_id="configuration-accepted",
                binding=profile.binding,
                **common,
            )
        )
    events.append(
        CapabilityResultEvent(
            type="capability.result",
            event_id="local-observation",
            capability="local_transport_observed",
            status="passed" if report["receipts"] else "failed",
            evidence_refs=list(files),
            limitations=[
                "Synthetic peer observations only; real accounts and cash remain unqualified."
            ],
            **common,
        )
    )
    document = ProbeDocument(
        schema_version="karajan.probe.v1",
        case_id=attempt.id,
        profile=profile,
        attempt=attempt,
        required_capabilities=["local_transport_observed"],
        events=events,
        provenance=Provenance(
            kind="fixture",
            runtime_version=report["runtime_version"],
            os=platform.system(),
            isolation="local_guarded",
            observed_at=datetime.now(UTC),
            evidence_refs=list(files),
            limitations=[
                "Management credentials share the server/tool OS identity.",
                "No OS egress containment or provider billing proof.",
            ],
        ),
    )
    (directory / "probe-document.json").write_text(
        document.model_dump_json(indent=2), encoding="utf-8"
    )
