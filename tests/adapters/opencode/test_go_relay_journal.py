"""The actual HTTP relay consumes durable grants; only upstream HTTP is synthetic."""

import httpx
from karajan.adapters.opencode.go_journal import GoCallJournal
from karajan.adapters.opencode.go_relay import GoRelay, GoRelayAuthorization
from test_go_relay import CANARY, SECRET, answer, post


def authorization(tmp_path, maximum=6):
    journal = GoCallJournal(tmp_path / "calls.sqlite", clock=lambda: 1000.0)
    binding = {
        "qualification_id": "qualification",
        "attempt_id": "attempt",
        "fence": 1,
        "profile_digest": "a" * 64,
        "runtime_digest": "b" * 64,
        "channel": "opencode-go",
        "model": "glm-5.3-flash",
        "auth_generation": "first",
        "expires_at": 2000.0,
        "max_requests": maximum,
    }
    grant = journal.create_grant(binding, grant_id="grant")
    return GoRelayAuthorization(journal, "grant", binding, grant["capability"])


def test_restarting_the_relay_does_not_reset_its_persistent_request_limit(tmp_path):
    auth = authorization(tmp_path, 2)
    upstream = []

    def receive(request):
        upstream.append(request)
        return answer()

    def relay():
        return GoRelay(
            SECRET,
            CANARY,
            authorization=auth,
            client_factory=lambda: httpx.Client(
                transport=httpx.MockTransport(receive),
                trust_env=False,
            ),
        )

    first = relay()
    first.start()
    try:
        assert post(first).status_code == 200
    finally:
        assert first.close()["status"] == "closed"
    second = relay()
    second.start()
    try:
        assert post(second).status_code == 200
        assert post(second).status_code == 429
    finally:
        assert second.close()["status"] == "closed"
    assert len(upstream) == 2
    facts = auth.journal.snapshot("grant")
    assert facts["request_count"] == 2
    assert [call["state"] for call in facts["calls"]] == ["response_received"] * 2
    assert all(call["outcome"]["protocol_passed"] for call in facts["calls"])


def test_revoked_grant_cannot_emit_a_new_upstream_request(tmp_path):
    auth = authorization(tmp_path)
    upstream = []

    def receive(request):
        upstream.append(request)
        return answer()

    relay = GoRelay(
        SECRET,
        CANARY,
        authorization=auth,
        client_factory=lambda: httpx.Client(
            transport=httpx.MockTransport(receive),
            trust_env=False,
        ),
    )
    relay.start()
    try:
        auth.journal.revoke_grant("grant")
        response = post(relay)
        assert response.status_code == 403
        assert response.json()["error"]["type"] == "GRANT_REVOKED"
    finally:
        assert relay.close()["status"] == "closed"
    assert upstream == []
    assert auth.journal.snapshot("grant")["request_count"] == 0


def test_transport_failure_remains_unknown_after_reopening_the_journal(tmp_path):
    auth = authorization(tmp_path)
    upstream = []

    def receive(request):
        upstream.append(request)
        raise httpx.ReadError("synthetic lost response", request=request)

    relay = GoRelay(
        SECRET,
        CANARY,
        authorization=auth,
        client_factory=lambda: httpx.Client(
            transport=httpx.MockTransport(receive),
            trust_env=False,
        ),
    )
    relay.start()
    try:
        assert post(relay).status_code == 502
    finally:
        assert relay.close()["status"] == "closed"
    assert len(upstream) == 1
    reopened = GoCallJournal(tmp_path / "calls.sqlite", clock=lambda: 1001.0)
    facts = reopened.snapshot("grant")
    assert facts["request_count"] == 1
    assert facts["calls"][0]["state"] == "send_unknown"
    assert facts["calls"][0]["outcome"]["reason_codes"] == ["RELAY_TRANSPORT_ERROR"]
    assert (
        reopened.begin_call(
            "grant",
            facts["calls"][0]["call_id"],
            capability=auth.capability,
            binding=auth.binding,
        )["send_allowed"]
        is False
    )
