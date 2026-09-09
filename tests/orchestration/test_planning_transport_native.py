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
from karajan.runs import RunError

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux namespaces required")


def _prepared_runtime() -> Path:
    configured = os.environ.get("KARAJAN_OPENCODE_LINUX_BINARY") or os.environ.get(
        "KARAJAN_GO_RUNTIME"
    )
    runtime = Path(configured) if configured else (
        Path(__file__).resolve().parents[2]
        / "runtimes/opencode/node_modules/opencode-linux-x64/bin/opencode"
    )
    if not runtime.is_file():
        if os.environ.get("KARAJAN_REQUIRE_OPENCODE_ISOLATION") == "1":
            pytest.fail("Prepared fixed Linux OpenCode artifact is required")
        pytest.skip("Prepared Linux OpenCode artifact is not available")
    return runtime


def _prepared_tokenizer() -> Path:
    configured = os.environ.get("KARAJAN_GO_TOKENIZER_DIRECTORY")
    tokenizer = Path(configured) if configured else Path(".cache/go-context-artifacts")
    tokenizer = tokenizer.resolve()
    if not tokenizer.is_dir():
        if os.environ.get("KARAJAN_REQUIRE_GO_TOKENIZER") == "1":
            pytest.fail("Prepared Go tokenizer artifacts are required")
        pytest.skip("Prepared Go tokenizer artifacts are not available")
    return tokenizer


def test_native_fixture_relay_journal_returns_only_final_assistant_content(tmp_path: Path) -> None:
    runtime = _prepared_runtime()
    accounting = GoRequestAccounting(_prepared_tokenizer())
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


def test_native_output_rejects_multiple_completed_assistant_finals() -> None:
    """Shared production completion logic cannot choose an arbitrary last final."""

    class Native:
        def start(self) -> dict[str, object]:
            return {"state": "running"}

        def request(self, method: str, route: str, body: object = None) -> object:
            del body
            if method == "POST" and route == "/session":
                return {"id": "one"}
            if method == "POST":
                return {}
            return [
                {
                    "info": {"role": "assistant", "time": {"completed": 1}, "finish": "stop"},
                    "parts": [{"type": "text", "text": "first"}],
                },
                {
                    "info": {"role": "assistant", "time": {"completed": 2}, "finish": "stop"},
                    "parts": [{"type": "text", "text": "second"}],
                },
            ]

    model_input = PlanningModelInput.model_construct(
        request={"messages": [{}, {"content": "prompt"}]},
        request_bytes=b"{}",
    )
    with pytest.raises(RunError, match="^PLANNING_NATIVE_OUTPUT_AMBIGUOUS$"):
        FixtureGoPlanningProducer._native_output(Native(), model_input, timeout_seconds=1)  # type: ignore[arg-type]
