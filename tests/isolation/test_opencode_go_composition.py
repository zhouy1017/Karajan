"""Actual Linux OpenCode, namespaces, UDS relay and journal; only upstream is fake."""

import json
import os
import sys
import time
from pathlib import Path

import httpx
import pytest
from karajan.adapters.opencode.go_journal import GoCallJournal
from karajan.adapters.opencode.go_relay import GoRelay, GoRelayAuthorization
from karajan.isolation.opencode_runtime import RUNTIME_SHA256, IsolatedOpenCode

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux namespaces required")
SECRET = "synthetic-upstream-credential-kept-outside"
CANARY = "synthetic-denied-file-content-must-not-leave"
INITIAL = "def clamp(value, low, high):\n    return value\n"
EXPECTED = "def clamp(value, low, high):\n    return min(high, max(low, value))\n"


def runtime_artifact():
    path = Path(
        os.environ.get(
            "KARAJAN_OPENCODE_LINUX_BINARY",
            str(
                Path(__file__).resolve().parents[2]
                / "runtimes/opencode/node_modules/opencode-linux-x64/bin/opencode"
            ),
        )
    )
    if not path.is_file():
        if os.environ.get("KARAJAN_REQUIRE_OPENCODE_ISOLATION") == "1":
            pytest.fail("Prepared fixed Linux OpenCode artifact is required")
        pytest.skip("Prepared fixed Linux OpenCode artifact is unavailable")
    return path


def native_response(index, denied=False, old_string="return value"):
    delta, finish = {"content": "Done."}, "stop"
    if index == 1 or (index == 2 and not denied):
        arguments = {"filePath": "/workspace/blocked.txt" if denied else "/workspace/fixture.py"}
        if index == 2:
            arguments.update(oldString=old_string, newString="return min(high, max(low, value))")
        delta = {
            "tool_calls": [
                {
                    "index": 0,
                    "id": f"call_{index}",
                    "type": "function",
                    "function": {
                        "name": "read" if index == 1 else "edit",
                        "arguments": json.dumps(arguments),
                    },
                }
            ]
        }
        finish = "tool_calls"
    events = []
    for change, reason in (({"role": "assistant"}, None), (delta, None), ({}, finish)):
        events.append(
            {
                "id": "chatcmpl-fixed",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "glm-5.3-flash",
                "choices": [{"index": 0, "delta": change, "finish_reason": reason}],
            }
        )
    data = "".join("data: " + json.dumps(event) + "\n\n" for event in events)
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=(data + "data: [DONE]\n\n").encode(),
    )


@pytest.mark.parametrize("denied", [False, True], ids=["read-edit", "denied-read"])
def test_native_tool_roundtrip_is_durable_and_keeps_credentials_outside(tmp_path, denied):
    artifact = runtime_artifact()
    journal = GoCallJournal(tmp_path / "calls.sqlite")
    binding = {
        "qualification_id": "composition",
        "attempt_id": "attempt",
        "fence": 1,
        "profile_digest": "a" * 64,
        "runtime_digest": RUNTIME_SHA256,
        "channel": "opencode-go",
        "model": "glm-5.3-flash",
        "auth_generation": "synthetic",
        "expires_at": time.time() + 90,
        "max_requests": 6,
    }
    grant = journal.create_grant(binding, grant_id="grant")
    upstream = []

    def receive(request):
        # The actual durable send intent is visible BEFORE upstream receives bytes.
        facts = journal.snapshot("grant")
        assert facts["request_count"] == len(upstream) + 1
        assert facts["calls"][-1]["state"] == "send_unknown"
        assert str(request.url) == "https://opencode.ai/zen/go/v1/chat/completions"
        assert request.headers["Authorization"] == f"Bearer {SECRET}"
        body = json.loads(request.content)
        assert body["model"] == "glm-5.3-flash"
        assert CANARY not in request.content.decode()
        assert SECRET not in request.content.decode()
        upstream.append(body)
        return native_response(len(upstream), denied)

    relay = GoRelay(
        SECRET,
        CANARY,
        authorization=GoRelayAuthorization(journal, "grant", binding, grant["capability"]),
        client_factory=lambda: httpx.Client(
            transport=httpx.MockTransport(receive), trust_env=False
        ),
    )
    socket_path = tmp_path / "inference.sock"
    relay.start(unix_socket=socket_path)
    runtime = None
    try:
        runtime = IsolatedOpenCode(artifact, tmp_path / "runtime", socket_path, relay.capability)
        fixture = runtime.workspace / "fixture.py"
        fixture.write_text(INITIAL)
        (runtime.workspace / "blocked.txt").write_text(CANARY)
        runtime.start()
        session = runtime.request("POST", "/session", {"title": "Composition", "agent": "probe"})
        runtime.request(
            "POST",
            f"/session/{session['id']}/prompt_async",
            {
                "agent": "probe",
                "model": {"providerID": "opencode-go", "modelID": "glm-5.3-flash"},
                "parts": [{"type": "text", "text": "Execute the requested fixed tool check."}],
            },
        )
        deadline = time.monotonic() + 30
        expected_calls = 2 if denied else 3
        messages = []
        while time.monotonic() < deadline:
            messages = runtime.request("GET", f"/session/{session['id']}/message")
            facts = journal.snapshot("grant")
            if facts["request_count"] >= expected_calls and all(
                call["outcome"] is not None for call in facts["calls"]
            ):
                break
            time.sleep(0.1)
        assert len(upstream) == expected_calls, relay.receipts
        assert fixture.read_text() == (INITIAL if denied else EXPECTED)
        states = [
            part["state"]
            for message in messages
            for part in message["parts"]
            if part["type"] == "tool"
        ]
        if denied:
            assert len(states) == 1
            assert states[0]["status"] == "error"
            assert states[0]["error"].startswith("The user has specified a rule")
        else:
            assert len(states) == 2
            assert all(state["status"] == "completed" for state in states)
        for credential in (SECRET, grant["capability"], relay.capability, CANARY):
            assert credential not in json.dumps(messages)
            assert credential not in json.dumps(relay.receipts)
            assert credential not in json.dumps(journal.snapshot("grant"))
    finally:
        # Revoke future sends before stopping the native process and transport.
        journal.revoke_grant("grant")
        stopped = runtime.close() if runtime is not None else None
        relay_stopped = relay.close()
    assert stopped["local_stop"] == "confirmed"
    assert stopped["remote_stop"] == "unknown"
    assert relay_stopped["status"] == "closed"
    reopened = GoCallJournal(tmp_path / "calls.sqlite").snapshot("grant")
    assert reopened["state"] == "revoked"
    assert reopened["request_count"] == expected_calls
    assert all(call["state"] == "response_received" for call in reopened["calls"])
    assert all(call["outcome"]["protocol_passed"] for call in reopened["calls"])
    assert runtime.snapshot()["dispatch_eligible"] is False


@pytest.mark.parametrize("scenario", ["edit", "denied_read"])
def test_fixed_observer_uses_the_actual_composed_runtime(tmp_path, scenario):
    from karajan.isolation.go_probe import go_runtime_source, observe_go_tools, source_digest

    artifact = runtime_artifact()
    journal = GoCallJournal(tmp_path / "journal.sqlite")
    binding = {
        "qualification_id": "fixed-suite",
        "attempt_id": "attempt",
        "fence": 1,
        "profile_digest": "a" * 64,
        "runtime_digest": source_digest(go_runtime_source(artifact)),
        "channel": "opencode-go",
        "model": "glm-5.3-flash",
        "auth_generation": "synthetic",
        "expires_at": time.time() + 90,
        "max_requests": 6,
    }
    grant = journal.create_grant(binding, grant_id="fixed-grant")
    requests = []

    def receive(request):
        requests.append(request)
        return native_response(
            len(requests), scenario == "denied_read", "return min(low, max(value, high))"
        )

    result = observe_go_tools(
        artifact,
        tmp_path / "observation",
        SECRET,
        GoRelayAuthorization(journal, "fixed-grant", binding, grant["capability"]),
        scenario=scenario,
        client_factory=lambda: httpx.Client(
            transport=httpx.MockTransport(receive), trust_env=False
        ),
    )
    assert result["status"] == "passed", result
    assert len(requests) == (3 if scenario == "edit" else 2)
    assert result["journal"]["request_count"] == len(requests)
    assert result["journal"]["state"] == "revoked"
    assert result["native_cleanup"]["local_stop"] == "confirmed"
    assert result["provider_remote_stop"] == "unknown"
    assert result["dispatch_eligible"] is False
    assert result["runtime_tools_status"] == "not_run"
    assert result["runtime"]["native_control_fd_inherited"] is False
    assert result["runtime"]["network_interfaces"] == ["lo"]
    assert result["runtime"]["ipv4_routes"] == []
    assert SECRET not in json.dumps(result)
    assert grant["capability"] not in json.dumps(result)
