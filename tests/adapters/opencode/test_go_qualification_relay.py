"""Qualification accounting across actual local HTTP and persistent send intents."""

import copy
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
from test_go_relay import CANARY, SECRET, answer, post
from test_go_relay_context import metered_answer
from test_go_task_grants import task_binding

__all__ = ["accounting", "artifacts"]


def test_qualification_context_is_durable_before_the_first_upstream_bytes(tmp_path, accounting):
    bound = qualification_binding(accounting)
    context = relay_module.GoQualificationContext(
        accounting=accounting,
        probe_spec_digest=bound["probe_spec_digest"],
        scenario=bound["scenario"],
        **bound["context"],
    )
    journal = GoCallJournal(tmp_path / "calls.sqlite", clock=lambda: 1000.0)
    grant = journal.create_grant(bound, grant_id="qualification")
    upstream = []

    def receive(request):
        saved = GoCallJournal(journal.path, clock=lambda: 1000.0).snapshot("qualification")
        assert saved["request_count"] == 1
        assert saved["calls"][0]["state"] == "send_unknown"
        assert (
            saved["calls"][0]["request_context"]["source_sha256"]
            == bound["context"]["source_sha256"]
        )
        upstream.append(request)
        return metered_answer()

    relay = relay_module.GoRelay(
        SECRET,
        CANARY,
        context=context,
        authorization=relay_module.GoRelayAuthorization(
            journal, "qualification", bound, grant["capability"]
        ),
        client_factory=lambda: httpx.Client(
            transport=httpx.MockTransport(receive), trust_env=False
        ),
    )
    relay.start()
    try:
        assert post(relay).status_code == 200, relay.receipts
    finally:
        assert relay.close()["status"] == "closed"
    assert len(upstream) == 1
    saved = journal.snapshot("qualification")
    assert saved["calls"][0]["outcome"]["protocol_passed"] is True
    assert saved["calls"][0]["request_context"] == relay.receipts[0]["request_context"]


def context_for(accounting, bound):
    return relay_module.GoQualificationContext(
        accounting=accounting,
        probe_spec_digest=bound["probe_spec_digest"],
        scenario=bound["scenario"],
        **bound["context"],
    )


@contextmanager
def exchange(tmp_path, registered, context, *, presented=None, receive=None, capability=None):
    journal = GoCallJournal(tmp_path / "calls.sqlite", clock=lambda: 1000.0)
    grant = journal.create_grant(registered, grant_id="qualification")
    upstream = []

    def respond(request):
        upstream.append(request)
        return receive(request) if receive else metered_answer()

    relay = relay_module.GoRelay(
        SECRET,
        CANARY,
        context=context,
        authorization=relay_module.GoRelayAuthorization(
            journal,
            "qualification",
            registered if presented is None else presented,
            grant["capability"] if capability is None else capability,
        ),
        client_factory=lambda: httpx.Client(
            transport=httpx.MockTransport(respond), trust_env=False
        ),
    )
    relay.start()
    try:
        yield relay, journal, upstream
    finally:
        assert relay.close()["status"] == "closed"


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "task-context",
        "legacy-grant",
        "task-grant",
        "spec",
        "scenario",
        "source_sha256",
        "approved_input_tokens",
        "reserved_output_tokens",
        "operating_context_tokens",
        "fixed_margin",
        "ratio_margin_basis_points",
        "unknown-version",
    ],
)
def test_wrong_context_or_subject_never_reaches_upstream_or_spends_a_slot(
    tmp_path, accounting, change
):
    bound = qualification_binding(accounting)
    context = context_for(accounting, bound)
    presented = None
    expected = "QUALIFICATION_CONTEXT_BINDING_MISMATCH"
    if change == "missing":
        context = None
        expected = "QUALIFICATION_CONTEXT_ACCOUNTING_REQUIRED"
    elif change == "task-context":
        context = relay_module.GoRelayContext(
            accounting=accounting,
            execution_policy_digest="a" * 64,
            **bound["context"],
        )
        expected = "TASK_CONTEXT_POLICY_MISMATCH"
    elif change == "legacy-grant":
        bound = binding()
    elif change == "task-grant":
        bound = task_binding()
    elif change == "spec":
        context = replace(context, probe_spec_digest="d" * 64)
    elif change == "scenario":
        context = replace(context, scenario="denied_read")
    elif change == "unknown-version":
        presented = {**bound, "schema_version": "karajan.go-qualification-grant.v3"}
        expected = "GO_JOURNAL_INPUT_INVALID"
    else:
        value = "d" * 64 if change == "source_sha256" else bound["context"][change] + 1
        context = replace(context, **{change: value})
    with exchange(tmp_path, bound, context, presented=presented) as (relay, journal, upstream):
        response = post(relay)
        assert response.status_code == 403
        assert response.json()["error"]["type"] == expected
        assert upstream == []
        saved = journal.snapshot("qualification")
        assert saved["request_count"] == 0
        assert saved["state"] == "active"
        assert saved["binding"] == bound


@pytest.mark.parametrize(
    "field,value", [("probe_spec_digest", "d" * 64), ("scenario", "denied_read")]
)
def test_context_matching_fabricated_binding_cannot_change_a_persisted_grant(
    tmp_path, accounting, field, value
):
    bound = qualification_binding(accounting)
    presented = {**bound, field: value}
    context = context_for(accounting, presented)
    with exchange(tmp_path, bound, context, presented=presented) as (relay, journal, upstream):
        response = post(relay)
        assert response.status_code == 403
        assert response.json()["error"]["type"] == "GRANT_BINDING_MISMATCH"
        assert upstream == []
        assert journal.snapshot("qualification")["request_count"] == 0
        assert journal.snapshot("qualification")["state"] == "active"


@pytest.mark.parametrize("change", ["source", "input", "output", "window", "history"])
def test_bound_but_unmeasurable_request_is_rejected_before_a_durable_send(
    tmp_path, accounting, change
):
    bound = qualification_binding(accounting)
    if change == "source":
        bound["context"]["source_sha256"] = "e" * 64
    elif change == "input":
        bound["context"]["approved_input_tokens"] = 1
    elif change == "output":
        bound["context"]["reserved_output_tokens"] = 1
    elif change == "window":
        bound["context"]["operating_context_tokens"] = 4096
    context = context_for(accounting, bound)
    with exchange(tmp_path, bound, context) as (relay, journal, upstream):
        body = None
        if change == "history":
            from test_go_relay import payload

            body = payload()
            body["messages"].append(
                {"role": "tool", "content": "orphan", "tool_call_id": "unknown"}
            )
        response = post(relay, body)
        assert response.status_code == 422
        assert upstream == []
        assert journal.snapshot("qualification")["request_count"] == 0


@pytest.mark.parametrize("failure", ["missing-usage", "input-usage", "output-usage", "transport"])
def test_failed_qualification_response_withdraws_remaining_sends_and_keeps_history(
    tmp_path, accounting, failure
):
    bound = qualification_binding(accounting)

    def receive(request):
        if failure == "transport":
            raise httpx.ReadTimeout("synthetic lost response")
        if failure == "missing-usage":
            return answer()
        return metered_answer(
            {
                "prompt_tokens": 5000 if failure == "input-usage" else 20,
                "completion_tokens": 257 if failure == "output-usage" else 2,
            }
        )

    with exchange(tmp_path, bound, context_for(accounting, bound), receive=receive) as (
        relay,
        journal,
        upstream,
    ):
        assert post(relay).status_code == 502
        # Revocation precedes the error response; the handler's final journal
        # completion may follow it. Join cleanup before reading that final fact.
        assert journal.snapshot("qualification")["state"] == "revoked"
        assert post(relay).status_code == 503
        assert relay.close()["status"] == "closed"
        saved = journal.snapshot("qualification")
        assert saved["state"] == "revoked"
        assert saved["request_count"] == 1
        call = saved["calls"][0]
        assert call["state"] == ("send_unknown" if failure == "transport" else "response_received")
        assert call["outcome"]["protocol_passed"] is False
        assert len(upstream) == 1
        assert GoCallJournal(journal.path, clock=lambda: 1001.0).snapshot("qualification") == saved
        for private in (SECRET, CANARY, relay.capability, "synthetic lost response"):
            assert private not in json.dumps(saved)
            assert private not in json.dumps(relay.receipts)


def test_bad_capability_never_withdraws_another_grants_authority(tmp_path, accounting):
    bound = qualification_binding(accounting)
    with exchange(tmp_path, bound, context_for(accounting, bound), capability="a" * 43) as (
        relay,
        journal,
        upstream,
    ):
        response = post(relay)
        assert response.status_code == 403
        assert response.json()["error"]["type"] == "INVALID_CAPABILITY"
        assert upstream == []
        assert journal.snapshot("qualification")["state"] == "active"
        assert journal.snapshot("qualification")["request_count"] == 0


def test_context_export_is_detached_and_contains_only_fixed_limits(accounting):
    bound = qualification_binding(accounting)
    context = context_for(accounting, bound)
    actual = context.limits()
    assert actual == bound["context"]
    original = copy.deepcopy(actual)
    actual["approved_input_tokens"] = 1
    assert context.limits() == original


@pytest.mark.parametrize(
    "values",
    [
        {"scenario": "arbitrary"},
        {"probe_spec_digest": "private-text"},
        {"source_sha256": "unknown"},
        {"approved_input_tokens": True},
        {"reserved_output_tokens": 0},
        {"operating_context_tokens": 1000001},
        {"fixed_margin": -1},
        {"ratio_margin_basis_points": 10001},
    ],
)
def test_invalid_qualification_context_construction_has_a_fixed_error(accounting, values):
    context = context_for(accounting, qualification_binding(accounting))
    with pytest.raises(ValueError, match="^QUALIFICATION_CONTEXT_INVALID$"):
        replace(context, **values)
