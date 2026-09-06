"""Stopped projection bytes from the actual native runtime; no real provider."""

import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from dataclasses import FrozenInstanceError, asdict

import httpx
import pytest
from karajan.adapters.opencode.go_relay import GoRelay
from karajan.isolation.opencode_runtime import IsolatedOpenCode, StoppedProjection
from test_baseline_materialization import registered as registered
from test_opencode_go_composition import CANARY, SECRET, native_response, runtime_artifact
from test_projected_capture import inputs

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux namespaces required")


def descriptor(path, content, writable=True):
    return {"path": path, "sha256": hashlib.sha256(content).hexdigest(), "writable": writable}


@pytest.fixture
def native(tmp_path):
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    address = tmp_path / "relay.sock"
    listener.bind(str(address))
    listener.listen()
    runtimes = []

    def create(projection):
        runtime = IsolatedOpenCode(
            runtime_artifact(),
            tmp_path / f"native-{len(runtimes)}",
            address,
            "synthetic-capability",
            projection=projection,
        )
        runtimes.append(runtime)
        return runtime

    yield create
    for runtime in runtimes:
        runtime.close()
    listener.close()


def test_capture_owns_stop_and_caches_detached_immutable_bytes(native):
    original = b"def add(a, b):\n    return a - b\n"
    runtime = native([descriptor("src/math.py", original)])
    source = runtime.workspace / "src/math.py"
    source.parent.mkdir()
    source.write_bytes(original)
    runtime.start()
    assert runtime.request("GET", "/path")["directory"] == "/workspace"

    capture = runtime.capture_projection()

    assert isinstance(capture, StoppedProjection)
    assert capture.files == (("src/math.py", original),)
    assert capture.projection[0].path == "src/math.py"
    assert capture.projection[0].writable is True
    assert capture.stop_evidence["local_stop"] == "confirmed"
    assert capture.stop_evidence["namespace_init_stopped"] is True
    assert capture.stop_evidence["observed_processes_still_running"] == []
    assert capture.stop_evidence["remote_stop"] == "unknown"
    with pytest.raises(FrozenInstanceError):
        capture.runtime_sha256 = "forged"
    capture.stop_evidence["local_stop"] = "forged"
    source.write_bytes(b"after-capture tamper")
    assert runtime.capture_projection() is capture
    assert capture.files == (("src/math.py", original),)
    assert capture.stop_evidence["local_stop"] == "confirmed"


def test_native_edit_capture_freezes_full_baseline_and_requires_real_validation(
    registered, tmp_path
):
    store, baseline, files = registered
    projection, expected, freeze = inputs(baseline, files)
    requests = []

    def receive(request):
        requests.append(json.loads(request.content))
        response = native_response(len(requests), old_string="print('base')")
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=response.content.replace(
                b"/workspace/fixture.py", b"/workspace/src/task.py"
            ).replace(b"return min(high, max(low, value))", b"print('changed')"),
        )

    relay = GoRelay(
        SECRET,
        CANARY,
        client_factory=lambda: httpx.Client(
            transport=httpx.MockTransport(receive), trust_env=False
        ),
    )
    address = tmp_path / "provider.sock"
    relay.start(unix_socket=address)
    runtime = None
    try:
        runtime = IsolatedOpenCode(
            runtime_artifact(),
            tmp_path / "native",
            address,
            relay.capability,
            projection=projection,
        )
        for row in projection:
            path = runtime.workspace / row["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(files[row["path"]])
        inode = (runtime.workspace / "src/task.py").stat().st_ino
        runtime.start()
        session = runtime.request("POST", "/session", {"title": "Capture", "agent": "probe"})
        runtime.request(
            "POST",
            f"/session/{session['id']}/prompt_async",
            {
                "agent": "probe",
                "model": {"providerID": "opencode-go", "modelID": "glm-5.3-flash"},
                "parts": [{"type": "text", "text": "Update the requested print call."}],
            },
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            messages = runtime.request("GET", f"/session/{session['id']}/message")
            states = [
                part["state"]
                for message in messages
                for part in message["parts"]
                if part["type"] == "tool"
            ]
            if len(requests) == 3 and [s["status"] for s in states] == ["completed", "completed"]:
                break
            time.sleep(0.1)
        assert len(requests) == 3
        assert [s["status"] for s in states] == ["completed", "completed"]

        captured = runtime.capture_projection()

        assert (runtime.workspace / "src/task.py").stat().st_ino == inode
        assert dict(captured.files) == expected
        assert captured.stop_evidence["local_stop"] == "confirmed"
        result = store.freeze_projection(
            [asdict(row) for row in captured.projection], dict(captured.files), freeze
        )
        restored = tmp_path / "restored"
        store.materialize(result["id"], restored)
        assert {
            path.relative_to(restored).as_posix(): path.read_bytes()
            for path in restored.rglob("*")
            if path.is_file()
        } == files | expected
        assert (restored / "bin/run").stat().st_mode & 0o777 == 0o755
        assert result["changed_paths"] == ["src/task.py"]
        gate = store.gate(
            result["id"],
            current={
                key: result[key]
                for key in ("repository_identity", "base_sha", "input_sha256", "policy_sha256")
            },
        )
        assert gate["reasons"] == ["CHECK_EVIDENCE_MISSING:tests", "REVIEW_EVIDENCE_MISSING"]
        assert gate["local_gate_passed"] is False
    finally:
        if runtime is not None:
            runtime.close()
        relay.close()


def test_readonly_rewrite_is_rejected_even_if_bytes_and_mtime_are_restored(native):
    runtime = native([descriptor("guide.txt", b"approved", False)])
    path = runtime.workspace / "guide.txt"
    path.write_bytes(b"approved")
    runtime.start()
    runtime.close()
    before = path.stat()
    path.write_bytes(b"tampered")
    path.write_bytes(b"approved")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(ValueError, match="PROJECTION_CAPTURE_READONLY_CHANGED"):
        runtime.capture_projection()


@pytest.mark.parametrize(
    "fault",
    [
        "extra",
        "extra-directory",
        "missing",
        "replace",
        "symlink",
        "hardlink",
        "parent",
        "root",
        "size",
    ],
)
def test_capture_rejects_changed_paths_links_and_outside_tree(native, tmp_path, fault):
    runtime = native([descriptor("src/task.py", b"approved")])
    path = runtime.workspace / "src/task.py"
    path.parent.mkdir()
    path.write_bytes(b"approved")
    runtime.start()
    assert runtime.close()["local_stop"] == "confirmed"
    if fault == "extra":
        (runtime.workspace / "outside.txt").write_bytes(b"outside")
    elif fault == "extra-directory":
        (runtime.workspace / "empty").mkdir()
    elif fault == "missing":
        path.unlink()
    elif fault == "replace":
        replacement = tmp_path / "replacement"
        replacement.write_bytes(b"approved")
        os.replace(replacement, path)
    elif fault == "symlink":
        path.unlink()
        outside = tmp_path / "outside"
        outside.write_bytes(b"approved")
        path.symlink_to(outside)
    elif fault == "hardlink":
        os.link(path, tmp_path / "shared")
    elif fault == "parent":
        path.parent.rename(runtime.workspace / "old-src")
        path.parent.mkdir()
        path.write_bytes(b"approved")
    elif fault == "root":
        runtime.workspace.rename(runtime.directory / "old-workspace")
        runtime.workspace.mkdir()
        path.parent.mkdir()
        path.write_bytes(b"approved")
    else:
        with path.open("r+b") as stream:
            stream.truncate(8 * 1024 * 1024 + 1)
    with pytest.raises(ValueError, match="PROJECTION_CAPTURE_"):
        runtime.capture_projection()


def test_total_capture_limit_cannot_be_split_across_files(native):
    projection = [descriptor(f"file-{index}", b"x") for index in range(9)]
    runtime = native(projection)
    for row in projection:
        (runtime.workspace / row["path"]).write_bytes(b"x")
    runtime.start()
    runtime.close()
    for row in projection:
        with (runtime.workspace / row["path"]).open("r+b") as stream:
            stream.truncate(8 * 1024 * 1024)
    with pytest.raises(ValueError, match="PROJECTION_CAPTURE_LIMIT_EXCEEDED"):
        runtime.capture_projection()


def test_unstarted_failed_start_and_implicit_fixture_never_produce_capture(native):
    runtime = native([descriptor("target", b"approved")])
    with pytest.raises(ValueError, match="PROJECTION_CAPTURE_NOT_STARTED"):
        runtime.capture_projection()
    (runtime.workspace / "target").write_bytes(b"changed")
    with pytest.raises(ValueError, match="WORKSPACE_PROJECTION_CONTENT_CHANGED"):
        runtime.start()
    assert runtime.close()["local_stop"] == "confirmed"
    with pytest.raises(ValueError, match="PROJECTION_CAPTURE_NOT_STARTED"):
        runtime.capture_projection()
    implicit = native(None)
    with pytest.raises(ValueError, match="EXPLICIT_PROJECTION_REQUIRED"):
        implicit.capture_projection()


def test_unknown_process_stop_receipt_does_not_authorize_capture(native, monkeypatch):
    runtime = native([descriptor("target", b"approved")])
    (runtime.workspace / "target").write_bytes(b"approved")
    runtime.start()
    processes = runtime.probe_lifecycle()["observed_processes"]

    def unavailable_wait(self, timeout=None):
        raise subprocess.TimeoutExpired("synthetic-unshare", timeout)

    # The real pidfd kill still runs. Only the OS wait/query receipts are unavailable.
    with monkeypatch.context() as fault:
        fault.setattr(subprocess.Popen, "wait", unavailable_wait)
        fault.setattr(subprocess.Popen, "poll", lambda self: None)
        assert runtime.close()["local_stop"] == "unknown"
    with pytest.raises(ValueError, match="PROJECTION_CAPTURE_STOP_UNCONFIRMED"):
        runtime.capture_projection()
    assert runtime.close()["local_stop"] == "unknown"
    assert processes


def test_unprojected_host_file_remains_start_compatible_but_capture_rejects_it(native):
    runtime = native([descriptor("target", b"approved")])
    (runtime.workspace / "target").write_bytes(b"approved")
    (runtime.workspace / "private.txt").write_bytes(b"synthetic-unprojected")
    runtime.start()
    assert runtime.request("GET", "/path")["directory"] == "/workspace"
    with pytest.raises(ValueError, match="PROJECTION_CAPTURE_TREE_MISMATCH"):
        runtime.capture_projection()
