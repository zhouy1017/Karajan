"""Independent, entirely offline Go diagnostic Standards cases."""

import json

import httpx
import pytest
from karajan.adapters.opencode.go_live import _scan
from karajan.adapters.opencode.go_relay import GoRelay

SECRET = "synthetic-review-provider-secret"


def request_from_mock_upstream(raw):
    sent = []

    def respond(request):
        sent.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=raw)

    relay = GoRelay(
        SECRET,
        "synthetic-denied-marker",
        client_factory=lambda: httpx.Client(
            transport=httpx.MockTransport(respond),
            trust_env=False,
            follow_redirects=False,
        ),
    )
    relay.start()
    try:
        with httpx.Client(trust_env=False, timeout=5) as client:
            response = client.post(
                relay.url + "/chat/completions",
                headers={
                    "Authorization": f"Bearer {relay.capability}",
                    "x-opencode-session": "independent-standards",
                },
                json={
                    "model": "glm-5.3-flash",
                    "stream": True,
                    "max_tokens": 32,
                    "messages": [{"role": "user", "content": "Reply with OK"}],
                },
            )
    finally:
        cleanup = relay.close()
    return response, relay.receipts, sent, cleanup


def stream(content):
    chunk = {
        "model": "glm-5.3-flash",
        "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": "stop"}],
    }
    return ("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode()


@pytest.mark.parametrize("escaped", [False, True], ids=["literal", "json-unicode-escaped"])
def test_upstream_credential_echo_never_reaches_native_runtime(escaped):
    raw = stream("upstream diagnostic: " + SECRET)
    if escaped:
        raw = raw.replace(SECRET.encode(), "".join(f"\\u{ord(c):04x}" for c in SECRET).encode())
    response, receipts, sent, cleanup = request_from_mock_upstream(raw)
    assert len(sent) == 1
    assert sent[0].headers["Authorization"] == f"Bearer {SECRET}"
    assert cleanup == {"status": "closed", "errors": []}
    assert response.status_code >= 400, "Credential-bearing valid SSE was forwarded to native"
    assert SECRET not in response.text
    assert SECRET not in json.dumps(receipts)
    assert receipts[0]["protocol_passed"] is False


def test_clean_upstream_stream_still_passes_without_content_in_receipts():
    raw = stream("synthetic reply not for persisted receipts")
    response, receipts, sent, cleanup = request_from_mock_upstream(raw)
    assert response.status_code == 200
    assert response.content == raw
    assert cleanup == {"status": "closed", "errors": []}
    assert receipts[0]["protocol_passed"] is True
    assert SECRET not in json.dumps(receipts)
    assert "synthetic reply not for persisted receipts" not in json.dumps(receipts)
    assert str(sent[0].url) == "https://opencode.ai/zen/go/v1/chat/completions"


@pytest.mark.parametrize("channel", ["content", "reasoning_content", "arguments"])
def test_fragmented_upstream_credential_echo_is_not_forwarded(channel):
    pieces = [SECRET[:13], SECRET[13:]]
    if channel == "arguments":
        deltas = [
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call-review",
                        "type": "function",
                        "function": {"name": "read", "arguments": '{"filePath":"' + pieces[0]},
                    }
                ]
            },
            {"tool_calls": [{"index": 0, "function": {"arguments": pieces[1] + '"}'}}]},
        ]
        finish = "tool_calls"
    else:
        deltas = [{channel: value} for value in pieces]
        finish = "stop"
    chunks = [
        {"model": "glm-5.3-flash", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
        for delta in deltas
    ]
    chunks.append(
        {"model": "glm-5.3-flash", "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]}
    )
    raw = (
        "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
    ).encode()
    assert SECRET.encode() not in raw
    response, receipts, sent, cleanup = request_from_mock_upstream(raw)
    assert len(sent) == 1
    assert cleanup == {"status": "closed", "errors": []}
    assert response.status_code >= 400, (
        "Native could reconstruct the real credential from ordinary SSE deltas"
    )
    assert receipts[0]["protocol_passed"] is False


def test_incomplete_upstream_stream_cannot_be_forwarded_as_success():
    response, receipts, _, cleanup = request_from_mock_upstream(
        stream("OK").split(b"data: [DONE]")[0]
    )
    assert response.status_code >= 400
    assert receipts[0]["protocol_passed"] is False
    assert cleanup == {"status": "closed", "errors": []}


def test_credential_scan_detects_cross_chunk_boundary(tmp_path):
    raw = b"x" * (65536 - 5) + SECRET.encode() + b"tail"
    (tmp_path / "synthetic.log").write_bytes(raw)
    result = _scan(tmp_path, SECRET.encode())
    assert result == {
        "completed": True,
        "scanned_files": 1,
        "leak_files": ["synthetic.log"],
        "errors": [],
    }
