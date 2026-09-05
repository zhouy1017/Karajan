"""Two local HTTP peers: a bounded admission probe and a scripted provider."""

import http.client
import json
import socket
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from karajan.contracts.probe import AttemptManifest


class LocalTransport:
    """Synthetic peers only; no provider key, arbitrary upstream or cash ledger."""

    def __init__(
        self,
        fixture: Path,
        secret: str,
        scenario: str,
        manifest: AttemptManifest,
        profile_digest: str,
    ) -> None:
        self.fixture = fixture
        self.secret = secret
        self.scenario = scenario
        self.manifest = manifest
        self.profile_digest = profile_digest
        self.binding_headers = {
            "x-karajan-attempt": manifest.id,
            "x-karajan-fence": str(manifest.fence),
            "x-karajan-profile-digest": profile_digest,
        }
        self.capability = uuid.uuid4().hex
        self.receipts: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []
        self.tool_output_observed = False
        self.streaming = threading.Event()
        self.stopping = threading.Event()
        self._lock = threading.Lock()
        self.provider = ThreadingHTTPServer(("127.0.0.1", 0), self._provider_handler())
        self.broker = ThreadingHTTPServer(("127.0.0.1", 0), self._broker_handler())
        self._threads: list[threading.Thread] = []

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.broker.server_port}/v1"

    def start(self) -> None:
        for server in (self.provider, self.broker):
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self._threads.append(thread)

    def close(self) -> None:
        self.stopping.set()
        for server in (self.broker, self.provider):
            server.shutdown()
            server.server_close()
        for thread in self._threads:
            thread.join(timeout=2)

    def _provider_handler(self) -> type[BaseHTTPRequestHandler]:
        transport = self

        class Provider(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                pass

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with transport._lock:
                    fault = transport.scenario if not transport.requests else None
                    limited = fault == "rate_limit_once"
                    observation = {
                        "path": self.path,
                        "body": body,
                        "received_at": time.time(),
                        "response_status": 429 if limited else 200,
                        "fault": fault,
                    }
                    transport.requests.append(observation)
                if fault == "disconnect_once":
                    observation["response_status"] = None
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.close_connection = True
                    return
                if fault == "timeout_once":
                    time.sleep(1.5)
                    observation["response_status"] = None
                    self.close_connection = True
                    return
                if limited:
                    payload = json.dumps(
                        {"error": {"message": "fixture rate limit", "type": "rate_limit_error"}}
                    ).encode()
                    self.send_response(429)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.send_header("Retry-After", "0.01")
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                tool_messages = [item for item in body["messages"] if item["role"] == "tool"]
                observed = any(transport.secret in json.dumps(item) for item in tool_messages)
                transport.tool_output_observed |= observed
                delta: dict[str, Any]
                if tool_messages:
                    delta = {
                        "content": f"fixture completed: {transport.secret}"
                        if observed
                        else "tool failed"
                    }
                    finish = "stop"
                else:
                    delta = {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_fixture_read",
                                "type": "function",
                                "function": {
                                    "name": "read",
                                    "arguments": json.dumps({"filePath": str(transport.fixture)}),
                                },
                            }
                        ]
                    }
                    finish = "tool_calls"
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                if transport.scenario == "cancel_stream":
                    transport.streaming.set()
                    try:
                        deadline = time.monotonic() + 5
                        while not transport.stopping.is_set() and time.monotonic() < deadline:
                            self.wfile.write(b": fixture stream heartbeat\n\n")
                            self.wfile.flush()
                            time.sleep(0.05)
                    except OSError as error:
                        observation["peer_closed"] = type(error).__name__
                    self.close_connection = True
                    return
                for change, reason in (({"role": "assistant"}, None), (delta, None), ({}, finish)):
                    data = {
                        "id": "chatcmpl-fixture",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "fixture-model",
                        "choices": [{"index": 0, "delta": change, "finish_reason": reason}],
                    }
                    self.wfile.write(f"data: {json.dumps(data)}\n\n".encode())
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                self.close_connection = True

        return Provider

    def _broker_handler(self) -> type[BaseHTTPRequestHandler]:
        transport = self

        class Broker(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                payload = self.rfile.read(length) if 0 < length <= 1_000_000 else b"{}"
                try:
                    body = json.loads(payload)
                except ValueError:
                    body = {}
                admitted = (
                    self.path == "/v1/chat/completions"
                    and self.headers.get("Authorization") == f"Bearer {transport.capability}"
                    and body.get("model") == "fixture-model"
                    and 0 < length <= 1_000_000
                    and all(
                        self.headers.get(key) == value
                        for key, value in transport.binding_headers.items()
                    )
                    and type(body.get("max_tokens")) is int
                    and 0 < body["max_tokens"] <= 256
                    and body.get("stream") is True
                    and all(
                        tool.get("function", {}).get("name") == "read"
                        for tool in body.get("tools", [])
                    )
                )
                with transport._lock:
                    limit = 1 if transport.scenario == "admission_limit" else 6
                    reason = "FIXTURE_PROFILE_MISMATCH" if not admitted else None
                    if len(transport.receipts) >= limit:
                        admitted = False
                        reason = "FIXTURE_CALL_LIMIT"
                    receipt = {
                        "attempt_id": transport.manifest.id,
                        "fence": transport.manifest.fence,
                        "profile_id": transport.manifest.profile_id,
                        "profile_revision": transport.manifest.profile_revision,
                        "profile_digest": transport.profile_digest,
                        "receipt_id": str(uuid.uuid4()),
                        "logical_call_id": None,
                        "deduplication": "new_call_per_receipt",
                        "admitted": admitted,
                        "rejection_reason": reason,
                        "path": self.path,
                        "received_at": time.time(),
                        "headers": {
                            key.lower(): value
                            for key, value in self.headers.items()
                            if key.lower() != "authorization"
                        },
                        "body": body,
                    }
                    transport.receipts.append(receipt)
                if not admitted:
                    receipt["response_status"] = 403
                    self.send_error(403, reason)
                    return
                upstream = http.client.HTTPConnection(
                    "127.0.0.1", transport.provider.server_port, timeout=5
                )
                try:
                    upstream.request(
                        "POST",
                        "/v1/chat/completions",
                        payload,
                        {"Content-Type": "application/json"},
                    )
                    response = upstream.getresponse()
                    receipt["response_status"] = response.status
                    self.send_response(response.status)
                    self.send_header(
                        "Content-Type", response.getheader("Content-Type", "application/json")
                    )
                    self.send_header("Connection", "close")
                    if retry_after := response.getheader("Retry-After"):
                        self.send_header("Retry-After", retry_after)
                    self.end_headers()
                    while line := response.readline():
                        self.wfile.write(line)
                        self.wfile.flush()
                except (OSError, http.client.HTTPException) as error:
                    receipt["transport_error"] = type(error).__name__
                finally:
                    upstream.close()
                    self.close_connection = True

        return Broker
