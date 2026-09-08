"""Business Relay grants stay distinct from legacy and qualification records."""

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
from karajan.adapters.opencode.go_context import GoRequestAccounting
from karajan.adapters.opencode.go_journal import GoCallJournal, GoJournalError
from karajan.adapters.opencode.go_relay import (
    GoPlanningRelayContext,
    GoQualificationContext,
    GoRelay,
    GoRelayAuthorization,
    GoReviewerRelayContext,
)
from karajan.routing.compiler import digest
from test_go_context import measure
from test_go_relay import CANARY, SECRET, answer, event, payload, post, stream


@pytest.fixture(scope="module")
def accounting() -> GoRequestAccounting:
    return GoRequestAccounting(Path(os.environ["KARAJAN_GO_TOKENIZER_DIRECTORY"]))


def _common() -> dict[str, object]:
    return {
        "attempt_id": "attempt",
        "fence": 1,
        "profile_digest": "a" * 64,
        "runtime_digest": "b" * 64,
        "channel": "opencode-go",
        "model": "glm-5.3-flash",
        "auth_generation": "generation",
        "expires_at": 2000.0,
        "max_requests": 2,
    }


def _context(source: str = "c" * 64) -> dict[str, object]:
    return {
        "source_sha256": source,
        "approved_input_tokens": 4000,
        "reserved_output_tokens": 4096,
        "operating_context_tokens": 8192,
        "fixed_margin": 100,
        "ratio_margin_basis_points": 1000,
    }


def _planning(source: str = "c" * 64) -> dict[str, object]:
    return {
        **_common(),
        "schema_version": "karajan.go-planning-native-grant.v1",
        "subject": {
            "kind": "planning_execution",
            "project_id": "project",
            "run_id": "run",
            "intent_id": "intent",
            "execution_id": "execution",
        },
        "planning_binding_sha256": "d" * 64,
        "admission_sha256": "e" * 64,
        "input_sha256": "f" * 64,
        "authentication_source_digest": "0" * 64,
        "context": _context(source),
        "tool_policy": "none",
    }


def _reviewer(source: str = "c" * 64) -> dict[str, object]:
    return {
        **_common(),
        "schema_version": "karajan.go-reviewer-native-grant.v1",
        "subject": {
            "kind": "reviewer_execution",
            "project_id": "project",
            "run_id": "run",
            "reviewer_operation_id": "review-op",
            "worker_operation_id": "worker-op",
            "reviewer_task_id": "review",
            "execution_id": "execution",
        },
        "review_binding_sha256": "d" * 64,
        "reviewer_input_sha256": "e" * 64,
        "candidate_checks_sha256": "f" * 64,
        "authentication_source_digest": "0" * 64,
        "context": _context(source),
        "tool_policy": "read",
    }


def _qualification(source: str) -> dict[str, object]:
    return {
        **_common(),
        "schema_version": "karajan.go-qualification-grant.v2",
        "qualification_id": "qualification",
        "probe_spec_digest": "d" * 64,
        "scenario": "edit",
        "context": _context(source),
    }


def _qualification_context(accounting, binding):
    return GoQualificationContext(
        accounting=accounting,
        probe_spec_digest=binding["probe_spec_digest"],
        scenario=binding["scenario"],
        **binding["context"],
    )


_BUSINESS_CONTEXTS = [
    (_planning, GoPlanningRelayContext),
    (_reviewer, GoReviewerRelayContext),
]


def _business_context(accounting, binding, context_type):
    keys = (
        ("planning_binding_sha256", "admission_sha256", "input_sha256")
        if context_type is GoPlanningRelayContext
        else ("review_binding_sha256", "reviewer_input_sha256", "candidate_checks_sha256")
    )
    return context_type(
        accounting=accounting,
        **binding["context"],
        **{key: binding[key] for key in keys},
    )


def _metered_answer():
    return answer(stream(event(usage={"prompt_tokens": 20, "completion_tokens": 2})))


def _business_relay(journal, grant, binding, context, guard, upstream, receive):
    def observed(request):
        upstream.append(request)
        return receive(request)

    return GoRelay(
        SECRET,
        CANARY,
        context=context,
        authorization=GoRelayAuthorization(
            journal, grant["grant_id"], binding, grant["capability"]
        ),
        send_guard=guard,
        client_factory=lambda: httpx.Client(
            transport=httpx.MockTransport(observed), trust_env=False
        ),
    )


def _measurement(accounting, binding):
    limits = dict(binding["context"])
    del limits["source_sha256"]
    return measure(accounting, payload(), **limits)


@pytest.mark.parametrize("factory", [_planning, _reviewer])
def test_business_bindings_are_durable_and_context_bound(tmp_path, accounting, factory):
    source = digest(accounting.source())
    binding = factory(source)
    journal = GoCallJournal(tmp_path / "journal.sqlite", clock=lambda: 1000.0)
    created = journal.create_grant(binding, grant_id="grant")
    call = journal.begin_call(
        "grant",
        "relay-owned-call",
        capability=created["capability"],
        binding=binding,
        request_context=_measurement(accounting, binding),
    )
    assert call["send_allowed"] is True
    assert journal.snapshot("grant")["calls"][0]["state"] == "send_unknown"
    assert (
        journal.begin_call(
            "grant",
            "relay-owned-call",
            capability=created["capability"],
            binding=binding,
            request_context=_measurement(accounting, binding),
        )["send_allowed"]
        is False
    )


def test_business_context_tamper_is_pre_send_and_legacy_is_readable(tmp_path, accounting):
    journal = GoCallJournal(tmp_path / "journal.sqlite", clock=lambda: 1000.0)
    binding = _planning(digest(accounting.source()))
    created = journal.create_grant(binding, grant_id="new")
    bad = _measurement(accounting, binding)
    bad["approved_input_tokens"] = 1
    with pytest.raises(GoJournalError, match="GO_JOURNAL_INPUT_INVALID"):
        journal.begin_call(
            "new", "call", capability=created["capability"], binding=binding, request_context=bad
        )
    assert journal.snapshot("new")["request_count"] == 0
    legacy = {**_common(), "qualification_id": "historical"}
    old = journal.create_grant(legacy, grant_id="old")
    result = journal.authenticate_grant("old", capability=old["capability"], binding=legacy)
    assert result["binding"] == legacy


@pytest.mark.parametrize(
    "factory, context_type",
    [(_planning, GoPlanningRelayContext), (_reviewer, GoReviewerRelayContext)],
)
def test_business_grant_uses_relay_journal_and_actual_accounting(
    tmp_path, accounting, factory, context_type
):
    source = digest(accounting.source())
    binding = factory(source)
    journal = GoCallJournal(tmp_path / "journal.sqlite", clock=lambda: 1000.0)
    grant = journal.create_grant(binding, grant_id="grant")
    upstream = []
    context = _business_context(accounting, binding, context_type)

    def receive(request):
        snapshot = journal.snapshot("grant")
        assert snapshot["request_count"] == 1
        call = snapshot["calls"][0]
        assert call["state"] == "send_unknown"
        actual_payload = json.loads(request.content)
        expected = accounting.measure(
            actual_payload,
            **{key: value for key, value in binding["context"].items() if key != "source_sha256"},
        )
        assert call["request_context"] == expected
        assert call["request_context"]["request_digest"] == hashlib.sha256(
            request.content
        ).hexdigest()
        upstream.append(request)
        return answer(stream(event(usage={"prompt_tokens": 20, "completion_tokens": 2})))

    relay = GoRelay(
        SECRET,
        CANARY,
        context=context,
        authorization=GoRelayAuthorization(journal, "grant", binding, grant["capability"]),
        send_guard=lambda: _allowed(),
        client_factory=lambda: httpx.Client(
            transport=httpx.MockTransport(receive), trust_env=False
        ),
    )
    relay.start()
    try:
        assert post(relay).status_code == 200, relay.receipts
    finally:
        relay.close()
    assert len(upstream) == 1
    reopened = GoCallJournal(journal.path, clock=lambda: 1001.0)
    reopened_snapshot = reopened.snapshot("grant")
    call = reopened_snapshot["calls"][0]
    assert call["call_id"] == relay.receipts[0]["journal_call_id"]
    assert reopened_snapshot["binding"] == binding
    assert call["request_context"]["request_digest"] == hashlib.sha256(
        upstream[0].content
    ).hexdigest()
    assert call["outcome"]["state"] == "response_received"
    assert call["outcome"]["usage"] == {"prompt_tokens": 20, "completion_tokens": 2}
    assert call["outcome"]["protocol_passed"] is True
    assert relay.receipts[0]["usage"] == {"prompt_tokens": 20, "completion_tokens": 2}
    assert relay.receipts[0]["protocol_passed"] is True


@contextmanager
def _allowed():
    yield


@pytest.mark.parametrize(
    "factory, context_type",
    [(_planning, GoPlanningRelayContext), (_reviewer, GoReviewerRelayContext)],
)
@pytest.mark.parametrize("fault", ["guard", "context", "cross", "source", "tools"])
def test_business_relay_rejects_invalid_authority_before_journal_or_upstream(
    tmp_path, accounting, factory, context_type, fault
):
    source = digest(accounting.source())
    binding = factory(source)
    journal = GoCallJournal(tmp_path / "journal.sqlite", clock=lambda: 1000.0)
    grant = journal.create_grant(binding, grant_id="grant")
    context = _business_context(accounting, binding, context_type)
    if fault == "cross":
        other = _reviewer(source) if context_type is GoPlanningRelayContext else _planning(source)
        other_type = (
            GoReviewerRelayContext
            if context_type is GoPlanningRelayContext
            else GoPlanningRelayContext
        )
        context = _business_context(accounting, other, other_type)
    elif fault == "source":
        changed = {**binding["context"], "source_sha256": "0" * 64}
        changed_binding = {**binding, "context": changed}
        context = _business_context(accounting, changed_binding, context_type)
    upstream = []
    relay = GoRelay(
        SECRET,
        CANARY,
        context=None if fault == "context" else context,
        authorization=GoRelayAuthorization(journal, "grant", binding, grant["capability"]),
        send_guard=None if fault == "guard" else (lambda: _allowed()),
        client_factory=lambda: httpx.Client(
            transport=httpx.MockTransport(upstream.append), trust_env=False
        ),
    )
    relay.start()
    try:
        request = payload()
        if fault == "tools":
            request["tools"] = [
                {"type": "function", "function": {"name": "edit", "parameters": {}}}
            ]
        assert post(relay, request).status_code in {403, 422}
    finally:
        relay.close()
    assert journal.snapshot("grant")["request_count"] == 0
    assert upstream == []


@pytest.mark.parametrize("factory, context_type", _BUSINESS_CONTEXTS)
@pytest.mark.parametrize("direction", ["qualification_grant", "qualification_context"])
def test_business_and_qualification_authority_cannot_mix_through_relay(
    tmp_path, accounting, factory, context_type, direction
):
    """Both durable, valid authority types reject the other's real relay context."""
    source = digest(accounting.source())
    business = factory(source)
    qualification = _qualification(source)
    journal = GoCallJournal(tmp_path / "journal.sqlite", clock=lambda: 1000.0)
    binding = qualification if direction == "qualification_grant" else business
    grant = journal.create_grant(binding, grant_id="target")
    unrelated = journal.create_grant(factory(source), grant_id="unrelated")
    before = journal.snapshot("unrelated")
    context = (
        _business_context(accounting, business, context_type)
        if direction == "qualification_grant"
        else _qualification_context(accounting, qualification)
    )
    upstream = []
    relay = _business_relay(
        journal, grant, binding, context, _allowed, upstream, lambda _: _metered_answer()
    )
    relay.start()
    try:
        response = post(relay)
        assert response.status_code == 403
        assert response.json()["error"]["type"] == (
            "TASK_CONTEXT_POLICY_MISMATCH"
            if direction == "qualification_grant"
            else "QUALIFICATION_CONTEXT_BINDING_MISMATCH"
        )
    finally:
        relay.close()
    assert journal.snapshot("target")["calls"] == []
    assert upstream == []
    assert journal.snapshot("unrelated") == before
    assert journal.authenticate_grant(
        "unrelated", capability=unrelated["capability"], binding=before["binding"]
    )["state"] == "active"


@pytest.mark.parametrize("factory, context_type", _BUSINESS_CONTEXTS)
def test_legacy_planning_grant_cannot_send_with_business_context(
    tmp_path, accounting, factory, context_type
):
    source = digest(accounting.source())
    binding = _planning(source)
    binding = {
        key: value for key, value in binding.items() if key not in {"context", "tool_policy"}
    }
    binding["schema_version"] = "karajan.go-planning-grant.v1"
    journal = GoCallJournal(tmp_path / "journal.sqlite", clock=lambda: 1000.0)
    grant = journal.create_grant(binding, grant_id="legacy")
    unrelated_binding = factory(source)
    unrelated = journal.create_grant(unrelated_binding, grant_id="unrelated")
    before = journal.snapshot("unrelated")
    upstream = []
    relay = _business_relay(
        journal,
        grant,
        binding,
        _business_context(accounting, factory(source), context_type),
        _allowed,
        upstream,
        lambda _: _metered_answer(),
    )
    relay.start()
    try:
        response = post(relay)
        assert response.status_code == 403
        assert response.json()["error"]["type"] == "GO_JOURNAL_INPUT_INVALID"
    finally:
        relay.close()
    assert journal.snapshot("legacy")["calls"] == []
    assert upstream == []
    assert journal.snapshot("unrelated") == before
    assert journal.authenticate_grant(
        "unrelated", capability=unrelated["capability"], binding=unrelated_binding
    )["state"] == "active"


@pytest.mark.parametrize("factory, context_type", _BUSINESS_CONTEXTS)
@pytest.mark.parametrize(
    "fault",
    [
        "binding_digest",
        "input_digest",
        "third_digest",
        "context_limit",
        "false_matching_source",
    ],
)
def test_business_authority_seals_limits_and_actual_source_reject_before_send(
    tmp_path, accounting, factory, context_type, fault
):
    source = digest(accounting.source())
    binding = factory("0" * 64 if fault == "false_matching_source" else source)
    journal = GoCallJournal(tmp_path / "journal.sqlite", clock=lambda: 1000.0)
    grant = journal.create_grant(binding, grant_id="target")
    unrelated_binding = factory(source)
    unrelated = journal.create_grant(unrelated_binding, grant_id="unrelated")
    before = journal.snapshot("unrelated")
    changed = dict(binding)
    if fault == "binding_digest":
        key = (
            "planning_binding_sha256"
            if context_type is GoPlanningRelayContext
            else "review_binding_sha256"
        )
        changed[key] = "1" * 64
    elif fault == "input_digest":
        key = (
            "admission_sha256"
            if context_type is GoPlanningRelayContext
            else "reviewer_input_sha256"
        )
        changed[key] = "1" * 64
    elif fault == "third_digest":
        key = (
            "input_sha256"
            if context_type is GoPlanningRelayContext
            else "candidate_checks_sha256"
        )
        changed[key] = "1" * 64
    elif fault == "context_limit":
        changed["context"] = {**binding["context"], "approved_input_tokens": 3999}
    context = _business_context(accounting, changed, context_type)
    upstream = []
    relay = _business_relay(
        journal, grant, binding, context, _allowed, upstream, lambda _: _metered_answer()
    )
    relay.start()
    try:
        response = post(relay)
        assert response.status_code == (422 if fault == "false_matching_source" else 403)
        assert response.json()["error"]["type"] == (
            "CONTEXT_SOURCE_CHANGED"
            if fault == "false_matching_source"
            else "TASK_CONTEXT_POLICY_MISMATCH"
        )
    finally:
        relay.close()
    assert journal.snapshot("target")["calls"] == []
    assert upstream == []
    assert journal.snapshot("unrelated") == before
    assert journal.authenticate_grant(
        "unrelated", capability=unrelated["capability"], binding=unrelated_binding
    )["state"] == "active"


@pytest.mark.parametrize("factory", [_planning, _reviewer])
@pytest.mark.parametrize("restriction", ["cap", "expiry", "revoke"])
def test_business_grant_limits_preserve_history_and_never_reauthorize_replay(
    tmp_path, accounting, factory, restriction
):
    binding = factory(digest(accounting.source()))
    if restriction == "cap":
        binding["max_requests"] = 1
    journal = GoCallJournal(tmp_path / "journal.sqlite", clock=lambda: 1000.0)
    grant = journal.create_grant(binding, grant_id="grant")
    measured = _measurement(accounting, binding)
    first = journal.begin_call(
        "grant", "first", capability=grant["capability"], binding=binding, request_context=measured
    )
    assert first["send_allowed"] is True
    assert (
        journal.begin_call(
            "grant",
            "first",
            capability=grant["capability"],
            binding=binding,
            request_context=measured,
        )["send_allowed"]
        is False
    )
    if restriction == "revoke":
        journal.revoke_grant("grant")
    elif restriction == "expiry":
        journal = GoCallJournal(journal.path, clock=lambda: 3000.0)
    with pytest.raises(
        GoJournalError,
        match={
            "cap": "REQUEST_LIMIT_REACHED",
            "expiry": "GRANT_EXPIRED",
            "revoke": "GRANT_REVOKED",
        }[restriction],
    ):
        journal.begin_call(
            "grant",
            "second",
            capability=grant["capability"],
            binding=binding,
            request_context=measured,
        )
    snapshot = journal.snapshot("grant")
    assert snapshot["request_count"] == 1
    assert snapshot["calls"][0]["state"] == "send_unknown"


@pytest.mark.parametrize("factory", [_planning, _reviewer])
def test_business_grant_concurrent_distinct_calls_stop_at_original_cap(
    tmp_path, accounting, factory
):
    binding = factory(digest(accounting.source()))
    journal = GoCallJournal(tmp_path / "journal.sqlite", clock=lambda: 1000.0)
    grant = journal.create_grant(binding, grant_id="grant")
    measured = _measurement(accounting, binding)

    def begin(number: int) -> bool:
        try:
            return journal.begin_call(
                "grant",
                f"call-{number}",
                capability=grant["capability"],
                binding=binding,
                request_context=measured,
            )["send_allowed"]
        except GoJournalError as error:
            assert str(error) == "REQUEST_LIMIT_REACHED"
            return False

    with ThreadPoolExecutor(max_workers=3) as workers:
        results = list(workers.map(begin, range(3)))
    assert results.count(True) == 2
    assert journal.snapshot("grant")["request_count"] == 2


def _tool_event(name: str, usage: dict[str, int] | None = None) -> bytes:
    return stream(
        event(
            choices=[
                {
                    "index": 0,
                    "delta": {"tool_calls": [{"index": 0, "function": {"name": name}}]},
                    "finish_reason": "tool_calls",
                }
            ],
            usage=usage or {"prompt_tokens": 20, "completion_tokens": 2},
        )
    )


def _tool_request(name: str, variant: str) -> dict[str, object]:
    request = payload()
    if variant == "declaration":
        request["tools"] = [{"type": "function", "function": {"name": name, "parameters": {}}}]
    else:
        request["messages"] = [
            {"role": "user", "content": "Inspect this candidate."},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": name, "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "source"},
            {"role": "user", "content": "Continue."},
        ]
    return request


@pytest.mark.parametrize("variant", ["declaration", "assistant_tool_calls_and_history"])
@pytest.mark.parametrize("name", ["read", "edit", "shell", "mcp", "unknown"])
def test_business_planning_rejects_every_request_tool_variant_before_send(
    tmp_path, accounting, variant, name
):
    binding = _planning(digest(accounting.source()))
    journal = GoCallJournal(tmp_path / "journal.sqlite", clock=lambda: 1000.0)
    grant = journal.create_grant(binding, grant_id="grant")
    upstream = []
    relay = _business_relay(
        journal,
        grant,
        binding,
        _business_context(accounting, binding, GoPlanningRelayContext),
        _allowed,
        upstream,
        lambda _: _metered_answer(),
    )
    relay.start()
    try:
        assert post(relay, _tool_request(name, variant)).status_code == 403
    finally:
        relay.close()
    assert journal.snapshot("grant")["calls"] == []
    assert upstream == []


@pytest.mark.parametrize("name", ["read", "edit", "shell", "mcp", "unknown"])
def test_business_planning_rejects_every_returned_tool_variant_after_accounted_send(
    tmp_path, accounting, name
):
    binding = _planning(digest(accounting.source()))
    journal = GoCallJournal(tmp_path / "journal.sqlite", clock=lambda: 1000.0)
    grant = journal.create_grant(binding, grant_id="grant")
    upstream = []
    relay = _business_relay(
        journal,
        grant,
        binding,
        _business_context(accounting, binding, GoPlanningRelayContext),
        _allowed,
        upstream,
        lambda _: answer(_tool_event(name)),
    )
    relay.start()
    try:
        assert post(relay).status_code == 502
        assert post(relay).status_code == 503
    finally:
        relay.close()
    saved = journal.snapshot("grant")
    assert len(upstream) == saved["request_count"] == 1
    assert saved["calls"][0]["state"] == "response_received"
    assert saved["calls"][0]["outcome"]["protocol_passed"] is False


@pytest.mark.parametrize("variant", ["declaration", "assistant_tool_calls_and_history", "returned"])
def test_business_reviewer_allows_read_through_relay_journal_and_stream_identity(
    tmp_path, accounting, variant
):
    binding = _reviewer(digest(accounting.source()))
    journal = GoCallJournal(tmp_path / "journal.sqlite", clock=lambda: 1000.0)
    grant = journal.create_grant(binding, grant_id="grant")
    upstream = []
    relay = _business_relay(
        journal,
        grant,
        binding,
        _business_context(accounting, binding, GoReviewerRelayContext),
        _allowed,
        upstream,
        lambda _: answer(
            _tool_event("read") if variant == "returned" else _metered_answer().content
        ),
    )
    relay.start()
    try:
        request = _tool_request("read", variant) if variant != "returned" else payload()
        assert post(relay, request).status_code == 200, relay.receipts
    finally:
        relay.close()
    saved = GoCallJournal(journal.path, clock=lambda: 1001.0).snapshot("grant")
    assert len(upstream) == saved["request_count"] == 1
    assert saved["calls"][0]["state"] == "response_received"
    assert saved["calls"][0]["outcome"]["protocol_passed"] is True
    assert relay.receipts[0]["tool_names"] == (["read"] if variant == "returned" else [])


@pytest.mark.parametrize("variant", ["declaration", "assistant_tool_calls_and_history", "returned"])
@pytest.mark.parametrize("name", ["edit", "shell", "mcp", "unknown"])
def test_business_reviewer_rejects_nonread_tool_identities_with_correct_send_boundary(
    tmp_path, accounting, variant, name
):
    binding = _reviewer(digest(accounting.source()))
    journal = GoCallJournal(tmp_path / "journal.sqlite", clock=lambda: 1000.0)
    grant = journal.create_grant(binding, grant_id="grant")
    upstream = []
    relay = _business_relay(
        journal,
        grant,
        binding,
        _business_context(accounting, binding, GoReviewerRelayContext),
        _allowed,
        upstream,
        lambda _: answer(_tool_event(name) if variant == "returned" else _metered_answer().content),
    )
    relay.start()
    try:
        request = _tool_request(name, variant) if variant != "returned" else payload()
        assert post(relay, request).status_code == (502 if variant == "returned" else 403)
        if variant == "returned":
            assert post(relay).status_code == 503
    finally:
        relay.close()
    saved = journal.snapshot("grant")
    if variant == "returned":
        assert len(upstream) == saved["request_count"] == 1
        assert saved["calls"][0]["state"] == "response_received"
        assert saved["calls"][0]["outcome"]["protocol_passed"] is False
    else:
        assert saved["calls"] == []
        assert upstream == []


@pytest.mark.parametrize("factory, context_type", _BUSINESS_CONTEXTS)
def test_forbidden_returned_tool_persists_valid_observed_usage_despite_protocol_failure(
    tmp_path, accounting, factory, context_type
):
    binding = factory(digest(accounting.source()))
    journal = GoCallJournal(tmp_path / "journal.sqlite", clock=lambda: 1000.0)
    grant = journal.create_grant(binding, grant_id="grant")
    forbidden = "read" if context_type is GoPlanningRelayContext else "edit"
    observed = {"prompt_tokens": 5000, "completion_tokens": 4097}
    partial = {"prompt_tokens": 4999, "completion_tokens": 4096}
    upstream = []
    relay = _business_relay(
        journal,
        grant,
        binding,
        _business_context(accounting, binding, context_type),
        _allowed,
        upstream,
        lambda _: answer(
            stream(
                event(
                    choices=[
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {"index": 0, "function": {"name": forbidden}}
                                ]
                            },
                            "finish_reason": None,
                        }
                    ],
                    usage=partial,
                ),
                event(
                    choices=[
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {"index": 0, "function": {"name": None}}
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    usage=observed,
                ),
            )
        ),
    )
    relay.start()
    try:
        assert post(relay).status_code == 502
    finally:
        relay.close()
    reopened = GoCallJournal(journal.path, clock=lambda: 1001.0).snapshot("grant")
    call = reopened["calls"][0]
    assert len(upstream) == reopened["request_count"] == 1
    assert reopened["state"] == "revoked"
    assert call["outcome"]["usage"] == observed
    assert call["outcome"]["protocol_passed"] is False
    assert call["outcome"]["reason_codes"] == ["UNAPPROVED_TOOL"]
    assert relay.receipts[0]["usage"] == observed
    assert relay.receipts[0]["tool_names"] == []
    assert forbidden not in json.dumps(relay.receipts)


@pytest.mark.parametrize(
    "factory, context_type",
    [(_planning, GoPlanningRelayContext), (_reviewer, GoReviewerRelayContext)],
)
@pytest.mark.parametrize(
    "response", ["tool", "malformed", "missing_usage", "over_input", "over_output"]
)
def test_business_response_validation_failures_are_accounted_and_withdraw_future_send(
    tmp_path, accounting, factory, context_type, response
):
    source = digest(accounting.source())
    binding = factory(source)
    journal = GoCallJournal(tmp_path / "journal.sqlite", clock=lambda: 1000.0)
    grant = journal.create_grant(binding, grant_id="grant")
    context = _business_context(accounting, binding, context_type)
    upstream = []

    def receive(request):
        upstream.append(request)
        if response == "tool":
            return answer(_tool_event("read" if context_type is GoPlanningRelayContext else "edit"))
        if response == "malformed":
            return answer(b"data: not-json\n\n")
        usage = {"prompt_tokens": 20, "completion_tokens": 2}
        if response == "missing_usage":
            usage = None
        elif response == "over_input":
            usage["prompt_tokens"] = 5000
        elif response == "over_output":
            usage["completion_tokens"] = 4097
        return answer(stream(event(usage=usage)))

    relay = GoRelay(
        SECRET,
        CANARY,
        context=context,
        authorization=GoRelayAuthorization(journal, "grant", binding, grant["capability"]),
        send_guard=lambda: _allowed(),
        client_factory=lambda: httpx.Client(
            transport=httpx.MockTransport(receive), trust_env=False
        ),
    )
    relay.start()
    try:
        assert post(relay).status_code == 502
        assert post(relay).status_code == 503
    finally:
        relay.close()
    saved = journal.snapshot("grant")
    assert len(upstream) == saved["request_count"] == 1
    assert saved["state"] == "revoked"
    assert saved["calls"][0]["state"] == "response_received"
    assert saved["calls"][0]["outcome"]["protocol_passed"] is False


@pytest.mark.parametrize(
    "factory, context_type",
    [(_planning, GoPlanningRelayContext), (_reviewer, GoReviewerRelayContext)],
)
@pytest.mark.parametrize(
    "withdrawal", ["cancel", "disable", "qualification", "source", "generation", "window", "fence"]
)
def test_business_guard_rechecks_current_withdrawal_before_each_send(
    tmp_path, accounting, factory, context_type, withdrawal
):
    source = digest(accounting.source())
    binding = factory(source)
    journal = GoCallJournal(tmp_path / "journal.sqlite", clock=lambda: 1000.0)
    grant = journal.create_grant(binding, grant_id="grant")
    context = _business_context(accounting, binding, context_type)
    current = {"allowed": True, "enters": 0}
    upstream = []

    @contextmanager
    def guard():
        current["enters"] += 1
        if not current["allowed"]:
            raise RuntimeError(withdrawal)
        yield

    relay = GoRelay(
        SECRET,
        CANARY,
        context=context,
        authorization=GoRelayAuthorization(journal, "grant", binding, grant["capability"]),
        send_guard=guard,
        client_factory=lambda: httpx.Client(
            transport=httpx.MockTransport(
                lambda request: (
                    upstream.append(request)
                    or answer(stream(event(usage={"prompt_tokens": 20, "completion_tokens": 2})))
                )
            ),
            trust_env=False,
        ),
    )
    relay.start()
    try:
        assert post(relay).status_code == 200
        current["allowed"] = False
        assert post(relay).status_code == 403
    finally:
        relay.close()
    assert current["enters"] == 2
    assert len(upstream) == journal.snapshot("grant")["request_count"] == 1


@pytest.mark.parametrize("factory, context_type", _BUSINESS_CONTEXTS)
@pytest.mark.parametrize("phase", ["enter", "exit"])
def test_business_guard_lifecycle_faults_do_not_refund_or_repeat_send(
    tmp_path, accounting, factory, context_type, phase
):
    source = digest(accounting.source())
    binding = factory(source)
    journal = GoCallJournal(tmp_path / "journal.sqlite", clock=lambda: 1000.0)
    grant = journal.create_grant(binding, grant_id="grant")
    context = _business_context(accounting, binding, context_type)
    upstream = []
    guard_enters = 0

    @contextmanager
    def exiting():
        yield
        raise RuntimeError("private guard failure")

    def guard():
        nonlocal guard_enters
        guard_enters += 1
        if phase == "enter":
            raise RuntimeError("private guard failure")
        return exiting()

    relay = _business_relay(
        journal, grant, binding, context, guard, upstream, lambda _: _metered_answer()
    )
    relay.start()
    try:
        assert post(relay).status_code == 403
        assert post(relay).status_code == (503 if phase == "exit" else 403)
    finally:
        relay.close()
    saved = journal.snapshot("grant")
    assert len(upstream) == saved["request_count"] == (1 if phase == "exit" else 0)
    assert guard_enters == (1 if phase == "exit" else 2)
    if phase == "exit":
        assert saved["state"] == "revoked"
        assert saved["calls"][0]["state"] == "send_unknown"
    else:
        assert saved["state"] == "active"
        assert saved["calls"] == []


@pytest.mark.parametrize("factory, context_type", _BUSINESS_CONTEXTS)
def test_business_lost_begin_reply_keeps_one_unknown_slot_without_replay_or_refund(
    tmp_path, accounting, factory, context_type, monkeypatch
):
    """A committed begin whose return is lost is not a permission to resend.

    The second HTTP request is a newly requested relay call, not recovery of
    the failed call.  The relay has closed/revoked after the uncertain begin,
    so it receives 503 before it can allocate the grant's unused second slot.
    """
    source = digest(accounting.source())
    binding = factory(source)
    journal = GoCallJournal(tmp_path / "journal.sqlite", clock=lambda: 1000.0)
    grant = journal.create_grant(binding, grant_id="grant")
    context = _business_context(accounting, binding, context_type)
    upstream = []
    original = journal.begin_call

    def commit_then_lose(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError("synthetic lost begin reply")

    monkeypatch.setattr(journal, "begin_call", commit_then_lose)
    relay = _business_relay(
        journal, grant, binding, context, _allowed, upstream, lambda _: _metered_answer()
    )
    relay.start()
    try:
        assert post(relay).status_code == 502
        # This is a distinct relay request.  It must not replay the unknown call
        # or spend the still-unused second cap slot after the relay closes.
        assert post(relay).status_code == 503
    finally:
        relay.close()

    reopened = GoCallJournal(journal.path, clock=lambda: 1001.0)
    saved = reopened.snapshot("grant")
    assert len(upstream) == 0
    assert saved["state"] == "revoked"
    assert saved["request_count"] == 1
    call = saved["calls"][0]
    assert call["state"] == "send_unknown"
    assert call["outcome"]["state"] == "send_unknown"
    assert call["outcome"]["protocol_passed"] is False
    assert (
        reopened.begin_call(
            "grant",
            call["call_id"],
            capability=grant["capability"],
            binding=binding,
            request_context=_measurement(accounting, binding),
        )["send_allowed"]
        is False
    )
    assert reopened.snapshot("grant")["request_count"] == 1


@pytest.mark.parametrize("factory, context_type", _BUSINESS_CONTEXTS)
def test_business_lost_completion_ack_preserves_committed_response_received_without_retry_or_refund(
    tmp_path, accounting, factory, context_type, monkeypatch
):
    source = digest(accounting.source())
    binding = factory(source)
    journal = GoCallJournal(tmp_path / "journal.sqlite", clock=lambda: 1000.0)
    grant = journal.create_grant(binding, grant_id="grant")
    context = _business_context(accounting, binding, context_type)
    upstream = []
    original = journal.complete_call

    def commit_then_lose(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError("synthetic lost completion reply")

    monkeypatch.setattr(journal, "complete_call", commit_then_lose)
    relay = _business_relay(
        journal, grant, binding, context, _allowed, upstream, lambda _: _metered_answer()
    )
    relay.start()
    try:
        assert post(relay).status_code == 200
        assert post(relay).status_code == 503
    finally:
        relay.close()
    saved = GoCallJournal(journal.path, clock=lambda: 1001.0).snapshot("grant")
    assert len(upstream) == saved["request_count"] == 1
    assert saved["state"] == "revoked"
    assert saved["calls"][0]["state"] == "response_received"
