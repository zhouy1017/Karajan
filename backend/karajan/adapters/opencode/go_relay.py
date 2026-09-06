"""Bounded Go diagnostic relay: the native runtime receives only a local capability.

This is a credential boundary, not OS isolation or a provider spending guarantee.
The upstream SSE is buffered in memory, checked, then forwarded; raw payloads,
credentials, headers and reasoning text are never included in receipt snapshots.
Only tests inject an HTTP client; the production destination is fixed here.
"""

from __future__ import annotations

import copy
import hmac
import json
import re
import secrets
import threading
import time
from collections.abc import Callable
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx

_UPSTREAM = "https://opencode.ai/zen/go/v1/chat/completions"
_MODEL = "glm-5.3-flash"
_REQUEST_LIMIT = 262_144
_RESPONSE_LIMIT = 1_048_576
_MAX_REQUESTS = 6
_TOOLS = frozenset({"read", "edit"})
_SESSION = re.compile(r"[A-Za-z0-9_-]{1,160}\Z")
_COST = re.compile(r"(?:0|[1-9][0-9]{0,15})(?:\.[0-9]{1,24})?(?:[eE]([+-]?[0-9]{1,3}))?\Z")


class _Rejected(Exception):
    def __init__(self, reason: str, status: int = 502) -> None:
        self.reason = reason
        self.status = status


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def _constant(_: str) -> None:
    raise ValueError("NONFINITE_JSON_NUMBER")


def _decode(raw: bytes | str) -> Any:
    return json.loads(raw, object_pairs_hook=_object, parse_constant=_constant)


def _usage(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _Rejected("INVALID_USAGE")
    result: dict[str, Any] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        if key in value:
            count = value[key]
            if type(count) is not int or not 0 <= count <= 2**63 - 1:
                raise _Rejected("INVALID_USAGE")
            result[key] = count
    for parent, child in (
        ("prompt_tokens_details", "cached_tokens"),
        ("completion_tokens_details", "reasoning_tokens"),
    ):
        if parent not in value or value[parent] is None:
            continue
        details = value[parent]
        if not isinstance(details, dict):
            raise _Rejected("INVALID_USAGE")
        if child in details:
            count = details[child]
            if type(count) is not int or not 0 <= count <= 2**63 - 1:
                raise _Rejected("INVALID_USAGE")
            result[parent] = {child: count}
    return result


def _cost_trailer(payload: str, secret: str) -> str:
    """Validate Go's one post-DONE cost frame without interpreting its unit."""
    try:
        trailer = _decode(payload)
    except (ValueError, RecursionError):
        raise _Rejected("DATA_AFTER_TERMINATOR") from None
    if _contains_secret(trailer, secret):
        raise _Rejected("UPSTREAM_CREDENTIAL_ECHO")
    if (
        not isinstance(trailer, dict)
        or set(trailer) != {"choices", "cost"}
        or trailer["choices"] != []
    ):
        raise _Rejected("DATA_AFTER_TERMINATOR")
    cost = trailer["cost"]
    if not isinstance(cost, str):
        raise _Rejected("INVALID_GO_COST_TRAILER")
    match = _COST.fullmatch(cost)
    if (
        match is None
        or (match[1] is not None and abs(int(match[1])) > 100)
        or Decimal(cost) > Decimal("1e12")
    ):
        raise _Rejected("INVALID_GO_COST_TRAILER")
    return cost


def _contains_secret(value: Any, secret: str) -> bool:
    if isinstance(value, str):
        return secret in value
    if isinstance(value, dict):
        return any(secret in key or _contains_secret(item, secret) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_secret(item, secret) for item in value)
    return False


def _text_channels(
    value: Any,
    channels: dict[tuple[str | int, ...], list[str]],
    path: tuple[str | int, ...] = (),
) -> None:
    """Keep deltas separate by field and tool-call index, only until validation."""
    if isinstance(value, str):
        channels.setdefault(path, []).append(value)
    elif isinstance(value, dict):
        for key, item in value.items():
            _text_channels(item, channels, (*path, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            if isinstance(item, dict) and type(item.get("index")) is int:
                index = item["index"]
            _text_channels(item, channels, (*path, index))


def _stream_facts(raw: bytes, secret: str) -> dict[str, Any]:
    """Accept the single-choice Chat Completions stream used by this diagnostic."""
    if secret.encode() in raw:
        raise _Rejected("UPSTREAM_CREDENTIAL_ECHO")
    try:
        decoded = raw.decode("utf-8")
    except UnicodeError:
        raise _Rejected("INVALID_SSE") from None
    normalized = decoded.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.endswith("\n\n"):
        raise _Rejected("INCOMPLETE_SSE")
    model_seen = False
    done = False
    cost: str | None = None
    finish: str | None = None
    usage: dict[str, Any] = {}
    names: dict[int, str] = {}
    channels: dict[tuple[str | int, ...], list[str]] = {}
    for event in normalized.split("\n\n"):
        data = []
        for line in event.split("\n"):
            if line.startswith("data:"):
                data.append(line[5:].removeprefix(" "))
            elif line and not line.startswith(":"):
                raise _Rejected("UNEXPECTED_SSE_FIELD")
        if not data:
            continue
        payload = "\n".join(data)
        if done:
            if cost is not None:
                raise _Rejected("DATA_AFTER_TERMINATOR")
            cost = _cost_trailer(payload, secret)
            continue
        if payload == "[DONE]":
            done = True
            continue
        try:
            chunk = _decode(payload)
        except (ValueError, RecursionError):
            raise _Rejected("INVALID_SSE_JSON") from None
        if not isinstance(chunk, dict) or "error" in chunk:
            raise _Rejected("UPSTREAM_ERROR_EVENT")
        if "cost" in chunk:
            raise _Rejected("INVALID_GO_COST_TRAILER")
        if _contains_secret(chunk, secret):
            raise _Rejected("UPSTREAM_CREDENTIAL_ECHO")
        _text_channels(chunk, channels)
        if "model" in chunk:
            if chunk["model"] != _MODEL:
                raise _Rejected("MODEL_MISMATCH")
            model_seen = True
        if chunk.get("usage") is not None:
            usage.update(_usage(chunk["usage"]))
        choices = chunk.get("choices", [])
        if not isinstance(choices, list) or len(choices) > 1:
            raise _Rejected("INVALID_CHOICES")
        for choice in choices:
            if not isinstance(choice, dict) or choice.get("index", 0) != 0:
                raise _Rejected("INVALID_CHOICES")
            if finish is not None:
                raise _Rejected("CHOICE_AFTER_FINISH")
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                raise _Rejected("INVALID_DELTA")
            calls = delta.get("tool_calls")
            if calls is not None:
                if not isinstance(calls, list):
                    raise _Rejected("INVALID_TOOL_CALLS")
                for call in calls:
                    if not isinstance(call, dict):
                        raise _Rejected("INVALID_TOOL_CALLS")
                    index = call.get("index")
                    function = call.get("function")
                    if (
                        type(index) is not int
                        or not 0 <= index < 16
                        or not isinstance(function, dict)
                    ):
                        raise _Rejected("INVALID_TOOL_CALLS")
                    name = function.get("name", "")
                    if not isinstance(name, str) or len(name) > 32:
                        raise _Rejected("INVALID_TOOL_NAME")
                    names[index] = names.get(index, "") + name
                    if len(names[index]) > 32:
                        raise _Rejected("INVALID_TOOL_NAME")
            final = choice.get("finish_reason")
            if final is not None:
                if final not in {"stop", "tool_calls"}:
                    raise _Rejected("UNSUCCESSFUL_FINISH")
                finish = final
    if not done or finish is None:
        raise _Rejected("INCOMPLETE_SSE")
    if not model_seen:
        raise _Rejected("MISSING_MODEL")
    for parts in channels.values():
        text = "".join(parts)
        if secret in text:
            raise _Rejected("UPSTREAM_CREDENTIAL_ECHO")
        # Tool arguments are themselves JSON strings, sometimes split over deltas.
        try:
            nested = _decode(text)
        except (ValueError, RecursionError):
            continue
        if _contains_secret(nested, secret):
            raise _Rejected("UPSTREAM_CREDENTIAL_ECHO")
    if any(name not in _TOOLS for name in names.values()):
        raise _Rejected("UNAPPROVED_TOOL")
    if (finish == "tool_calls") != bool(names):
        raise _Rejected("INCOMPLETE_TOOL_CALL")
    return {
        "reported_models": [_MODEL],
        "tool_names": sorted(set(names.values())),
        "usage": usage,
        "finish_reason": finish,
        "stream_terminated": True,
        "go_cost_trailer_seen": cost is not None,
        "provider_reported_cost": cost,
        "provider_reported_cost_unit": "unknown",
    }


def _client() -> httpx.Client:
    return httpx.Client(timeout=90, trust_env=False, follow_redirects=False)


class GoRelay:
    """One local diagnostic, at most six validated upstream send attempts.

    ``client_factory`` is a code-only seam for an HTTP fixture, never a CLI
    endpoint option. Receipt lists are detached snapshots, including while a
    request is pending. ``close`` reports unknown if an active handler remains.
    """

    def __init__(
        self,
        secret: str,
        canary: str,
        *,
        client_factory: Callable[[], httpx.Client] | None = None,
    ) -> None:
        if (
            not secret
            or len(secret) > 8192
            or "\r" in secret
            or "\n" in secret
            or not secret.isascii()
            or not canary
        ):
            raise ValueError("INVALID_RELAY_CREDENTIAL_OR_CANARY")
        self._secret = secret
        self._canary = canary
        self._capability = secrets.token_urlsafe(32)
        self._client_factory = client_factory or _client
        self._condition = threading.Condition()
        self._receipts: list[dict[str, Any]] = []
        self._clients: set[httpx.Client] = set()
        self._active = 0
        self._closing = False
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def capability(self) -> str:
        return self._capability

    @property
    def url(self) -> str:
        if self._server is None:
            raise RuntimeError("RELAY_NOT_STARTED")
        return f"http://127.0.0.1:{self._server.server_port}/v1"

    @property
    def receipts(self) -> list[dict[str, Any]]:
        with self._condition:
            return copy.deepcopy(self._receipts)

    def start(self) -> None:
        with self._condition:
            if self._server is not None or self._closing:
                raise RuntimeError("RELAY_ALREADY_STARTED_OR_CLOSED")
            owner = self

            class Server(ThreadingHTTPServer):
                def process_request(self, request: Any, client_address: Any) -> None:
                    # Count before spawning, including peers still sending headers.
                    with owner._condition:
                        owner._active += 1
                    try:
                        super().process_request(request, client_address)
                    except Exception:
                        with owner._condition:
                            owner._active -= 1
                            owner._condition.notify_all()
                        raise

                def process_request_thread(self, request: Any, client_address: Any) -> None:
                    try:
                        super().process_request_thread(request, client_address)
                    finally:
                        with owner._condition:
                            owner._active -= 1
                            owner._condition.notify_all()

            class Handler(BaseHTTPRequestHandler):
                def do_POST(self) -> None:
                    owner._handle(self)

                def do_GET(self) -> None:
                    owner._error(self, 405, "METHOD_NOT_ALLOWED")

                def log_message(self, format: str, *args: Any) -> None:
                    pass

                def setup(self) -> None:
                    super().setup()
                    self.connection.settimeout(5)

            self._server = Server(("127.0.0.1", 0), Handler)
            self._server.daemon_threads = True
            self._thread = threading.Thread(
                target=self._server.serve_forever,
                kwargs={"poll_interval": 0.05},
                daemon=True,
            )
            self._thread.start()

    @staticmethod
    def _error(handler: BaseHTTPRequestHandler, status: int, reason: str) -> None:
        content = json.dumps({"error": {"type": reason}}).encode()
        try:
            handler.send_response(status)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(content)))
            handler.send_header("Connection", "close")
            handler.end_headers()
            handler.wfile.write(content)
        except OSError:
            pass
        handler.close_connection = True

    def _read_request(self, handler: BaseHTTPRequestHandler) -> tuple[dict[str, Any], str]:
        if handler.path != "/v1/chat/completions":
            raise _Rejected("INVALID_PATH", 404)
        auth = handler.headers.get_all("Authorization", [])
        if len(auth) != 1 or not hmac.compare_digest(
            auth[0].encode(), f"Bearer {self._capability}".encode()
        ):
            raise _Rejected("INVALID_CAPABILITY", 403)
        lengths = handler.headers.get_all("Content-Length", [])
        if (
            handler.headers.get_all("Transfer-Encoding")
            or len(lengths) != 1
            or not re.fullmatch(r"[0-9]{1,9}", lengths[0])
        ):
            raise _Rejected("INVALID_BODY_LENGTH", 400)
        size = int(lengths[0])
        if not 1 <= size <= _REQUEST_LIMIT:
            raise _Rejected("REQUEST_TOO_LARGE", 413)
        sessions = handler.headers.get_all("x-opencode-session", [])
        if len(sessions) != 1 or _SESSION.fullmatch(sessions[0]) is None:
            raise _Rejected("INVALID_SESSION_HEADER", 400)
        raw = handler.rfile.read(size)
        if len(raw) != size:
            raise _Rejected("INCOMPLETE_REQUEST", 400)
        try:
            payload = _decode(raw)
        except (ValueError, UnicodeError, RecursionError):
            raise _Rejected("INVALID_JSON", 400) from None
        if not isinstance(payload, dict):
            raise _Rejected("INVALID_REQUEST_OBJECT", 400)
        if payload.get("model") != _MODEL:
            raise _Rejected("INVALID_MODEL", 422)
        if payload.get("stream") is not True:
            raise _Rejected("STREAM_REQUIRED", 422)
        tokens = payload.get("max_tokens")
        if type(tokens) is not int or not 1 <= tokens <= 4096:
            raise _Rejected("INVALID_MAX_TOKENS", 422)
        messages = payload.get("messages")
        if (
            not isinstance(messages, list)
            or not messages
            or any(not isinstance(message, dict) for message in messages)
        ):
            raise _Rejected("INVALID_MESSAGES", 422)
        return payload, sessions[0]

    def _handle(self, handler: BaseHTTPRequestHandler) -> None:
        receipt: dict[str, Any] | None = None
        client: httpx.Client | None = None
        try:
            payload, session = self._read_request(handler)
            tool_results = [
                message for message in payload["messages"] if message.get("role") == "tool"
            ]
            receipt = {
                "requested_model": _MODEL,
                "session_header_present": True,
                "tool_results_in_request": len(tool_results),
                "fixture_content_in_tool_result": any(
                    "def clamp" in json.dumps(message, ensure_ascii=False)
                    for message in tool_results
                ),
                "denied_canary_in_request": self._canary in json.dumps(payload, ensure_ascii=False),
                "requested_stream": True,
                "reported_models": [],
                "tool_names": [],
                "usage": {},
                "upstream_send_attempted": False,
                "upstream_status": None,
                "response_bytes": 0,
                "stream_terminated": False,
                "protocol_passed": False,
                "relay_completed": False,
                "reason_codes": [],
            }
            with self._condition:
                if self._closing:
                    raise _Rejected("RELAY_CLOSING", 503)
                if len(self._receipts) >= _MAX_REQUESTS:
                    raise _Rejected("REQUEST_LIMIT_REACHED", 429)
                receipt["sequence"] = len(self._receipts) + 1
                self._receipts.append(copy.deepcopy(receipt))
            if receipt["denied_canary_in_request"]:
                raise _Rejected("DENIED_CANARY_IN_REQUEST", 403)
            client = self._client_factory()
            with self._condition:
                if self._closing:
                    raise _Rejected("RELAY_CLOSING", 503)
                self._clients.add(client)
                receipt["upstream_send_attempted"] = True
                self._receipts[receipt["sequence"] - 1] = copy.deepcopy(receipt)
            with client.stream(
                "POST",
                _UPSTREAM,
                headers={
                    "Authorization": f"Bearer {self._secret}",
                    "User-Agent": "opencode/1.18.29 Karajan/0.1",
                    "x-opencode-session": session,
                    "Accept": "text/event-stream",
                    "Accept-Encoding": "identity",
                },
                json=payload,
                follow_redirects=False,
            ) as response:
                receipt["upstream_status"] = response.status_code
                if response.status_code != 200:
                    raise _Rejected("UPSTREAM_HTTP_ERROR")
                content_type = response.headers.get("content-type", "").split(";", 1)[0]
                if content_type.strip().lower() != "text/event-stream":
                    raise _Rejected("INVALID_UPSTREAM_CONTENT_TYPE")
                if response.headers.get("content-encoding", "identity") != "identity":
                    raise _Rejected("UNEXPECTED_CONTENT_ENCODING")
                content = bytearray()
                for chunk in response.iter_bytes(chunk_size=8192):
                    with self._condition:
                        if self._closing:
                            raise _Rejected("RELAY_CLOSING", 503)
                    receipt["response_bytes"] += len(chunk)
                    if receipt["response_bytes"] > _RESPONSE_LIMIT:
                        raise _Rejected("UPSTREAM_RESPONSE_TOO_LARGE")
                    content.extend(chunk)
                receipt.update(_stream_facts(bytes(content), self._secret))
                handler.send_response(200)
                handler.send_header("Content-Type", "text/event-stream")
                handler.send_header("Content-Length", str(len(content)))
                handler.send_header("Connection", "close")
                handler.end_headers()
                handler.wfile.write(content)
                receipt["protocol_passed"] = True
                receipt["relay_completed"] = True
                handler.close_connection = True
        except _Rejected as error:
            if receipt is not None:
                receipt["reason_codes"] = [error.reason]
            self._error(handler, error.status, error.reason)
        except Exception:
            if receipt is not None:
                receipt["reason_codes"] = ["RELAY_TRANSPORT_ERROR"]
            self._error(handler, 502, "RELAY_TRANSPORT_ERROR")
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    if receipt is not None:
                        receipt["protocol_passed"] = False
                        receipt["reason_codes"].append("CLIENT_CLOSE_FAILED")
            with self._condition:
                if client is not None:
                    self._clients.discard(client)
                if receipt is not None and "sequence" in receipt:
                    self._receipts[receipt["sequence"] - 1] = receipt

    def close(self) -> dict[str, Any]:
        errors: list[str] = []
        with self._condition:
            self._closing = True
            clients = list(self._clients)
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        for client in clients:
            try:
                client.close()
            except Exception:
                errors.append("CLIENT_CLOSE_FAILED")
        if self._thread is not None:
            self._thread.join(timeout=2)
            if self._thread.is_alive():
                errors.append("SERVER_THREAD_REMAINS")
        deadline = time.monotonic() + 2
        with self._condition:
            while self._active and time.monotonic() < deadline:
                self._condition.wait(timeout=max(0, deadline - time.monotonic()))
            if self._active:
                errors.append("ACTIVE_HANDLER_REMAINS")
        return {"status": "unknown" if errors else "closed", "errors": errors}
