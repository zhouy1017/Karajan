"""Independent actual HTTP/durable-journal cases; only synthetic upstream/material."""

import json
from contextlib import contextmanager

import httpx
import pytest
from karajan.adapters.opencode.go_journal import GoCallJournal
from karajan.adapters.opencode.go_relay import GoRelay, GoRelayAuthorization, GoRelayContext
from karajan.routing.compiler import digest
from test_go_context import accounting, artifacts, tool_history
from test_go_relay import CANARY, SECRET, answer, event, post, stream
from test_go_task_grants import task_binding

__all__ = ["accounting", "artifacts"]


@contextmanager
def metered(tmp_path, accounting, response=None, binding_change=None):
    journal = GoCallJournal(tmp_path / "calls.sqlite", clock=lambda: 1000.0)
    binding = task_binding()
    grant = journal.create_grant(binding, grant_id="task")
    sent = []

    def receive(request):
        sent.append(request)
        return (
            response(request, journal)
            if response is not None
            else answer(
                stream(
                    event(usage={"prompt_tokens": 20, "completion_tokens": 2, "total_tokens": 22})
                )
            )
        )

    auth_binding = dict(binding)
    if binding_change:
        auth_binding.update(binding_change)
    context = GoRelayContext(
        accounting,
        digest(accounting.source()),
        auth_binding["execution_policy_digest"],
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
        authorization=GoRelayAuthorization(journal, "task", auth_binding, grant["capability"]),
        client_factory=lambda: httpx.Client(
            transport=httpx.MockTransport(receive), trust_env=False
        ),
    )
    relay.start()
    try:
        yield relay, journal, sent
    finally:
        assert relay.close()["status"] == "closed"


@pytest.mark.parametrize("phase", ["begin", "complete"])
def test_durable_unknown_after_lost_journal_response_withdraws_remaining_grant_sends(
    tmp_path, accounting, monkeypatch, phase
):
    with metered(tmp_path, accounting) as (relay, journal, sent):
        original = journal.begin_call

        def lose_begin(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("synthetic begin committed but response lost")

        def fail_completion(*args, **kwargs):
            raise RuntimeError("synthetic completion unavailable before commit")

        with monkeypatch.context() as inject:
            if phase == "begin":
                inject.setattr(journal, "begin_call", lose_begin)
            else:
                inject.setattr(journal, "complete_call", fail_completion)
            response = post(relay)
            assert response.status_code == (502 if phase == "begin" else 200)
            # A closed relay waits for the handler finalizer; reopen using the same durable grant.
            assert relay.close()["status"] == "closed"
        record = GoCallJournal(journal.path, clock=lambda: 1001.0).snapshot("task")
        assert record["request_count"] == 1
        assert record["calls"][0]["state"] == "send_unknown"
        assert record["calls"][0]["request_context"]["measurement_confidence"] == "local_estimate"
        assert len(sent) == (0 if phase == "begin" else 1)
        assert record["state"] == "revoked", record


def test_later_usage_frame_cannot_erase_a_provider_reported_input_exceedance(tmp_path, accounting):
    def decreasing_usage(request, journal):
        return answer(
            stream(
                event(usage={"prompt_tokens": 5000, "completion_tokens": 2}),
                event(choices=[], usage={"prompt_tokens": 20, "completion_tokens": 2}),
            )
        )

    with metered(tmp_path, accounting, decreasing_usage) as (relay, journal, sent):
        response = post(relay)
        assert response.status_code == 502, response.text
        assert relay.close()["status"] == "closed"
        assert len(sent) == 1
        assert journal.snapshot("task")["state"] == "revoked"


def test_wrong_exact_binding_does_not_revoke_another_grants_remaining_authority(
    tmp_path, accounting
):
    with metered(tmp_path, accounting, binding_change={"fence": 2}) as (relay, journal, sent):
        assert post(relay).status_code == 403
        assert relay.close()["status"] == "closed"
        assert sent == []
        assert journal.snapshot("task")["state"] == "active"
        assert journal.snapshot("task")["request_count"] == 0


def test_complete_tool_history_measurement_matches_durable_sent_request_without_content_leak(
    tmp_path, accounting
):
    observed = []

    def receive(request, journal):
        prior = GoCallJournal(journal.path, clock=lambda: 1000.0).snapshot("task")
        assert prior["calls"][0]["state"] == "send_unknown"
        measured = prior["calls"][0]["request_context"]
        sent = json.loads(request.content)
        expected = accounting.measure(
            sent,
            approved_input_tokens=4000,
            reserved_output_tokens=4096,
            operating_context_tokens=8192,
            fixed_margin=100,
            ratio_margin_basis_points=1000,
        )
        assert measured == expected
        assert "PRIVATE_REASONING_CANARY" not in json.dumps(prior)
        observed.append(measured)
        return answer(
            stream(
                event(
                    usage={"prompt_tokens": measured["local_input_tokens"], "completion_tokens": 2}
                )
            )
        )

    with metered(tmp_path, accounting, receive) as (relay, journal, sent):
        request = tool_history()
        assert post(relay, request).status_code == 200, relay.receipts
        assert relay.close()["status"] == "closed"
        assert len(observed) == len(sent) == 1
        assert journal.snapshot("task")["calls"][0]["outcome"]["protocol_passed"] is True
