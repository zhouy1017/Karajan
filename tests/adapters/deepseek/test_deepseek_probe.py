import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from karajan.adapters.deepseek.offline import DeepSeekOfflineProbe
from karajan.contracts.probe import ProbeDocument


def runtime_path() -> Path:
    return (
        Path(__file__).resolve().parents[3]
        / "runtimes"
        / "opencode"
        / "node_modules"
        / "opencode-ai"
        / "bin"
        / "opencode.exe"
    )


def test_real_runtime_read_loop_has_atomic_broker_receipt_for_every_local_request(
    tmp_path: Path,
) -> None:
    specification = tmp_path / "case.json"
    specification.write_text(
        json.dumps(
            {
                "schema_version": "karajan.deepseek.offline.v1",
                "scenario": "tool_loop",
                "model": "deepseek-v4-flash",
            }
        )
    )
    directory = tmp_path / "probe"
    report = DeepSeekOfflineProbe(runtime_path(), directory).run_file(specification)
    assert report["status"] == "passed", report
    assert report["runtime_version"] == "1.18.29"
    assert report["final_text"] == "fixture completed: " + report["fixture_text"]
    assert report["tool_output_observed"]
    assert len(report["provider_requests"]) == 2
    assert len(report["receipts"]) == 2
    assert {item["call_id"] for item in report["provider_requests"]} == {
        item["call_id"] for item in report["receipts"]
    }
    assert all(item["state"] == "settled" for item in report["receipts"])
    assert report["ledger"]["budgets"]["CNY"]["held"] == "0.020000"
    assert all(item["body"]["model"] == "deepseek-v4-flash" for item in report["provider_requests"])
    assert report["live_qualification"] == "not_run"
    assert report["cash_api_calls"] == 0
    assert report["profile_enabled"] is False
    document = ProbeDocument.model_validate_json((directory / "probe-document.json").read_bytes())
    assert document.provenance.kind == "fixture"
    assert document.profile.binding.model_id == "deepseek-v4-flash"
    assert document.attempt.id == report["receipts"][0]["attempt_id"]
    assert report["cleanup"]["server"]["status"] == "exited"


def run_case(tmp_path: Path, scenario: str) -> dict[str, object]:
    specification = tmp_path / "case.json"
    specification.write_text(
        json.dumps(
            {
                "schema_version": "karajan.deepseek.offline.v1",
                "scenario": scenario,
                "model": "deepseek-v4-flash",
            }
        )
    )
    return DeepSeekOfflineProbe(runtime_path(), tmp_path / "probe").run_file(specification)


@pytest.mark.parametrize(
    ("scenario", "reason"),
    [
        ("budget_zero", "BUDGET_EXHAUSTED"),
        ("price_expired", "PRICE_UNBOUNDED_OR_EXPIRED"),
        ("unknown_charges", "PRICE_UNBOUNDED_OR_EXPIRED"),
        ("missing_output_price", "PRICE_UNBOUNDED_OR_EXPIRED"),
        ("missing_output_limit", "OUTPUT_UNBOUNDED"),
        ("wire_model_drift", "MODEL_BINDING_MISMATCH"),
    ],
)
def test_refused_wire_request_has_no_fake_provider_receive(
    tmp_path: Path,
    scenario: str,
    reason: str,
) -> None:
    report = run_case(tmp_path, scenario)
    assert report["status"] == "passed", report
    assert report["provider_requests"] == []
    assert len(report["receipts"]) == 1
    assert report["receipts"][0]["reason_code"] == reason
    assert report["ledger"]["calls"] == []
    assert report["ledger"]["budgets"]["CNY"]["held"] == "0.000000"
    assert report["cash_api_calls"] == 0


def test_rate_limit_retry_has_new_call_and_does_not_release_unknown_charge(tmp_path: Path) -> None:
    report = run_case(tmp_path, "rate_limit_once")
    assert report["status"] == "passed", report
    assert report["tool_output_observed"]
    assert [item["state"] for item in report["receipts"]] == ["send_unknown", "settled", "settled"]
    assert len({item["call_id"] for item in report["receipts"]}) == 3
    assert len(report["provider_requests"]) == 3
    assert report["ledger"]["budgets"]["CNY"]["held"] == "0.030000"


@pytest.mark.parametrize("scenario", ["disconnect_once", "missing_usage", "missing_done"])
def test_unknown_response_retains_reservation_and_reopening_does_not_resend(
    tmp_path: Path,
    scenario: str,
) -> None:
    from karajan.resources import ResourceBroker

    report = run_case(tmp_path, scenario)
    assert report["status"] == "passed", report
    assert len(report["provider_requests"]) == 1
    assert report["receipts"][0]["state"] == "send_unknown"
    assert report["ledger"]["budgets"]["CNY"]["held"] == "0.010000"
    reopened = ResourceBroker(tmp_path / "probe" / "resources.sqlite")
    assert reopened.recover() == report["ledger"]
    assert report["final_text"] == ""


def test_missing_runtime_is_rejected_before_creating_any_probe_state(tmp_path: Path) -> None:
    from karajan.adapters.deepseek import ProtocolError

    specification = tmp_path / "case.json"
    specification.write_text(
        json.dumps(
            {
                "schema_version": "karajan.deepseek.offline.v1",
                "scenario": "tool_loop",
                "model": "deepseek-v4-flash",
            }
        )
    )
    with pytest.raises(ProtocolError, match="RUNTIME_UNAVAILABLE"):
        DeepSeekOfflineProbe(tmp_path / "missing-runtime", tmp_path / "probe").run_file(
            specification
        )
    assert not (tmp_path / "probe").exists()


def test_public_cli_runs_fixed_input_without_inherited_auth_or_proxy(tmp_path: Path) -> None:
    specification = tmp_path / "case.json"
    specification.write_text(
        json.dumps(
            {
                "schema_version": "karajan.deepseek.offline.v1",
                "scenario": "tool_loop",
                "model": "deepseek-v4-pro",
            }
        )
    )
    config = tmp_path / "unrelated.json"
    config.write_text('{"model":"unrelated/must-not-run"}')
    environment = dict(os.environ)
    environment.update(
        {
            "DEEPSEEK_API_KEY": "synthetic-poison-credential",
            "OPENCODE_CONFIG": str(config),
            "http_proxy": "http://127.0.0.1:1",
            "HTTP_PROXY": "http://127.0.0.1:1",
            "NO_PROXY": "",
            "no_proxy": "",
        }
    )
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "karajan.adapters.deepseek",
            "probe",
            str(specification),
            "--runtime",
            str(runtime_path()),
            "--directory",
            str(tmp_path / "probe"),
        ],
        capture_output=True,
        text=True,
        timeout=35,
        env=environment,
    )
    assert process.returncode == 0, process.stdout + process.stderr
    summary = json.loads(process.stdout)
    assert summary["status"] == "passed"
    assert summary["live_qualification"] == "not_run"
    report_text = (tmp_path / "probe" / "report.json").read_text()
    assert "synthetic-poison-credential" not in report_text
    assert "unrelated/must-not-run" not in report_text
    assert json.loads(report_text)["provider_requests"][0]["body"]["model"] == "deepseek-v4-pro"


@pytest.mark.parametrize(
    "scenario",
    [
        "config_permission_drift",
        "config_endpoint_drift",
        "config_model_drift",
    ],
)
def test_effective_configuration_drift_stops_before_any_model_request(
    tmp_path: Path,
    scenario: str,
) -> None:
    report = run_case(tmp_path, scenario)
    assert report["status"] == "passed", report
    assert report["reason_codes"] == ["CONFIGURATION_MISMATCH"]
    assert report["configuration_accepted"] is False
    assert report["receipts"] == report["provider_requests"] == []
    assert report["ledger"]["calls"] == []
