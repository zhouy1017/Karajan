"""Real Reviewer namespace negatives; upstream is exclusively an HTTP fixture."""

import hashlib
import json
import sys
from contextlib import contextmanager

import httpx
import pytest
from karajan.adapters.opencode.go_evidence import DENIAL_PREFIX
from karajan.isolation.go_reviewer_probe import observe_go_reviewer_tools
from test_go_context import accounting as accounting
from test_go_context import artifacts as artifacts
from test_go_reviewer_probe import reviewer_authorization, reviewer_response
from test_opencode_go_composition import SECRET, runtime_artifact

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Fixed Linux native required")


def _response(index, scenario, *, tool=None, path=None):
    original = reviewer_response(index, scenario)
    events = []
    for line in original.text.splitlines():
        if not line.startswith("data: ") or line == "data: [DONE]":
            continue
        event = json.loads(line[6:])
        for choice in event.get("choices", []):
            for call in choice["delta"].get("tool_calls", []):
                if tool:
                    call["function"]["name"] = tool
                    if tool == "bash":
                        call["function"]["arguments"] = json.dumps(
                            {"command": "curl https://example.invalid/forbidden"}
                        )
                if path:
                    call["function"]["arguments"] = json.dumps({"filePath": path})
        events.append(event)
    data = "".join("data: " + json.dumps(event) + "\n\n" for event in events)
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=(data + "data: [DONE]\n\n").encode(),
    )


def _observe(tmp_path, accounting, *, tool=None, path=None):
    runtime = runtime_artifact()
    scenario = "denied_read" if path else "clean_review"
    auth = reviewer_authorization(tmp_path, accounting, runtime, scenario)
    payloads, guarded = [], []

    @contextmanager
    def current_guard():
        guarded.append(len(guarded) + 1)
        yield

    def receive(request):
        payloads.append(json.loads(request.content))
        assert auth.journal.snapshot(auth.grant_id)["calls"][-1]["state"] == "send_unknown"
        return _response(len(payloads), scenario, tool=tool, path=path)

    result = observe_go_reviewer_tools(
        runtime,
        tmp_path / "observation",
        SECRET,
        auth,
        scenario=scenario,
        accounting=accounting,
        current_guard=current_guard,
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(receive)),
    )
    (tmp_path / "public-report.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    assert result["status"] == "failed"
    assert result["dispatch_eligible"] is False
    assert result["parsed_review"] is None
    assert result["readonly"]["before"] == result["readonly"]["after"]
    assert result["readonly"]["unchanged"] is True
    assert result["native_cleanup"]["local_stop"] == "confirmed"
    assert result["journal"]["state"] == "revoked"
    assert result["provider_remote_stop"] == "unknown"
    assert result["runtime"]["network_interfaces"] == ["lo"]
    assert result["runtime"]["ipv4_routes"] == []
    assert len(guarded) == 2 + len(payloads)  # Native start, prompt, then each actual send.
    return result, payloads


@pytest.mark.parametrize("tool", ["edit", "bash", "mcp__external__invoke", "create_pull_request"])
def test_actual_readonly_reviewer_rejects_unapproved_tool_before_native_execution(
    tmp_path, accounting, tool
):
    result, payloads = _observe(tmp_path, accounting, tool=tool)
    assert len(payloads) == result["journal"]["request_count"] == 1
    assert "RELAY_REJECTED_REQUEST" in result["reason_codes"]
    assert any("UNAPPROVED_TOOL" in row["reason_codes"] for row in result["requests"])
    assert all(t["function"]["name"] == "read" for t in payloads[0]["tools"])
    assert result["tools"] == []  # No delivered tool call is claimed as executed.


def test_actual_readonly_reviewer_control_file_read_is_native_permission_denied(
    tmp_path, accounting
):
    result, payloads = _observe(tmp_path, accounting, path="/control/opencode.json")
    assert len(payloads) == result["journal"]["request_count"] == 2
    feedback = [m for m in payloads[-1]["messages"] if m["role"] == "tool"]
    assert len(feedback) == 1
    assert DENIAL_PREFIX in feedback[0]["content"]
    assert "NATIVE_READ_TOOL_INCOMPLETE" in result["reason_codes"]
    # The controlled fixture response does not become an in-scope successful review.
    (tmp_path / "denial-observation.json").write_text(
        json.dumps(
            {
                "path": "/control/opencode.json",
                "native_permission_denied": True,
                "feedback_sha256": hashlib.sha256(feedback[0]["content"].encode()).hexdigest(),
                "requests": len(payloads),
                "evidence_layer": "P_http_fixture",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
