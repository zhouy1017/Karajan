import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from karajan.adapters.opencode import OpenCodeProbe
from karajan.contracts.probe import ProbeDocument


def runtime_path() -> Path:
    root = Path(__file__).resolve().parents[3]
    name = "opencode.exe" if os.name == "nt" else "opencode"
    return root / "runtimes" / "opencode" / "node_modules" / "opencode-ai" / "bin" / name


def test_official_runtime_completes_a_real_read_tool_loop_through_the_local_broker(
    tmp_path: Path,
) -> None:
    report = OpenCodeProbe(runtime_path(), tmp_path / "probe").run("tool_loop")
    assert report.runtime_version == "1.18.29"
    assert report.final_text == f"fixture completed: {report.fixture_secret}"
    assert report.tool_output_observed
    assert report.provider_requests
    assert all(request["body"]["model"] == "fixture-model" for request in report.provider_requests)
    assert len({receipt["receipt_id"] for receipt in report.receipts}) == len(report.receipts)
    assert all(receipt["admitted"] for receipt in report.receipts)
    assert report.events
    assert not report.live_qualified


def test_probe_persists_replayable_evidence_using_the_shared_identity_contract(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "probe"
    report = OpenCodeProbe(runtime_path(), directory).run("tool_loop")
    document = ProbeDocument.model_validate_json((directory / "probe-document.json").read_text())
    evidence = json.loads((directory / "report.json").read_text())
    assert document.attempt.id == report.receipts[0]["attempt_id"]
    assert document.attempt.fence == report.receipts[0]["fence"]
    assert document.profile.id == report.receipts[0]["profile_id"]
    assert evidence["live_qualified"] is False
    assert evidence["profile_enabled"] is False
    assert evidence["capabilities"]["management_tool_isolation"] == "unsupported"
    assert evidence["capabilities"]["cash_hard_budget"] == "not_run"
    assert (
        json.loads((directory / "provider-requests.json").read_text()) == report.provider_requests
    )


def test_cleanup_failure_preserves_observations_and_still_closes_local_peers(
    tmp_path: Path,
) -> None:
    import socket

    directory = tmp_path / "probe"
    report = OpenCodeProbe(runtime_path(), directory).run("cleanup_fault")
    evidence = json.loads((directory / "report.json").read_text())
    assert report.status == "unknown"
    assert evidence["cleanup"]["server"]["status"] == "exited"
    assert evidence["cleanup"]["transport"] == "closed"
    assert "INJECTED_AFTER_SERVER_CLEANUP" in evidence["cleanup"]["errors"]
    assert len(json.loads((directory / "provider-requests.json").read_text())) == 2
    port = int(report.receipts[0]["headers"]["host"].split(":")[1])
    with socket.socket() as connection:
        connection.settimeout(1)
        assert connection.connect_ex(("127.0.0.1", port)) != 0


@pytest.mark.parametrize("poison_proxy", [False, True])
def test_public_cli_uses_isolated_configuration_and_never_promotes_live_qualification(
    tmp_path: Path,
    poison_proxy: bool,
) -> None:
    unrelated_config = tmp_path / "inherited.json"
    unrelated_config.write_text(json.dumps({"model": "unrelated/must-not-run"}), encoding="utf-8")
    environment = dict(os.environ)
    environment.update(
        {
            "OPENCODE_CONFIG": str(unrelated_config),
            "OPENAI_API_KEY": "synthetic-poison-not-a-real-key",
        }
    )
    if poison_proxy:
        environment.update(
            {
                "HTTP_PROXY": "http://127.0.0.1:1",
                "http_proxy": "http://127.0.0.1:1",
                "HTTPS_PROXY": "http://127.0.0.1:1",
                "https_proxy": "http://127.0.0.1:1",
                "NO_PROXY": "",
                "no_proxy": "",
            }
        )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "karajan.adapters.opencode",
            "--runtime",
            str(runtime_path()),
            "--directory",
            str(tmp_path / "probe"),
            "--scenario",
            "tool_loop",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=environment,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    summary = json.loads(result.stdout)
    assert summary["live_qualified"] is False
    assert summary["qualification_decision"] == "rejected"
    trace = (tmp_path / "probe" / "broker-receipts.json").read_text()
    assert "synthetic-poison" not in trace
    assert "unrelated/must-not-run" not in trace
    assert json.loads(trace)[0]["body"]["model"] == "fixture-model"


def test_every_request_including_the_second_tool_step_needs_fresh_admission(tmp_path: Path) -> None:
    report = OpenCodeProbe(runtime_path(), tmp_path / "probe").run("admission_limit")
    assert len(report.receipts) == 2
    assert [receipt["admitted"] for receipt in report.receipts] == [True, False]
    assert report.receipts[1]["rejection_reason"] == "FIXTURE_CALL_LIMIT"
    assert len(report.provider_requests) == 1
    assert report.final_text == ""


@pytest.mark.parametrize("tamper", ["model", "permission", "endpoint"])
def test_changed_effective_configuration_cannot_pass_as_the_fixed_profile(
    tmp_path: Path, tamper: str
) -> None:
    report = OpenCodeProbe(runtime_path(), tmp_path / "probe").run("tool_loop", tamper=tamper)
    assert report.status == "rejected"
    assert report.reason_codes == ["CONFIGURATION_MISMATCH"]
    assert report.receipts == []
    assert not report.live_qualified


def test_runtime_retry_after_429_is_a_new_admitted_transport_receipt(tmp_path: Path) -> None:
    report = OpenCodeProbe(runtime_path(), tmp_path / "probe").run("rate_limit_once")
    assert report.tool_output_observed
    assert [request["response_status"] for request in report.provider_requests] == [429, 200, 200]
    assert len(report.receipts) == 3
    assert all(receipt["logical_call_id"] is None for receipt in report.receipts)
    assert len({receipt["receipt_id"] for receipt in report.receipts}) == 3


def test_disconnection_exposes_real_runtime_retries(tmp_path: Path) -> None:
    report = OpenCodeProbe(runtime_path(), tmp_path / "probe").run("disconnect_once")
    assert report.tool_output_observed
    assert len(report.provider_requests) == 3
    assert report.provider_requests[0]["fault"] == "disconnect_once"
    assert len(report.receipts) == 3
    assert any(
        event.get("type") == "session.status"
        and event.get("properties", {}).get("status", {}).get("type") == "retry"
        for event in report.events
    )


def test_header_timeout_is_reported_as_error_without_inventing_a_retry(tmp_path: Path) -> None:
    report = OpenCodeProbe(runtime_path(), tmp_path / "probe").run("timeout_once")
    assert report.status == "runtime_error"
    assert report.final_text == ""
    assert len(report.receipts) == 1
    assert report.provider_requests[0]["fault"] == "timeout_once"
    errors = [event for event in report.events if event["type"] == "session.error"]
    assert len(errors) == 1
    assert errors[0]["properties"]["error"]["data"]["message"] == "The operation timed out."


def test_abort_records_bounded_post_cancel_observation_without_claiming_remote_stop(
    tmp_path: Path,
) -> None:
    report = OpenCodeProbe(runtime_path(), tmp_path / "probe").run("cancel_stream")
    assert report.cancellation["acknowledged"]
    assert report.cancellation["requests_after_cancel"] == 0
    assert report.cancellation["observation_seconds"] >= 0.5
    assert report.cancellation["remote_stop"] == "unknown"
    assert not report.live_qualified
