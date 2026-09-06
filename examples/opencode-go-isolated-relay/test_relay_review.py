"""Independent relay boundary checks; upstream HTTP is explicitly synthetic."""

import json
import socket
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from karajan.adapters.opencode.go_journal import GoCallJournal
from karajan.adapters.opencode.go_relay import GoRelay, GoRelayAuthorization
from test_go_relay import CANARY, SECRET, answer, payload, post


def grant(tmp_path: Path, *, journal_type=GoCallJournal):
    journal = journal_type(tmp_path / "review.sqlite", clock=lambda: 1000.0)
    binding = {
        "qualification_id": "qualification-review",
        "attempt_id": "attempt-review",
        "fence": 1,
        "profile_digest": "a" * 64,
        "runtime_digest": "b" * 64,
        "channel": "opencode-go",
        "model": "glm-5.3-flash",
        "auth_generation": "generation-review",
        "expires_at": 2000.0,
        "max_requests": 6,
    }
    created = journal.create_grant(binding, grant_id="grant-review")
    return GoRelayAuthorization(journal, "grant-review", binding, created["capability"])


@contextmanager
def running(auth, receive):
    relay = GoRelay(
        SECRET,
        CANARY,
        authorization=auth,
        client_factory=lambda: httpx.Client(
            transport=httpx.MockTransport(receive), trust_env=False
        ),
    )
    relay.start()
    try:
        yield relay
    finally:
        assert relay.close()["status"] == "closed"


def test_partial_response_failure_does_not_become_a_completed_response(tmp_path):
    auth = grant(tmp_path)

    class PartialStream(httpx.SyncByteStream):
        def __iter__(self):
            yield b"data: {"
            raise httpx.ReadError("synthetic loss during SSE body")

    def receive(_):
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=PartialStream()
        )

    with running(auth, receive) as relay:
        assert post(relay).status_code == 502
    facts = auth.journal.snapshot(auth.grant_id)
    assert facts["request_count"] == 1
    assert facts["calls"][0]["state"] == "send_unknown"
    assert facts["calls"][0]["outcome"]["protocol_passed"] is False
    assert facts["calls"][0]["outcome"]["upstream_status"] == 200


@pytest.mark.parametrize("mutate_before_start", [False, True])
def test_exact_authorization_binding_is_copied_and_verified_before_send(
    tmp_path, mutate_before_start
):
    auth = grant(tmp_path)
    received = []

    def receive(request):
        received.append(request)
        return answer()

    if mutate_before_start:
        auth.binding["fence"] = 2
    with running(auth, receive) as relay:
        if not mutate_before_start:
            auth.binding["fence"] = 2
        response = post(relay)
        assert response.status_code == (403 if mutate_before_start else 200)
    assert len(received) == (0 if mutate_before_start else 1)
    assert auth.journal.snapshot(auth.grant_id)["request_count"] == len(received)


def test_native_headers_do_not_select_logical_call_identity_or_expose_credentials(tmp_path):
    auth = grant(tmp_path)
    received = []

    def receive(request):
        received.append(request)
        return answer()

    with running(auth, receive) as relay:
        with httpx.Client(trust_env=False) as client:
            headers = {
                "Authorization": f"Bearer {auth.capability}",
                "x-opencode-session": "same-native-session",
                "logical-call-id": "native-chosen-id",
            }
            assert (
                client.post(
                    relay.url + "/chat/completions", json=payload(), headers=headers
                ).status_code
                == 403
            )
            headers["Authorization"] = f"Bearer {relay.capability}"
            for _ in range(2):
                response = client.post(
                    relay.url + "/chat/completions", json=payload(), headers=headers
                )
                assert response.status_code == 200
                assert SECRET not in response.text
                assert auth.capability not in response.text
    calls = auth.journal.snapshot(auth.grant_id)["calls"]
    assert len(calls) == len(received) == 2
    assert len({call["call_id"] for call in calls}) == 2
    assert all(call["call_id"] != "native-chosen-id" for call in calls)
    for secret in (SECRET, auth.capability, relay.capability):
        assert secret not in json.dumps(relay.receipts)
        assert secret not in json.dumps(auth.journal.snapshot(auth.grant_id))


def test_lost_send_intent_return_stops_before_upstream_and_preserves_durable_unknown(tmp_path):
    class LostBeginReturn(GoCallJournal):
        def begin_call(self, *args, **kwargs):
            super().begin_call(*args, **kwargs)
            raise RuntimeError("synthetic lost commit return")

    auth = grant(tmp_path, journal_type=LostBeginReturn)
    received = []

    def receive(request):
        received.append(request)
        return answer()

    with running(auth, receive) as relay:
        assert post(relay).status_code == 502
    assert not received
    facts = GoCallJournal(tmp_path / "review.sqlite", clock=lambda: 1001.0).snapshot(auth.grant_id)
    assert facts["request_count"] == 1
    assert facts["calls"][0]["state"] == "send_unknown"
    assert facts["calls"][0]["outcome"] is None


def test_completion_failure_stays_visible_and_does_not_upgrade_journal_unknown(tmp_path):
    class FailedCompletion(GoCallJournal):
        def complete_call(self, *args, **kwargs):
            raise RuntimeError("synthetic persistence failure")

    auth = grant(tmp_path, journal_type=FailedCompletion)
    with running(auth, lambda _: answer()) as relay:
        # HTTP delivery can already have succeeded; it is not a qualification receipt.
        assert post(relay).status_code == 200
    facts = auth.journal.snapshot(auth.grant_id)
    assert facts["request_count"] == 1
    assert facts["calls"][0]["state"] == "send_unknown"
    assert facts["calls"][0]["outcome"] is None
    assert relay.receipts[0]["protocol_passed"] is False
    assert "JOURNAL_COMPLETION_FAILED" in relay.receipts[0]["reason_codes"]


def test_a_historical_begin_receipt_never_grants_the_relay_send_permission(tmp_path):
    class HistoricalCallPort(GoCallJournal):
        def begin_call(self, grant_id, call_id, **kwargs):
            return super().begin_call(grant_id, "existing-logical-call", **kwargs)

    auth = grant(tmp_path, journal_type=HistoricalCallPort)
    auth.journal.begin_call(
        auth.grant_id, "existing-logical-call", capability=auth.capability, binding=auth.binding
    )
    received = []

    def receive(request):
        received.append(request)
        return answer()

    with running(auth, receive) as relay:
        response = post(relay)
        assert response.status_code == 409
        assert response.json()["error"]["type"] == "CALL_SEND_NOT_AUTHORIZED"
    assert not received
    assert auth.journal.snapshot(auth.grant_id)["request_count"] == 1
    assert auth.journal.snapshot(auth.grant_id)["calls"][0]["outcome"] is None


def test_cancel_revokes_before_close_and_does_not_claim_active_handler_stopped(tmp_path):
    auth = grant(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    received = []

    def receive(request):
        received.append(request)
        entered.set()
        assert release.wait(timeout=10)
        return answer()

    relay = GoRelay(
        SECRET,
        CANARY,
        authorization=auth,
        client_factory=lambda: httpx.Client(
            transport=httpx.MockTransport(receive), trust_env=False
        ),
    )
    relay.start()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(post, relay)
        try:
            assert entered.wait(timeout=5)
            auth.journal.revoke_grant(auth.grant_id)
            assert relay.close() == {"status": "unknown", "errors": ["ACTIVE_HANDLER_REMAINS"]}
            assert auth.journal.snapshot(auth.grant_id)["calls"][0]["state"] == "send_unknown"
            with running(auth, receive) as restarted:
                assert post(restarted).status_code == 403
            assert len(received) == 1
        finally:
            release.set()
        assert future.result(timeout=5).status_code == 503
    assert relay.close() == {"status": "closed", "errors": []}
    assert auth.journal.snapshot(auth.grant_id)["request_count"] == 1
    assert not relay.receipts[0]["protocol_passed"]


@pytest.fixture
def unix_directory():
    # WSL DrvFS does not support AF_UNIX pathnames; use Linux's native filesystem.
    with tempfile.TemporaryDirectory(prefix="karajan-relay-review-", dir="/tmp") as directory:
        yield Path(directory)


@pytest.mark.skipif(sys.platform != "linux", reason="actual pathname Unix sockets")
@pytest.mark.parametrize("existing", ["dangling_symlink", "socket"])
def test_unix_start_preserves_preexisting_socket_or_dangling_symlink(existing, unix_directory):
    directory = unix_directory
    path = directory / (uuid4().hex[:6] + ".sock")
    other = None
    if existing == "dangling_symlink":
        path.symlink_to(directory / "nonexistent")
    else:
        other = socket.socket(socket.AF_UNIX)
        other.bind(str(path))
    relay = GoRelay(SECRET, CANARY)
    try:
        before = path.lstat()
        with pytest.raises(RuntimeError, match="^RELAY_SOCKET_PATH_EXISTS$"):
            relay.start(unix_socket=path)
        assert relay.close() == {"status": "closed", "errors": []}
        assert path.lstat().st_ino == before.st_ino
    finally:
        if other is not None:
            other.close()
        path.unlink()


@pytest.mark.skipif(sys.platform != "linux", reason="actual pathname Unix sockets")
def test_unix_cleanup_preserves_a_different_socket_at_its_old_path(unix_directory):
    directory = unix_directory
    path = directory / (uuid4().hex[:6] + ".sock")
    relay = GoRelay(SECRET, CANARY)
    relay.start(unix_socket=path)
    identity = path.lstat().st_ino
    path.unlink()
    other = socket.socket(socket.AF_UNIX)
    try:
        other.bind(str(path))
        assert path.lstat().st_ino != identity
        assert relay.close() == {"status": "closed", "errors": []}
        assert path.exists()
    finally:
        other.close()
        path.unlink(missing_ok=True)
