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
from karajan.isolation.go_commander_probe import (
    _context,
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
    assert result["parsed_plan"] == spec["cases"][scenario]["expected_plan"]
    assert result["journal"]["state"] == "revoked"
    assert result["native_cleanup"]["local_stop"] == "confirmed"
    assert result["provider_remote_stop"] == "unknown"
    assert len(received) == len(result["journal"]["calls"])
