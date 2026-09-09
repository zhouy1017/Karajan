"""Commander native producer C/P boundary: actual Linux child, fixture HTTP only."""

import json
import sys
import time
from contextlib import nullcontext

import httpx
import pytest
from karajan.adapters.opencode.go_context import GoRequestAccounting
from karajan.adapters.opencode.go_journal import GoCallJournal
from karajan.adapters.opencode.go_relay import GoRelayAuthorization
from karajan.isolation import go_commander_probe as commander_probe
from karajan.isolation.go_commander_probe import (
    _context,
    _native_log_evidence,
    commander_runtime_source,
    observe_go_commander_probe,
)
from karajan.routing.compiler import digest
from test_opencode_go_composition import SECRET, runtime_artifact


@pytest.fixture(scope="module")
def accounting():
    from os import environ
    from pathlib import Path

    return GoRequestAccounting(Path(environ["KARAJAN_GO_TOKENIZER_DIRECTORY"]))


def _response(plan):
    events = [
        {
            "id": "commander-local",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "glm-5.3-flash",
            "choices": [{"index": 0, "delta": change, "finish_reason": finish}],
        }
        for change, finish in (
            ({"role": "assistant"}, None),
            ({"content": json.dumps(plan)}, None),
            ({}, "stop"),
        )
    ]
    events.append({"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 20}})
    body = "".join("data: " + json.dumps(event) + "\n\n" for event in events)
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=(body + "data: [DONE]\n\n").encode(),
    )


def test_native_log_evidence_requires_confirmed_stop_and_stays_bounded(tmp_path) -> None:
    log = tmp_path / "native" / "namespace.log"
    log.parent.mkdir()
    log.write_bytes(b"native output")

    assert _native_log_evidence(tmp_path, {"local_stop": "confirmed"}) == {
        "bytes": len(b"native output"),
        "sha256": "331bf5c0f2b33f05b6fbf0d49ac6ef7ed564359c74c6efb18759fe9b8e06b3a3",
    }
    with pytest.raises(ValueError, match="^NATIVE_LOG_EVIDENCE_UNAVAILABLE$"):
        _native_log_evidence(tmp_path, {"local_stop": "unknown"})
    log.write_bytes(b"x" * (1_048_576 + 1))
    with pytest.raises(ValueError, match="^COMMANDER_NATIVE_LOG_LIMIT_EXCEEDED$"):
        _native_log_evidence(tmp_path, {"local_stop": "confirmed"})


@pytest.mark.skipif(sys.platform != "linux", reason="Fixed Linux native required")
@pytest.mark.parametrize("scenario", ["legal_plan", "denied_tool"])
def test_commander_native_probe_uses_original_journal_and_empty_tools(
    tmp_path, accounting, scenario
):
    runtime = runtime_artifact()
    source = commander_runtime_source(runtime, accounting)
    spec = source["probe_spec"]
    journal = GoCallJournal(tmp_path / "calls.sqlite")
    binding = {
        "schema_version": "karajan.go-commander-qualification-grant.v1",
        "qualification_id": "commander-qualification",
        "attempt_id": "commander-attempt:" + scenario,
        "fence": 1,
        "profile_digest": "a" * 64,
        "runtime_digest": digest(source),
        "channel": "opencode-go",
        "model": "glm-5.3-flash",
        "auth_generation": "fixture-generation",
        "expires_at": time.time() + 300,
        "max_requests": 6,
        "probe_spec_digest": source["probe_spec_digest"],
        "scenario": scenario,
        "context": _context(accounting, spec),
    }
    grant = journal.create_grant(binding, grant_id="commander-grant:" + scenario)
    authorization = GoRelayAuthorization(journal, grant["grant_id"], binding, grant["capability"])
    received = []

    def receive(request):
        payload = json.loads(request.content)
        received.append(payload)
        assert payload.get("tools", []) == []
        assert journal.snapshot(authorization.grant_id)["calls"][-1]["state"] == "send_unknown"
        return _response(spec["cases"][scenario]["expected_plan"])

    result = observe_go_commander_probe(
        runtime,
        tmp_path / "observation",
        SECRET,
        authorization,
        scenario=scenario,
        accounting=accounting,
        current_guard=nullcontext,
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(receive)),
    )
    assert result["status"] == "passed", result["reason_codes"]
    assert result["observation_origin"] == "http_fixture"
    assert result["native_final"]["finish"] == "stop"
    assert "text" not in result["native_final"]
    assert result["parsed_plan"] == spec["cases"][scenario]["expected_plan"]
    assert result["journal"]["state"] == "revoked"
    assert result["native_cleanup"]["local_stop"] == "confirmed"
    assert result["native_log"]["bytes"] >= 0
    assert len(result["native_log"]["sha256"]) == 64
    assert result["provider_remote_stop"] == "unknown"
    assert len(received) == len(result["journal"]["calls"])


@pytest.mark.skipif(sys.platform != "linux", reason="Fixed Linux native required")
@pytest.mark.parametrize(
    ("finish", "text_parts", "reason_code"),
    [
        ("length", ["FAKE_INCOMPLETE_SECRET"], "NATIVE_FINAL_INCOMPLETE"),
        ("stop", ["FAKE_PART_SECRET_ONE", "FAKE_PART_SECRET_TWO"], "NATIVE_FINAL_TEXT_AMBIGUOUS"),
    ],
)
def test_commander_native_probe_persists_rejected_final_shape_diagnostic(
    tmp_path, accounting, monkeypatch, finish, text_parts, reason_code
):
    runtime = runtime_artifact()
    source = commander_runtime_source(runtime, accounting)
    spec = source["probe_spec"]
    journal = GoCallJournal(tmp_path / "calls.sqlite")
    binding = {
        "schema_version": "karajan.go-commander-qualification-grant.v1",
        "qualification_id": "commander-qualification",
        "attempt_id": "commander-attempt:legal_plan",
        "fence": 1,
        "profile_digest": "a" * 64,
        "runtime_digest": digest(source),
        "channel": "opencode-go",
        "model": "glm-5.3-flash",
        "auth_generation": "fixture-generation",
        "expires_at": time.time() + 300,
        "max_requests": 6,
        "probe_spec_digest": source["probe_spec_digest"],
        "scenario": "legal_plan",
        "context": _context(accounting, spec),
    }
    grant = journal.create_grant(binding, grant_id="commander-grant:shape")
    authorization = GoRelayAuthorization(journal, grant["grant_id"], binding, grant["capability"])

    original_select_final = commander_probe.select_final

    def reject_shape(messages, session_id, prompt):
        assistants = [
            message for message in messages if message.get("info", {}).get("role") == "assistant"
        ]
        assistant = assistants[-1]
        assistant["info"]["finish"] = finish
        assistant["parts"] = [{"type": "text", "text": text} for text in text_parts]
        return original_select_final(messages, session_id, prompt)

    monkeypatch.setattr(commander_probe, "select_final", reject_shape)
    result = observe_go_commander_probe(
        runtime,
        tmp_path / "observation",
        SECRET,
        authorization,
        scenario="legal_plan",
        accounting=accounting,
        current_guard=nullcontext,
        client_factory=lambda: httpx.Client(
            transport=httpx.MockTransport(
                lambda request: _response(spec["cases"]["legal_plan"]["expected_plan"])
            )
        ),
    )

    assert result["status"] == "failed"
    assert result["reason_codes"] == [reason_code]
    diagnostic = result["planning_output_diagnostic"]
    assert diagnostic["category"] == "selection"
    assert diagnostic["finish"] == finish
    assert diagnostic["text_part_count"] == len(text_parts)
    assert "FAKE_" not in json.dumps(result, sort_keys=True)
