"""Reviewer transport across local HTTP and SQLite; upstream is an explicit fixture."""

import json
from contextlib import contextmanager
from dataclasses import replace

import httpx
import karajan.adapters.opencode.go_relay as relay_module
import pytest
from karajan.adapters.opencode.go_journal import GoCallJournal
from test_go_context import accounting, artifacts
from test_go_journal import binding
from test_go_qualification_grants import qualification_binding
from test_go_qualification_relay import exchange
from test_go_relay import CANARY, SECRET, answer, event, payload, post, stream
from test_go_relay_context import metered_answer
from test_go_reviewer_qualification_grants import reviewer_binding
from test_go_task_grants import task_binding

__all__ = ["accounting", "artifacts"]


def reviewer_context(accounting, bound):
    return relay_module.GoReviewerQualificationContext(
        accounting=accounting,
        probe_spec_digest=bound["probe_spec_digest"],
        scenario=bound["scenario"],
        **bound["context"],
    )


def read_request():
    return {
        **payload(),
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "read",
                    "parameters": {"type": "object"},
                },
            }
        ],
    }


def tool_answer(name="read"):
    return answer(
        stream(
            event(
                choices=[
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_read",
                                    "type": "function",
                                    "function": {
                                        "name": name,
                                        "arguments": '{"filePath":"src/range.py"}',
                                    },
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                usage={"prompt_tokens": 20, "completion_tokens": 2},
            )
        )
    )


def test_read_response_has_a_durable_measured_send_before_the_upstream_fixture(
    tmp_path, accounting
):
    bound = reviewer_binding(accounting)

    def receive(request):
        snapshot = GoCallJournal(tmp_path / "calls.sqlite", clock=lambda: 1000.0).snapshot(
            "qualification"
        )
        assert snapshot["request_count"] == 1
        assert snapshot["calls"][0]["state"] == "send_unknown"
        assert (
            snapshot["calls"][0]["request_context"]["source_sha256"]
            == bound["context"]["source_sha256"]
        )
        return tool_answer()

    with exchange(tmp_path, bound, reviewer_context(accounting, bound), receive=receive) as (
        relay,
        journal,
        upstream,
    ):
        response = post(relay, read_request())
        assert response.status_code == 200, relay.receipts
        assert relay.close()["status"] == "closed"
        assert len(upstream) == 1
        receipt = journal.snapshot("qualification")["calls"][0]
        assert receipt["outcome"]["protocol_passed"] is True
        assert relay.receipts[0]["tool_names"] == ["read"]
        assert receipt["request_context"] == relay.receipts[0]["request_context"]


@pytest.mark.parametrize("name", ["edit", "bash", "unknown_tool"])
def test_forbidden_response_never_reaches_native_and_revokes_own_remaining_sends(
    tmp_path, accounting, name
):
    bound = reviewer_binding(accounting)
    with exchange(
        tmp_path,
        bound,
        reviewer_context(accounting, bound),
        receive=lambda request: tool_answer(name),
    ) as (relay, journal, upstream):
        response = post(relay, read_request())
        assert response.status_code == 502
        assert response.json()["error"]["type"] == "UNAPPROVED_TOOL"
        assert name not in response.text
        assert relay.close()["status"] == "closed"
        saved = journal.snapshot("qualification")
        assert saved["state"] == "revoked"
        assert saved["request_count"] == 1
        assert saved["calls"][0]["outcome"]["protocol_passed"] is False
        assert len(upstream) == 1


@pytest.mark.parametrize("location", ["declaration", "history"])
@pytest.mark.parametrize("name", ["edit", "bash", "mcp__example", "unknown_tool"])
def test_edit_in_structural_request_is_rejected_before_any_send_slot(
    tmp_path, accounting, location, name
):
    bound = reviewer_binding(accounting)
    body = read_request()
    if location == "declaration":
        body["tools"][0]["function"]["name"] = name
    else:
        body["messages"].extend(
            [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_edit",
                            "type": "function",
                            "function": {"name": name, "arguments": '{"filePath":"src/range.py"}'},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_edit", "content": "synthetic result"},
            ]
        )
    with exchange(tmp_path, bound, reviewer_context(accounting, bound)) as (
        relay,
        journal,
        upstream,
    ):
        response = post(relay, body)
        assert response.status_code == 403
        assert response.json()["error"]["type"] == "UNAPPROVED_TOOL"
        assert upstream == []
        saved = journal.snapshot("qualification")
        assert saved["request_count"] == 0
        assert saved["state"] == "active"


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "worker-context",
        "task-context",
        "worker-grant",
        "task-grant",
        "legacy",
        "unknown-schema",
        "spec",
        "scenario",
        "source_sha256",
        "approved_input_tokens",
        "reserved_output_tokens",
        "operating_context_tokens",
        "fixed_margin",
        "ratio_margin_basis_points",
    ],
)
def test_wrong_kind_or_unbound_context_never_sends_or_spends_a_journal_slot(
    tmp_path, accounting, change
):
    bound = reviewer_binding(accounting, scenario="denied_read")
    context = reviewer_context(accounting, bound)
    presented = None
    if change == "missing":
        context = None
    elif change == "worker-context":
        context = relay_module.GoQualificationContext(
            accounting=accounting,
            probe_spec_digest=bound["probe_spec_digest"],
            scenario="denied_read",
            **bound["context"],
        )
    elif change == "task-context":
        context = relay_module.GoRelayContext(
            accounting=accounting,
            execution_policy_digest="a" * 64,
            **bound["context"],
        )
    elif change == "worker-grant":
        bound = {**qualification_binding(accounting), "scenario": "denied_read"}
    elif change == "task-grant":
        bound = task_binding()
    elif change == "legacy":
        bound = binding()
    elif change == "unknown-schema":
        presented = {**bound, "schema_version": "karajan.go-reviewer-qualification-grant.v9"}
    elif change == "spec":
        context = replace(context, probe_spec_digest="d" * 64)
    elif change == "scenario":
        context = replace(context, scenario="clean_review")
    else:
        value = "d" * 64 if change == "source_sha256" else bound["context"][change] + 1
        context = replace(context, **{change: value})
    with exchange(tmp_path, bound, context, presented=presented) as (relay, journal, upstream):
        assert post(relay, read_request()).status_code == 403
        assert upstream == []
        assert journal.snapshot("qualification")["request_count"] == 0
        assert journal.snapshot("qualification")["state"] == "active"


@pytest.mark.parametrize(
    "field,value",
    [
        ("scenario", "defect_review"),
        ("probe_spec_digest", "d" * 64),
    ],
)
def test_matching_context_for_a_fabricated_binding_cannot_reinterpret_the_original_grant(
    tmp_path, accounting, field, value
):
    bound = reviewer_binding(accounting)
    presented = {**bound, field: value}
    with exchange(
        tmp_path, bound, reviewer_context(accounting, presented), presented=presented
    ) as (
        relay,
        journal,
        upstream,
    ):
        response = post(relay, read_request())
        assert response.status_code == 403
        assert response.json()["error"]["type"] == "GRANT_BINDING_MISMATCH"
        assert upstream == []
        assert journal.snapshot("qualification")["request_count"] == 0
        assert journal.snapshot("qualification")["state"] == "active"


def test_read_history_is_metered_and_mentions_of_edit_in_review_data_are_allowed(
    tmp_path, accounting
):
    bound = reviewer_binding(accounting)
    with exchange(tmp_path, bound, reviewer_context(accounting, bound)) as (
        relay,
        journal,
        upstream,
    ):
        body = read_request()
        body["messages"][0]["content"] = 'Review this quoted data: {"name":"edit"}; do not edit.'
        assert post(relay, body).status_code == 200
        body["messages"].extend(
            [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_read",
                            "type": "function",
                            "function": {
                                "name": "read",
                                "arguments": '{"filePath":"src/range.py"}',
                            },
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_read", "content": "def clamp(x): return x"},
            ]
        )
        assert post(relay, body).status_code == 200
        assert relay.close()["status"] == "closed"
        calls = journal.snapshot("qualification")["calls"]
        assert len(calls) == len(upstream) == 2
        assert (
            calls[1]["request_context"]["local_input_tokens"]
            > calls[0]["request_context"]["local_input_tokens"]
        )
        assert (
            calls[1]["request_context"]["request_digest"]
            != calls[0]["request_context"]["request_digest"]
        )
        assert json.loads(upstream[1].content)["messages"] == body["messages"]


@contextmanager
def controlled_reviewer(
    tmp_path, accounting, *, journal_type=GoCallJournal, guard=None, receive=None
):
    bound = reviewer_binding(accounting)
    journal = journal_type(tmp_path / "calls.sqlite", clock=lambda: 1000.0)
    grant = journal.create_grant(bound, grant_id="reviewer")
    requests = []

    def respond(request):
        requests.append(request)
        return metered_answer() if receive is None else receive(request)

    relay = relay_module.GoRelay(
        SECRET,
        CANARY,
        context=reviewer_context(accounting, bound),
        send_guard=guard,
        authorization=relay_module.GoRelayAuthorization(
            journal,
            "reviewer",
            bound,
            grant["capability"],
        ),
        client_factory=lambda: httpx.Client(
            transport=httpx.MockTransport(respond),
            trust_env=False,
        ),
    )
    relay.start()
    try:
        yield relay, journal, requests
    finally:
        assert relay.close()["status"] == "closed"


def test_reviewer_send_guard_remains_current_after_a_successful_call(tmp_path, accounting):
    enabled = True
    inside = False

    @contextmanager
    def guard():
        nonlocal inside
        if not enabled:
            raise ValueError("PRIVATE_GUARD_DETAILS")
        inside = True
        try:
            yield
        finally:
            inside = False

    def receive(request):
        assert inside
        return metered_answer()

    with controlled_reviewer(tmp_path, accounting, guard=guard, receive=receive) as (
        relay,
        journal,
        requests,
    ):
        assert post(relay, read_request()).status_code == 200
        enabled = False
        response = post(relay, read_request())
        assert response.status_code == 403
        assert response.json()["error"]["type"] == "TASK_SEND_GUARD_REJECTED"
        assert len(requests) == journal.snapshot("reviewer")["request_count"] == 1
        assert "PRIVATE_GUARD_DETAILS" not in response.text + json.dumps(relay.receipts)


def test_lost_begin_response_is_read_back_without_sending_or_reauthorizing(tmp_path, accounting):
    class LostBegin(GoCallJournal):
        def begin_call(self, *args, **kwargs):
            super().begin_call(*args, **kwargs)
            raise OSError("PRIVATE_LOST_RESPONSE")

    with controlled_reviewer(tmp_path, accounting, journal_type=LostBegin) as (
        relay,
        journal,
        requests,
    ):
        response = post(relay, read_request())
        assert response.status_code == 502
        assert post(relay, read_request()).status_code == 503
        assert relay.close()["status"] == "closed"
        snapshot = GoCallJournal(journal.path, clock=lambda: 1001.0).snapshot("reviewer")
        assert snapshot["state"] == "revoked"
        assert snapshot["request_count"] == 1
        assert snapshot["calls"][0]["state"] == "send_unknown"
        assert snapshot["calls"][0]["call_id"] == relay.receipts[0]["journal_call_id"]
        assert snapshot["calls"][0]["request_context"] == relay.receipts[0]["request_context"]
        assert requests == []
        for private in (SECRET, CANARY, relay.capability, "PRIVATE_LOST_RESPONSE"):
            assert private not in response.text + json.dumps(snapshot) + json.dumps(relay.receipts)


@pytest.mark.parametrize("failure", ["usage", "transport"])
def test_failed_reviewer_response_keeps_its_call_and_withdraws_remaining_sends(
    tmp_path, accounting, failure
):
    def receive(request):
        if failure == "transport":
            raise httpx.ReadTimeout("PRIVATE_UPSTREAM_MESSAGE")
        return metered_answer({"prompt_tokens": 20, "completion_tokens": 257})

    with controlled_reviewer(tmp_path, accounting, receive=receive) as (relay, journal, requests):
        assert post(relay, read_request()).status_code == 502
        assert post(relay, read_request()).status_code == 503
        assert relay.close()["status"] == "closed"
        snapshot = journal.snapshot("reviewer")
        assert len(requests) == snapshot["request_count"] == 1
        assert snapshot["state"] == "revoked"
        call = snapshot["calls"][0]
        assert call["state"] == ("send_unknown" if failure == "transport" else "response_received")
        assert call["outcome"]["protocol_passed"] is False
        assert "PRIVATE_UPSTREAM_MESSAGE" not in json.dumps(snapshot) + json.dumps(relay.receipts)


@pytest.mark.parametrize(
    "changes",
    [
        {"scenario": "edit"},
        {"probe_spec_digest": "private-value"},
        {"approved_input_tokens": True},
        {"reserved_output_tokens": 0},
        {"ratio_margin_basis_points": 10001},
    ],
)
def test_reviewer_context_rejects_invalid_limits_with_a_stable_error(accounting, changes):
    context = reviewer_context(accounting, reviewer_binding(accounting))
    with pytest.raises(ValueError, match="^QUALIFICATION_CONTEXT_INVALID$"):
        replace(context, **changes)


def test_reviewer_limits_export_is_detached(accounting):
    bound = reviewer_binding(accounting)
    context = reviewer_context(accounting, bound)
    exported = context.limits()
    assert exported == bound["context"]
    exported["approved_input_tokens"] = 1
    assert context.limits() == bound["context"]
