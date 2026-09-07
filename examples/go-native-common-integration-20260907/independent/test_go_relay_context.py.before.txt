"""Actual HTTP relay and durable journal; synthetic upstream and credentials."""

from typing import Any

import httpx
import pytest
from karajan.adapters.opencode.go_journal import GoCallJournal
from karajan.adapters.opencode.go_relay import GoRelay, GoRelayAuthorization, GoRelayContext
from karajan.routing.compiler import digest
from test_go_context import accounting, artifacts
from test_go_relay import CANARY, SECRET, answer, event, payload, post, stream
from test_go_task_grants import task_binding

__all__ = ["accounting", "artifacts"]


def metered_answer(usage=None):
    return answer(
        stream(
            event(usage=usage or {"prompt_tokens": 20, "completion_tokens": 2, "total_tokens": 22})
        )
    )


def test_task_grant_without_approved_context_accounting_cannot_send(tmp_path):
    journal = GoCallJournal(tmp_path / "calls.sqlite", clock=lambda: 1000.0)
    binding = task_binding()
    grant = journal.create_grant(binding, grant_id="task")
    upstream = []

    def receive(request):
        upstream.append(request)
        return metered_answer()

    relay = GoRelay(
        SECRET,
        CANARY,
        authorization=GoRelayAuthorization(journal, "task", binding, grant["capability"]),
        client_factory=lambda: httpx.Client(
            transport=httpx.MockTransport(receive), trust_env=False
        ),
    )
    relay.start()
    try:
        response = post(relay)
        assert response.status_code == 403
        assert response.json()["error"]["type"] == "TASK_CONTEXT_ACCOUNTING_REQUIRED"
        assert upstream == []
        assert journal.snapshot("task")["request_count"] == 0
    finally:
        assert relay.close()["status"] == "closed"


def test_complete_request_measurement_is_durable_before_upstream_receives_bytes(
    tmp_path, accounting
):
    journal = GoCallJournal(tmp_path / "calls.sqlite", clock=lambda: 1000.0)
    binding = task_binding()
    grant = journal.create_grant(binding, grant_id="task")
    upstream = []
    context = GoRelayContext(
        accounting=accounting,
        source_sha256=digest(accounting.source()),
        execution_policy_digest=binding["execution_policy_digest"],
        approved_input_tokens=4000,
        reserved_output_tokens=4096,
        operating_context_tokens=8192,
        fixed_margin=100,
        ratio_margin_basis_points=1000,
    )

    def receive(request):
        prior = GoCallJournal(journal.path, clock=lambda: 1000.0).snapshot("task")
        assert prior["request_count"] == 1
        assert prior["calls"][0]["state"] == "send_unknown"
        measured = prior["calls"][0]["request_context"]
        assert measured["measurement_confidence"] == "local_estimate"
        assert measured["requested_output_tokens"] == 256
        assert measured["approved_input_tokens"] == 4000
        assert measured["source_sha256"] == context.source_sha256
        upstream.append(request)
        return metered_answer()

    relay = GoRelay(
        SECRET,
        CANARY,
        context=context,
        authorization=GoRelayAuthorization(journal, "task", binding, grant["capability"]),
        client_factory=lambda: httpx.Client(
            transport=httpx.MockTransport(receive), trust_env=False
        ),
    )
    relay.start()
    try:
        assert post(relay).status_code == 200, relay.receipts
        assert len(upstream) == 1
        assert (
            relay.receipts[0]["request_context"]
            == journal.snapshot("task")["calls"][0]["request_context"]
        )
    finally:
        assert relay.close()["status"] == "closed"


@pytest.mark.parametrize("failure", ["input", "output", "context", "source", "policy", "history"])
def test_every_request_checks_approved_limits_before_spending_a_send_slot(
    tmp_path, accounting, failure
):
    journal = GoCallJournal(tmp_path / "calls.sqlite", clock=lambda: 1000.0)
    binding = task_binding()
    grant = journal.create_grant(binding, grant_id="task")
    options = {
        "accounting": accounting,
        "source_sha256": digest(accounting.source()),
        "execution_policy_digest": binding["execution_policy_digest"],
        "approved_input_tokens": 4000,
        "reserved_output_tokens": 4096,
        "operating_context_tokens": 8192,
        "fixed_margin": 100,
        "ratio_margin_basis_points": 1000,
    }
    if failure == "input":
        options["approved_input_tokens"] = 1
    elif failure == "output":
        options["reserved_output_tokens"] = 1
    elif failure == "context":
        options["operating_context_tokens"] = 4096
    elif failure == "source":
        options["source_sha256"] = "0" * 64
    elif failure == "policy":
        options["execution_policy_digest"] = "0" * 64
    upstream = []

    def receive(request):
        upstream.append(request)
        return metered_answer()

    relay = GoRelay(
        SECRET,
        CANARY,
        context=GoRelayContext(**options),
        authorization=GoRelayAuthorization(journal, "task", binding, grant["capability"]),
        client_factory=lambda: httpx.Client(
            transport=httpx.MockTransport(receive), trust_env=False
        ),
    )
    relay.start()
    try:
        request = payload()
        expected_calls = 0
        if failure == "history":
            assert post(relay, request).status_code == 200
            expected_calls = 1
            request["messages"].append({"role": "user", "content": " long history" * 10000})
        response = post(relay, request)
        assert 400 <= response.status_code < 500, relay.receipts
        assert len(upstream) == expected_calls
        assert journal.snapshot("task")["request_count"] == expected_calls
        assert relay.receipts[-1]["upstream_send_attempted"] is False
    finally:
        assert relay.close()["status"] == "closed"


def test_context_receipt_cannot_be_replaced_by_replaying_a_logical_call(tmp_path, accounting):
    from karajan.adapters.opencode.go_journal import GoJournalError

    journal = GoCallJournal(tmp_path / "calls.sqlite", clock=lambda: 1000.0)
    binding = task_binding()
    grant = journal.create_grant(binding, grant_id="task")
    context = GoRelayContext(
        accounting,
        digest(accounting.source()),
        binding["execution_policy_digest"],
        4000,
        4096,
        8192,
        100,
        1000,
    ).measure(payload())
    first = journal.begin_call(
        "task", "call", capability=grant["capability"], binding=binding, request_context=context
    )
    reopened = GoCallJournal(journal.path, clock=lambda: 1001.0)
    replay = reopened.begin_call(
        "task", "call", capability=grant["capability"], binding=binding, request_context=context
    )
    assert replay == {"send_allowed": False, "receipt": first["receipt"]}
    changed = {**context, "request_digest": "1" * 64}
    for altered in (changed, None):
        with pytest.raises(GoJournalError, match="CALL_CONTEXT_CONFLICT"):
            reopened.begin_call(
                "task",
                "call",
                capability=grant["capability"],
                binding=binding,
                request_context=altered,
            )
    assert reopened.snapshot("task")["request_count"] == 1
    assert reopened.call_receipt("task", "call") == first["receipt"]
    with pytest.raises(GoJournalError):
        reopened.begin_call(
            "task",
            "other",
            capability=grant["capability"],
            binding=binding,
            request_context={**context, "raw_prompt": "PRIVATE_CANARY"},
        )
    assert reopened.snapshot("task")["request_count"] == 1


@pytest.mark.parametrize("violation", ["missing", "input", "output", "transport"])
def test_failed_context_observation_revokes_remaining_sends(tmp_path, accounting, violation):
    journal = GoCallJournal(tmp_path / "calls.sqlite", clock=lambda: 1000.0)
    binding = task_binding()
    grant = journal.create_grant(binding, grant_id="task")
    upstream = []

    def receive(request):
        upstream.append(request)
        if violation == "transport":
            raise httpx.ReadTimeout("synthetic lost response")
        if violation == "missing":
            return answer()
        return metered_answer(
            {
                "prompt_tokens": 5000 if violation == "input" else 20,
                "completion_tokens": 4097 if violation == "output" else 2,
            }
        )

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
        authorization=GoRelayAuthorization(journal, "task", binding, grant["capability"]),
        client_factory=lambda: httpx.Client(
            transport=httpx.MockTransport(receive), trust_env=False
        ),
    )
    relay.start()
    try:
        assert post(relay).status_code == 502
        assert len(upstream) == 1
        assert journal.snapshot("task")["state"] == "revoked"
        recorded = journal.snapshot("task")["calls"][0]
        assert recorded["state"] == (
            "send_unknown" if violation == "transport" else "response_received"
        )
        assert recorded["outcome"]["protocol_passed"] is False
        assert post(relay).status_code == 503
        assert len(upstream) == 1
        assert journal.snapshot("task")["request_count"] == 1
    finally:
        assert relay.close()["status"] == "closed"


def test_lost_metered_begin_reply_revokes_original_grant_and_blocks_next_send(
    tmp_path, accounting, monkeypatch
) -> None:
    journal = GoCallJournal(tmp_path / "calls.sqlite", clock=lambda: 1000.0)
    binding = task_binding()
    grant = journal.create_grant(binding, grant_id="task")
    upstream: list[bool] = []

    def receive(request):
        upstream.append(True)
        return metered_answer()

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
        authorization=GoRelayAuthorization(journal, "task", binding, grant["capability"]),
        client_factory=lambda: httpx.Client(
            transport=httpx.MockTransport(receive), trust_env=False
        ),
    )
    original = journal.begin_call

    def commit_then_lose(*args: Any, **kwargs: Any) -> None:
        original(*args, **kwargs)
        raise OSError("synthetic lost committed begin result")

    monkeypatch.setattr(journal, "begin_call", commit_then_lose)
    relay.start()
    try:
        first_status = post(relay).status_code
        first = journal.snapshot("task")
        monkeypatch.setattr(journal, "begin_call", original)
        second_status = post(relay).status_code
        final = journal.snapshot("task")
        assert first_status == 502
        assert first["state"] == "revoked"
        assert first["calls"][0]["state"] == "send_unknown"
        assert second_status == 503
        assert final["request_count"] == 1
        assert not upstream
    finally:
        assert relay.close()["status"] == "closed"
