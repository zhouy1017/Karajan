"""Run a fixed, local-only fixture; no production or live-account entry point."""

import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Any

from karajan.adapters.opencode._evidence import fixture_identity, persist
from karajan.adapters.opencode._server import OfficialServer

from ._local_transport import LocalExchange
from .protocol import MODELS, ProtocolError, _object

SCENARIOS = frozenset(
    {
        "tool_loop",
        "budget_zero",
        "price_expired",
        "unknown_charges",
        "missing_output_price",
        "missing_output_limit",
        "wire_model_drift",
        "rate_limit_once",
        "disconnect_once",
        "missing_usage",
        "missing_done",
        "config_permission_drift",
        "config_endpoint_drift",
        "config_model_drift",
    }
)


class DeepSeekOfflineProbe:
    def __init__(self, runtime: Path, directory: Path) -> None:
        self.runtime, self.directory = runtime, directory.resolve()

    def run_file(self, spec: Path) -> dict[str, Any]:
        with spec.open("rb") as stream:
            specification = _object(stream.read(65537), 65536)
        if (
            set(specification) != {"schema_version", "scenario", "model"}
            or specification["schema_version"] != "karajan.deepseek.offline.v1"
            or not isinstance(specification["scenario"], str)
            or specification["scenario"] not in SCENARIOS
            or not isinstance(specification["model"], str)
            or specification["model"] not in MODELS
        ):
            raise ProtocolError("OFFLINE_SPECIFICATION_INVALID")
        try:
            runtime = self.runtime.resolve(strict=True)
            if not runtime.is_file():
                raise OSError
        except OSError:
            raise ProtocolError("RUNTIME_UNAVAILABLE") from None
        self.directory.mkdir(parents=True, exist_ok=False)
        fixture_text = uuid.uuid4().hex
        fixture = self.directory / "workspace" / "fixture.txt"
        model = specification["model"]
        profile, attempt = fixture_identity(fixture_text, specification["scenario"])
        binding = profile.binding.model_copy(
            update={"model_id": model, "channel_id": "deepseek-protocol-local-fixture"}
        )
        profile = profile.model_copy(update={"id": "deepseek-offline", "binding": binding})
        attempt = attempt.model_copy(
            update={"profile_id": profile.id, "requested_binding": binding}
        )
        digest = hashlib.sha256(profile.model_dump_json().encode()).hexdigest()
        exchange = LocalExchange(
            self.directory, specification["scenario"], model, fixture, fixture_text, attempt, digest
        )
        server = OfficialServer(
            runtime,
            self.directory,
            exchange.url,
            exchange.capability,
            exchange.binding_headers,
        )
        models = server.config["provider"]["fixture"]["models"]
        models[model] = models.pop("fixture-model")
        server.config["model"] = server.config["small_model"] = "fixture/" + model
        actual_config = json.loads(json.dumps(server.config))
        if specification["scenario"] == "config_permission_drift":
            actual_config["permission"] = "allow"
        elif specification["scenario"] == "config_endpoint_drift":
            actual_config["provider"]["fixture"]["options"]["baseURL"] += "/unexpected"
        elif specification["scenario"] == "config_model_drift":
            actual_config["model"] = "fixture/other-model"
        server.environment["OPENCODE_CONFIG_CONTENT"] = json.dumps(actual_config)
        fixture.write_text(fixture_text, encoding="utf-8")
        report: dict[str, Any] = {
            "schema_version": "karajan.deepseek.offline.report.v1",
            "status": "unknown",
            "scenario": specification["scenario"],
            "fixture_text": fixture_text,
            "final_text": "",
            "tool_output_observed": False,
            "configuration_accepted": False,
            "runtime_version": "unknown",
            "profile_enabled": False,
            "live_qualified": False,
            "live_qualification": "not_run",
            "cash_api_calls": 0,
            "real_model_calls": 0,
            "billing_scope": "synthetic-flat-price",
            "qualification_scope": "offline_local_fixture",
            "limitations": [
                "No real DeepSeek API requests, credentials, or billing evidence.",
                "No production egress or management credential isolation proof.",
                "Existing local /infer broker envelope; not a live DeepSeek transport.",
            ],
        }
        exchange.start()
        try:
            report["runtime_version"] = server.start()
            effective = server.request("GET", "/config")
            if any(effective.get(k) != value for k, value in server.config.items()):
                raise ProtocolError("CONFIGURATION_MISMATCH")
            report["configuration_accepted"] = True
            session = server.request(
                "POST", "/session", {"title": "DeepSeek offline fixture", "agent": "probe"}
            )
            server.request(
                "POST",
                f"/session/{session['id']}/prompt_async",
                {
                    "agent": "probe",
                    "model": {"providerID": "fixture", "modelID": model},
                    "parts": [{"type": "text", "text": "Read fixture.txt and report its content."}],
                },
            )
            until = time.monotonic() + 20
            while time.monotonic() < until:
                messages = server.request("GET", f"/session/{session['id']}/message")
                for message in messages:
                    if message["info"]["role"] == "assistant" and message["info"].get(
                        "time", {}
                    ).get("completed"):
                        report["final_text"] = "".join(
                            part.get("text", "")
                            for part in message["parts"]
                            if part["type"] == "text"
                        )
                if report["final_text"] or any(e["type"] == "session.error" for e in server.events):
                    break
                time.sleep(0.05)
        except Exception as error:
            report["reason_codes"] = [
                str(error) if isinstance(error, ProtocolError) else type(error).__name__
            ]
        finally:
            cleanup: dict[str, Any] = {"errors": []}
            try:
                cleanup["server"] = server.close()
                cleanup["errors"].extend(cleanup["server"]["errors"])
            except Exception as error:
                cleanup["errors"].append(type(error).__name__)
            try:
                exchange.close()
                cleanup["transport"] = "closed"
            except Exception as error:
                cleanup["errors"].append(type(error).__name__)
            report.update(
                cleanup=cleanup,
                receipts=exchange.receipts,
                provider_requests=exchange.requests,
                response_observations=exchange.observations,
                ledger=exchange.broker.snapshot(),
                events=list(server.events),
                tool_output_observed=exchange.tool_output_observed,
            )
            report["conditions"] = _conditions(report)
            report["status"] = "passed" if all(report["conditions"].values()) else "failed"
            if cleanup["errors"]:
                report["status"] = "unknown"
            persist(self.directory, report, profile, attempt)
        return report


def _conditions(report: dict[str, Any]) -> dict[str, bool]:
    scenario, receipts, requests = (
        report["scenario"],
        report["receipts"],
        report["provider_requests"],
    )
    codes = {
        "budget_zero": "BUDGET_EXHAUSTED",
        "price_expired": "PRICE_UNBOUNDED_OR_EXPIRED",
        "unknown_charges": "PRICE_UNBOUNDED_OR_EXPIRED",
        "missing_output_price": "PRICE_UNBOUNDED_OR_EXPIRED",
        "missing_output_limit": "OUTPUT_UNBOUNDED",
        "wire_model_drift": "MODEL_BINDING_MISMATCH",
    }
    common = {
        "configuration_accepted": report["configuration_accepted"],
        "server_exited": report["cleanup"].get("server", {}).get("status") == "exited",
        "no_live_qualification": report["live_qualification"] == "not_run",
        "no_cash_api_calls": report["cash_api_calls"] == 0,
    }
    if scenario.startswith("config_"):
        return {
            **common,
            "configuration_accepted": report["configuration_accepted"] is False,
            "rejected_before_requests": not receipts and not requests,
            "explicit_mismatch": report.get("reason_codes") == ["CONFIGURATION_MISMATCH"],
            "no_calls": not report["ledger"]["calls"],
        }
    if scenario in codes:
        return {
            **common,
            "zero_provider_receives": not requests,
            "one_rejected_receipt": len(receipts) == 1
            and receipts[0]["reason_code"] == codes[scenario],
            "no_call_or_bill": not report["ledger"]["calls"]
            and report["ledger"]["budgets"]["CNY"]["held"] == "0.000000",
        }
    if scenario in {"missing_done", "missing_usage", "disconnect_once"}:
        return {
            **common,
            "one_unknown_request": len(requests) == len(receipts) == 1
            and receipts[0]["state"] == "send_unknown",
            "held_unknown": report["ledger"]["budgets"]["CNY"]["held"] == "0.010000",
            "no_runtime_completion": report["final_text"] == "",
        }
    expected = 3 if scenario == "rate_limit_once" else 2
    return {
        **common,
        "tool_loop": report["tool_output_observed"]
        and report["final_text"] == "fixture completed: " + report["fixture_text"],
        "bounded_requests": len(receipts) == len(requests) == expected,
        "one_receipt_per_call": len({r["call_id"] for r in receipts}) == expected,
        "expected_spending": report["ledger"]["budgets"]["CNY"]["held"]
        == ("0.030000" if expected == 3 else "0.020000"),
        "expected_outcomes": [r["state"] for r in receipts]
        == (["send_unknown"] if expected == 3 else []) + ["settled", "settled"],
    }
