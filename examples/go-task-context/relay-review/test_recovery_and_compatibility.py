"""Post-fix independent boundaries, with real HTTP and SQLite and no provider."""

import httpx
from karajan.adapters.opencode.go_journal import GoCallJournal
from karajan.adapters.opencode.go_relay import GoRelay, GoRelayAuthorization, GoRelayContext
from karajan.routing.compiler import digest
from test_go_relay import CANARY, SECRET, answer, event, post, stream
from test_go_relay_journal import authorization
from test_go_task_grants import task_binding
from test_independent_context_relay import accounting, artifacts, metered

__all__ = ["accounting", "artifacts"]


def test_later_usage_cannot_erase_completion_exceedance(tmp_path, accounting):
    def response(request, journal):
        return answer(
            stream(
                event(usage={"prompt_tokens": 20, "completion_tokens": 300}),
                event(choices=[], usage={"prompt_tokens": 20, "completion_tokens": 2}),
            )
        )

    with metered(tmp_path, accounting, response) as (relay, journal, sent):
        response = post(relay)
        assert response.status_code == 502
        assert response.json()["error"]["type"] == "CONTEXT_PROVIDER_OUTPUT_EXCEEDED"
        assert relay.close()["status"] == "closed"
        recorded = journal.snapshot("task")
        assert len(sent) == recorded["request_count"] == 1
        assert recorded["state"] == "revoked"
        assert recorded["calls"][0]["outcome"]["usage"]["completion_tokens"] == 300


def test_lost_begin_and_unavailable_lookup_only_close_transport_until_controller_reconciles(
    tmp_path, accounting, monkeypatch
):
    with metered(tmp_path, accounting) as (relay, journal, sent):
        original = journal.begin_call

        def lose_reply(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("synthetic committed begin with lost response")

        def unavailable_lookup(*args, **kwargs):
            raise RuntimeError("synthetic read unavailable")

        with monkeypatch.context() as inject:
            inject.setattr(journal, "begin_call", lose_reply)
            inject.setattr(journal, "snapshot", unavailable_lookup)
            assert post(relay).status_code == 502
            assert post(relay).status_code == 503
            assert relay.close()["status"] == "closed"
        record = GoCallJournal(journal.path, clock=lambda: 1001.0).snapshot("task")
        assert sent == []
        assert record["request_count"] == 1
        assert record["calls"][0]["state"] == "send_unknown"
        # No successful revocation can be claimed while ownership lookup is unavailable.
        # A future controller must reconcile this old grant before a new relay is built.
        assert record["state"] == "active"


def test_wrong_capability_with_matching_public_binding_never_revokes_the_grant(
    tmp_path, accounting
):
    journal = GoCallJournal(tmp_path / "wrong-capability.sqlite", clock=lambda: 1000.0)
    binding = task_binding()
    journal.create_grant(binding, grant_id="task")
    sent = []

    def receive(request):
        sent.append(request)
        return answer()

    context = GoRelayContext(
        accounting,
        digest(accounting.source()),
        binding["execution_policy_digest"],
        4000,
        4096,
        8192,
        100,
        1000,
    )
    relay = GoRelay(
        SECRET,
        CANARY,
        context=context,
        authorization=GoRelayAuthorization(journal, "task", binding, "wrong-capability"),
        client_factory=lambda: httpx.Client(
            transport=httpx.MockTransport(receive), trust_env=False
        ),
    )
    relay.start()
    try:
        assert post(relay).status_code == 403
        assert relay.close()["status"] == "closed"
        assert journal.snapshot("task")["state"] == "active"
        assert journal.snapshot("task")["request_count"] == 0
        assert sent == []
    finally:
        assert relay.close()["status"] == "closed"


def test_legacy_qualification_still_accepts_missing_usage_without_task_context(tmp_path):
    auth = authorization(tmp_path)
    relay = GoRelay(
        SECRET,
        CANARY,
        authorization=auth,
        client_factory=lambda: httpx.Client(
            transport=httpx.MockTransport(lambda request: answer()), trust_env=False
        ),
    )
    relay.start()
    try:
        assert post(relay).status_code == 200
        assert relay.close()["status"] == "closed"
        recorded = GoCallJournal(auth.journal.path, clock=lambda: 1001.0).snapshot("grant")
        assert recorded["state"] == "active"
        assert recorded["request_count"] == 1
        call = recorded["calls"][0]
        assert "request_context" not in call
        assert call["outcome"]["protocol_passed"] is True
        assert call["outcome"]["usage"] == {}
    finally:
        assert relay.close()["status"] == "closed"
