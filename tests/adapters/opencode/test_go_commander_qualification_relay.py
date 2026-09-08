"""Commander qualification Relay boundary, using local HTTP and the real Journal.

The receiving server is a C fixture only: it proves local wire/Journaling
causality and never produces an official Commander source observation.
"""

import json
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import karajan.adapters.opencode.go_relay as relay_module
import pytest
from karajan.adapters.opencode.go_journal import GoCallJournal
from karajan.routing.compiler import digest
from test_go_context import accounting, artifacts
from test_go_journal import binding
from test_go_relay import CANARY, SECRET, event, payload, post, stream

__all__ = ["accounting", "artifacts"]

_LIMITS = {
    "approved_input_tokens": 12_288,
    "reserved_output_tokens": 4_096,
    "operating_context_tokens": 16_384,
    "fixed_margin": 2_048,
    "ratio_margin_basis_points": 2_000,
}


def commander_binding(accounting, *, scenario="legal_plan", expires_at=2_000.0, max_requests=6):
    return binding(
        schema_version="karajan.go-commander-qualification-grant.v1",
        probe_spec_digest="c" * 64,
        scenario=scenario,
        expires_at=expires_at,
        max_requests=max_requests,
        context={"source_sha256": digest(accounting.source()), **_LIMITS},
    )


def commander_context(accounting, bound):
    return relay_module.GoCommanderQualificationContext(
        accounting=accounting,
        probe_spec_digest=bound["probe_spec_digest"],
        scenario=bound["scenario"],
        **bound["context"],
    )


def inline_payload():
    return {**payload(), "tools": []}


@contextmanager
def local_receiver(journal, grant_id, *, response_body=None):
    received = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers["Content-Length"])
            received.append((dict(self.headers), self.rfile.read(length)))
            saved = GoCallJournal(journal.path, clock=lambda: 1000.0).snapshot(grant_id)
            assert saved["request_count"] == len(received)
            assert saved["calls"][-1]["state"] == "send_unknown"
            body = response_body or stream(
                event(usage={"prompt_tokens": 20, "completion_tokens": 2, "total_tokens": 22})
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    import threading

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1/chat/completions", received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@contextmanager
def controlled_commander(
    tmp_path, accounting, monkeypatch, *, guard, journal_type=GoCallJournal, response_body=None
):
    bound = commander_binding(accounting)
    journal = journal_type(tmp_path / "calls.sqlite", clock=lambda: 1000.0)
    grant = journal.create_grant(bound, grant_id="commander")
    with local_receiver(journal, "commander", response_body=response_body) as (upstream, received):
        monkeypatch.setattr(relay_module, "_UPSTREAM", upstream)
        relay = relay_module.GoRelay(
            SECRET,
            CANARY,
            context=commander_context(accounting, bound),
            send_guard=guard,
            authorization=relay_module.GoRelayAuthorization(
                journal, "commander", bound, grant["capability"]
            ),
        )
        relay.start()
        try:
            yield relay, journal, bound, received
        finally:
            assert relay.close()["status"] == "closed"


@contextmanager
def allowed():
    yield


def throwing_guard():
    raise RuntimeError("private")
    yield


def test_legal_inline_no_tools_send_is_measured_and_journaled_before_actual_local_http(
    tmp_path, accounting, monkeypatch
):
    with controlled_commander(tmp_path, accounting, monkeypatch, guard=lambda: allowed()) as (
        relay,
        journal,
        bound,
        received,
    ):
        body = inline_payload()
        assert post(relay, body).status_code == 200
        assert relay.close()["status"] == "closed"
        saved = journal.snapshot("commander")
        assert len(received) == saved["request_count"] == 1
        assert json.loads(received[0][1]) == body
        call = saved["calls"][0]
        assert call["outcome"]["protocol_passed"] is True
        assert call["request_context"] == relay.receipts[0]["request_context"]
        assert call["request_context"]["source_sha256"] == bound["context"]["source_sha256"]
        assert call["request_context"]["requested_output_tokens"] == body["max_tokens"]


@pytest.mark.parametrize("change", ["missing", "kind", "source", "spec", "scenario", "limits"])
def test_wrong_or_missing_commander_context_has_zero_send_and_zero_journal_call(
    tmp_path, accounting, monkeypatch, change
):
    def guard():
        return allowed()
    with controlled_commander(tmp_path, accounting, monkeypatch, guard=guard) as (
        relay,
        journal,
        bound,
        received,
    ):
        if change == "missing":
            relay._context = None
        elif change == "kind":
            relay._context = relay_module.GoQualificationContext(
                accounting=accounting,
                probe_spec_digest=bound["probe_spec_digest"],
                scenario="edit",
                **bound["context"],
            )
        elif change == "source":
            relay._context = replace(relay._context, source_sha256="d" * 64)
        elif change == "spec":
            relay._context = replace(relay._context, probe_spec_digest="d" * 64)
        elif change == "scenario":
            relay._context = replace(relay._context, scenario="denied_tool")
        else:
            object.__setattr__(relay._context, "approved_input_tokens", 12_287)
        response = post(relay, inline_payload())
        assert response.status_code == 403
        assert received == []
        assert journal.snapshot("commander")["request_count"] == 0


def test_throwing_current_guard_has_zero_next_effect(tmp_path, accounting, monkeypatch):
    with controlled_commander(tmp_path, accounting, monkeypatch, guard=throwing_guard) as (
        relay,
        journal,
        _,
        received,
    ):
        assert post(relay, inline_payload()).status_code == 403
        assert received == []
        assert journal.snapshot("commander")["request_count"] == 0


@pytest.mark.parametrize(
    "fault", ["missing-context", "wrong-source", "wrong-spec", "wrong-scenario", "missing-guard"]
)
def test_constructor_rejects_invalid_commander_assembly_before_any_journal_or_http_effect(
    tmp_path, accounting, fault
):
    bound = commander_binding(accounting)
    journal = GoCallJournal(tmp_path / "calls.sqlite", clock=lambda: 1000.0)
    grant = journal.create_grant(bound, grant_id="commander")
    context = commander_context(accounting, bound)

    def guard():
        return allowed()

    if fault == "missing-context":
        context = None
    elif fault == "wrong-source":
        context = replace(context, source_sha256="d" * 64)
    elif fault == "wrong-spec":
        context = replace(context, probe_spec_digest="d" * 64)
    elif fault == "wrong-scenario":
        context = replace(context, scenario="denied_tool")
    else:
        guard = None
    with pytest.raises(ValueError, match="^COMMANDER_QUALIFICATION_RELAY_INVALID$"):
        relay_module.GoRelay(
            SECRET,
            CANARY,
            context=context,
            send_guard=guard,
            authorization=relay_module.GoRelayAuthorization(
                journal, "commander", bound, grant["capability"]
            ),
        )
    assert journal.snapshot("commander")["request_count"] == 0


def test_guard_revocation_after_one_send_blocks_the_next_effect(tmp_path, accounting, monkeypatch):
    current = True

    @contextmanager
    def guard():
        if not current:
            raise RuntimeError("private revocation")
        yield

    with controlled_commander(tmp_path, accounting, monkeypatch, guard=guard) as (
        relay,
        journal,
        _,
        received,
    ):
        assert post(relay, inline_payload()).status_code == 200
        current = False
        assert post(relay, inline_payload()).status_code == 403
        assert len(received) == journal.snapshot("commander")["request_count"] == 1


@pytest.mark.parametrize("tool_request", ["tools", "history"])
def test_tools_permissions_mcp_or_shell_cannot_cross_commander_probe_boundary(
    tmp_path, accounting, monkeypatch, tool_request
):
    with controlled_commander(tmp_path, accounting, monkeypatch, guard=lambda: allowed()) as (
        relay,
        journal,
        _,
        received,
    ):
        body = inline_payload()
        if tool_request == "tools":
            body["tools"] = [{"type": "function", "function": {"name": "mcp__shell"}}]
        else:
            body["messages"].append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "x",
                            "type": "function",
                            "function": {"name": "shell", "arguments": "{}"},
                        }
                    ],
                }
            )
        assert post(relay, body).status_code == 403
        assert received == []
        assert journal.snapshot("commander")["request_count"] == 0


@pytest.mark.parametrize("alter", ["output", "context"])
def test_fixed_output_and_context_bounds_reject_before_send(
    tmp_path, accounting, monkeypatch, alter
):
    with controlled_commander(tmp_path, accounting, monkeypatch, guard=lambda: allowed()) as (
        relay,
        journal,
        _,
        received,
    ):
        body = inline_payload()
        if alter == "output":
            body["max_tokens"] = 4_097
        else:
            body["messages"][0]["content"] = "x" * 100_000
        assert post(relay, body).status_code == 422
        assert received == []
        assert journal.snapshot("commander")["request_count"] == 0


def test_commander_scene_cannot_consume_more_than_its_six_sealed_requests(
    tmp_path, accounting, monkeypatch
):
    with controlled_commander(tmp_path, accounting, monkeypatch, guard=lambda: allowed()) as (
        relay,
        journal,
        _,
        received,
    ):
        for _ in range(6):
            assert post(relay, inline_payload()).status_code == 200
        assert post(relay, inline_payload()).status_code == 429
        assert relay.close()["status"] == "closed"
        assert len(received) == journal.snapshot("commander")["request_count"] == 6


def test_expired_grant_has_zero_upstream_and_no_commander_call(tmp_path, accounting, monkeypatch):
    bound = commander_binding(accounting, expires_at=1001.0)
    journal = GoCallJournal(tmp_path / "calls.sqlite", clock=lambda: 1000.0)
    grant = journal.create_grant(bound, grant_id="commander")
    journal = GoCallJournal(journal.path, clock=lambda: 1001.0)
    relay = relay_module.GoRelay(
        SECRET,
        CANARY,
        context=commander_context(accounting, bound),
        send_guard=lambda: allowed(),
        authorization=relay_module.GoRelayAuthorization(
            journal, "commander", bound, grant["capability"]
        ),
    )
    relay.start()
    try:
        assert post(relay, inline_payload()).status_code == 403
    finally:
        assert relay.close()["status"] == "closed"
    assert journal.snapshot("commander")["request_count"] == 0


def test_lost_committed_begin_or_complete_reply_never_duplicates_a_commander_send(
    tmp_path, accounting, monkeypatch
):
    class LostComplete(GoCallJournal):
        def complete_call(self, *args, **kwargs):
            super().complete_call(*args, **kwargs)
            raise OSError("lost complete reply")

    with controlled_commander(
        tmp_path, accounting, monkeypatch, guard=lambda: allowed(), journal_type=LostComplete
    ) as (relay, journal, _, received):
        assert post(relay, inline_payload()).status_code == 200
        assert post(relay, inline_payload()).status_code == 503
        saved = GoCallJournal(journal.path, clock=lambda: 1001.0).snapshot("commander")
        assert len(received) == saved["request_count"] == 1
        assert saved["calls"][0]["outcome"]["protocol_passed"] is True


def test_malformed_stream_preserves_observed_usage_without_refunding_a_commander_send(
    tmp_path, accounting, monkeypatch
):
    malformed = stream(
        event(usage={"prompt_tokens": 20, "completion_tokens": 2, "total_tokens": 22}),
        done=False,
    )
    with controlled_commander(
        tmp_path,
        accounting,
        monkeypatch,
        guard=lambda: allowed(),
        response_body=malformed,
    ) as (relay, journal, _, received):
        assert post(relay, inline_payload()).status_code == 502
        assert relay.close()["status"] == "closed"
        saved = journal.snapshot("commander")
        assert len(received) == saved["request_count"] == 1
        assert saved["calls"][0]["outcome"]["usage"]["prompt_tokens"] == 20
        assert saved["calls"][0]["outcome"]["protocol_passed"] is False
