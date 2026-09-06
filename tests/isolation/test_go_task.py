"""Native Task producer: real Linux tools/accounting, explicit controller guard doubles.

These tests do not approve a Run or qualify a Profile. Root integration tests
must replace the no-op business guards with actual durable controller services.
"""

import hashlib
import json
import sys
import time
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, replace

import httpx
import pytest
from karajan.adapters.opencode.go_journal import GoCallJournal
from karajan.adapters.opencode.go_relay import GoRelayAuthorization, GoRelayContext
from karajan.isolation.go_probe import source_digest
from karajan.isolation.go_task import GoTaskFile, GoTaskInput, execute_go_task, native_task_source
from karajan.projects.credential_sources import ResolvedCredential
from test_go_context import accounting as accounting
from test_go_context import artifacts as artifacts
from test_opencode_go_composition import runtime_artifact

SECRET = "synthetic-task-credential-not-a-provider-key"
INITIAL = b"def add(a, b):\n    return a - b\n"
EXPECTED = b"def add(a, b):\n    return a + b\n"
REFERENCE = b"Contract: add returns the sum of its two arguments.\n"


def file(path, content, writable):
    return GoTaskFile(path, hashlib.sha256(content).hexdigest(), writable, content)


def test_input_is_immutable_and_descriptor_contains_only_bound_summaries():
    task = GoTaskInput(
        "a" * 64,
        "b" * 64,
        "c" * 64,
        "synthetic private task text",
        (file("src/add.py", INITIAL, True),),
        60,
    )
    descriptor = task.descriptor()
    assert descriptor["workspace_digest"] == "a" * 64
    assert descriptor["prompt_sha256"] == hashlib.sha256(task.prompt.encode()).hexdigest()
    assert descriptor["files"] == [
        {
            "path": "src/add.py",
            "sha256": hashlib.sha256(INITIAL).hexdigest(),
            "writable": True,
            "size": len(INITIAL),
        }
    ]
    assert "synthetic private task text" not in repr(task)
    assert "return a - b" not in repr(task)
    with pytest.raises(FrozenInstanceError):
        task.prompt = "changed"
    descriptor["files"].clear()
    assert len(task.descriptor()["files"]) == 1


def prepared(tmp_path, accounting):
    runtime = runtime_artifact()
    source = native_task_source(runtime, accounting)
    native_digest = source_digest(source)
    runner_digest = source_digest({"fixture_root_runner": True, "native": source})
    task = GoTaskInput(
        "a" * 64,
        native_digest,
        runner_digest,
        "Read /workspace/src/add.py and fix add(a,b) to return a+b. "
        "Use only read and edit, modify only that existing file, then stop.",
        (file("docs/contract.txt", REFERENCE, False), file("src/add.py", INITIAL, True)),
        60,
    )
    journal = GoCallJournal(tmp_path / "calls.sqlite")
    binding = {
        "subject": {
            "kind": "task_attempt",
            "project_id": "project",
            "run_id": "run",
            "task_id": "add",
        },
        "attempt_id": "task-attempt",
        "fence": 1,
        "approval_digest": "d" * 64,
        "execution_policy_digest": "e" * 64,
        "workspace_digest": task.workspace_digest,
        "authentication_source_digest": "f" * 64,
        "profile_digest": "b" * 64,
        "runtime_digest": runner_digest,
        "channel": "opencode-go",
        "model": "glm-5.3-flash",
        "auth_generation": "generation",
        "expires_at": time.time() + 180,
        "max_requests": 6,
    }
    grant = journal.create_grant(binding, grant_id="task-grant")
    auth = GoRelayAuthorization(journal, grant["grant_id"], binding, grant["capability"])
    context = GoRelayContext(
        accounting,
        source_digest(accounting.source()),
        binding["execution_policy_digest"],
        12288,
        4096,
        16384,
        2048,
        2000,
    )
    credential = ResolvedCredential("project", "secret:synthetic", "generation", "source", SECRET)
    return runtime, task, credential, auth, context


@contextmanager
def permitted():
    """Explicit test-only replacement for the root's per-send current guard."""
    yield


@pytest.mark.skipif(sys.platform != "linux", reason="Fixed Linux runtime required")
def test_real_native_task_returns_owned_stopped_projection_and_durable_usage(tmp_path, accounting):
    runtime, task, credential, auth, context = prepared(tmp_path, accounting)
    requests = []
    starts = []

    def start(native):
        starts.append(native)
        return native.start()

    def receive(request):
        payload = json.loads(request.content)
        requests.append(payload)
        call = auth.journal.snapshot(auth.grant_id)["calls"][-1]
        assert call["state"] == "send_unknown"
        assert call["request_context"] == context.measure(payload)
        return response(len(requests))

    result = execute_go_task(
        runtime,
        tmp_path / "native-task",
        task,
        credential,
        auth,
        context,
        start_native=start,
        send_guard=permitted,
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(receive)),
    )
    (tmp_path / "public-report.json").write_text(json.dumps(result.report, indent=2))
    assert result.report["status"] == "completed", result.report["reason_codes"]
    assert len(starts) == 1 and len(requests) == 3
    assert result.capture is not None
    assert dict(result.capture.files) == {"docs/contract.txt": REFERENCE, "src/add.py": EXPECTED}
    assert result.capture.stop_evidence["local_stop"] == "confirmed"
    assert result.report["journal"]["state"] == "revoked"
    assert result.report["candidate_validation"] == "not_run"
    assert result.report["provider_remote_stop"] == "unknown"
    assert SECRET not in json.dumps(result.report)
    assert task.prompt not in json.dumps(result.report)
    result.report["status"] = "forged"
    assert result.report["status"] == "completed"


def response(index, *, usage=True):
    delta, finish = {"content": "Implemented."}, "stop"
    if index in (1, 2):
        arguments = {"filePath": "/workspace/src/add.py"}
        if index == 2:
            arguments.update(oldString="return a - b", newString="return a + b")
        delta = {
            "tool_calls": [
                {
                    "index": 0,
                    "id": f"task_call_{index}",
                    "type": "function",
                    "function": {
                        "name": "read" if index == 1 else "edit",
                        "arguments": json.dumps(arguments),
                    },
                }
            ]
        }
        finish = "tool_calls"
    frames = [
        {
            "model": "glm-5.3-flash",
            "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
        },
        {"model": "glm-5.3-flash", "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
    ]
    if usage:
        frames.append(
            {
                "model": "glm-5.3-flash",
                "choices": [],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            }
        )
    raw = "".join("data: " + json.dumps(frame) + "\n\n" for frame in frames)
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=(raw + "data: [DONE]\n\n").encode(),
    )


@pytest.mark.skipif(sys.platform != "linux", reason="Fixed Linux runtime required")
@pytest.mark.parametrize(
    "fault",
    [
        "native_source",
        "runner_source",
        "workspace",
        "context_source",
        "policy",
        "context_limit",
        "margin",
        "credential_project",
        "credential_generation",
        "capability",
        "revoked",
    ],
)
def test_misbound_inputs_have_no_start_or_send_callback(tmp_path, accounting, fault):
    runtime, task, credential, auth, context = prepared(tmp_path, accounting)
    if fault == "native_source":
        task = replace(task, native_source_sha256="0" * 64)
    elif fault == "runner_source":
        task = replace(task, runner_source_digest="0" * 64)
    elif fault == "workspace":
        task = replace(task, workspace_digest="0" * 64)
    elif fault == "context_source":
        context = replace(context, source_sha256="0" * 64)
    elif fault == "policy":
        context = replace(context, execution_policy_digest="0" * 64)
    elif fault == "context_limit":
        context = replace(context, operating_context_tokens=32768)
    elif fault == "margin":
        context = replace(context, fixed_margin=2047)
    elif fault == "credential_project":
        credential = replace(credential, project_id="foreign")
    elif fault == "credential_generation":
        credential = replace(credential, generation="other-generation")
    elif fault == "capability":
        auth = replace(auth, capability="wrong-capability")
    else:
        auth.journal.revoke_grant(auth.grant_id)

    def unexpected(*args):
        raise AssertionError("Preflight must not enter either controller callback")

    with pytest.raises(ValueError):
        execute_go_task(
            runtime,
            tmp_path / "never-created",
            task,
            credential,
            auth,
            context,
            start_native=unexpected,
            send_guard=unexpected,
        )
    assert not (tmp_path / "never-created").exists()
    assert auth.journal.snapshot(auth.grant_id)["request_count"] == 0


@pytest.mark.skipif(sys.platform != "linux", reason="Fixed Linux runtime required")
@pytest.mark.parametrize("mode", ["deny", "fabricate"])
def test_start_callback_cannot_manufacture_namespace_or_capture(tmp_path, accounting, mode):
    runtime, task, credential, auth, context = prepared(tmp_path, accounting)
    natives = []

    def callback(native):
        natives.append(native)
        if mode == "deny":
            raise RuntimeError("synthetic approval withdrawn")
        return {"state": "running", "namespace_pid": 1}

    result = execute_go_task(
        runtime,
        tmp_path / "blocked",
        task,
        credential,
        auth,
        context,
        start_native=callback,
        send_guard=permitted,
    )
    assert result.report["status"] == "failed"
    assert result.capture is None
    assert not (natives[0].directory / "projection.json").exists()
    assert auth.journal.snapshot(auth.grant_id)["request_count"] == 0
    assert auth.journal.snapshot(auth.grant_id)["state"] == "revoked"
    assert result.report["requests"] == []


@pytest.mark.skipif(sys.platform != "linux", reason="Fixed Linux runtime required")
def test_send_guard_withdrawal_after_first_response_cannot_send_second_call(tmp_path, accounting):
    runtime, task, credential, auth, context = prepared(tmp_path, accounting)
    entries = 0
    requests = []

    @contextmanager
    def withdrawn():
        nonlocal entries
        entries += 1
        if entries > 1:
            raise RuntimeError("synthetic task cancellation")
        yield

    def receive(request):
        requests.append(request)
        return response(len(requests))

    result = execute_go_task(
        runtime,
        tmp_path / "withdrawn",
        task,
        credential,
        auth,
        context,
        start_native=lambda native: native.start(),
        send_guard=withdrawn,
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(receive)),
    )
    assert result.report["status"] == "failed"
    assert len(requests) == 1 and entries == 2
    assert result.report["journal"]["request_count"] == 1
    assert result.report["journal"]["state"] == "revoked"
    assert any(
        "TASK_SEND_GUARD_REJECTED" in row["reason_codes"] for row in result.report["requests"]
    )
    assert result.capture is not None
    assert dict(result.capture.files)["src/add.py"] == INITIAL
    assert result.report["candidate_validation"] == "not_run"


@pytest.mark.parametrize(
    "fault", ["prompt_size", "empty_prompt", "mutable_files", "bad_bytes", "path", "no_write"]
)
def test_invalid_input_fails_before_runtime_construction(fault):
    task = GoTaskInput("a" * 64, "b" * 64, "c" * 64, "Task", (file("code.py", INITIAL, True),), 30)
    with pytest.raises(ValueError):
        if fault == "prompt_size":
            replace(task, prompt="x" * 8193)
        elif fault == "empty_prompt":
            replace(task, prompt=" ")
        elif fault == "mutable_files":
            replace(task, files=list(task.files))
        elif fault == "bad_bytes":
            replace(task.files[0], content=b"changed")
        elif fault == "path":
            replace(task.files[0], path="../outside")
        else:
            replace(task, files=(file("code.py", INITIAL, False),))
