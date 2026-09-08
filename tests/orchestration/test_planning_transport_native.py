"""Linux C/P proof for the native planning output path."""

import json
import os
import sys
from pathlib import Path

import httpx
import pytest
from karajan.adapters.opencode.go_context import GoRequestAccounting
from karajan.adapters.opencode.go_journal import GoCallJournal
from karajan.orchestration.planning_input import PlanningModelInput
from karajan.orchestration.planning_transport import FixtureGoPlanningProducer

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux namespaces required")


def test_native_fixture_relay_journal_returns_only_final_assistant_content(tmp_path: Path) -> None:
    runtime = Path(os.environ["KARAJAN_GO_RUNTIME"])
    accounting = GoRequestAccounting(Path(os.environ["KARAJAN_GO_TOKENIZER_DIRECTORY"]))
    request = {
        "model": "glm-5.3-flash",
        "stream": True,
        "max_tokens": 4096,
        "messages": [
            {"role": "system", "content": "Return only JSON."},
            {"role": "user", "content": "Make one planning proposal."},
        ],
        "reasoning_effort": "max",
        "clear_thinking": False,
    }
    expected = '{"summary":"Fixture plan","authorization":{},"tasks":[]}'

    def upstream(received: httpx.Request) -> httpx.Response:
        payload = json.loads(received.content)
        assert payload["model"] == "glm-5.3-flash"
        body = {
            "model": "glm-5.3-flash",
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": expected, "tool_calls": None},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 20, "completion_tokens": 2, "total_tokens": 22},
        }
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=("data: " + json.dumps(body) + "\n\ndata: [DONE]\n\n").encode(),
        )

    model_input = PlanningModelInput.model_construct(
        request=request,
        request_bytes=json.dumps(request, separators=(",", ":")).encode(),
        accounting_source=accounting.source(),
        execution_policy={
            "max_context_tokens": 16384,
            "context_policy": {"reserved_output_tokens": 4096},
        },
        artifact_sha256="a" * 64,
    )
    binding = {
        "attempt_id": "planning-execution",
        "fence": 1,
        "profile_sha256": "b" * 64,
        "project_id": "project",
        "run_id": "run",
        "intent_id": "intent",
        "execution_id": "execution",
    }
    producer = FixtureGoPlanningProducer(
        GoCallJournal(tmp_path / "journal.sqlite"),
        accounting,
        upstream=upstream,
        runtime=runtime,
        work_root=tmp_path / "native-work",
    )
    output = producer.produce(model_input, binding=binding, admission={"state": "admitted"})

    assert output.decode() == expected
    journal = producer.journal.snapshot("planning-execution")
    assert journal["calls"][0]["outcome"]["protocol_passed"] is True
    assert journal["state"] == "revoked"
