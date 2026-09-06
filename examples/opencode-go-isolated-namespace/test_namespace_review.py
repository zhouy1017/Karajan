"""Independent real Linux runtime checks; no provider or credential access."""

import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from karajan.adapters.opencode.go_relay import GoRelay
from karajan.isolation.opencode_runtime import IsolatedOpenCode
from test_go_relay import answer, event, stream

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="real Linux namespaces")


@pytest.fixture
def instance():
    artifact = Path(os.environ["KARAJAN_OPENCODE_LINUX_BINARY"])
    with tempfile.TemporaryDirectory(prefix="karajan-ns-review-", dir="/tmp") as directory:
        root = Path(directory)
        listener = socket.socket(socket.AF_UNIX)
        upstream = root / "inference.sock"
        listener.bind(str(upstream))
        listener.listen()
        runtime = IsolatedOpenCode(artifact, root / "run", upstream, "synthetic-review-capability")
        try:
            yield runtime, root
        finally:
            runtime.close()
            listener.close()


def test_closed_evidence_is_detached_from_callers_mutating_previous_result(instance):
    runtime, _ = instance
    runtime.start()
    observed = runtime.probe_lifecycle()["observed_processes"]
    closed = runtime.close()
    assert closed["local_stop"] == "confirmed"
    assert len(closed["observed_processes"]) >= len(observed) >= 4
    closed["observed_processes"].clear()
    again = runtime.close()
    assert len(again["observed_processes"]) >= len(observed)
    assert again["remote_stop"] == "unknown"


def test_process_disappearing_during_proc_stat_read_does_not_break_close(instance):
    runtime, _ = instance
    runtime.start()
    processes = runtime.probe_lifecycle()["observed_processes"]
    victim = next(item for item in processes if item["namespace_pid"] != 1)
    original = Path.read_text
    observed = False

    def disappearing(path, *args, **kwargs):
        nonlocal observed
        if str(path) == f"/proc/{victim['pid']}/stat" and not observed:
            observed = True
            raise ProcessLookupError(3, "No such process")
        return original(path, *args, **kwargs)

    # Reproduce the actual procfs race recorded in initial.xml at its OS read
    # boundary. Start/kill/wait still use a real namespace and native process.
    with patch.object(Path, "read_text", disappearing):
        closed = runtime.close()
    assert observed
    assert closed["local_stop"] == "confirmed"
    assert closed["observed_processes_still_running"] == []


def test_two_simultaneous_starts_cannot_launch_two_namespace_processes(instance):
    runtime, _ = instance
    original_socketpair = socket.socketpair
    import subprocess

    original_popen = subprocess.Popen
    launches = []
    barrier = threading.Barrier(2)

    def paired(*args, **kwargs):
        pair = original_socketpair(*args, **kwargs)
        # Pause at the public OS port after both start calls crossed their guard.
        # A correctly serialized second start cannot enter: release the first on
        # timeout without manufacturing a startup failure in the corrected code.
        try:
            barrier.wait(timeout=3)
        except threading.BrokenBarrierError:
            pass
        return pair

    def spawned(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        launches.append(process)
        return process

    def start():
        try:
            return runtime.start()
        except Exception as error:
            return type(error).__name__

    try:
        with (
            patch("socket.socketpair", side_effect=paired),
            patch("subprocess.Popen", side_effect=spawned),
        ):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: start(), range(2)))
        assert len(launches) == 1, (len(launches), results)
    finally:
        for process in launches:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)


def test_public_management_limits_session_creation_and_rejects_extra_authority(instance):
    runtime, _ = instance
    runtime.start()
    sessions = []
    for index in range(8):
        session = runtime.request(
            "POST", "/session", {"title": f"session-{index}", "agent": "probe"}
        )
        sessions.append(session["id"])
    assert len(set(sessions)) == 8
    with pytest.raises(ValueError, match="MANAGEMENT_REQUEST_NOT_ALLOWED"):
        runtime.request("POST", "/session", {"title": "ninth", "agent": "probe"})
    for method, route, body in (
        ("PUT", "/auth/opencode-go", {"key": "synthetic"}),
        ("PATCH", "/config", {"permission": "allow"}),
        ("POST", "/session/" + sessions[0] + "/shell", {"command": "echo expanded"}),
        ("POST", "/permission/synthetic/reply", {"reply": "always"}),
        ("GET", "/session/ses_notcreated/message", None),
        ("GET", "/config?directory=/tmp", None),
    ):
        with pytest.raises(ValueError, match="MANAGEMENT_REQUEST_NOT_ALLOWED"):
            runtime.request(method, route, body)
    assert runtime.request("GET", "/global/health")["healthy"] is True
    assert runtime.close()["local_stop"] == "confirmed"


def test_artifact_changed_after_construction_is_rejected_before_start(instance):
    _, root = instance
    artifact = root / "copied-opencode"
    shutil.copy2(Path(os.environ["KARAJAN_OPENCODE_LINUX_BINARY"]), artifact)
    runtime = IsolatedOpenCode(
        artifact, root / "changed-run", root / "inference.sock", "synthetic-review-capability"
    )
    # The ELF still starts and reports its original version; only its exact bytes
    # changed. Never mutate the shared installed artifact in this test.
    with artifact.open("ab") as file:
        file.write(b"independent-review-appended-bytes")
    try:
        with pytest.raises(ValueError, match="RUNTIME_ARTIFACT_MISMATCH"):
            runtime.start()
    finally:
        runtime.close()


def test_native_file_permissions_and_inherited_material_boundaries(monkeypatch):
    marker = "SYNTHETIC_HOST_ENV_NOT_FOR_NATIVE"
    blocked = "SYNTHETIC_DENIED_CONTENT"
    monkeypatch.setenv("OPENAI_API_KEY", marker)
    requests = []
    calls = [
        ("read", "/workspace/fixture.py"),
        ("read", "/workspace/blocked.txt"),
        ("edit", "/workspace/blocked.txt"),
        ("read", "/workspace/alias.py"),
        ("read", "/proc/self/environ"),
        ("read", "/proc/1/fd"),
        ("read", "/control/inner.py"),
        ("edit", "/control/inner.py"),
    ]

    def receive(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            tool_calls = []
            for index, (tool, path) in enumerate(calls):
                arguments = {"filePath": path}
                if tool == "edit":
                    arguments.update(oldString="KNOWN_TEST_SEED", newString="changed")
                tool_calls.append(
                    {
                        "index": index,
                        "id": f"call_{index}",
                        "type": "function",
                        "function": {"name": tool, "arguments": json.dumps(arguments)},
                    }
                )
            return answer(
                stream(
                    event(
                        choices=[
                            {
                                "index": 0,
                                "delta": {"tool_calls": tool_calls},
                                "finish_reason": "tool_calls",
                            }
                        ]
                    )
                )
            )
        return answer()

    with tempfile.TemporaryDirectory(prefix="karajan-native-boundaries-", dir="/tmp") as directory:
        root = Path(directory)
        descriptor_path = root / "inherited-descriptor-canary"
        descriptor_path.write_text("SYNTHETIC_FD_CONTENT")
        descriptor = os.open(descriptor_path, os.O_RDONLY)
        os.set_inheritable(descriptor, True)
        relay = GoRelay(
            "synthetic-provider-review-key",
            blocked,
            client_factory=lambda: httpx.Client(
                transport=httpx.MockTransport(receive), trust_env=False
            ),
        )
        upstream = root / "relay.sock"
        relay.start(unix_socket=upstream)
        runtime = IsolatedOpenCode(
            Path(os.environ["KARAJAN_OPENCODE_LINUX_BINARY"]),
            root / "run",
            upstream,
            relay.capability,
        )
        (runtime.workspace / "fixture.py").write_text("PUBLIC_FIXTURE_CONTENT\n")
        (runtime.workspace / "blocked.txt").write_text("KNOWN_TEST_SEED\n" + blocked)
        (runtime.workspace / "alias.py").symlink_to("/workspace/blocked.txt")
        try:
            snapshot = runtime.start()
            assert snapshot["native_control_fd_inherited"] is False
            assert snapshot["host_mount_visible"] is False
            assert snapshot["wsl_interop_visible"] is False
            assert snapshot["network_interfaces"] == ["lo"]
            assert snapshot["ipv4_routes"] == []
            observed = runtime.probe_lifecycle()["observed_processes"]
            namespace_init = next(item for item in observed if item["namespace_pid"] == 1)
            visible_workspace = Path(f"/proc/{namespace_init['pid']}/root/workspace")
            assert sorted(path.name for path in visible_workspace.iterdir()) == ["fixture.py"]
            assert (visible_workspace / "fixture.py").stat().st_ino == (
                runtime.workspace / "fixture.py"
            ).stat().st_ino
            native = next(
                item
                for item in observed
                if Path(f"/proc/{item['pid']}/cmdline")
                .read_bytes()
                .startswith(b"/opt/opencode\x00")
            )
            native_env = Path(f"/proc/{native['pid']}/environ").read_bytes()
            if marker.encode() in native_env or b"synthetic-provider-review-key" in native_env:
                pytest.fail("Inherited or provider material reached native environment")
            fd_targets = []
            for path in Path(f"/proc/{native['pid']}/fd").iterdir():
                try:
                    fd_targets.append(os.readlink(path))
                except FileNotFoundError:
                    pass
            assert str(descriptor_path) not in fd_targets
            session = runtime.request(
                "POST", "/session", {"title": "Boundary review", "agent": "probe"}
            )
            runtime.request(
                "POST",
                f"/session/{session['id']}/prompt_async",
                {
                    "agent": "probe",
                    "model": {"providerID": "opencode-go", "modelID": "glm-5.3-flash"},
                    "parts": [{"type": "text", "text": "Perform the fixed file permission check."}],
                },
            )
            deadline = time.monotonic() + 20
            states = []
            while time.monotonic() < deadline:
                messages = runtime.request("GET", f"/session/{session['id']}/message")
                states = [
                    part["state"]
                    for message in messages
                    for part in message["parts"]
                    if part["type"] == "tool"
                ]
                if len(states) == len(calls) and all(
                    state["status"] in {"completed", "error"} for state in states
                ):
                    break
                time.sleep(0.1)
            assert len(states) == len(calls)
            completed = [state for state in states if state["status"] == "completed"]
            assert len(completed) == 1
            assert "PUBLIC_FIXTURE_CONTENT" in completed[0]["output"]
            errors = [state for state in states if state["status"] == "error"]
            assert len(errors) == 7
            for state in errors:
                if (
                    state["input"]["filePath"] == "/workspace/blocked.txt"
                    and "oldString" in state["input"]
                ):
                    # The host file is not mounted. This proves OS exclusion,
                    # independently from the native edit tool's permission order.
                    assert "not found" in state["error"].lower(), state["error"]
                    assert "/workspace/blocked.txt" in state["error"], state["error"]
                else:
                    assert state["error"].startswith("The user has specified a rule"), state
            assert blocked not in json.dumps(states)
            assert blocked not in json.dumps(requests)
            assert (runtime.workspace / "blocked.txt").read_text() == "KNOWN_TEST_SEED\n" + blocked
        finally:
            closed = runtime.close()
            assert closed["local_stop"] == "confirmed"
            assert closed["remote_stop"] == "unknown"
            relay.close()
            os.close(descriptor)


def test_nonfixture_edit_does_not_disclose_protected_contents():
    requests = []

    def receive(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            calls = [
                {
                    "index": index,
                    "id": f"guess_{index}",
                    "type": "function",
                    "function": {
                        "name": "edit",
                        "arguments": json.dumps(
                            {
                                "filePath": "/workspace/blocked.txt",
                                "oldString": guess,
                                "newString": "changed",
                            }
                        ),
                    },
                }
                for index, guess in enumerate(("SYNTHETIC_RIGHT_GUESS", "SYNTHETIC_WRONG_GUESS"))
            ]
            return answer(
                stream(
                    event(
                        choices=[
                            {
                                "index": 0,
                                "delta": {"tool_calls": calls},
                                "finish_reason": "tool_calls",
                            }
                        ]
                    )
                )
            )
        return answer()

    with tempfile.TemporaryDirectory(prefix="karajan-edit-oracle-", dir="/tmp") as directory:
        root = Path(directory)
        relay = GoRelay(
            "synthetic-provider-review-key",
            "UNRELATED_CANARY",
            client_factory=lambda: httpx.Client(
                transport=httpx.MockTransport(receive), trust_env=False
            ),
        )
        upstream = root / "relay.sock"
        relay.start(unix_socket=upstream)
        runtime = IsolatedOpenCode(
            Path(os.environ["KARAJAN_OPENCODE_LINUX_BINARY"]),
            root / "run",
            upstream,
            relay.capability,
        )
        protected = runtime.workspace / "blocked.txt"
        protected.write_text("SYNTHETIC_RIGHT_GUESS")
        try:
            runtime.start()
            session = runtime.request(
                "POST", "/session", {"title": "Denied edit equality oracle", "agent": "probe"}
            )
            runtime.request(
                "POST",
                f"/session/{session['id']}/prompt_async",
                {
                    "agent": "probe",
                    "model": {"providerID": "opencode-go", "modelID": "glm-5.3-flash"},
                    "parts": [{"type": "text", "text": "Try the two fixed edits once."}],
                },
            )
            states = []
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                messages = runtime.request("GET", f"/session/{session['id']}/message")
                states = [
                    part["state"]
                    for message in messages
                    for part in message["parts"]
                    if part["type"] == "tool"
                ]
                if len(states) == 2 and all(state["status"] == "error" for state in states):
                    if len(requests) >= 2:
                        break
                time.sleep(0.1)
            assert protected.read_text() == "SYNTHETIC_RIGHT_GUESS"
            assert len(states) == 2
            assert len(requests) >= 2
            facts = [
                {
                    "guess": state["input"]["oldString"],
                    "permission_denied": state["error"].startswith("The user has specified a rule"),
                    "content_mismatch": state["error"].startswith("Could not find oldString"),
                }
                for state in states
            ]
            # Either an identical permission rejection or an identical missing
            # file error is acceptable. A guess-dependent error is an oracle even
            # when neither call succeeds or includes the protected file's text.
            assert states[0]["error"] == states[1]["error"], facts
            assert not any(fact["content_mismatch"] for fact in facts), facts
        finally:
            runtime.close()
            relay.close()
