"""Exercise the actual local HTTP process and its bootstrap file."""

import http.client
import json
import os
import socket
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path


def test_server_creates_private_code_and_serves_real_authenticated_http(tmp_path: Path) -> None:
    state = tmp_path / "control"
    with socket.socket() as available:
        available.bind(("127.0.0.1", 0))
        port = available.getsockname()[1]
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "karajan.web",
            "serve",
            "--state-directory",
            str(state),
            "--port",
            str(port),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    token = ""
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                with closing(http.client.HTTPConnection("127.0.0.1", port, timeout=1)) as client:
                    client.request("GET", "/health")
                    if client.getresponse().status == 200:
                        break
            except OSError:
                if process.poll() is not None:
                    break
                time.sleep(0.05)
        else:
            raise AssertionError("Server readiness timed out")
        files = list(state.glob("bootstrap-*.txt"))
        assert len(files) == 1
        token = files[0].read_text().strip()
        assert len(token) >= 32
        if os.name != "nt":
            assert files[0].stat().st_mode & 0o077 == 0
        with closing(http.client.HTTPConnection("127.0.0.1", port, timeout=2)) as client:
            client.request(
                "POST",
                "/v1/session/bootstrap",
                json.dumps({"token": token}),
                {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}"},
            )
            response = client.getresponse()
            assert response.status == 200
            assert response.getheader("Set-Cookie")
            assert json.loads(response.read())["csrf_token"]
    finally:
        process.terminate()
        stdout, stderr = process.communicate(timeout=5)
        if token:
            assert token.encode() not in stdout + stderr
