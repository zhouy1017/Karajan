"""Turn a bounded provider body into observations, without money or authority."""

from dataclasses import dataclass, field
from typing import Any

from .protocol import MODELS, ProtocolError, _object, _tool

MAX_RESPONSE_BYTES = 4_000_000


@dataclass(frozen=True)
class ResponseObservation:
    status: str
    reason_codes: tuple[str, ...] = ()
    request_id: str | None = None
    model: str | None = None
    content: str = ""
    tool_calls: tuple[dict[str, Any], ...] = ()
    usage: dict[str, int | None] = field(default_factory=dict)
    usage_status: str = "unknown"
    actual_charge: None = None


def observe_response(
    payload: bytes,
    *,
    model: str,
    content_type: str,
    status: int,
) -> ResponseObservation:
    if (
        type(status) is not int
        or not isinstance(model, str)
        or model not in MODELS
        or not isinstance(content_type, str)
    ):
        return ResponseObservation("failed", ("RESPONSE_BINDING_INVALID",))
    if status != 200:
        reason = {
            400: "PROVIDER_REQUEST_INVALID",
            401: "PROVIDER_AUTH_REQUIRED",
            402: "PROVIDER_BALANCE_EXHAUSTED",
            422: "PROVIDER_PARAMETER_INVALID",
            429: "PROVIDER_RATE_LIMITED",
            500: "PROVIDER_SERVER_ERROR",
            503: "PROVIDER_OVERLOADED",
        }.get(status, "PROVIDER_HTTP_ERROR")
        return ResponseObservation("failed", (reason,))
    state = _Stream(model)
    try:
        if not isinstance(payload, bytes) or not 0 < len(payload) <= MAX_RESPONSE_BYTES:
            raise ProtocolError("PAYLOAD_UNBOUNDED")
        media = content_type.split(";", 1)[0].strip().lower()
        if media == "application/json":
            document = _object(payload, MAX_RESPONSE_BYTES)
            if document.get("object") != "chat.completion":
                raise ProtocolError("RESPONSE_BINDING_MISMATCH")
            choices = document.get("choices")
            if not isinstance(choices, list) or len(choices) != 1:
                raise ProtocolError("CHOICE_UNSUPPORTED")
            choice = choices[0]
            if not isinstance(choice, dict):
                raise ProtocolError("CHOICE_UNSUPPORTED")
            message = choice.get("message")
            if not isinstance(message, dict):
                raise ProtocolError("RESPONSE_INVALID")
            if "tool_calls" in message:
                message["tool_calls"] = [
                    {"index": index, **call} for index, call in enumerate(message["tool_calls"])
                ]
            document["object"] = "chat.completion.chunk"
            document["choices"] = [{**choice, "delta": message}]
            state.consume(document)
            state.done = True
            return state.observation()
        if media != "text/event-stream":
            raise ProtocolError("RESPONSE_MEDIA_UNSUPPORTED")
        # SSE permits comments and CRLF. The supported data events are JSON or DONE.
        # Multi-line JSON is folded according to SSE; the byte limit bounds buffering.
        events = payload.replace(b"\r\n", b"\n").replace(b"\r", b"\n").split(b"\n\n")
        if len(events) > 4096:
            raise ProtocolError("STREAM_UNBOUNDED")
        # EOF does not dispatch an unterminated SSE event. Decode each complete
        # event separately so a damaged tail cannot erase usage already observed.
        for encoded_event in events[:-1]:
            event = encoded_event.decode("utf-8")
            data = []
            for line in event.splitlines():
                if line.startswith("data:"):
                    data.append(line[5:].removeprefix(" "))
                elif line and not line.startswith(":"):
                    raise ProtocolError("SSE_FIELD_UNSUPPORTED")
            if not data:
                continue
            if state.done:
                raise ProtocolError("STREAM_AFTER_DONE")
            value = "\n".join(data)
            if value == "[DONE]":
                state.done = True
            else:
                state.consume(_object(value.encode("utf-8"), MAX_RESPONSE_BYTES))
        return state.observation()
    except ProtocolError as error:
        return ResponseObservation(
            "failed",
            (str(error),),
            request_id=state.request_id,
            model=state.model,
            usage=state.usage,
            usage_status=state.usage_status,
        )
    except (UnicodeError, ValueError, TypeError, KeyError, RecursionError):
        return ResponseObservation(
            "failed",
            ("RESPONSE_INVALID",),
            request_id=state.request_id,
            model=state.model,
            usage=state.usage,
            usage_status=state.usage_status,
        )


def _usage(value: Any) -> tuple[dict[str, int | None], str]:
    keys = [
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "prompt_cache_hit_tokens",
        "prompt_cache_miss_tokens",
        "reasoning_tokens",
    ]
    result: dict[str, int | None] = dict.fromkeys(keys)
    if value is None:
        return result, "unknown"
    if not isinstance(value, dict):
        raise ProtocolError("USAGE_INVALID")
    details = value.get("completion_tokens_details")
    if details is not None and not isinstance(details, dict):
        raise ProtocolError("USAGE_INVALID")
    for key in keys:
        item = (details or {}).get(key) if key == "reasoning_tokens" else value.get(key)
        if item is not None and (type(item) is not int or not 0 <= item <= 10**12):
            raise ProtocolError("USAGE_INVALID")
        result[key] = item
    prompt, output, total, hit, miss, reasoning = (result[key] for key in keys)
    if (
        (
            prompt is not None
            and output is not None
            and total is not None
            and prompt + output != total
        )
        or (prompt is not None and hit is not None and hit > prompt)
        or (prompt is not None and miss is not None and miss > prompt)
        or (prompt is not None and hit is not None and miss is not None and hit + miss != prompt)
        or (output is not None and reasoning is not None and reasoning > output)
    ):
        raise ProtocolError("USAGE_INVALID")
    return result, "observed" if all(result[key] is not None for key in keys[:3]) else "partial"


class _Stream:
    def __init__(self, expected_model: str) -> None:
        self.expected_model = expected_model
        self.request_id: str | None = None
        self.model: str | None = None
        self.created: int | None = None
        self.content = ""
        self.finish: str | None = None
        self.done = False
        self.usage, self.usage_status = _usage(None)
        self.call: dict[str, Any] | None = None
        self.unexpected_thinking = False

    def consume(self, chunk: dict[str, Any]) -> None:
        if self.finish is not None:
            raise ProtocolError("STREAM_AFTER_FINISH")
        request_id = chunk.get("id")
        created = chunk.get("created")
        if (
            chunk.get("object") != "chat.completion.chunk"
            or not isinstance(request_id, str)
            or not request_id
            or len(request_id) > 256
            or type(created) is not int
            or created < 0
            or chunk.get("model") != self.expected_model
            or (
                self.request_id is not None
                and (request_id != self.request_id or created != self.created)
            )
        ):
            raise ProtocolError("RESPONSE_BINDING_MISMATCH")
        self.request_id, self.created, self.model = request_id, created, self.expected_model
        choices = chunk.get("choices")
        if not isinstance(choices, list) or len(choices) != 1:
            raise ProtocolError("CHOICE_UNSUPPORTED")
        choice = choices[0]
        if (
            not isinstance(choice, dict)
            or type(choice.get("index")) is not int
            or choice["index"] != 0
        ):
            raise ProtocolError("CHOICE_UNSUPPORTED")
        delta = choice.get("delta")
        if not isinstance(delta, dict) or set(delta) - {
            "role",
            "content",
            "reasoning_content",
            "tool_calls",
        }:
            raise ProtocolError("DELTA_UNSUPPORTED")
        if delta.get("role", "assistant") != "assistant":
            raise ProtocolError("DELTA_UNSUPPORTED")
        for key in ("content", "reasoning_content"):
            if delta.get(key) is not None and not isinstance(delta[key], str):
                raise ProtocolError("DELTA_INVALID")
        self.unexpected_thinking |= bool(delta.get("reasoning_content"))
        self.content += delta.get("content") or ""
        if "tool_calls" in delta:
            self._tool_delta(delta["tool_calls"])
        finish = choice.get("finish_reason")
        if finish not in (
            None,
            "stop",
            "tool_calls",
            "length",
            "content_filter",
            "insufficient_system_resource",
        ):
            raise ProtocolError("FINISH_UNSUPPORTED")
        if chunk.get("usage") is not None:
            if finish is None:
                raise ProtocolError("USAGE_BEFORE_FINISH")
            self.usage, self.usage_status = _usage(chunk["usage"])
        self.finish = finish

    def _tool_delta(self, calls: Any) -> None:
        if not isinstance(calls, list) or len(calls) != 1:
            raise ProtocolError("TOOL_UNSUPPORTED")
        call = calls[0]
        if (
            not isinstance(call, dict)
            or set(call) - {"index", "id", "type", "function"}
            or type(call.get("index")) is not int
            or call["index"] != 0
            or not isinstance(call.get("function"), dict)
            or set(call["function"]) - {"name", "arguments"}
        ):
            raise ProtocolError("TOOL_UNSUPPORTED")
        if self.call is None:
            self.call = {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
        for key in ("id", "type"):
            if key in call:
                if not isinstance(call[key], str) or (
                    self.call[key] and self.call[key] != call[key]
                ):
                    raise ProtocolError("TOOL_IDENTITY_MISMATCH")
                self.call[key] = call[key]
        for key, part in call["function"].items():
            if not isinstance(part, str):
                raise ProtocolError("TOOL_INVALID")
            self.call["function"][key] += part

    def observation(self) -> ResponseObservation:
        if self.unexpected_thinking:
            return ResponseObservation(
                "failed",
                ("THINKING_UNEXPECTED",),
                request_id=self.request_id,
                model=self.model,
                usage=self.usage,
                usage_status=self.usage_status,
            )
        status = "unknown"
        reasons: tuple[str, ...] = ("STREAM_INCOMPLETE",)
        if self.done and self.finish:
            status = {"stop": "completed", "tool_calls": "tool_requested"}.get(
                self.finish, "incomplete"
            )
            reasons = () if status != "incomplete" else ("PROVIDER_" + self.finish.upper(),)
            if self.finish == "tool_calls":
                _tool(self.call)
                assert self.call is not None
                arguments = _object(self.call["function"]["arguments"].encode(), MAX_RESPONSE_BYTES)
                if not isinstance(arguments.get("filePath"), str):
                    raise ProtocolError("TOOL_ARGUMENT_INVALID")
            elif self.call is not None:
                raise ProtocolError("TOOL_FINISH_MISMATCH")
        return ResponseObservation(
            status,
            reasons,
            self.request_id,
            self.model,
            self.content,
            tool_calls=(self.call,) if self.call is not None and status == "tool_requested" else (),
            usage=self.usage,
            usage_status=self.usage_status,
        )
