from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.client import HTTPConnection
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from karajan.adapters.opencode.go_relay import GoRelay

SECRET = "synthetic-provider-secret"
CANARY = "synthetic-denied-canary"
MODEL = "glm-5.3-flash"


def payload() -> dict[str, Any]:
    return {
        "model": MODEL,
        "stream": True,
        "max_tokens": 256,
        "messages": [{"role": "user", "content": "Reply with OK"}],
    }


def event(**overrides: Any) -> dict[str, Any]:
    return {
        "model": MODEL,
        "choices": [{"index": 0, "delta": {"tool_calls": None}, "finish_reason": "stop"}],
        **overrides,
    }


def stream(*events: dict[str, Any], done: bool = True) -> bytes:
    body = "".join(f"data: {json.dumps(item)}\n\n" for item in events)
    return (body + ("data: [DONE]\n\n" if done else "")).encode()


def answer(body: bytes | None = None) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=stream(event()) if body is None else body,
    )


@contextmanager
def running(
    respond: Callable[[httpx.Request], httpx.Response] | None = None,
) -> Iterator[tuple[GoRelay, list[httpx.Request]]]:
    requests: list[httpx.Request] = []

    def receive(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return answer() if respond is None else respond(request)

    relay = GoRelay(
        SECRET,
        CANARY,
        client_factory=lambda: httpx.Client(
            transport=httpx.MockTransport(receive), trust_env=False, follow_redirects=True
        ),
    )
    relay.start()
    try:
        yield relay, requests
    finally:
        relay.close()


def post(relay: GoRelay, body: Any = None, **kwargs: Any) -> httpx.Response:
    headers = kwargs.pop(
        "headers",
        {
            "Authorization": f"Bearer {relay.capability}",
            "x-opencode-session": "ses_test",
        },
    )
    with httpx.Client(trust_env=False, timeout=10) as client:
        return client.post(
            relay.url + kwargs.pop("path", "/chat/completions"),
            headers=headers,
            content=json.dumps(payload() if body is None else body),
            **kwargs,
        )


def post_with_raw_socket(
    relay: GoRelay,
    body: bytes,
    headers: dict[str, str],
    *,
    split_body: bool = False,
    send_delay: float = 0.02,
) -> int:
    request_body = body
    request_headers = {
        "Host": f"127.0.0.1:{urlsplit(relay.url).port}",
        "Content-Length": str(len(request_body)),
        "Content-Type": "application/json",
    } | headers
    request = (
        b"POST /v1/chat/completions HTTP/1.1\r\n"
        + b"".join(f"{name}: {value}\r\n".encode() for name, value in request_headers.items())
        + b"\r\n"
    )
    with socket.create_connection(("127.0.0.1", urlsplit(relay.url).port), timeout=5) as raw:
        raw.sendall(request)
        if split_body and request_body:
            midpoint = len(request_body) // 2
            raw.sendall(request_body[:midpoint])
            time.sleep(send_delay)
            raw.sendall(request_body[midpoint:])
        else:
            raw.sendall(request_body)
        raw.settimeout(3)
        try:
            response = raw.recv(1024)
        except OSError as error:
            raise RuntimeError(f"socket reset while reading response: {error}") from error
    if not response:
        raise RuntimeError("connection closed before response headers were read")
    status_line = response.split(b"\r\n", 1)[0]
    return int(status_line.split()[1])


def raw_request_lines(relay: GoRelay, length: int, *, valid: bool = False) -> bytes:
    capability = relay.capability if valid else SECRET
    return (
        b"POST /v1/chat/completions HTTP/1.1\r\n"
        + b"Host: localhost\r\n"
        + f"Authorization: Bearer {capability}\r\n".encode()
        + b"x-opencode-session: ses_test\r\n"
        + f"Content-Length: {length}\r\n".encode()
        + b"Content-Type: application/json\r\n"
        + b"\r\n"
    )


def test_complete_stream_keeps_real_credential_only_at_upstream() -> None:
    secret = "synthetic-provider-secret"
    upstream: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        upstream.append(request)
        payload = {
            "model": "glm-5.3-flash",
            "choices": [{"delta": {"tool_calls": None}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        }
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=f"data: {json.dumps(payload)}\n\ndata: [DONE]\n\n".encode(),
        )

    relay = GoRelay(
        secret,
        "synthetic-denied-canary",
        client_factory=lambda: httpx.Client(
            transport=httpx.MockTransport(respond), trust_env=False, follow_redirects=False
        ),
    )
    relay.start()
    try:
        with httpx.Client(trust_env=False) as client:
            response = client.post(
                relay.url + "/chat/completions",
                headers={
                    "Authorization": f"Bearer {relay.capability}",
                    "x-opencode-session": "session-test",
                },
                json={
                    "model": "glm-5.3-flash",
                    "stream": True,
                    "max_tokens": 256,
                    "messages": [{"role": "user", "content": "Reply with OK"}],
                },
            )
        assert response.status_code == 200
        assert "[DONE]" in response.text
        assert str(upstream[0].url) == "https://opencode.ai/zen/go/v1/chat/completions"
        assert upstream[0].headers["authorization"] == f"Bearer {secret}"
        assert relay.capability != secret
        assert relay.receipts[0]["protocol_passed"] is True
        assert relay.receipts[0]["reported_models"] == ["glm-5.3-flash"]
        assert relay.receipts[0]["usage"]["total_tokens"] == 12
        assert secret not in json.dumps(relay.receipts)
    finally:
        assert relay.close() == {"status": "closed", "errors": []}


@pytest.mark.parametrize(
    "patch",
    [
        {"model": "other-model"},
        {"stream": False},
        {"stream": 1},
        {"max_tokens": True},
        {"max_tokens": 0},
        {"max_tokens": 4097},
        {"max_tokens": 12.5},
        {"max_tokens": "256"},
        {"messages": []},
        {"messages": [SECRET]},
    ],
)
def test_invalid_request_cannot_send_upstream(patch: dict[str, Any]) -> None:
    with running() as (relay, requests):
        response = post(relay, {**payload(), **patch})
        assert response.status_code == 422
        assert not requests
        assert not relay.receipts
        assert SECRET not in response.text


@pytest.mark.parametrize("body", [[], 42, True, "secret"])
def test_json_root_must_be_an_object(body: Any) -> None:
    with running() as (relay, requests):
        assert post(relay, body).status_code == 400
        assert not requests


@pytest.mark.parametrize("path", ["/chat/completions?x=1", "/chat/completions/", "/models"])
def test_local_path_is_exact(path: str) -> None:
    with running() as (relay, requests):
        assert post(relay, path=path).status_code == 404
        assert not requests


def test_capability_and_session_are_required_and_never_forward_arbitrary_headers() -> None:
    with running() as (relay, requests):
        response = post(
            relay,
            headers={
                "Authorization": "Bearer " + SECRET,
                "x-opencode-session": "ses_test",
            },
        )
        assert response.status_code == 403
        assert response.json() == {"error": {"type": "INVALID_CAPABILITY"}}
        assert not requests
        assert not relay.receipts
        assert (
            post(
                relay,
                headers={
                    "Authorization": "Bearer " + relay.capability,
                },
            ).status_code
            == 400
        )
        assert (
            post(
                relay,
                headers=[
                    ("Authorization", "Bearer " + relay.capability),
                    ("Authorization", "Bearer " + relay.capability),
                    ("x-opencode-session", "ses_test"),
                ],
            ).status_code
            == 403
        )
        assert not requests
        assert (
            post(
                relay,
                headers={
                    "Authorization": "Bearer " + relay.capability,
                    "x-opencode-session": "ses_test",
                    "User-Agent": SECRET,
                    "x-opencode-request": SECRET,
                    "Cookie": SECRET,
                },
            ).status_code
            == 200
        )
        assert requests[0].headers["user-agent"] == "opencode/1.18.29 Karajan/0.1"
        assert requests[0].headers["x-opencode-session"] == "ses_test"
        assert "cookie" not in requests[0].headers
        assert "x-opencode-request" not in requests[0].headers
        assert SECRET not in json.dumps(relay.receipts)


def test_wrong_capability_body_drain_stops_at_declared_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads: list[tuple[int, int]] = []
    original = GoRelay._read_request

    class ObservedReader:
        def __init__(self, inner: Any):
            self._inner = inner

        def read1(self, count: int = -1) -> bytes:
            result = self._inner.read1(count)
            reads.append((count, len(result)))
            return result

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

    def observe(self: GoRelay, handler: Any) -> tuple[dict[str, Any], str]:
        handler.rfile = ObservedReader(handler.rfile)
        return original(self, handler)

    monkeypatch.setattr(GoRelay, "_read_request", observe)
    body = b"x" * 20
    with running() as (relay, requests):
        with socket.create_connection(("127.0.0.1", urlsplit(relay.url).port)) as peer:
            peer.sendall(raw_request_lines(relay, len(body)) + body)
            peer.shutdown(socket.SHUT_WR)
            peer.settimeout(2)
            response = peer.recv(2048)
            assert b" 403 " in response.split(b"\r\n", 1)[0]
        assert reads == [(len(body), len(body))]
        assert relay.receipts == []
        assert not requests


def test_rejection_split_body_is_dropped_without_side_effects() -> None:
    body = b"x" * 20_000
    with running() as (relay, requests):
        assert (
            post_with_raw_socket(
                relay,
                body,
                {
                    "Authorization": "Bearer " + SECRET,
                    "x-opencode-session": "ses_test",
                },
                split_body=True,
            )
            == 403
        )
        assert not requests
        assert not relay.receipts


def test_rejection_drain_has_total_deadline_under_slow_trickle() -> None:
    body_length = 4096
    with running() as (relay, requests):
        with socket.create_connection(("127.0.0.1", urlsplit(relay.url).port)) as peer:
            peer.sendall(raw_request_lines(relay, body_length))
            stopped = threading.Event()

            def trickle() -> None:
                for _ in range(25):
                    if stopped.is_set():
                        return
                    try:
                        peer.sendall(b"x")
                    except OSError:
                        return
                    stopped.wait(0.1)

            sender = threading.Thread(target=trickle)
            started = time.monotonic()
            sender.start()
            try:
                peer.settimeout(1.2)
                response = peer.recv(2048)
                assert response and b" 403 " in response.split(b"\r\n", 1)[0]
                assert time.monotonic() - started < 1.2
            finally:
                stopped.set()
                sender.join(timeout=1)
                peer.shutdown(socket.SHUT_WR)
        assert not requests
        assert not relay.receipts


def test_fully_consumed_invalid_body_is_not_read_a_second_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = json.dumps({**payload(), "model": "unsupported-model"}).encode()
    reads: list[tuple[int, int]] = []
    original = GoRelay._read_request

    class ObservedReader:
        def __init__(self, inner: Any):
            self._inner = inner

        def read(self, count: int = -1) -> bytes:
            result = self._inner.read(count)
            reads.append((count, len(result)))
            return result

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

    def observe(self: GoRelay, handler: Any) -> tuple[dict[str, Any], str]:
        handler.rfile = ObservedReader(handler.rfile)
        return original(self, handler)

    monkeypatch.setattr(GoRelay, "_read_request", observe)

    with running() as (relay, _), socket.create_connection(
        ("127.0.0.1", urlsplit(relay.url).port)
    ) as peer:
        peer.sendall(raw_request_lines(relay, len(body), valid=True) + body)
        peer.shutdown(socket.SHUT_WR)
        peer.settimeout(2)
        response = peer.recv(2048)
        assert response and b" 422 " in response.split(b"\r\n", 1)[0]
    assert reads == [(len(body), len(body))]


@pytest.mark.parametrize(
    "body",
    [
        b'{"model":"glm-5.3-flash","model":"other"}',
        b'{"temperature":NaN}',
        b"{",
        b"\xff",
    ],
)
def test_invalid_json_is_safely_rejected(body: bytes) -> None:
    with running() as (relay, requests), httpx.Client(trust_env=False) as client:
        response = client.post(
            relay.url + "/chat/completions",
            content=body,
            headers={
                "Authorization": "Bearer " + relay.capability,
                "x-opencode-session": "ses_test",
            },
        )
        assert response.status_code == 400
        assert not requests


def test_request_body_limit_prevents_upstream_send() -> None:
    with running() as (relay, requests):
        # The relay rejects the declared size without consuming an oversized body.
        # Sending that body concurrently with rejection can reset a Windows socket.
        connection = HTTPConnection("127.0.0.1", urlsplit(relay.url).port, timeout=5)
        try:
            connection.putrequest("POST", "/v1/chat/completions")
            connection.putheader("Authorization", "Bearer " + relay.capability)
            connection.putheader("Content-Length", "262145")
            connection.putheader("x-opencode-session", "ses_test")
            connection.endheaders()
            assert connection.getresponse().status == 413
        finally:
            connection.close()
        assert not requests


@pytest.mark.parametrize("status", [302, 401, 429, 500])
def test_upstream_errors_never_echo_body_or_follow_redirects(status: int) -> None:
    with running(
        lambda _: httpx.Response(
            status,
            headers={"Location": "https://unapproved.invalid/" + SECRET},
            content=SECRET,
        )
    ) as (relay, requests):
        response = post(relay)
        relay.close()
        assert response.status_code == 502
        assert len(requests) == 1
        assert SECRET not in response.text
        receipt = relay.receipts[0]
        assert receipt["upstream_status"] == status
        assert receipt["upstream_send_attempted"] is True
        assert receipt["protocol_passed"] is False
        assert receipt["reason_codes"] == ["UPSTREAM_HTTP_ERROR"]
        assert SECRET not in json.dumps(receipt)


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (stream(event(model="foreign-provider-model")), "MODEL_MISMATCH"),
        (stream({"choices": event()["choices"]}), "MISSING_MODEL"),
        (stream(event(), done=False), "INCOMPLETE_SSE"),
        (stream(event())[:-1], "INCOMPLETE_SSE"),
        (stream(event()) + stream(event()), "DATA_AFTER_TERMINATOR"),
        (stream(event(choices=[{"delta": {}, "finish_reason": "length"}])), "UNSUCCESSFUL_FINISH"),
        (stream({"error": {"message": "private-provider-error"}}), "UPSTREAM_ERROR_EVENT"),
        (stream(event(choices=[{"delta": {}, "finish_reason": None}])), "INCOMPLETE_SSE"),
        (b"data: invalid-json\n\n", "INVALID_SSE_JSON"),
    ],
)
def test_incomplete_or_foreign_stream_is_never_forwarded_as_success(
    body: bytes,
    reason: str,
) -> None:
    with running(lambda _: answer(body)) as (relay, _):
        response = post(relay)
        relay.close()
        assert response.status_code == 502
        assert SECRET not in response.text
        assert relay.receipts[0]["protocol_passed"] is False
        assert relay.receipts[0]["reason_codes"] == [reason]
        assert SECRET not in json.dumps(relay.receipts)


@pytest.mark.parametrize("count", [True, -1, 1.5, "private-non-numeric", 2**63])
def test_usage_requires_bounded_integers(count: Any) -> None:
    with running(lambda _: answer(stream(event(usage={"total_tokens": count})))) as (relay, _):
        assert post(relay).status_code == 502
        relay.close()
        assert relay.receipts[0]["reason_codes"] == ["INVALID_USAGE"]
        assert SECRET not in json.dumps(relay.receipts)


def test_tool_loop_usage_and_detached_receipts_preserve_only_allowlisted_facts() -> None:
    body = stream(
        event(
            choices=[
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {"name": "re", "arguments": ""},
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ]
        ),
        event(
            choices=[
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {"name": "ad", "arguments": "{}"},
                            }
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            usage={
                "prompt_tokens": 31,
                "total_tokens": 35,
                "unknown": "private-unreported-text",
                "prompt_tokens_details": {"cached_tokens": 4, "unknown": "private-unreported-text"},
                "completion_tokens_details": {"reasoning_tokens": 2},
            },
        ),
    )
    with running(lambda _: answer(body)) as (relay, _):
        request = payload()
        request["messages"].append({"role": "tool", "content": "def clamp(): pass"})
        assert post(relay, request).status_code == 200
        relay.close()
        receipt = relay.receipts[0]
        assert receipt["protocol_passed"] is True
        assert receipt["tool_names"] == ["read"]
        assert receipt["tool_results_in_request"] == 1
        assert receipt["fixture_content_in_tool_result"] is True
        assert receipt["usage"] == {
            "prompt_tokens": 31,
            "total_tokens": 35,
            "prompt_tokens_details": {"cached_tokens": 4},
            "completion_tokens_details": {"reasoning_tokens": 2},
        }
        assert SECRET not in json.dumps(receipt)
        assert "private-unreported-text" not in json.dumps(receipt)
        receipt["tool_names"].append("fake")
        receipt["usage"]["total_tokens"] = 123
        assert relay.receipts[0]["tool_names"] == ["read"]
        assert relay.receipts[0]["usage"]["total_tokens"] == 35


def test_unapproved_tool_name_is_rejected_without_retaining_its_text() -> None:
    body = stream(
        event(
            choices=[
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {"name": "unapproved-tool"},
                            }
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        )
    )
    with running(lambda _: answer(body)) as (relay, _):
        response = post(relay)
        relay.close()
        assert response.status_code == 502
        assert relay.receipts[0]["reason_codes"] == ["UNAPPROVED_TOOL"]
        assert "unapproved-tool" not in json.dumps(relay.receipts)


@pytest.mark.parametrize("field", ["content", "reasoning_content", "arguments"])
def test_credentials_split_over_deltas_never_reach_native(field: str) -> None:
    parts = [SECRET[:8], SECRET[8:]]
    events = []
    for index, part in enumerate(parts):
        if field == "arguments":
            delta = {
                "tool_calls": [
                    {
                        "index": 0,
                        "function": {
                            "name": "edit" if index == 0 else "",
                            "arguments": part,
                        },
                    }
                ]
            }
            finish = "tool_calls"
        else:
            delta = {field: part}
            finish = "stop"
        events.append(
            event(
                choices=[
                    {
                        "delta": delta,
                        "finish_reason": finish if index == 1 else None,
                    }
                ]
            )
        )
    body = stream(*events)
    assert SECRET.encode() not in body
    with running(lambda _: answer(body)) as (relay, _):
        response = post(relay)
        relay.close()
        assert response.status_code == 502
        assert relay.receipts[0]["reason_codes"] == ["UPSTREAM_CREDENTIAL_ECHO"]
        assert SECRET not in response.text + json.dumps(relay.receipts)


def test_unicode_escaped_credential_inside_tool_argument_json_is_rejected() -> None:
    escaped = "".join(f"\\u{ord(char):04x}" for char in SECRET)
    body = stream(
        event(
            choices=[
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {
                                    "name": "edit",
                                    "arguments": '{"newString":"' + escaped + '"}',
                                },
                            }
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        )
    )
    assert SECRET.encode() not in body
    with running(lambda _: answer(body)) as (relay, _):
        response = post(relay)
        relay.close()
        assert response.status_code == 502
        assert relay.receipts[0]["reason_codes"] == ["UPSTREAM_CREDENTIAL_ECHO"]
        assert SECRET not in response.text + json.dumps(relay.receipts)


def test_denied_canary_is_detected_before_upstream_send() -> None:
    with running() as (relay, requests):
        request = payload()
        request["messages"].append({"role": "tool", "content": CANARY})
        assert post(relay, request).status_code == 403
        relay.close()
        assert not requests
        receipt = relay.receipts[0]
        assert receipt["denied_canary_in_request"] is True
        assert receipt["upstream_send_attempted"] is False
        assert receipt["reason_codes"] == ["DENIED_CANARY_IN_REQUEST"]
        assert CANARY not in json.dumps(receipt)


def test_response_size_limit_fails_closed_instead_of_forwarding_truncated_evidence() -> None:
    body = stream(event(extra="x" * 1_048_576))
    with running(lambda _: answer(body)) as (relay, _):
        assert post(relay).status_code == 502
        relay.close()
        assert relay.receipts[0]["protocol_passed"] is False
        assert relay.receipts[0]["reason_codes"] == ["UPSTREAM_RESPONSE_TOO_LARGE"]
        assert relay.receipts[0]["response_bytes"] > 1_048_576


def test_transport_error_is_redacted_and_distinguished_from_an_http_reply() -> None:
    def fail(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(SECRET)

    with running(fail) as (relay, _):
        response = post(relay)
        relay.close()
        assert response.status_code == 502
        receipt = relay.receipts[0]
        assert receipt["upstream_send_attempted"] is True
        assert receipt["upstream_status"] is None
        assert receipt["reason_codes"] == ["RELAY_TRANSPORT_ERROR"]
        assert SECRET not in response.text + json.dumps(receipt)


def test_concurrent_requests_reserve_only_six_slots() -> None:
    barrier = threading.Barrier(7)
    release = threading.Event()

    def wait_upstream(_: httpx.Request) -> httpx.Response:
        barrier.wait(timeout=10)
        assert release.wait(timeout=10)
        return answer()

    with running(wait_upstream) as (relay, requests), ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(post, relay) for _ in range(6)]
        try:
            barrier.wait(timeout=10)
            pending = relay.receipts
            assert len(pending) == 6
            assert all(item["upstream_send_attempted"] for item in pending)
            assert all(item["protocol_passed"] is False for item in pending)
            assert post(relay).status_code == 429
            assert len(requests) == 6
        finally:
            release.set()
        assert [future.result(timeout=10).status_code for future in futures] == [200] * 6
        assert relay.close() == {"status": "closed", "errors": []}
        assert len(relay.receipts) == 6
        assert all(item["protocol_passed"] for item in relay.receipts)


def test_close_reports_unknown_while_upstream_handler_is_unfinished() -> None:
    entered = threading.Event()
    release = threading.Event()

    def wait_upstream(_: httpx.Request) -> httpx.Response:
        entered.set()
        assert release.wait(timeout=10)
        return answer()

    with running(wait_upstream) as (relay, _), ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(post, relay)
        try:
            assert entered.wait(timeout=5)
            assert relay.close() == {
                "status": "unknown",
                "errors": ["ACTIVE_HANDLER_REMAINS"],
            }
            assert relay.receipts[0]["protocol_passed"] is False
        finally:
            release.set()
        assert future.result(timeout=5).status_code == 503
        assert relay.close() == {"status": "closed", "errors": []}
        assert relay.receipts[0]["reason_codes"] == ["RELAY_CLOSING"]


def test_close_includes_connections_waiting_for_complete_http_headers() -> None:
    with running() as (relay, requests):
        connection = socket.create_connection(("127.0.0.1", urlsplit(relay.url).port), timeout=5)
        try:
            connection.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\n")
            with httpx.Client(trust_env=False) as client:
                assert client.get(relay.url).status_code == 405
            assert relay.close() == {
                "status": "unknown",
                "errors": ["ACTIVE_HANDLER_REMAINS"],
            }
            assert not requests
        finally:
            connection.close()
        assert relay.close() == {"status": "closed", "errors": []}


def test_go_cost_trailer_after_done_keeps_native_bytes_and_usage() -> None:
    # Observed shape: finish+usage, DONE, {choices: [], cost: str}.
    # The cost below is synthetic; the live observation retained only its type.
    cost = "0.000014382"
    trailer = stream({"choices": [], "cost": cost}, done=False)
    body = stream(event(usage={"prompt_tokens": 21, "completion_tokens": 9})) + trailer
    with running(lambda _: answer(body)) as (relay, _):
        response = post(relay)
        relay.close()
        assert response.status_code == 200
        assert response.content == body
        receipt = relay.receipts[0]
        assert receipt["protocol_passed"] is True
        assert receipt["usage"] == {"prompt_tokens": 21, "completion_tokens": 9}
        assert receipt["provider_reported_cost"] == cost
        assert receipt["provider_reported_cost_unit"] == "unknown"
        assert receipt["go_cost_trailer_seen"] is True


@pytest.mark.parametrize("cost", ["0", "1.4382e-5"])
def test_go_cost_trailer_accepts_bounded_decimal_strings_without_float_conversion(
    cost: str,
) -> None:
    body = stream(event()) + stream({"choices": [], "cost": cost}, done=False)
    with running(lambda _: answer(body)) as (relay, _):
        assert post(relay).status_code == 200
        relay.close()
        assert relay.receipts[0]["provider_reported_cost"] == cost


@pytest.mark.parametrize(
    "trailer",
    [
        {"choices": [], "cost": 0},
        {"choices": [], "cost": True},
        {"choices": [], "cost": "NaN"},
        {"choices": [], "cost": "-1"},
        {"choices": [], "cost": "1e999"},
        {"choices": [], "cost": "1000000000001"},
        {"choices": [], "cost": "0.1", "model": MODEL},
        {"choices": [{"delta": {"content": "unapproved"}}], "cost": "0.1"},
        {"choices": [], "usage": {"total_tokens": 1}},
    ],
)
def test_go_trailer_does_not_allow_other_post_done_data(trailer: dict[str, Any]) -> None:
    body = stream(event()) + stream(trailer, done=False)
    with running(lambda _: answer(body)) as (relay, _):
        assert post(relay).status_code == 502
        relay.close()
        assert relay.receipts[0]["protocol_passed"] is False


@pytest.mark.parametrize("ordering", ["before_done", "duplicate", "extra_done", "new_content"])
def test_go_trailer_is_once_only_and_strictly_after_done(ordering: str) -> None:
    trailer = stream({"choices": [], "cost": "0.1"}, done=False)
    bodies = {
        "before_done": stream(event(), done=False) + trailer + b"data: [DONE]\n\n",
        "duplicate": stream(event()) + trailer + trailer,
        "extra_done": stream(event()) + trailer + b"data: [DONE]\n\n",
        "new_content": stream(event()) + trailer + stream(event(), done=False),
    }
    with running(lambda _: answer(bodies[ordering])) as (relay, _):
        assert post(relay).status_code == 502
        relay.close()
        assert relay.receipts[0]["protocol_passed"] is False
