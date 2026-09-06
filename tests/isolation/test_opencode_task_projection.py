"""Controller projection through real Linux mounts and the native management port."""

import hashlib
import json
import os
import socket
import socketserver
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest
from karajan.isolation.opencode_runtime import IsolatedOpenCode
from test_opencode_go_composition import runtime_artifact

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux namespaces required")


def projected(path, content, writable=False):
    return {"path": path, "sha256": hashlib.sha256(content).hexdigest(), "writable": writable}


@pytest.fixture
def native(tmp_path):
    artifact = runtime_artifact()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    address = tmp_path / "inference.sock"
    listener.bind(str(address))
    listener.listen()
    runtimes = []

    def create(projection):
        runtime = IsolatedOpenCode(
            artifact,
            tmp_path / f"native-{len(runtimes)}",
            address,
            "synthetic-capability",
            projection=projection,
        )
        runtimes.append(runtime)
        return runtime

    yield create
    for runtime in runtimes:
        assert runtime.close()["local_stop"] == "confirmed"
    listener.close()


def test_existing_task_paths_have_exact_native_permissions_and_file_mounts(native):
    source = b"def add(a, b):\n    return a - b\n"
    guide = b"Keep the two-argument interface.\n"
    runtime = native(
        [
            projected("src/math_ops.py", source, True),
            projected("docs/contract.txt", guide),
        ]
    )
    for path, content in [("src/math_ops.py", source), ("docs/contract.txt", guide)]:
        destination = runtime.workspace / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
    (runtime.workspace / "private.txt").write_text("SYNTHETIC_UNPROJECTED")
    runtime.start()
    permissions = runtime.request("GET", "/config")["permission"]
    assert permissions == {
        "*": "deny",
        "read": {
            "*": "deny",
            "workspace/docs/contract.txt": "allow",
            "workspace/src/math_ops.py": "allow",
        },
        "edit": {"*": "deny", "workspace/src/math_ops.py": "allow"},
    }
    processes = runtime.probe_lifecycle()["observed_processes"]
    init = next(row for row in processes if row["namespace_pid"] == 1)
    root = Path(f"/proc/{init['pid']}/root/workspace")
    assert sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()) == [
        "docs/contract.txt",
        "src/math_ops.py",
    ]
    assert (root / "docs/contract.txt").read_bytes() == guide
    with pytest.raises(OSError):
        (root / "docs/contract.txt").write_bytes(b"unauthorized")
    with pytest.raises(OSError):
        (root / "new.txt").write_bytes(b"unauthorized")
    (root / "src/math_ops.py").write_bytes(b"trusted mount observation")
    assert (runtime.workspace / "src/math_ops.py").read_bytes() == b"trusted mount observation"
    assert "SYNTHETIC_UNPROJECTED" not in json.dumps(runtime.snapshot())


@pytest.mark.parametrize("change", ["bytes", "symlink", "hardlink", "parent-link"])
def test_changed_or_linked_input_cannot_start_a_namespace(native, tmp_path, change):
    runtime = native([projected("src/file.py", b"approved", True)])
    directory = runtime.workspace / "src"
    directory.mkdir()
    source = directory / "file.py"
    external = tmp_path / "external"
    external.write_bytes(b"approved")
    if change == "bytes":
        source.write_bytes(b"changed")
    elif change == "symlink":
        source.symlink_to(external)
    elif change == "hardlink":
        os.link(external, source)
    else:
        directory.rmdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "file.py").write_bytes(b"approved")
        directory.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="WORKSPACE_PROJECTION_"):
        runtime.start()
    assert not (runtime.directory / "namespace.log").exists()
    assert runtime.close()["observed_processes"] == []
    assert external.read_bytes() == b"approved"


@pytest.mark.parametrize("mode", ["write", "readonly", "outside"])
def test_native_tools_enforce_the_controller_projection(tmp_path, mode):
    artifact = runtime_artifact()
    requests = []
    source = b"def add(a, b):\n    return a - b\n"
    target = "src/math_ops.py" if mode != "readonly" else "docs/reference.py"
    actions = [("read", {"filePath": "/workspace/" + target})]
    if mode == "outside":
        actions = [("read", {"filePath": "/workspace/private.txt"})]
    else:
        actions.append(
            (
                "edit",
                {
                    "filePath": "/workspace/" + target,
                    "oldString": "return a - b",
                    "newString": "return a + b",
                },
            )
        )

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            index = len(requests) - 1
            delta, finish = {"content": "Done."}, "stop"
            if index < len(actions):
                name, arguments = actions[index]
                delta = {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": f"call_{index}",
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(arguments)},
                        }
                    ]
                }
                finish = "tool_calls"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            for change, reason in (({"role": "assistant"}, None), (delta, None), ({}, finish)):
                event = {
                    "id": "chatcmpl-local",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "glm-5.3-flash",
                    "choices": [{"index": 0, "delta": change, "finish_reason": reason}],
                }
                self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            self.close_connection = True

    address = tmp_path / "provider.sock"
    server = socketserver.UnixStreamServer(str(address), Provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    runtime = None
    try:
        runtime = IsolatedOpenCode(
            artifact,
            tmp_path / "native",
            address,
            "synthetic-capability",
            projection=[
                projected("src/math_ops.py", source, True),
                projected("docs/reference.py", source),
            ],
        )
        for path in ("src/math_ops.py", "docs/reference.py"):
            destination = runtime.workspace / path
            destination.parent.mkdir()
            destination.write_bytes(source)
        (runtime.workspace / "private.txt").write_text("SYNTHETIC_MUST_NOT_LEAVE")
        runtime.start()
        session = runtime.request("POST", "/session", {"title": "Task paths", "agent": "probe"})
        runtime.request(
            "POST",
            f"/session/{session['id']}/prompt_async",
            {
                "agent": "probe",
                "model": {"providerID": "opencode-go", "modelID": "glm-5.3-flash"},
                "parts": [{"type": "text", "text": "Perform the requested task."}],
            },
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            messages = runtime.request("GET", f"/session/{session['id']}/message")
            if len(requests) == len(actions) + 1:
                break
            time.sleep(0.1)
        assert len(requests) == len(actions) + 1, messages
        assert runtime.close()["local_stop"] == "confirmed"
        states = [
            part["state"]
            for message in messages
            for part in message["parts"]
            if part["type"] == "tool"
        ]
        assert len(states) == len(actions)
        if mode == "write":
            assert [s["status"] for s in states] == ["completed", "completed"]
            assert (runtime.workspace / target).read_bytes() == source.replace(b"a - b", b"a + b")
        else:
            assert states[-1]["status"] == "error"
            assert states[-1]["error"].startswith("The user has specified a rule")
            assert (runtime.workspace / "src/math_ops.py").read_bytes() == source
        assert (runtime.workspace / "docs/reference.py").read_bytes() == source
        assert "SYNTHETIC_MUST_NOT_LEAVE" not in json.dumps(requests)
    finally:
        if runtime is not None:
            assert runtime.close()["local_stop"] == "confirmed"
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
