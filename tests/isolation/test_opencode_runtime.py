"""Public fixed-runtime lifecycle checks; real Linux namespaces, no provider."""

import errno
import json
import os
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from unittest.mock import patch


@unittest.skipUnless(sys.platform == "linux", "Linux namespaces are required")
class IsolatedOpenCodeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = Path(
            os.environ.get(
                "KARAJAN_OPENCODE_LINUX_BINARY",
                str(
                    Path(__file__).resolve().parents[2]
                    / "runtimes/opencode/node_modules/opencode-linux-x64/bin/opencode"
                ),
            )
        )
        if not self.runtime.is_file():
            if os.environ.get("KARAJAN_REQUIRE_OPENCODE_ISOLATION") == "1":
                self.fail("Prepared fixed Linux OpenCode artifact is required")
            self.skipTest("Prepared Linux OpenCode artifact is not available")
        self.temporary = tempfile.TemporaryDirectory(prefix="karajan-opencode-runtime-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.upstream = self.root / "upstream.sock"
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(self.upstream))
        self.listener.listen()
        self.addCleanup(self.listener.close)

    def test_fixed_binary_version_is_observed_in_new_namespaces_and_stops(self) -> None:
        from karajan.isolation.opencode_runtime import IsolatedOpenCode

        runtime = IsolatedOpenCode(
            self.runtime, self.root / "run", self.upstream, "synthetic-local-capability"
        )
        self.addCleanup(runtime.close)
        snapshot = runtime.start()
        self.assertEqual(snapshot["runtime_version"], "1.18.29")
        self.assertEqual(snapshot["runtime_tools_status"], "not_run")
        self.assertFalse(snapshot["dispatch_eligible"])
        self.assertEqual(snapshot["namespace_pid"], 1)
        for name in ("user", "mnt", "pid", "net"):
            self.assertNotEqual(snapshot["namespaces"][name], os.readlink(f"/proc/self/ns/{name}"))
        self.assertFalse(snapshot["host_mount_visible"])
        self.assertFalse(snapshot["wsl_interop_visible"])
        self.assertNotIn("synthetic-local-capability", str(snapshot))
        closed = runtime.close()
        self.assertEqual(closed["local_stop"], "confirmed")
        self.assertEqual(closed["remote_stop"], "unknown")
        self.assertEqual(runtime.close(), closed)

    def test_private_management_reads_fixed_native_configuration(self) -> None:
        from karajan.isolation.opencode_runtime import IsolatedOpenCode

        runtime = IsolatedOpenCode(
            self.runtime, self.root / "run", self.upstream, "synthetic-local-capability"
        )
        self.addCleanup(runtime.close)
        runtime.start()
        config = runtime.request("GET", "/config")
        self.assertEqual(config["model"], "opencode-go/glm-5.3-flash")
        self.assertEqual(
            config["permission"]["read"], {"*": "deny", "workspace/fixture.py": "allow"}
        )
        self.assertEqual(
            config["permission"]["edit"], {"*": "deny", "workspace/fixture.py": "allow"}
        )
        self.assertEqual(config["permission"]["*"], "deny")
        for disabled in ("lsp", "formatter", "snapshot", "autoupdate"):
            self.assertFalse(config[disabled])
        self.assertEqual(config["plugin"], [])
        self.assertEqual(config["mcp"], {})
        self.assertEqual(runtime.request("GET", "/path")["directory"], "/workspace")
        with self.assertRaisesRegex(ValueError, "MANAGEMENT_REQUEST_NOT_ALLOWED"):
            runtime.request("POST", "/config", {"permission": "allow"})

    def test_private_management_rejects_body_authority_and_unissued_session_ids(self) -> None:
        from karajan.isolation.opencode_runtime import IsolatedOpenCode

        runtime = IsolatedOpenCode(
            self.runtime, self.root / "run", self.upstream, "synthetic-local-capability"
        )
        self.addCleanup(runtime.close)
        runtime.start()
        session = runtime.request(
            "POST", "/session", {"title": "Restricted session", "agent": "probe"}
        )
        for method, path, body in (
            ("GET", "/config", {}),
            (
                "POST",
                "/session",
                {
                    "title": "escape",
                    "permission": [{"permission": "*", "action": "allow", "pattern": "*"}],
                },
            ),
            ("POST", "/session", {"agent": "build"}),
            ("GET", "/session/ses_unissued/message", None),
            (
                "POST",
                f"/session/{session['id']}/prompt_async",
                {"agent": "probe", "parts": [{"type": "file", "url": "file:///proc/self/environ"}]},
            ),
            (
                "POST",
                f"/session/{session['id']}/prompt_async",
                {
                    "agent": "probe",
                    "model": {"providerID": "other", "modelID": "other"},
                    "parts": [{"type": "text", "text": "hello"}],
                },
            ),
        ):
            with self.subTest(path=path, body=body), self.assertRaises(ValueError):
                runtime.request(method, path, body)

    def test_close_observes_detached_setsid_subtree_stop_and_revokes_control(self) -> None:
        from karajan.isolation.opencode_runtime import IsolatedOpenCode

        runtime = IsolatedOpenCode(
            self.runtime, self.root / "run", self.upstream, "synthetic-local-capability"
        )
        self.addCleanup(runtime.close)
        runtime.start()
        probe = runtime.probe_lifecycle()
        self.assertTrue(probe["heartbeat_growing"])
        self.assertEqual(probe["leader_pid"], probe["leader_sid"])
        self.assertEqual(probe["child_pid"], probe["child_sid"])
        self.assertNotEqual(probe["child_sid"], probe["leader_sid"])
        self.assertGreaterEqual(len(probe["observed_processes"]), 4)
        closed = runtime.close()
        self.assertEqual(closed["local_stop"], "confirmed")
        self.assertTrue(closed["namespace_init_stopped"])
        self.assertEqual(closed["observed_processes_still_running"], [])
        self.assertEqual(closed["remote_stop"], "unknown")
        self.assertFalse(closed["dispatch_eligible"])
        for process in probe["observed_processes"]:
            path = Path(f"/proc/{process['pid']}/stat")
            if path.exists():
                fields = path.read_text().rsplit(")", 1)[1].split()
                self.assertTrue(fields[0] == "Z" or fields[19] != process["birth"])
        with self.assertRaisesRegex(ValueError, "RUNTIME_NOT_RUNNING"):
            runtime.request("GET", "/global/health")

    def test_namespace_reports_native_control_fd_absence_and_network_confinement(self) -> None:
        from karajan.isolation.opencode_runtime import IsolatedOpenCode

        runtime = IsolatedOpenCode(
            self.runtime, self.root / "run", self.upstream, "synthetic-local-capability"
        )
        self.addCleanup(runtime.close)
        snapshot = runtime.start()
        self.assertFalse(snapshot["native_control_fd_inherited"])
        self.assertEqual(snapshot["network_interfaces"], ["lo"])
        self.assertEqual(snapshot["ipv4_routes"], [])
        self.assertEqual(
            snapshot["capabilities"],
            {
                "effective": "0000000000000000",
                "permitted": "0000000000000000",
                "bounding": "0000000000000000",
            },
        )
        self.assertTrue(snapshot["no_new_privileges"])

    def test_start_rejects_fixture_symlinks_and_host_hardlinks(self) -> None:
        from karajan.isolation.opencode_runtime import IsolatedOpenCode

        outside = self.root / "outside.txt"
        outside.write_text("SYNTHETIC_OUTSIDE")
        for kind in ("symlink", "hardlink"):
            with self.subTest(kind=kind):
                runtime = IsolatedOpenCode(
                    self.runtime, self.root / kind, self.upstream, "synthetic-local-capability"
                )
                self.addCleanup(runtime.close)
                fixture = runtime.workspace / "fixture.py"
                if kind == "symlink":
                    fixture.symlink_to("/proc/self/environ")
                else:
                    os.link(outside, fixture)
                with self.assertRaisesRegex(ValueError, "FIXTURE_MUST_BE_PRIVATE_REGULAR_FILE"):
                    runtime.start()
        self.assertEqual(outside.read_text(), "SYNTHETIC_OUTSIDE")

    def test_lost_startup_response_does_not_claim_unobserved_tree_stopped(self) -> None:
        from karajan.isolation.opencode_runtime import IsolatedOpenCode

        socketpair = socket.socketpair

        class LostResponse:
            def __init__(self, peer):
                self.peer = peer

            def __getattr__(self, name):
                return getattr(self.peer, name)

            def recv(self, size):
                self.peer.recv(size)
                raise OSError("SYNTHETIC_STARTUP_RESPONSE_LOSS")

        def lose_response():
            outer, inner = socketpair()
            return LostResponse(outer), inner

        runtime = IsolatedOpenCode(
            self.runtime, self.root / "run", self.upstream, "synthetic-local-capability"
        )
        self.addCleanup(runtime.close)
        with (
            patch("socket.socketpair", side_effect=lose_response),
            self.assertRaisesRegex(OSError, "SYNTHETIC_STARTUP_RESPONSE_LOSS"),
        ):
            runtime.start()
        closed = runtime.close()
        self.assertEqual(closed["local_stop"], "unknown")
        self.assertIsNone(closed["namespace_init_stopped"])
        self.assertEqual(closed["remote_stop"], "unknown")

    def test_close_before_spawn_returns_promptly_and_prevents_late_process_creation(self) -> None:
        from karajan.isolation.opencode_runtime import IsolatedOpenCode

        runtime = IsolatedOpenCode(
            self.runtime, self.root / "run", self.upstream, "synthetic-local-capability"
        )
        self.addCleanup(runtime.close)
        socketpair, popen = socket.socketpair, subprocess.Popen
        ready, release = threading.Event(), threading.Event()
        launches = []

        def paused_pair():
            pair = socketpair()
            ready.set()
            release.wait(timeout=5)
            return pair

        def observed_spawn(*args, **kwargs):
            process = popen(*args, **kwargs)
            launches.append(process)
            return process

        try:
            with (
                patch("socket.socketpair", side_effect=paused_pair),
                patch("subprocess.Popen", side_effect=observed_spawn),
                ThreadPoolExecutor(max_workers=1) as pool,
            ):
                future = pool.submit(runtime.start)
                try:
                    self.assertTrue(ready.wait(timeout=3))
                    before = time.monotonic()
                    closed = runtime.close()
                    self.assertLess(time.monotonic() - before, 1)
                    self.assertEqual(closed["local_stop"], "confirmed")
                finally:
                    release.set()
                with self.assertRaisesRegex(ValueError, "RUNTIME_START_CANCELLED"):
                    future.result(timeout=5)
            self.assertEqual(len(launches), 0)
            self.assertEqual(runtime.snapshot()["state"], "closed")
        finally:
            for process in launches:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)

    def test_artifact_atomic_replace_after_start_check_is_rejected_at_bound_inode(self) -> None:
        from karajan.isolation.opencode_runtime import IsolatedOpenCode

        artifact, replacement = self.root / "opencode", self.root / "replacement"
        shutil.copy2(self.runtime, artifact)
        shutil.copy2(self.runtime, replacement)
        with replacement.open("ab") as stream:
            stream.write(b"SYNTHETIC_PRIVATE_REPLACEMENT")
        runtime = IsolatedOpenCode(
            artifact, self.root / "run", self.upstream, "synthetic-local-capability"
        )
        self.addCleanup(runtime.close)
        popen = subprocess.Popen

        def replace_then_spawn(*args, **kwargs):
            os.replace(replacement, artifact)
            return popen(*args, **kwargs)

        with patch("subprocess.Popen", side_effect=replace_then_spawn), self.assertRaises(OSError):
            runtime.start()
        self.assertIn(
            "RUNTIME_ARTIFACT_MISMATCH", (runtime.directory / "namespace.log").read_text()
        )
        self.assertEqual(runtime.snapshot()["state"], "closed")
        self.assertFalse(runtime.close()["dispatch_eligible"])

    def test_close_does_not_wait_for_the_native_startup_response(self) -> None:
        from karajan.isolation.opencode_runtime import IsolatedOpenCode

        runtime = IsolatedOpenCode(
            self.runtime, self.root / "run", self.upstream, "synthetic-local-capability"
        )
        self.addCleanup(runtime.close)
        socketpair = socket.socketpair
        ready, release = threading.Event(), threading.Event()

        class PausedWelcome:
            def __init__(self, peer):
                self.peer = peer
                self.first = True

            def __getattr__(self, name):
                return getattr(self.peer, name)

            def recv(self, size):
                block = self.peer.recv(size)
                if self.first:
                    self.first = False
                    ready.set()
                    release.wait(timeout=5)
                return block

        def paused_pair():
            outer, inner = socketpair()
            return PausedWelcome(outer), inner

        with (
            patch("socket.socketpair", side_effect=paused_pair),
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            future = pool.submit(runtime.start)
            try:
                self.assertTrue(ready.wait(timeout=20))
                before = time.monotonic()
                closed = runtime.close()
                self.assertLess(time.monotonic() - before, 1)
                self.assertEqual(closed["local_stop"], "unknown")
            finally:
                release.set()
            with self.assertRaises((OSError, ValueError)):
                future.result(timeout=5)
        self.assertEqual(runtime.snapshot()["state"], "closed")

    def test_close_handles_exited_process_read_race_and_retains_immutable_evidence(self) -> None:
        from karajan.isolation.opencode_runtime import IsolatedOpenCode

        runtime = IsolatedOpenCode(
            self.runtime, self.root / "run", self.upstream, "synthetic-local-capability"
        )
        self.addCleanup(runtime.close)
        runtime.start()
        runtime.probe_lifecycle()
        read_text = Path.read_text
        exit_races = []

        def exited_process_race(path, *args, **kwargs):
            process_stat = str(path).startswith("/proc/") and path.name == "stat"
            try:
                result = read_text(path, *args, **kwargs)
            except FileNotFoundError:
                if not process_stat:
                    raise
                exit_races.append(str(path))
                raise ProcessLookupError(
                    errno.ESRCH, "synthetic exited-process read race"
                ) from None
            if process_stat and result.rsplit(")", 1)[1].split()[0] == "Z":
                exit_races.append(str(path))
                raise ProcessLookupError(errno.ESRCH, "synthetic exited-process read race")
            return result

        with patch.object(Path, "read_text", new=exited_process_race):
            closed = runtime.close()
        self.assertTrue(exit_races)
        self.assertEqual(closed["local_stop"], "confirmed")
        expected_count = len(closed["observed_processes"])
        self.assertGreaterEqual(expected_count, 4)
        closed["observed_processes"].clear()
        self.assertEqual(len(runtime.close()["observed_processes"]), expected_count)

    def test_only_the_fixed_fixture_is_projected_into_the_namespace_workspace(self) -> None:
        from karajan.isolation.opencode_runtime import IsolatedOpenCode

        runtime = IsolatedOpenCode(
            self.runtime, self.root / "run", self.upstream, "synthetic-local-capability"
        )
        self.addCleanup(runtime.close)
        (runtime.workspace / "fixture.py").write_text("PUBLIC_FIXTURE")
        (runtime.workspace / "blocked.txt").write_text("SYNTHETIC_HOST_ONLY")
        (runtime.workspace / "alias.py").symlink_to("blocked.txt")
        runtime.start()
        processes = runtime.probe_lifecycle()["observed_processes"]
        init = next(process for process in processes if process["namespace_pid"] == 1)
        projected = Path(f"/proc/{init['pid']}/root/workspace")
        self.assertEqual(sorted(path.name for path in projected.iterdir()), ["fixture.py"])
        self.assertEqual((projected / "fixture.py").read_text(), "PUBLIC_FIXTURE")
        self.assertFalse((projected / "blocked.txt").exists())
        self.assertFalse((projected / "alias.py").is_symlink())
        self.assertEqual((runtime.workspace / "blocked.txt").read_text(), "SYNTHETIC_HOST_ONLY")

    def test_native_read_edit_reaches_only_the_fixed_unix_inference_peer(self) -> None:
        from karajan.isolation.opencode_runtime import IsolatedOpenCode

        self.listener.close()
        self.upstream.unlink()
        requests = []

        class Provider(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                index = len(requests)
                delta = {"content": "done"}
                finish = "stop"
                if index <= 2:
                    arguments = {"filePath": "/workspace/fixture.py"}
                    if index == 2:
                        arguments.update(
                            oldString="return value", newString="return min(high, max(low, value))"
                        )
                    delta = {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_" + str(index),
                                "type": "function",
                                "function": {
                                    "name": "read" if index == 1 else "edit",
                                    "arguments": json.dumps(arguments),
                                },
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
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                self.close_connection = True

        upstream = socketserver.UnixStreamServer(str(self.upstream), Provider)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(upstream.server_close)
        self.addCleanup(upstream.shutdown)
        runtime = IsolatedOpenCode(
            self.runtime, self.root / "run", self.upstream, "synthetic-local-capability"
        )
        self.addCleanup(runtime.close)
        fixture = runtime.workspace / "fixture.py"
        fixture.write_text("def clamp(value, low, high):\n    return value\n")
        runtime.start()
        session = runtime.request(
            "POST", "/session", {"title": "Fixed native tools", "agent": "probe"}
        )
        runtime.request(
            "POST",
            f"/session/{session['id']}/prompt_async",
            {
                "agent": "probe",
                "model": {"providerID": "opencode-go", "modelID": "glm-5.3-flash"},
                "parts": [
                    {"type": "text", "text": "Read fixture.py and edit its return statement."}
                ],
            },
        )
        until = time.monotonic() + 20
        while time.monotonic() < until:
            messages = runtime.request("GET", f"/session/{session['id']}/message")
            if len(requests) >= 3:
                break
            time.sleep(0.1)
        self.assertEqual(
            fixture.read_text(),
            "def clamp(value, low, high):\n    return min(high, max(low, value))\n",
            str(messages),
        )
        self.assertGreaterEqual(len(requests), 3)
        for request in requests:
            self.assertEqual(request["path"], "/v1/chat/completions")
            headers = {key.lower(): value for key, value in request["headers"].items()}
            self.assertEqual(headers["authorization"], "Bearer synthetic-local-capability")
            self.assertEqual(headers["x-opencode-session"], session["id"])

    def test_native_read_rejects_proc_control_configuration_and_nonfixture(self) -> None:
        from karajan.isolation.opencode_runtime import IsolatedOpenCode

        self.listener.close()
        self.upstream.unlink()
        paths = [
            "/proc/self/environ",
            "/proc/1/fd",
            "/control/inner.py",
            "/tmp/config",
            "/tmp/data",
            "/workspace/blocked.txt",
        ]
        requests = []

        class Provider(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(body)
                delta = {"content": "done"}
                finish = "stop"
                if len(requests) == 1:
                    delta = {
                        "tool_calls": [
                            {
                                "index": index,
                                "id": f"deny_{index}",
                                "type": "function",
                                "function": {
                                    "name": "read",
                                    "arguments": json.dumps({"filePath": path}),
                                },
                            }
                            for index, path in enumerate(paths)
                        ]
                    }
                    finish = "tool_calls"
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                for change, reason in (({"role": "assistant"}, None), (delta, None), ({}, finish)):
                    event = {
                        "id": "chatcmpl-denial",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "glm-5.3-flash",
                        "choices": [{"index": 0, "delta": change, "finish_reason": reason}],
                    }
                    self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                self.close_connection = True

        upstream = socketserver.UnixStreamServer(str(self.upstream), Provider)
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        self.addCleanup(upstream.server_close)
        self.addCleanup(upstream.shutdown)
        runtime = IsolatedOpenCode(
            self.runtime, self.root / "run", self.upstream, "synthetic-local-capability"
        )
        self.addCleanup(runtime.close)
        (runtime.workspace / "blocked.txt").write_text("SYNTHETIC_SECRET_MUST_NOT_APPEAR")
        runtime.start()
        session = runtime.request("POST", "/session", {"title": "Native denials", "agent": "probe"})
        runtime.request(
            "POST",
            f"/session/{session['id']}/prompt_async",
            {
                "agent": "probe",
                "model": {"providerID": "opencode-go", "modelID": "glm-5.3-flash"},
                "parts": [{"type": "text", "text": "Try each requested read once."}],
            },
        )
        until = time.monotonic() + 20
        messages = []
        while time.monotonic() < until:
            messages = runtime.request("GET", f"/session/{session['id']}/message")
            states = [
                part["state"]
                for message in messages
                for part in message["parts"]
                if part["type"] == "tool"
            ]
            if len(states) == len(paths) and all(state["status"] == "error" for state in states):
                break
            time.sleep(0.1)
        self.assertEqual(len(states), len(paths), str(messages))
        for state in states:
            self.assertEqual(state["status"], "error", str(state))
            self.assertTrue(state["error"].startswith("The user has specified a rule"), str(state))
        self.assertNotIn("SYNTHETIC_SECRET_MUST_NOT_APPEAR", json.dumps(messages))
        self.assertNotIn("SYNTHETIC_SECRET_MUST_NOT_APPEAR", json.dumps(requests))


if __name__ == "__main__":
    unittest.main()
