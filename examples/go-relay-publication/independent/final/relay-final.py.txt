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
import socket
import stat
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

import httpx
from pydantic import ValidationError

from .go_journal import GoCallJournal, GoJournalError, GoQualificationLimits

if TYPE_CHECKING:
    from .go_context import GoRequestAccounting

_UPSTREAM = "https://opencode.ai/zen/go/v1/chat/completions"
_MODEL = "glm-5.3-flash"
_REQUEST_LIMIT = 262_144
_RESPONSE_LIMIT = 1_048_576
_MAX_REQUESTS = 6
_TOOLS = frozenset({"read", "edit"})
_SESSION = re.compile(r"[A-Za-z0-9_-]{1,160}\Z")
_COST = re.compile(r"(?:0|[1-9][0-9]{0,15})(?:\.[0-9]{1,24})?(?:[eE]([+-]?[0-9]{1,3}))?\Z")
if sys.platform == "linux":
    _UNIX_FAMILY = socket.AF_UNIX
else:
    _UNIX_FAMILY = socket.AF_INET  # Unix mode is rejected before creating a server.


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
    null_name_fragments = 0
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
            # Keep the highest reported cumulative count. A later usage-only
            # frame must never erase an already observed limit exceedance.
            for key, value in _usage(chunk["usage"]).items():
                if isinstance(value, dict):
                    details = usage.setdefault(key, {})
                    for child, count in value.items():
                        details[child] = max(details.get(child, 0), count)
                else:
                    usage[key] = max(usage.get(key, 0), value)
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
                    if name is None:
                        # The response schema permits null on a continuation.
                        # It contributes no name; the final allowlist still applies.
                        null_name_fragments += 1
                        name = ""
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
        **({"tool_name_null_fragments": null_name_fragments} if null_name_fragments else {}),
        "usage": usage,
        "finish_reason": finish,
        "stream_terminated": True,
        "go_cost_trailer_seen": cost is not None,
        "provider_reported_cost": cost,
        "provider_reported_cost_unit": "unknown",
    }


def _client() -> httpx.Client:
    return httpx.Client(timeout=90, trust_env=False, follow_redirects=False)


@dataclass(frozen=True)
class GoRelayAuthorization:
    """Controller-owned grant; neither capability is an upstream provider key."""

    journal: GoCallJournal
    grant_id: str
    binding: dict[str, Any]
    capability: str = field(repr=False)


@dataclass(frozen=True)
class GoRelayContext:
    """Controller-resolved limits from a fixed approved policy and task.

    The execution consumer must resolve these from its approved workspace, never
    accept this object from a native request. This port grants no Run authority.
    """

    accounting: GoRequestAccounting = field(repr=False)
    source_sha256: str
    execution_policy_digest: str
    approved_input_tokens: int
    reserved_output_tokens: int
    operating_context_tokens: int
    fixed_margin: int
    ratio_margin_basis_points: int

    def __post_init__(self) -> None:
        if any(
            type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None
            for value in (self.source_sha256, self.execution_policy_digest)
        ):
            raise ValueError("CONTEXT_POLICY_INVALID")
        if (
            any(
                type(value) is not int or value <= 0
                for value in (
                    self.approved_input_tokens,
                    self.reserved_output_tokens,
                    self.operating_context_tokens,
                )
            )
            or any(
                type(value) is not int or value < 0
                for value in (self.fixed_margin, self.ratio_margin_basis_points)
            )
            or self.ratio_margin_basis_points > 10000
        ):
            raise ValueError("CONTEXT_POLICY_INVALID")

    def measure(self, payload: dict[str, Any]) -> dict[str, Any]:
        from karajan.routing.compiler import digest

        from .go_context import GoContextError

        if digest(self.accounting.source()) != self.source_sha256:
            raise GoContextError("CONTEXT_SOURCE_CHANGED")
        return self.accounting.measure(
            payload,
            approved_input_tokens=self.approved_input_tokens,
            reserved_output_tokens=self.reserved_output_tokens,
            operating_context_tokens=self.operating_context_tokens,
            fixed_margin=self.fixed_margin,
            ratio_margin_basis_points=self.ratio_margin_basis_points,
        )


@dataclass(frozen=True)
class GoQualificationContext:
    """Fixed controller probe accounting, distinct from approved Task authority.

    The producer must resolve the spec, scenario and limits from its durable
    qualification start. This object cannot authorize a Task or qualify a Profile.
    """

    accounting: GoRequestAccounting = field(repr=False)
    source_sha256: str
    probe_spec_digest: str
    scenario: Literal["edit", "denied_read"]
    approved_input_tokens: int
    reserved_output_tokens: int
    operating_context_tokens: int
    fixed_margin: int
    ratio_margin_basis_points: int

    def __post_init__(self) -> None:
        if (
            type(self.probe_spec_digest) is not str
            or re.fullmatch(r"[0-9a-f]{64}", self.probe_spec_digest) is None
            or self.scenario not in ("edit", "denied_read")
        ):
            raise ValueError("QUALIFICATION_CONTEXT_INVALID")
        self.limits()

    def limits(self) -> dict[str, Any]:
        """Detached, strict context shape used in the immutable grant binding."""
        try:
            return GoQualificationLimits.model_validate(
                {
                    "source_sha256": self.source_sha256,
                    "approved_input_tokens": self.approved_input_tokens,
                    "reserved_output_tokens": self.reserved_output_tokens,
                    "operating_context_tokens": self.operating_context_tokens,
                    "fixed_margin": self.fixed_margin,
                    "ratio_margin_basis_points": self.ratio_margin_basis_points,
                }
            ).model_dump()
        except ValidationError:
            raise ValueError("QUALIFICATION_CONTEXT_INVALID") from None

    def measure(self, payload: dict[str, Any]) -> dict[str, Any]:
        from karajan.routing.compiler import digest

        from .go_context import GoContextError

        if digest(self.accounting.source()) != self.source_sha256:
            raise GoContextError("CONTEXT_SOURCE_CHANGED")
        limits = self.limits()
        del limits["source_sha256"]
        return self.accounting.measure(payload, **limits)


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
        authorization: GoRelayAuthorization | None = None,
        context: GoRelayContext | GoQualificationContext | None = None,
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
        self._unix_socket: Path | None = None
        self._socket_identity: tuple[int, int] | None = None
        self._context = context
        self._authorization = (
            GoRelayAuthorization(
                authorization.journal,
                authorization.grant_id,
                copy.deepcopy(authorization.binding),
                authorization.capability,
            )
            if authorization is not None
            else None
        )

    @property
    def capability(self) -> str:
        return self._capability

    @property
    def url(self) -> str:
        if self._server is None:
            raise RuntimeError("RELAY_NOT_STARTED")
        if self._unix_socket is not None:
            raise RuntimeError("UNIX_RELAY_HAS_NO_TCP_URL")
        return f"http://127.0.0.1:{self._server.server_port}/v1"

    @property
    def receipts(self) -> list[dict[str, Any]]:
        with self._condition:
            return copy.deepcopy(self._receipts)

    def _persist_receipt(self, receipt: dict[str, Any]) -> None:
        sequence = receipt.get("sequence")
        if type(sequence) is not int:
            return
        with self._condition:
            if 1 <= sequence <= len(self._receipts):
                self._receipts[sequence - 1] = copy.deepcopy(receipt)

    def start(self, *, unix_socket: Path | None = None) -> None:
        with self._condition:
            if self._server is not None or self._closing:
                raise RuntimeError("RELAY_ALREADY_STARTED_OR_CLOSED")
            if unix_socket is not None:
                if sys.platform != "linux":
                    raise RuntimeError("LINUX_UNIX_RELAY_REQUIRED")
                unix_socket = unix_socket.parent.resolve(strict=True) / unix_socket.name
                if unix_socket.exists() or unix_socket.is_symlink():
                    raise RuntimeError("RELAY_SOCKET_PATH_EXISTS")
            owner = self

            class Server(ThreadingHTTPServer):
                address_family: int = _UNIX_FAMILY if unix_socket is not None else socket.AF_INET

                def server_bind(self) -> None:
                    if unix_socket is None:
                        super().server_bind()
                    else:
                        # Keep the existing HTTP parser/lifecycle without ever
                        # allocating a host TCP listener for the namespace path.
                        self.socket.bind(str(unix_socket))

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
            if unix_socket is not None:
                self._unix_socket = unix_socket
                entry = unix_socket.lstat()
                self._socket_identity = (entry.st_dev, entry.st_ino)
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
                "upstream_response_complete": False,
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
            binding = self._authorization.binding if self._authorization is not None else {}
            task_grant = "subject" in binding
            qualification_v2 = (
                not task_grant
                and binding.get("schema_version") == "karajan.go-qualification-grant.v2"
            )
            if task_grant and self._context is None:
                raise _Rejected("TASK_CONTEXT_ACCOUNTING_REQUIRED", 403)
            if qualification_v2 and self._context is None:
                raise _Rejected("QUALIFICATION_CONTEXT_ACCOUNTING_REQUIRED", 403)
            if "schema_version" in binding and not qualification_v2:
                raise _Rejected("GO_JOURNAL_INPUT_INVALID", 403)
            if self._context is not None:
                from .go_context import GoContextError

                if isinstance(self._context, GoQualificationContext):
                    if (
                        not qualification_v2
                        or self._context.probe_spec_digest != binding.get("probe_spec_digest")
                        or self._context.scenario != binding.get("scenario")
                        or self._context.limits() != binding.get("context")
                    ):
                        raise _Rejected("QUALIFICATION_CONTEXT_BINDING_MISMATCH", 403)
                elif not (
                    isinstance(self._context, GoRelayContext)
                    and task_grant
                    and self._context.execution_policy_digest
                    == binding.get("execution_policy_digest")
                ):
                    raise _Rejected("TASK_CONTEXT_POLICY_MISMATCH", 403)
                try:
                    receipt["request_context"] = self._context.measure(payload)
                except GoContextError as error:
                    raise _Rejected(error.code, 422) from None
            client = self._client_factory()
            with self._condition:
                if self._closing:
                    raise _Rejected("RELAY_CLOSING", 503)
                self._clients.add(client)
                if self._authorization is not None:
                    auth = self._authorization
                    call_id = str(uuid4())
                    try:
                        grant = auth.journal.begin_call(
                            auth.grant_id,
                            call_id,
                            capability=auth.capability,
                            binding=auth.binding,
                            **(
                                {"request_context": receipt["request_context"]}
                                if "request_context" in receipt
                                else {}
                            ),
                        )
                    except GoJournalError as error:
                        self._recover_context_call(receipt, call_id)
                        reason = str(error)
                        raise _Rejected(
                            reason, 429 if reason == "REQUEST_LIMIT_REACHED" else 403
                        ) from None
                    except Exception:
                        self._recover_context_call(receipt, call_id)
                        raise
                    if not grant["send_allowed"]:
                        raise _Rejected("CALL_SEND_NOT_AUTHORIZED", 409)
                    receipt["journal_call_id"] = call_id
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
                receipt["upstream_response_complete"] = True
                receipt.update(_stream_facts(bytes(content), self._secret))
                if "request_context" in receipt:
                    measured = receipt["request_context"]
                    prompt = receipt["usage"].get("prompt_tokens")
                    completion = receipt["usage"].get("completion_tokens")
                    if type(prompt) is not int or type(completion) is not int:
                        raise _Rejected("CONTEXT_PROVIDER_USAGE_MISSING")
                    if prompt > measured["accounted_input_tokens"]:
                        raise _Rejected("CONTEXT_PROVIDER_INPUT_EXCEEDED")
                    if completion > measured["requested_output_tokens"]:
                        raise _Rejected("CONTEXT_PROVIDER_OUTPUT_EXCEEDED")
                receipt["protocol_passed"] = True
                self._persist_receipt(receipt)
                handler.send_response(200)
                handler.send_header("Content-Type", "text/event-stream")
                handler.send_header("Content-Length", str(len(content)))
                handler.send_header("Connection", "close")
                handler.end_headers()
                handler.wfile.write(content)
                receipt["relay_completed"] = True
                self._persist_receipt(receipt)
                handler.close_connection = True
        except _Rejected as error:
            if receipt is not None:
                receipt["reason_codes"] = [error.reason]
                self._withdraw_context_sends(receipt)
                self._persist_receipt(receipt)
            self._error(handler, error.status, error.reason)
        except Exception:
            if receipt is not None:
                receipt["protocol_passed"] = False
                receipt["relay_completed"] = False
                receipt["reason_codes"] = ["RELAY_TRANSPORT_ERROR"]
                self._withdraw_context_sends(receipt)
                self._persist_receipt(receipt)
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
                if receipt is not None and "journal_call_id" in receipt:
                    self._complete_journal(receipt)
                if receipt is not None and "sequence" in receipt:
                    self._receipts[receipt["sequence"] - 1] = copy.deepcopy(receipt)

    def _recover_context_call(self, receipt: dict[str, Any], call_id: str) -> None:
        """Read a lost begin result; this never retries begin or grants a send."""
        if "request_context" not in receipt:
            return
        assert self._authorization is not None
        auth = self._authorization
        try:
            recorded = auth.journal.snapshot(auth.grant_id)
            if recorded["binding"] != auth.binding:
                return
            if any(
                call["call_id"] == call_id
                and call.get("request_context") == receipt["request_context"]
                for call in recorded["calls"]
            ):
                receipt["journal_call_id"] = call_id
        except Exception:
            # If persistence is unavailable, close this transport. The
            # controller must reconcile the existing grant before any recovery.
            self._closing = True

    def _withdraw_context_sends(self, receipt: dict[str, Any]) -> None:
        # A durable call proves this relay authenticated the exact grant before
        # sending. Do not revoke unrelated grants on a pre-authentication error.
        if "request_context" not in receipt or "journal_call_id" not in receipt:
            return
        self._closing = True
        assert self._authorization is not None
        try:
            self._authorization.journal.revoke_grant(self._authorization.grant_id)
        except Exception:
            receipt["reason_codes"].append("CONTEXT_REVOCATION_FAILED")

    def _complete_journal(self, receipt: dict[str, Any]) -> None:
        auth = self._authorization
        assert auth is not None
        try:
            auth.journal.complete_call(
                auth.grant_id,
                receipt["journal_call_id"],
                capability=auth.capability,
                binding=auth.binding,
                outcome={
                    "state": (
                        "response_received"
                        if receipt["upstream_response_complete"]
                        else "send_unknown"
                    ),
                    "upstream_status": receipt["upstream_status"],
                    "response_bytes": receipt["response_bytes"],
                    "usage": receipt["usage"],
                    "protocol_passed": receipt["protocol_passed"],
                    "reason_codes": receipt["reason_codes"],
                },
            )
        except Exception:
            # Preserve the already durable unknown send if completion cannot be
            # recorded. No retry, refund or remote-stop claim follows this error.
            receipt["protocol_passed"] = False
            receipt["reason_codes"].append("JOURNAL_COMPLETION_FAILED")
            self._withdraw_context_sends(receipt)

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
        if self._unix_socket is not None:
            try:
                entry = self._unix_socket.lstat()
                if (
                    stat.S_ISSOCK(entry.st_mode)
                    and (entry.st_dev, entry.st_ino) == self._socket_identity
                ):
                    self._unix_socket.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                errors.append("RELAY_SOCKET_CLEANUP_FAILED")
        return {"status": "unknown" if errors else "closed", "errors": errors}
