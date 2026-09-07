"""Nullable Chat Completions name fragments through the public relay transport."""

import socketserver
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import httpx
import pytest
from karajan.adapters.opencode.go_relay import GoRelay
from test_go_relay import CANARY, SECRET, answer, event, post, running, stream
from test_go_relay_journal import authorization


def tool_frame(function):
    return event(
        choices=[
            {
                "index": 0,
                "delta": {"tool_calls": [{"index": 0, "function": function}]},
                "finish_reason": None,
            }
        ]
    )


def response(functions):
    return stream(
        *(tool_frame(function) for function in functions),
        event(choices=[{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]),
    )


@contextmanager
def running_with_journal(tmp_path, respond):
    auth = authorization(tmp_path)
    upstream = []

    def receive(request: httpx.Request) -> httpx.Response:
        upstream.append(request)
        return answer() if respond is None else respond(request)

    relay = GoRelay(
        SECRET,
        CANARY,
        authorization=auth,
        client_factory=lambda: httpx.Client(
            transport=httpx.MockTransport(receive),
            trust_env=False,
            follow_redirects=False,
        ),
    )
    relay.start()
    try:
        yield relay, auth, upstream
    finally:
        relay.close()


@pytest.mark.parametrize("names", [["read", None], [None, "re", None, "ad"], ["edit", None]])
def test_null_is_no_new_name_fragment_but_records_content_free_count(names):
    raw = response([{"name": name, "arguments": ""} for name in names])
    with running(lambda request: answer(raw)) as (relay, upstream):
        result = post(relay)
        assert result.status_code == 200
        receipt = relay.receipts[0]
        assert receipt["protocol_passed"] is True
        assert receipt["tool_names"] == ["".join(n for n in names if n is not None)]
        assert receipt["tool_name_null_fragments"] == names.count(None)
        assert len(upstream) == 1


def test_absent_name_keeps_legacy_receipt_shape():
    raw = response([{"name": "read"}, {"arguments": "{}"}])
    with running(lambda request: answer(raw)) as (relay, _upstream):
        assert post(relay).status_code == 200
        assert "tool_name_null_fragments" not in relay.receipts[0]


@pytest.mark.parametrize(
    ("names", "reason"),
    [
        ([None, None], "UNAPPROVED_TOOL"),
        (["rea", None], "UNAPPROVED_TOOL"),
        (["shell", None], "UNAPPROVED_TOOL"),
        (["read", "read", None], "UNAPPROVED_TOOL"),
        (["read", False], "INVALID_TOOL_NAME"),
        (["read", 0], "INVALID_TOOL_NAME"),
        (["read", ["read"]], "INVALID_TOOL_NAME"),
    ],
)
def test_nullable_names_never_expand_final_allowlist_or_accept_other_types(names, reason):
    raw = response([{"name": name} for name in names])
    with running(lambda request: answer(raw)) as (relay, _upstream):
        assert post(relay).status_code == 502
        assert relay.receipts[0]["protocol_passed"] is False
        assert relay.receipts[0]["reason_codes"] == [reason]


def test_receipt_protocol_state_publishes_before_body_is_observable(tmp_path, monkeypatch):
    raw = response([{"name": "read"}, {"name": None, "arguments": "{}"}])

    with running_with_journal(tmp_path, lambda request: answer(raw)) as (relay, auth, _upstream):
        written = threading.Event()
        resume = threading.Event()
        write_timeout = threading.Event()
        original_write = socketserver._SocketWriter.write
        write_pause_seconds = 10

        def delayed_write(self, data):
            result = original_write(self, data)
            if isinstance(data, bytes | bytearray) and data.startswith(b"data:"):
                written.set()
                if not resume.wait(timeout=write_pause_seconds):
                    write_timeout.set()
            return result

        monkeypatch.setattr(socketserver._SocketWriter, "write", delayed_write)
        response_result = None
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(post, relay)
                assert written.wait(timeout=5), "server did not emit first data frame"
                response_result = future.result(timeout=3)
            assert not write_timeout.is_set(), (
                "write pause watchdog expired while body should be observable"
            )
            assert response_result is not None
            assert response_result.status_code == 200
            assert len(response_result.content) == int(response_result.headers["content-length"])
            assert len(response_result.content) == len(raw)
            assert response_result.text == response_result.content.decode()
            receipt = relay.receipts[0]
            assert receipt["protocol_passed"] is True
            assert receipt["relay_completed"] is False
            assert receipt["tool_name_null_fragments"] == 1
            assert auth.journal.snapshot("grant")["request_count"] == 1
            assert auth.journal.snapshot("grant")["calls"][0]["state"] == "send_unknown"
        finally:
            resume.set()
            assert relay.close() == {"status": "closed", "errors": []}

        assert relay.receipts[0]["protocol_passed"] is True
        assert relay.receipts[0]["relay_completed"] is True
        assert relay.receipts[0]["tool_name_null_fragments"] == 1
        assert auth.journal.snapshot("grant")["calls"][0]["state"] == "response_received"
        assert not write_timeout.is_set()


def test_receipt_protocol_state_publishes_before_transport_complete(tmp_path):
    raw = response([{"name": "read"}, {"name": None, "arguments": "{}"}])

    auth = authorization(tmp_path)
    closing = threading.Event()
    release_close = threading.Event()

    class PausedCloseTransport(httpx.MockTransport):
        def close(self) -> None:
            closing.set()
            assert release_close.wait(timeout=10)
            super().close()

    relay = GoRelay(
        SECRET,
        CANARY,
        authorization=auth,
        client_factory=lambda: httpx.Client(
            transport=PausedCloseTransport(lambda request: answer(raw)),
            trust_env=False,
            follow_redirects=False,
        ),
    )
    relay.start()
    response_result = None
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(post, relay)
            response_result = future.result(timeout=3)
            assert closing.wait(timeout=3)
            receipt = relay.receipts[0]
            journal = auth.journal.snapshot("grant")
            assert response_result.status_code == 200
            assert len(response_result.content) == int(response_result.headers["content-length"])
            assert len(response_result.content) == len(raw)
            assert receipt["protocol_passed"] is True
            assert receipt["relay_completed"] is True
            assert journal["calls"][0]["state"] == "send_unknown"
    finally:
        release_close.set()
        assert relay.close() == {"status": "closed", "errors": []}
    assert auth.journal.snapshot("grant")["calls"][0]["state"] == "response_received"
