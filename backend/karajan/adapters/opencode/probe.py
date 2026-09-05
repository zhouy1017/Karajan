"""Execute an offline fixture using OpenCode's own model and tool loop."""

import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from ._evidence import fixture_identity, persist
from ._fixture import LocalTransport
from ._server import OfficialServer

SCENARIOS = frozenset(
    {
        "tool_loop",
        "rate_limit_once",
        "disconnect_once",
        "timeout_once",
        "cancel_stream",
        "admission_limit",
        "cleanup_fault",
    }
)


@dataclass(frozen=True)
class ProbeReport:
    runtime_version: str
    fixture_secret: str
    final_text: str
    tool_output_observed: bool
    receipts: list[dict[str, Any]]
    provider_requests: list[dict[str, Any]]
    events: list[dict[str, Any]]
    cancellation: dict[str, Any] = field(default_factory=dict)
    status: str = "observed"
    reason_codes: list[str] = field(default_factory=list)
    live_qualified: bool = False
    profile_enabled: bool = False
    qualification_scope: str = "local_fake_provider"
    qualification_decision: str = "rejected"
    qualification_reason_codes: tuple[str, ...] = (
        "MANAGEMENT_TOOL_ISOLATION_UNSUPPORTED",
        "OS_EGRESS_CONTAINMENT_UNSUPPORTED",
        "REAL_PROVIDER_NOT_RUN",
        "CASH_HARD_BUDGET_NOT_RUN",
    )
    capabilities: dict[str, str] = field(
        default_factory=lambda: {
            "management_tool_isolation": "unsupported",
            "os_egress_containment": "unsupported",
            "cash_hard_budget": "not_run",
            "real_provider_auth": "not_run",
            "remote_stop": "not_run",
        }
    )
    cleanup: dict[str, Any] = field(default_factory=dict)
    configuration_accepted: bool = False


class OpenCodeProbe:
    def __init__(self, runtime: Path, directory: Path) -> None:
        self.runtime = runtime
        self.directory = directory.resolve()

    def run(self, scenario: str, *, tamper: str | None = None) -> ProbeReport:
        if scenario not in SCENARIOS:
            raise ValueError("UNKNOWN_SCENARIO")
        if tamper not in {None, "model", "permission", "endpoint"}:
            raise ValueError("UNKNOWN_CONFIGURATION_FAULT")
        runtime = self.runtime.resolve(strict=True)
        self.directory.mkdir(parents=True, exist_ok=False)
        secret = uuid.uuid4().hex
        fixture = self.directory / "workspace" / "fixture.txt"
        profile, manifest = fixture_identity(secret, scenario)
        digest = hashlib.sha256(profile.model_dump_json().encode()).hexdigest()
        transport = LocalTransport(fixture, secret, scenario, manifest, digest)
        server = OfficialServer(
            runtime, self.directory, transport.url, transport.capability, transport.binding_headers
        )
        if scenario == "timeout_once":
            server.config["provider"]["fixture"]["options"].update(
                {"timeout": 500, "headerTimeout": 500}
            )
            server.environment["OPENCODE_CONFIG_CONTENT"] = json.dumps(server.config)
        actual_config = json.loads(json.dumps(server.config))
        if tamper == "model":
            actual_config["model"] = "fixture/other-model"
        elif tamper == "permission":
            actual_config["permission"] = "allow"
        elif tamper == "endpoint":
            actual_config["provider"]["fixture"]["options"]["baseURL"] += "/unexpected"
        server.environment["OPENCODE_CONFIG_CONTENT"] = json.dumps(actual_config)
        fixture.write_text(secret, encoding="utf-8")
        transport.start()
        report: ProbeReport | None = None
        try:
            report = self._observe(scenario, secret, transport, server)
        except Exception as error:
            report = ProbeReport(
                server.version,
                secret,
                "",
                transport.tool_output_observed,
                list(transport.receipts),
                list(transport.requests),
                list(server.events),
                status="unknown",
                reason_codes=["PROBE_INTERRUPTED", type(error).__name__],
            )
            raise
        finally:
            cleanup = self._cleanup(server, transport, scenario)
            if report is not None:
                report = replace(
                    report,
                    cleanup=cleanup,
                    receipts=list(transport.receipts),
                    provider_requests=list(transport.requests),
                    events=list(server.events),
                )
                if cleanup["errors"] or cleanup.get("server", {}).get("status") == "unknown":
                    report = replace(report, status="unknown")
                persist(self.directory, asdict(report), profile, manifest)
        return report

    def _cleanup(
        self, server: OfficialServer, transport: LocalTransport, scenario: str
    ) -> dict[str, Any]:
        cleanup: dict[str, Any] = {"errors": []}
        try:
            cleanup["server"] = server.close()
            cleanup["errors"].extend(cleanup["server"]["errors"])
            if scenario == "cleanup_fault":
                raise RuntimeError("INJECTED_AFTER_SERVER_CLEANUP")
        except Exception as error:
            cleanup["errors"].append(
                "INJECTED_AFTER_SERVER_CLEANUP"
                if scenario == "cleanup_fault"
                else type(error).__name__
            )
        finally:
            try:
                transport.close()
                cleanup["transport"] = "closed"
            except Exception as error:
                cleanup["transport"] = "unknown"
                cleanup["errors"].append(type(error).__name__)
        return cleanup

    def _observe(
        self, scenario: str, secret: str, transport: LocalTransport, server: OfficialServer
    ) -> ProbeReport:
        version = server.start()
        effective = server.request("GET", "/config")
        if any(effective.get(key) != value for key, value in server.config.items()):
            report = ProbeReport(
                version,
                secret,
                "",
                False,
                [],
                [],
                list(server.events),
                status="rejected",
                reason_codes=["CONFIGURATION_MISMATCH"],
            )
            return report
        session = server.request(
            "POST", "/session", {"title": "Karajan local fixture", "agent": "probe"}
        )
        session_id = session["id"]
        server.request(
            "POST",
            f"/session/{session_id}/prompt_async",
            {
                "agent": "probe",
                "model": {"providerID": "fixture", "modelID": "fixture-model"},
                "parts": [{"type": "text", "text": "Read fixture.txt and report its content."}],
            },
        )
        until = time.monotonic() + 20
        final_text = ""
        cancellation: dict[str, Any] = {}
        if scenario == "cancel_stream":
            if not transport.streaming.wait(timeout=10):
                raise TimeoutError("PROVIDER_STREAM_NOT_OBSERVED")
            requested_at = time.time()
            acknowledged = server.request("POST", f"/session/{session_id}/abort")
            observed_from = time.monotonic()
            time.sleep(0.5)
            cancellation = {
                "acknowledged": acknowledged,
                "requested_at": requested_at,
                "requests_after_cancel": sum(
                    receipt["received_at"] > requested_at for receipt in transport.receipts
                ),
                "observation_seconds": time.monotonic() - observed_from,
                "remote_stop": "unknown",
            }
        while time.monotonic() < until:
            if cancellation:
                break
            messages = server.request("GET", f"/session/{session_id}/message")
            for message in messages:
                if message["info"]["role"] == "assistant" and message["info"].get("time", {}).get(
                    "completed"
                ):
                    final_text = "".join(
                        part.get("text", "") for part in message["parts"] if part["type"] == "text"
                    )
            if final_text or any(event["type"] == "session.error" for event in server.events):
                break
            time.sleep(0.05)
        report = ProbeReport(
            version,
            secret,
            final_text,
            transport.tool_output_observed,
            list(transport.receipts),
            list(transport.requests),
            list(server.events),
            cancellation,
            configuration_accepted=True,
            status=(
                "cancel_observed"
                if cancellation
                else "completed"
                if final_text and transport.tool_output_observed
                else "runtime_error"
                if any(event["type"] == "session.error" for event in server.events)
                else "unknown"
            ),
        )
        return report
