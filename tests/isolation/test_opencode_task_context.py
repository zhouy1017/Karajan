"""Actual native OpenCode wire requests through Task accounting; local upstream only."""

import hashlib
import json
import os
import sys
import time
from pathlib import Path

import httpx
import pytest
from karajan.adapters.opencode.go_context import GoRequestAccounting
from karajan.adapters.opencode.go_journal import GoCallJournal
from karajan.adapters.opencode.go_relay import GoRelay, GoRelayAuthorization, GoRelayContext
from karajan.isolation.opencode_runtime import RUNTIME_SHA256, IsolatedOpenCode
from karajan.routing.compiler import digest
from test_opencode_go_composition import CANARY, SECRET, runtime_artifact

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux namespaces required")


@pytest.fixture(scope="module")
def accounting():
    directory = Path(
        os.environ.get("KARAJAN_GO_TOKENIZER_DIRECTORY", ".cache/go-context-artifacts")
    )
    if not directory.is_dir():
        if os.environ.get("KARAJAN_REQUIRE_GO_TOKENIZER") == "1":
            pytest.fail("Prepared fixed tokenizer artifacts are required")
        pytest.skip("Prepared fixed tokenizer artifacts are unavailable")
    return GoRequestAccounting(directory)


def response(index, usage):
    delta, finish = {"content": "Done."}, "stop"
    if index < 3:
        arguments = {"filePath": "/workspace/src/math_ops.py"}
        if index == 2:
            arguments.update(oldString="return a - b", newString="return a + b")
        delta = {
            "tool_calls": [
                {
                    "index": 0,
                    "id": f"call_{index}",
                    "type": "function",
                    "function": {
                        "name": "read" if index == 1 else "edit",
                        "arguments": json.dumps(arguments),
                    },
                }
            ]
        }
        finish = "tool_calls"
    events = []
    for change, reason in (({"role": "assistant"}, None), (delta, None), ({}, finish)):
        events.append(
            {
                "id": "chatcmpl-local",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "glm-5.3-flash",
                "choices": [{"index": 0, "delta": change, "finish_reason": reason}],
            }
        )
    events.append({"choices": [], "usage": usage})
    data = "".join("data: " + json.dumps(event) + "\n\n" for event in events)
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=(data + "data: [DONE]\n\n").encode(),
    )


@pytest.mark.parametrize("violation", [False, True], ids=["read-edit-history", "usage-stop"])
def test_native_task_roundtrip_measures_final_requests_and_stops_on_violation(
    tmp_path, accounting, violation
):
    journal = GoCallJournal(tmp_path / "calls.sqlite")
    binding = {
        "subject": {
            "kind": "task_attempt",
            "project_id": "project",
            "run_id": "run",
            "task_id": "math",
        },
        "attempt_id": "attempt",
        "fence": 1,
        "approval_digest": "a" * 64,
        "execution_policy_digest": "b" * 64,
        "workspace_digest": "c" * 64,
        "authentication_source_digest": "d" * 64,
        "profile_digest": "e" * 64,
        "runtime_digest": RUNTIME_SHA256,
        "channel": "opencode-go",
        "model": "glm-5.3-flash",
        "auth_generation": "synthetic",
        "expires_at": time.time() + 90,
        "max_requests": 6,
    }
    context = GoRelayContext(
        accounting,
        digest(accounting.source()),
        binding["execution_policy_digest"],
        16000,
        4096,
        32768,
        1024,
        2000,
    )
    grant = journal.create_grant(binding, grant_id="task")
    requests, measurements = [], []

    def receive(request):
        body = json.loads(request.content)
        expected = context.measure(body)
        # A separate connection must already see the complete final request's
        # content-free measurement before this upstream receives any bytes.
        current = GoCallJournal(journal.path).snapshot("task")["calls"][-1]
        assert current["state"] == "send_unknown"
        assert current["request_context"] == expected
        assert SECRET not in request.content.decode()
        assert CANARY not in request.content.decode()
        requests.append(body)
        measurements.append(expected)
        return response(
            len(requests),
            {
                "prompt_tokens": 999999 if violation else expected["local_input_tokens"],
                "completion_tokens": 32,
            },
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
    socket_path = tmp_path / "inference.sock"
    relay.start(unix_socket=socket_path)
    runtime = None
    source = b"def add(a, b):\n    return a - b\n"
    try:
        runtime = IsolatedOpenCode(
            runtime_artifact(),
            tmp_path / "native",
            socket_path,
            relay.capability,
            projection=[
                {
                    "path": "src/math_ops.py",
                    "sha256": hashlib.sha256(source).hexdigest(),
                    "writable": True,
                }
            ],
        )
        (runtime.workspace / "src").mkdir()
        target = runtime.workspace / "src/math_ops.py"
        target.write_bytes(source)
        (runtime.workspace / "private.txt").write_text(CANARY)
        runtime.start()
        session = runtime.request(
            "POST", "/session", {"title": "Task accounting", "agent": "probe"}
        )
        runtime.request(
            "POST",
            f"/session/{session['id']}/prompt_async",
            {
                "agent": "probe",
                "model": {"providerID": "opencode-go", "modelID": "glm-5.3-flash"},
                "parts": [{"type": "text", "text": "Read math_ops.py and fix addition."}],
            },
        )
        deadline = time.monotonic() + 30
        expected_calls = 1 if violation else 3
        while time.monotonic() < deadline:
            facts = journal.snapshot("task")
            if facts["request_count"] == expected_calls and all(
                call["outcome"] is not None for call in facts["calls"]
            ):
                break
            if relay.receipts and relay.receipts[-1]["reason_codes"]:
                break
            time.sleep(0.1)
        assert len(requests) == expected_calls, relay.receipts
        assert runtime.close()["local_stop"] == "confirmed"
        assert relay.close()["status"] == "closed"
        facts = journal.snapshot("task")
        assert len(facts["calls"]) == expected_calls
        if violation:
            assert facts["state"] == "revoked"
            assert target.read_bytes() == source
            assert facts["calls"][0]["outcome"]["reason_codes"] == [
                "CONTEXT_PROVIDER_INPUT_EXCEEDED"
            ]
        else:
            assert target.read_bytes() == source.replace(b"a - b", b"a + b")
            assert [sum(m["role"] == "tool" for m in r["messages"]) for r in requests] == [0, 1, 2]
            assert len({m["request_digest"] for m in measurements}) == 3
            assert measurements[-1]["local_input_tokens"] > measurements[0]["local_input_tokens"]
            assert all(c["outcome"]["protocol_passed"] for c in facts["calls"])
        for secret in (SECRET, CANARY, grant["capability"], relay.capability):
            assert secret not in json.dumps(facts)
    finally:
        journal.revoke_grant("task")
        if runtime is not None:
            assert runtime.close()["local_stop"] == "confirmed"
        assert relay.close()["status"] == "closed"
