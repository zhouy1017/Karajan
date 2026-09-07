"""Actual local HTTP, SQLite and fixed tokenizer; synthetic upstream and credentials."""

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import httpx
import pytest
from karajan.adapters.opencode.go_journal import GoCallJournal
from karajan.adapters.opencode.go_relay import GoRelay, GoRelayAuthorization, GoRelayContext
from karajan.routing.compiler import digest
from test_go_context import accounting, artifacts
from test_go_relay import CANARY, SECRET, event, post, stream
from test_go_relay_context import metered_answer
from test_go_task_grants import task_binding

__all__ = ["accounting", "artifacts"]


@contextmanager
def guarded_relay(tmp_path, accounting, guard, receive=None, journal_type=GoCallJournal):
    journal = journal_type(tmp_path / "calls.sqlite", clock=lambda: 1000.0)
    binding = task_binding()
    grant = journal.create_grant(binding, grant_id="task")
    upstream = []

    def respond(request):
        upstream.append(request)
        return metered_answer() if receive is None else receive(request)

    relay = GoRelay(
        SECRET,
        CANARY,
        send_guard=guard,
        authorization=GoRelayAuthorization(journal, "task", binding, grant["capability"]),
        context=GoRelayContext(
            accounting,
            digest(accounting.source()),
            binding["execution_policy_digest"],
            4000,
            4096,
            8192,
            100,
            1000,
        ),
        client_factory=lambda: httpx.Client(
            transport=httpx.MockTransport(respond), trust_env=False
        ),
    )
    relay.start()
    try:
        yield relay, journal, upstream
    finally:
        relay.close()


def test_guard_rejection_spends_no_journal_slot_and_sends_no_request(tmp_path, accounting):
    @contextmanager
    def denied():
        raise ValueError("private owner and credential details")
        yield

    with guarded_relay(tmp_path, accounting, denied) as (relay, journal, upstream):
        response = post(relay)
        assert relay.close()["status"] == "closed"
        assert response.status_code == 403
        assert response.json()["error"]["type"] == "TASK_SEND_GUARD_REJECTED"
        assert "private owner" not in response.text
        assert "private owner" not in str(relay.receipts)
        assert journal.snapshot("task")["request_count"] == 0
        assert upstream == []
        assert relay.receipts[-1]["upstream_send_attempted"] is False


class Authority:
    """Minimal controller test port with actual SQLite withdrawal serialization."""

    def __init__(self, path):
        self.path = path
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE control (enabled INTEGER NOT NULL)")
            db.execute("INSERT INTO control VALUES (1)")

    @contextmanager
    def guard(self):
        with sqlite3.connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("PRAGMA query_only=ON")
            if db.execute("SELECT enabled FROM control").fetchone()[0] != 1:
                raise ValueError("synthetic revoked private binding")
            yield

    def withdraw(self, *, timeout=1):
        with sqlite3.connect(self.path, timeout=timeout) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE control SET enabled=0")


def test_business_guard_covers_actual_send_but_releases_before_response_body(tmp_path, accounting):
    authority = Authority(tmp_path / "authority.sqlite")
    observations = []

    class Body(httpx.SyncByteStream):
        def __iter__(self):
            authority.withdraw(timeout=0)
            observations.append("withdrawal committed while body is read")
            yield stream(event(usage={"prompt_tokens": 20, "completion_tokens": 2}))

    def receive(request):
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            authority.withdraw(timeout=0)
        observations.append("withdrawal blocked at upstream send")
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Body())

    with guarded_relay(tmp_path, accounting, authority.guard, receive) as (
        relay,
        journal,
        upstream,
    ):
        assert post(relay).status_code == 200, relay.receipts
        assert observations == [
            "withdrawal blocked at upstream send",
            "withdrawal committed while body is read",
        ]
        assert post(relay).json()["error"]["type"] == "TASK_SEND_GUARD_REJECTED"
        assert relay.close()["status"] == "closed"
        assert len(upstream) == 1
        assert journal.snapshot("task")["request_count"] == 1


def test_guard_and_network_entry_do_not_hold_relay_condition(tmp_path, accounting):
    checked = []
    with ThreadPoolExecutor(max_workers=1) as reader:

        @contextmanager
        def guard():
            checked.append(reader.submit(lambda: relay.receipts).result(timeout=2))
            yield

        def receive(request):
            checked.append(reader.submit(lambda: relay.receipts).result(timeout=2))
            return metered_answer()

        with guarded_relay(tmp_path, accounting, guard, receive) as (relay, journal, upstream):
            assert post(relay).status_code == 200, relay.receipts
            assert relay.close()["status"] == "closed"
            assert len(checked) == 2
            assert len(upstream) == journal.snapshot("task")["request_count"] == 1


def test_cancel_persists_then_closes_while_handler_waits_for_business_guard(tmp_path, accounting):
    authority = Authority(tmp_path / "authority.sqlite")
    entering = threading.Event()
    released = threading.Event()

    @contextmanager
    def guard():
        entering.set()
        assert released.wait(timeout=5)
        with authority.guard():
            yield

    with guarded_relay(tmp_path, accounting, guard) as (relay, journal, upstream):
        with ThreadPoolExecutor(max_workers=2) as pool:
            request = pool.submit(post, relay)
            assert entering.wait(timeout=3)
            authority.withdraw()
            journal.revoke_grant("task")
            closing = pool.submit(relay.close)
            # A close result is bounded even while this handler has not entered
            # the controller lock. No condition -> controller lock inversion.
            try:
                result = closing.result(timeout=4)
                assert result["status"] == "unknown"
                assert "ACTIVE_HANDLER_REMAINS" in result["errors"]
            finally:
                released.set()
            assert request.result(timeout=3).status_code == 403
        assert relay.close()["status"] == "closed"
        assert journal.snapshot("task")["request_count"] == 0
        assert upstream == []


@pytest.mark.parametrize("failure", ["factory", "exit"])
def test_guard_lifecycle_errors_are_redacted_and_never_refund_a_send(tmp_path, accounting, failure):
    marker = "PRIVATE_GUARD_EXCEPTION_MATERIAL"

    @contextmanager
    def on_exit():
        yield
        raise RuntimeError(marker)

    def guard():
        if failure == "factory":
            raise RuntimeError(marker)
        return on_exit()

    with guarded_relay(tmp_path, accounting, guard) as (relay, journal, upstream):
        response = post(relay)
        assert relay.close()["status"] == "closed"
        assert response.status_code == 403
        assert response.json()["error"]["type"] == "TASK_SEND_GUARD_REJECTED"
        expected = 0 if failure == "factory" else 1
        snapshot = journal.snapshot("task")
        assert snapshot["request_count"] == len(upstream) == expected
        assert marker not in response.text + json.dumps(relay.receipts) + json.dumps(snapshot)
        if expected:
            assert snapshot["calls"][0]["state"] == "send_unknown"
            assert snapshot["revoked_at"] is not None


@pytest.mark.parametrize("phase", ["headers", "body"])
def test_transport_failure_after_send_is_unknown_on_reopen_and_cannot_be_suppressed(
    tmp_path, accounting, phase
):
    @contextmanager
    def attempts_to_suppress():
        try:
            yield
        except httpx.ReadError:
            return

    class LostBody(httpx.SyncByteStream):
        def __iter__(self):
            raise httpx.ReadError("synthetic response lost after headers")
            yield

    def receive(request):
        if phase == "headers":
            raise httpx.ReadError("synthetic response lost before headers")
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=LostBody())

    with guarded_relay(tmp_path, accounting, attempts_to_suppress, receive) as (
        relay,
        journal,
        upstream,
    ):
        assert post(relay).status_code == 502
        assert post(relay).status_code == 503
        assert relay.close()["status"] == "closed"
        reopened = GoCallJournal(journal.path, clock=lambda: 1001.0)
        snapshot = reopened.snapshot("task")
        assert snapshot["request_count"] == len(upstream) == 1
        assert snapshot["calls"][0]["state"] == "send_unknown"
        assert snapshot["calls"][0]["outcome"]["reason_codes"] == ["RELAY_TRANSPORT_ERROR"]
        assert snapshot["revoked_at"] is not None
        assert reopened.snapshot("task")["request_count"] == 1


def test_lost_committed_begin_reply_never_sends_or_spends_a_second_slot(tmp_path, accounting):
    class LostReplyJournal(GoCallJournal):
        def begin_call(self, *args, **kwargs):
            super().begin_call(*args, **kwargs)
            raise RuntimeError("synthetic loss after actual SQLite commit")

    @contextmanager
    def allowed():
        yield

    with guarded_relay(tmp_path, accounting, allowed, journal_type=LostReplyJournal) as (
        relay,
        journal,
        upstream,
    ):
        assert post(relay).status_code == 502
        assert post(relay).status_code == 503
        assert relay.close()["status"] == "closed"
        reopened = GoCallJournal(journal.path, clock=lambda: 1001.0)
        snapshot = reopened.snapshot("task")
        assert snapshot["request_count"] == 1
        assert snapshot["calls"][0]["state"] == "send_unknown"
        assert snapshot["revoked_at"] is not None
        assert upstream == []
        assert relay.receipts[0]["upstream_send_attempted"] is False
        assert reopened.snapshot("task")["request_count"] == 1
