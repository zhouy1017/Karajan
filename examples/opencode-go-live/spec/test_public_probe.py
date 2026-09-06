"""Independent public Go probe boundaries, with synthetic peers and zero network."""

import copy
import json
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest
from karajan.adapters.opencode import go_live
from karajan.adapters.opencode._server import OfficialServer
from karajan.adapters.opencode.go_evidence import DENIAL_PREFIX, MODEL

SECRET = "synthetic_go_spec_credential_12345"
FIXED = "def clamp(value, low, high):\n    return max(low, min(value, high))\n"


@pytest.fixture
def harness(tmp_path, monkeypatch):
    runtime = tmp_path / "synthetic-runtime.exe"
    runtime.write_bytes(b"Never executed by this test")
    credential = tmp_path / "synthetic-credential.txt"
    credential.write_text(SECRET, encoding="ascii")
    state = SimpleNamespace(
        scenario="edit",
        fault=None,
        prompt_calls=0,
        server_closed=0,
        relay_closed=0,
        relay_started=0,
        relay_constructed=0,
        network_attempts=0,
        server=None,
        message_polls=0,
        abort_calls=0,
    )

    def no_network(*args, **kwargs):
        state.network_attempts += 1
        raise AssertionError("Unexpected network in synthetic Spec test")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)

    class SyntheticRelay:
        def __init__(self, secret, canary):
            assert secret == SECRET
            state.relay_constructed += 1
            self.canary = canary
            self.url = "http://127.0.0.1:1/v1"
            self.capability = "synthetic-capability-only"

        def start(self):
            state.relay_started += 1

        @property
        def receipts(self):
            if state.fault == "empty_receipts":
                return []
            return [
                {
                    "sequence": 1,
                    "requested_model": MODEL,
                    "reported_models": [MODEL],
                    "upstream_send_attempted": True,
                    "upstream_status": 200,
                    "stream_terminated": True,
                    "relay_completed": True,
                    "protocol_passed": state.fault not in {"provider_error", "pending_relay_error"},
                    "reason_codes": ["UPSTREAM_HTTP_ERROR"]
                    if state.fault in {"provider_error", "pending_relay_error"}
                    else [],
                    "denied_canary_in_request": state.fault == "canary_upstream",
                }
            ]

        def close(self):
            state.relay_closed += 1
            return {
                "status": "unknown" if state.fault == "relay_cleanup" else "closed",
                "errors": [],
            }

    class SyntheticServer(OfficialServer):
        def __init__(self, runtime, directory, provider_url, capability, binding_headers):
            assert SECRET not in json.dumps(
                [str(runtime), str(directory), provider_url, capability, binding_headers]
            )
            super().__init__(runtime, directory, provider_url, capability, binding_headers)
            state.server = self

        def start(self):
            assert SECRET not in json.dumps(self.environment)
            assert SECRET not in json.dumps(self.config)
            self.version = "1.18.29"
            if state.fault == "runtime_version":
                raise ValueError("RUNTIME_VERSION_MISMATCH")
            return self.version

        def request(self, method, route, body=None):
            if route == "/config":
                effective = copy.deepcopy(self.config)
                if state.fault == "configuration":
                    effective["model"] = "different/model"
                return effective
            if route == "/path":
                return {"worktree": str(self.workspace if state.fault != "workspace" else tmp_path)}
            if route == "/session":
                return {"id": "synthetic-session"}
            if route.endswith("/prompt_async"):
                state.prompt_calls += 1
                assert body["model"] == {"providerID": "opencode-go", "modelID": MODEL}
                if (
                    state.scenario == "edit"
                    and state.fault != "unchanged_edit"
                    or state.fault == "denied_modified"
                ):
                    (self.workspace / "fixture.py").write_text(FIXED, encoding="utf-8")
                if state.fault == "extra_file":
                    (self.workspace / "extra.py").write_text("extra", encoding="utf-8")
                if state.fault == "credential_leak":
                    (self.directory / "server.log").write_text(SECRET, encoding="ascii")
                if state.fault == "credential_split":
                    (self.directory / "server.log").write_bytes(
                        b"x" * (65536 - 8) + SECRET.encode()
                    )
                if state.fault == "scan_unreadable":
                    (self.directory / "server.log").write_text(
                        "unreadable fixture", encoding="ascii"
                    )
                if state.fault == "type_parameter":
                    (self.workspace / "fixture.py").write_text(
                        "def clamp[T: unexpected_callable()](value, low, high):\n"
                        "    return max(low, min(value, high))\n",
                        encoding="utf-8",
                    )
                if state.fault == "session_error":
                    self.events.append({"type": "session.error"})
                return None
            if route.endswith("/abort"):
                state.abort_called = True
                state.abort_calls += 1
                if state.fault == "abort_error":
                    raise RuntimeError("Synthetic abort failure")
                return True
            if route.endswith("/message"):
                state.message_polls += 1
                info = {
                    "role": "assistant",
                    "modelID": MODEL,
                    "providerID": "opencode-go",
                    "finish": "stop",
                    "time": {"completed": 1001},
                    "tokens": {},
                }
                if state.fault == "model_drift":
                    info["modelID"] = "unexpected-model"
                if state.fault in {"timeout", "abort_error", "pending_relay_error"}:
                    info["time"] = {}
                    info["finish"] = "unknown"
                if state.scenario == "edit":
                    parts = [
                        {
                            "type": "tool",
                            "tool": tool,
                            "state": {
                                "status": "completed",
                                "input": {"filePath": str(self.workspace / "fixture.py")},
                            },
                        }
                        for tool in ("read", "edit")
                    ]
                else:
                    parts = [
                        {
                            "type": "tool",
                            "tool": "read",
                            "state": {
                                "status": "error",
                                "input": {"filePath": str(self.workspace / "blocked.txt")},
                                "error": DENIAL_PREFIX + " Synthetic explicit deny rule.",
                            },
                        }
                    ]
                if state.fault == "no_tools":
                    parts = [{"type": "text", "text": "I completed the task"}]
                if state.fault == "wrong_path":
                    parts[-1]["state"]["input"]["filePath"] = str(tmp_path / "fixture.py")
                if state.fault == "generic_denial":
                    parts[0]["state"]["error"] = "File not found"
                messages = [{"info": info, "parts": parts}]
                if state.fault == "last_incomplete":
                    final = copy.deepcopy(info)
                    final["time"] = {}
                    messages.append({"info": final, "parts": []})
                return messages
            raise AssertionError((method, route))

        def close(self):
            state.server_closed += 1
            if state.fault == "server_cleanup_raise":
                raise RuntimeError("Synthetic cleanup failure")
            return {"scope": "server_process_only", "status": "exited", "errors": []}

    def synthetic_git(arguments, **kwargs):
        assert arguments[:3] == ["git", "init", "--initial-branch=main"]
        assert SECRET not in json.dumps(kwargs.get("env", {}))
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(go_live, "GoRelay", SyntheticRelay)
    monkeypatch.setattr(go_live, "OfficialServer", SyntheticServer)
    monkeypatch.setattr(go_live.subprocess, "run", synthetic_git)
    state.runtime, state.credential = runtime, credential
    state.output = tmp_path / "probe-output"
    return state


def run(harness):
    result = go_live.GoLiveProbe(harness.runtime, harness.output, harness.credential).run(
        harness.scenario,
        live=True,
    )
    assert harness.network_attempts == 0
    assert harness.server_closed == 1
    assert harness.relay_closed == 1
    assert result["profile_enabled"] is False
    assert result["dispatch_eligible"] is False
    assert result["full_qualification"] == "not_run"
    assert result["provider_remote_cancel"] == "not_run"
    assert result["workspace_os_isolation"] == "not_run"
    assert SECRET not in json.dumps(result)
    assert SECRET not in (harness.output / "report.json").read_text(encoding="utf-8")
    return result


@pytest.mark.parametrize("scenario", ["edit", "denied_read"])
def test_synthetic_scenarios_have_independent_success_criteria(harness, scenario):
    harness.scenario = scenario
    result = run(harness)
    assert result["status"] == "passed"
    if scenario == "edit":
        assert result["function_cases_passed"] == [True] * 4
        assert result["fixture_file_changed"] is True
    else:
        assert result["function_cases_passed"] is None
        assert result["fixture_file_changed"] is False
        assert result["tool_results"][0]["error_category"] == "permission_denied_by_rule"


@pytest.mark.parametrize(
    "scenario,fault,expected",
    [
        ("edit", "no_tools", "EDIT_TOOL_EVIDENCE_MISSING"),
        ("edit", "unchanged_edit", "FIXTURE_BEHAVIOR_FAILED"),
        ("edit", "wrong_path", "UNEXPECTED_TOOL_RESULT"),
        ("edit", "extra_file", "WORKSPACE_CHANGED"),
        ("edit", "model_drift", "NATIVE_EXECUTION_INCOMPLETE"),
        ("edit", "last_incomplete", "NATIVE_EXECUTION_INCOMPLETE"),
        ("edit", "session_error", "NATIVE_EXECUTION_INCOMPLETE"),
        ("edit", "provider_error", "PROVIDER_PROTOCOL_INCOMPLETE"),
        ("edit", "empty_receipts", "PROVIDER_PROTOCOL_INCOMPLETE"),
        ("edit", "relay_cleanup", "CLEANUP_INCOMPLETE"),
        ("edit", "server_cleanup_raise", "CLEANUP_INCOMPLETE"),
        ("edit", "credential_leak", "CREDENTIAL_SCAN_FAILED"),
        ("edit", "credential_split", "CREDENTIAL_SCAN_FAILED"),
        ("denied_read", "generic_denial", "PERMISSION_DENIAL_NOT_OBSERVED"),
        ("denied_read", "denied_modified", "WORKSPACE_CHANGED"),
        ("denied_read", "canary_upstream", "PROVIDER_PROTOCOL_INCOMPLETE"),
    ],
)
def test_public_probe_cannot_pass_incomplete_or_contradictory_evidence(
    harness, scenario, fault, expected
):
    harness.scenario, harness.fault = scenario, fault
    result = run(harness)
    assert result["status"] == "failed"
    assert expected in result["reason_codes"]


@pytest.mark.parametrize("fault", ["runtime_version", "configuration", "workspace"])
def test_preflight_mismatch_sends_no_prompt(harness, fault):
    harness.fault = fault
    result = run(harness)
    assert harness.prompt_calls == 0
    assert result["status"] == "failed"


@pytest.mark.parametrize("fault", ["timeout", "abort_error"])
def test_timeout_or_abort_error_still_cleans_both_boundaries(harness, monkeypatch, fault):
    harness.fault = fault
    ticks = iter([0.0, 1.0, 151.0])
    monkeypatch.setattr(go_live.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(go_live.time, "sleep", lambda _: None)
    result = run(harness)
    assert harness.abort_called is True
    assert result["timed_out"] is True
    assert result["status"] == "failed"


def test_cli_without_live_stops_before_any_boundary_or_file_read(harness, monkeypatch, capsys):
    original = Path.read_text

    def blocked_read(path, *args, **kwargs):
        if path == harness.credential:
            raise AssertionError("Credential read without live authorization")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", blocked_read)
    outcome = go_live.main(
        [
            "--runtime",
            str(harness.runtime),
            "--directory",
            str(harness.output),
            "--credential-file",
            str(harness.credential),
            "--scenario",
            "edit",
        ]
    )
    assert outcome == 1
    summary = json.loads(capsys.readouterr().out)
    assert summary["reason_code"] == "LIVE_AUTHORIZATION_REQUIRED"
    assert summary["profile_enabled"] is False
    assert harness.relay_constructed == 0
    assert harness.server is None
    assert not harness.output.exists()


def test_missing_credential_never_constructs_relay_or_starts_runtime(harness, capsys):
    outcome = go_live.main(
        [
            "--live",
            "--runtime",
            str(harness.runtime),
            "--directory",
            str(harness.output),
            "--credential-file",
            str(harness.credential.with_name("missing-file")),
            "--scenario",
            "edit",
        ]
    )
    assert outcome == 1
    assert json.loads(capsys.readouterr().out)["status"] == "failed"
    assert harness.relay_constructed == 0
    assert harness.server is None
    assert not harness.output.exists()


def test_unreadable_artifact_keeps_scan_incomplete_and_result_failed(harness, monkeypatch):
    harness.fault = "scan_unreadable"
    original = Path.open

    def unreadable(path, mode="r", *args, **kwargs):
        if path.name == "server.log" and mode == "rb":
            raise PermissionError("Synthetic unreadable artifact")
        return original(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", unreadable)
    result = run(harness)
    assert result["status"] == "failed"
    assert "CREDENTIAL_SCAN_FAILED" in result["reason_codes"]
    assert result["credential_scan"]["completed"] is False
    assert result["credential_scan"]["errors"] == [
        {"path": "runner/server.log", "reason": "FILE_UNREADABLE"}
    ]


def test_existing_output_rejects_before_credential_read_and_preserves_artifacts(
    harness, monkeypatch
):
    harness.output.mkdir()
    existing = harness.output / "report.json"
    existing.write_text("preserve existing evidence", encoding="utf-8")
    original = Path.read_text

    def blocked_read(path, *args, **kwargs):
        if path == harness.credential:
            raise AssertionError("Credential must not be read for an existing output directory")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", blocked_read)
    with pytest.raises(ValueError, match="FRESH_SEPARATE_DIRECTORY_REQUIRED"):
        go_live.GoLiveProbe(harness.runtime, harness.output, harness.credential).run(
            "edit", live=True
        )
    assert existing.read_text(encoding="utf-8") == "preserve existing evidence"
    assert harness.relay_constructed == 0
    assert harness.server is None


def test_unapproved_type_parameter_expression_is_not_a_safe_fixture(harness):
    harness.fault = "type_parameter"
    result = run(harness)
    assert result["status"] == "failed"
    assert "FIXTURE_BEHAVIOR_FAILED" in result["reason_codes"]


def test_relay_failure_aborts_on_first_poll_without_waiting_for_native_completion(
    harness, monkeypatch
):
    harness.fault = "pending_relay_error"
    ticks = iter([0.0, 1.0])
    monkeypatch.setattr(go_live.time, "monotonic", lambda: next(ticks))

    def no_sleep(_):
        raise AssertionError("Must abort when the first relay failure receipt is visible")

    monkeypatch.setattr(go_live.time, "sleep", no_sleep)
    result = run(harness)
    assert harness.message_polls == 1
    assert harness.abort_calls == 1
    assert result["relay_failure_abort_requested"] is True
    assert result["abort_acknowledged"] is True
    assert "timed_out" not in result
    assert "probe_error" not in result
    assert result["status"] == "failed"
    assert "PROVIDER_PROTOCOL_INCOMPLETE" in result["reason_codes"]
