"""Offline exchange only: a local chat gateway, existing ledger, and scripted peer.

The /infer envelope and flat fake CNY charge are ResourceBroker test protocol.
They are not a DeepSeek transport, token price, or proof of cash containment.
"""

import json
import socket
import threading
import time
import uuid
from dataclasses import asdict, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from karajan.contracts.probe import AttemptManifest
from karajan.resources import Price, Profile, ResourceBroker

from .protocol import OFFICIAL_ENDPOINT, ProtocolError, prepare_request
from .response import observe_response


class LocalExchange:
    def __init__(
        self,
        directory: Path,
        scenario: str,
        model: str,
        fixture: Path,
        fixture_text: str,
        manifest: AttemptManifest,
        profile_digest: str,
    ) -> None:
        self.scenario, self.model, self.fixture = scenario, model, fixture
        self.fixture_text, self.manifest = fixture_text, manifest
        self.capability = uuid.uuid4().hex
        self.binding_headers = {
            "x-karajan-attempt": manifest.id,
            "x-karajan-fence": str(manifest.fence),
            "x-karajan-profile-digest": profile_digest,
        }
        self.receipts: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []
        self.observations: list[dict[str, Any]] = []
        self.responses: dict[str, tuple[int, str, bytes]] = {}
        self.tool_output_observed = False
        self.lock = threading.Lock()
        self.provider = ThreadingHTTPServer(("127.0.0.1", 0), self._provider_handler())
        self.gateway = ThreadingHTTPServer(("127.0.0.1", 0), self._gateway_handler())
        self.threads: list[threading.Thread] = []
        self.broker = ResourceBroker(directory / "resources.sqlite")
        self.broker.configure_budget("CNY", "0.060000")
        price = Price(
            revision="synthetic-flat-cny-v1",
            currency="CNY",
            fixed_charge="0.010000",
            input_byte_rate="0",
            output_token_rate="0",
            covers_all_charges=True,
            valid_until=time.time() + 300,
        )
        if scenario == "price_expired":
            price = replace(price, valid_until=0)
        elif scenario == "unknown_charges":
            price = replace(price, covers_all_charges=False)
        elif scenario == "missing_output_price":
            price = replace(price, output_token_rate=None)
        self.broker.reserve_attempt(
            manifest.id,
            profile=Profile(
                id=manifest.profile_id,
                model=model,
                endpoint=f"http://127.0.0.1:{self.provider.server_port}/infer",
                price=price,
            ),
            amount="0" if scenario == "budget_zero" else "0.060000",
            authorization_id=manifest.authorization_ref,
            fence=manifest.fence,
            authorization_expires_at=time.time() + 300,
        )

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.gateway.server_port}/v1"

    def start(self) -> None:
        for peer in (self.provider, self.gateway):
            thread = threading.Thread(
                target=peer.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
            )
            thread.start()
            self.threads.append(thread)

    def close(self) -> None:
        for peer in (self.gateway, self.provider):
            peer.shutdown()
            peer.server_close()
        for thread in self.threads:
            thread.join(timeout=3)
        self.broker.recover()
        self.broker.finish_attempt(self.manifest.id)

    def _gateway_handler(self) -> type[BaseHTTPRequestHandler]:
        exchange = self

        class Gateway(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                pass

            def do_POST(self) -> None:
                self.connection.settimeout(5)
                payload = b"{}"
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if (
                        self.path != "/v1/chat/completions"
                        or not 0 < length <= 1_000_000
                        or self.headers.get("Authorization") != "Bearer " + exchange.capability
                        or any(
                            self.headers.get(k) != v for k, v in exchange.binding_headers.items()
                        )
                    ):
                        raise ProtocolError("TRANSPORT_BINDING_MISMATCH")
                    payload = self.rfile.read(length)
                    if exchange.scenario in {"missing_output_limit", "wire_model_drift"}:
                        body = json.loads(payload)
                        if exchange.scenario == "missing_output_limit":
                            body.pop("max_tokens", None)
                        else:
                            body["model"] = "other-model"
                        payload = json.dumps(body).encode()
                    prepared = prepare_request(payload, model=exchange.model, output_limit=256)
                except (ProtocolError, ValueError, OSError) as error:
                    reason = str(error) if isinstance(error, ProtocolError) else "REQUEST_INVALID"
                    with exchange.lock:
                        exchange.receipts.append(
                            {
                                "attempt_id": exchange.manifest.id,
                                "state": "rejected",
                                "call_id": None,
                                "reason_code": reason,
                                "request_shape": _shape(payload),
                            }
                        )
                    self.send_error(403, reason)
                    return
                receipt = exchange.broker.submit(
                    exchange.manifest.id,
                    fence=exchange.manifest.fence,
                    prompt=prepared.body.decode(),
                    max_output_tokens=prepared.max_output_tokens,
                )
                with exchange.lock:
                    exchange.receipts.append(
                        {
                            **asdict(receipt),
                            "attempt_id": exchange.manifest.id,
                            "fence": exchange.manifest.fence,
                            "profile_id": exchange.manifest.profile_id,
                            "profile_revision": exchange.manifest.profile_revision,
                            "logical_call_id": None,
                            "wire_body": json.loads(prepared.body),
                            "profile_digest": exchange.binding_headers["x-karajan-profile-digest"],
                        }
                    )
                    response = exchange.responses.get(receipt.call_id or "")
                if response is None:
                    # Unknown delivery cannot be represented as a successful model response.
                    self.send_error(403, receipt.reason_code or "LOCAL_RESPONSE_UNAVAILABLE")
                    return
                code, media, payload = response
                self.send_response(code)
                self.send_header("Content-Type", media)
                self.send_header("Content-Length", str(len(payload)))
                if code == 429:
                    self.send_header("Retry-After", "0.01")
                self.end_headers()
                self.wfile.write(payload)

        return Gateway

    def _provider_handler(self) -> type[BaseHTTPRequestHandler]:
        exchange = self

        class Provider(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                pass

            def do_POST(self) -> None:
                self.connection.settimeout(5)
                if self.path != "/infer":
                    self.send_error(404)
                    return
                envelope = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                body = json.loads(envelope["prompt"])
                call_id = envelope["call_id"]
                with exchange.lock:
                    first_request = not exchange.requests
                    exchange.requests.append(
                        {
                            "call_id": call_id,
                            "body": body,
                            "path": self.path,
                            "protocol_endpoint_identity": OFFICIAL_ENDPOINT,
                            "actual_destination": "local-scripted-provider",
                            "received_at": time.time(),
                        }
                    )
                if first_request and exchange.scenario == "disconnect_once":
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.close_connection = True
                    return
                if first_request and exchange.scenario == "rate_limit_once":
                    payload = b'{"error":{"message":"synthetic rate limit"}}'
                    with exchange.lock:
                        exchange.responses[call_id] = (429, "application/json", payload)
                        exchange.observations.append(
                            {
                                "call_id": call_id,
                                **asdict(
                                    observe_response(
                                        payload,
                                        model=exchange.model,
                                        status=429,
                                        content_type="application/json",
                                    )
                                ),
                            }
                        )
                    self.send_error(429)
                    return
                payload = exchange._stream(body, call_id)
                observation = observe_response(
                    payload, model=exchange.model, status=200, content_type="text/event-stream"
                )
                with exchange.lock:
                    exchange.observations.append({"call_id": call_id, **asdict(observation)})
                    if (
                        observation.status in {"completed", "tool_requested"}
                        and observation.usage_status == "observed"
                    ):
                        exchange.responses[call_id] = (200, "text/event-stream", payload)
                if call_id not in exchange.responses:
                    self.send_error(503, "LOCAL_RESPONSE_INCOMPLETE")
                    return
                reply = json.dumps(
                    {
                        "request_id": observation.request_id,
                        "usage_event_id": "fixture-usage:" + call_id,
                        "actual_charge": "0.010000",
                        "currency": "CNY",
                        "output": "synthetic envelope; response held by local exchange",
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)

        return Provider

    def _stream(self, body: dict[str, Any], call_id: str) -> bytes:
        tool_messages = [message for message in body["messages"] if message["role"] == "tool"]
        observed = any(self.fixture_text in json.dumps(message) for message in tool_messages)
        self.tool_output_observed |= observed
        if tool_messages:
            delta: dict[str, Any] = {
                "content": "fixture completed: " + self.fixture_text if observed else "tool failed"
            }
            finish = "stop"
        else:
            delta = {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "fixture-read",
                        "type": "function",
                        "function": {
                            "name": "read",
                            "arguments": json.dumps({"filePath": str(self.fixture)}),
                        },
                    }
                ]
            }
            finish = "tool_calls"
        chunks = []
        for content, reason in [({"role": "assistant"}, None), (delta, None), ({}, finish)]:
            chunks.append(
                "data: "
                + json.dumps(
                    {
                        "id": "fixture:" + call_id,
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": self.model,
                        "choices": [{"index": 0, "delta": content, "finish_reason": reason}],
                        "usage": {
                            "prompt_tokens": 10,
                            "completion_tokens": 5,
                            "total_tokens": 15,
                            "prompt_cache_hit_tokens": 4,
                            "prompt_cache_miss_tokens": 6,
                        }
                        if reason and self.scenario != "missing_usage"
                        else None,
                    }
                )
                + "\n\n"
            )
        return (
            "".join(chunks) + ("" if self.scenario == "missing_done" else "data: [DONE]\n\n")
        ).encode()


def _shape(payload: bytes) -> dict[str, Any]:
    """Structural diagnostics only, without message contents or transport credentials."""
    try:
        body = json.loads(payload)
        if not isinstance(body, dict):
            return {}
        return {
            "keys": list(body),
            "messages": [
                {
                    "keys": list(item),
                    "role": item.get("role"),
                    "content_type": type(item.get("content")).__name__,
                }
                for item in body.get("messages", [])
                if isinstance(item, dict)
            ],
        }
    except (ValueError, TypeError):
        return {}
