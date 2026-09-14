"""Controlled local upstream and owner session helpers for gateway HTTP tests.

No test in this package contacts a real provider, reads a provider credential or
performs inference. The upstream is a loopback HTTP server started per test that
records exactly what it received.
"""

import json
import socket
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

ORIGIN = "http://127.0.0.1:8765"
SENTINEL = "FAKE_SECRET_SENTINEL"


@contextmanager
def upstream(
    handler: type[BaseHTTPRequestHandler], *, host: str = "127.0.0.1"
) -> Iterator[ThreadingHTTPServer]:
    """Serve one controlled upstream, binding the requested address family.

    ``ThreadingHTTPServer`` defaults to IPv4, so an IPv6 literal needs an explicit
    ``address_family``; otherwise the family's own ``getaddrinfo`` fails and the
    test would report a helper error instead of exercising the product path.
    """
    server = ThreadingHTTPServer((host, 0), handler, bind_and_activate=False)
    try:
        if ":" in host:
            server.address_family = socket.AF_INET6
            server.socket = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            server.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        else:
            server.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.server_bind()
        server.server_activate()
    except OSError:
        server.server_close()
        raise
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
    thread.daemon = True
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def base_url(server: ThreadingHTTPServer) -> str:
    host, port = server.server_address[0], server.server_address[1]
    return f"http://[{host}]:{port}" if ":" in host else f"http://{host}:{port}"


class RecordingHandler(BaseHTTPRequestHandler):
    """Answer one registered discovery path and record every request line."""

    catalog: dict[str, Any] = {"object": "list", "data": [{"id": "vendor-model-a"}]}
    status = 200
    redirect_to: str | None = None
    body_override: bytes | None = None

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        self.server.received.append(  # type: ignore[attr-defined]
            {"path": self.path, "headers": dict(self.headers.items())}
        )
        if self.redirect_to is not None:
            self.send_response(302)
            self.send_header("Location", self.redirect_to)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.status != 200:
            self.send_response(self.status)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        data = self.body_override
        if data is None:
            data = json.dumps(self.catalog).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        """Record and refuse: this slice must never issue a generation request."""
        length = int(self.headers.get("Content-Length", "0"))
        if length:
            self.rfile.read(min(length, 65536))
        self.server.received.append(  # type: ignore[attr-defined]
            {"path": self.path, "method": "POST"}
        )
        self.send_response(500)
        self.send_header("Content-Length", "0")
        self.end_headers()


def recording_server(host: str = "127.0.0.1") -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, 0), RecordingHandler)
    server.received = []  # type: ignore[attr-defined]
    return server


def make_repository(tmp_path: Path, name: str = "fixture") -> Path:
    repository = tmp_path / "repositories" / name
    repository.mkdir(parents=True)
    (repository / "fixture.txt").write_text("fixture\n", encoding="utf-8")
    for arguments in (
        ["init", "--initial-branch=main", str(repository)],
        ["-C", str(repository), "add", "fixture.txt"],
        [
            "-C",
            str(repository),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
    ):
        subprocess.run(["git", *arguments], check=True, capture_output=True)
    return repository


def connection_payload(server: ThreadingHTTPServer, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "connection_id": "local-gateway",
        "base_url": base_url(server),
        "protocol": {"family": "openai_compatible", "protocol_version": "v1"},
        "secret_ref": None,
        "request_transformation": {
            "policy_id": "no-transform",
            "revision": 1,
            "payload_override": False,
            "prompt_rewrite": False,
            "managed_tools_injected": False,
        },
    }
    payload.update(overrides)
    return payload


def binding_payload(connection_revision: int = 1, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "binding_id": "vendor-binding",
        "connection_id": "local-gateway",
        "connection_revision": connection_revision,
        "model_alias": "vendor-model-a",
        "declared": {
            "provider_id": "vendor",
            "account_id": "vendor-account",
            "billing_path": "subscription_only",
            "required_parameters": {"temperature": 0.2},
        },
        "request_transformation": {
            "policy_id": "no-transform",
            "revision": 1,
            "payload_override": False,
            "prompt_rewrite": False,
            "managed_tools_injected": False,
        },
    }
    payload.update(overrides)
    return payload


def login(client: TestClient, token: str = "bootstrap") -> dict[str, str]:
    response = client.post(
        "/v1/session/bootstrap", json={"token": token}, headers={"Origin": ORIGIN}
    )
    assert response.status_code == 200, response.text
    return {"Origin": ORIGIN, "X-CSRF-Token": response.json()["csrf_token"]}
