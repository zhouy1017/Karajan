"""Bounded non-thinking chat-completion subset, documented 2026-09-05.

This module neither authenticates nor sends requests. Its official endpoint is
identity metadata. Neither token observations nor a fixture price prove billing.
"""

import json
from dataclasses import dataclass
from typing import Any

OFFICIAL_ENDPOINT = "https://api.deepseek.com/chat/completions"
MODELS = frozenset({"deepseek-v4-flash", "deepseek-v4-pro"})
MAX_REQUEST_BYTES = 1_000_000


class ProtocolError(ValueError):
    """Stable reason code only; never include untrusted response or secret text."""


@dataclass(frozen=True)
class PreparedRequest:
    endpoint: str
    model: str
    body: bytes
    max_output_tokens: int


def prepare_request(payload: bytes, *, model: str, output_limit: int) -> PreparedRequest:
    body = _object(payload, MAX_REQUEST_BYTES)
    if not isinstance(model, str) or model not in MODELS or body.get("model") != model:
        raise ProtocolError("MODEL_BINDING_MISMATCH")
    maximum = body.get("max_tokens")
    if (
        type(output_limit) is not int
        or not 0 < output_limit <= 256
        or type(maximum) is not int
        or not 0 < maximum <= output_limit
    ):
        raise ProtocolError("OUTPUT_UNBOUNDED")
    allowed = {
        "model",
        "messages",
        "max_tokens",
        "stream",
        "stream_options",
        "thinking",
        "tools",
        "tool_choice",
        "temperature",
        "top_p",
    }
    if set(body) - allowed:
        raise ProtocolError("REQUEST_PARAMETER_UNSUPPORTED")
    if type(body.get("stream")) is not bool:
        raise ProtocolError("REQUEST_INVALID")
    if body.get("thinking", {"type": "disabled"}) != {"type": "disabled"}:
        raise ProtocolError("THINKING_UNSUPPORTED")
    if "stream_options" in body and (
        not body["stream"]
        or not isinstance(body["stream_options"], dict)
        or set(body["stream_options"]) != {"include_usage"}
        or type(body["stream_options"]["include_usage"]) is not bool
    ):
        raise ProtocolError("REQUEST_INVALID")
    for key in ("temperature", "top_p"):
        if key in body and (
            type(body[key]) not in (int, float)
            or not 0 <= body[key] <= (2 if key == "temperature" else 1)
        ):
            raise ProtocolError("REQUEST_INVALID")
    messages = body.get("messages")
    if not isinstance(messages, list) or not 1 <= len(messages) <= 256:
        raise ProtocolError("REQUEST_INVALID")
    for message in messages:
        _message(message)
    tools = body.get("tools", [])
    if not isinstance(tools, list) or len(tools) > 1:
        raise ProtocolError("TOOL_UNSUPPORTED")
    for tool in tools:
        if (
            not isinstance(tool, dict)
            or set(tool) != {"type", "function"}
            or tool["type"] != "function"
            or not isinstance(tool["function"], dict)
            or tool["function"].get("name") != "read"
            or set(tool["function"]) - {"name", "description", "parameters"}
            or not isinstance(tool["function"].get("parameters"), dict)
        ):
            raise ProtocolError("TOOL_UNSUPPORTED")
    if body.get("tool_choice", "auto") not in ("auto", "none", "required"):
        raise ProtocolError("TOOL_UNSUPPORTED")
    body["thinking"] = {"type": "disabled"}
    if body["stream"]:
        body["stream_options"] = {"include_usage": True}
    return PreparedRequest(
        OFFICIAL_ENDPOINT,
        model,
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode(),
        body["max_tokens"],
    )


def _message(message: Any) -> None:
    if not isinstance(message, dict) or set(message) - {
        "role",
        "content",
        "tool_calls",
        "tool_call_id",
        "name",
        "reasoning_content",
    }:
        raise ProtocolError("MESSAGE_UNSUPPORTED")
    if message.get("role") not in ("system", "user", "assistant", "tool"):
        raise ProtocolError("MESSAGE_UNSUPPORTED")
    if "reasoning_content" in message:
        if message["role"] != "assistant" or message["reasoning_content"] not in (None, ""):
            raise ProtocolError("THINKING_UNSUPPORTED")
        del message["reasoning_content"]
    content = message.get("content")
    if isinstance(content, list):
        if not all(
            isinstance(part, dict)
            and set(part) == {"type", "text"}
            and part["type"] == "text"
            and isinstance(part["text"], str)
            for part in content
        ):
            raise ProtocolError("MESSAGE_UNSUPPORTED")
        message["content"] = "".join(part["text"] for part in content)
    elif content is not None and not isinstance(content, str):
        raise ProtocolError("MESSAGE_UNSUPPORTED")
    if message["role"] == "tool" and not isinstance(message.get("tool_call_id"), str):
        raise ProtocolError("MESSAGE_INVALID")
    if "tool_calls" in message:
        calls = message["tool_calls"]
        if message["role"] != "assistant" or not isinstance(calls, list) or len(calls) != 1:
            raise ProtocolError("TOOL_UNSUPPORTED")
        _tool(calls[0])


def _tool(call: Any) -> None:
    if (
        not isinstance(call, dict)
        or set(call) != {"id", "type", "function"}
        or not isinstance(call["id"], str)
        or not call["id"]
        or call["type"] != "function"
        or not isinstance(call["function"], dict)
        or set(call["function"]) != {"name", "arguments"}
        or call["function"]["name"] != "read"
        or not isinstance(call["function"]["arguments"], str)
    ):
        raise ProtocolError("TOOL_UNSUPPORTED")


def _object(payload: bytes, limit: int) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ProtocolError("JSON_AMBIGUOUS")
            result[key] = value
        return result

    def constant(value: str) -> Any:
        raise ProtocolError("JSON_INVALID")

    try:
        if not isinstance(payload, bytes) or not 0 < len(payload) <= limit:
            raise ProtocolError("PAYLOAD_UNBOUNDED")
        result = json.loads(
            payload.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant
        )
        # Reject unpaired escaped surrogates as well as invalid raw UTF-8.
        json.dumps(result, ensure_ascii=False).encode("utf-8")
        if not isinstance(result, dict):
            raise ProtocolError("JSON_OBJECT_REQUIRED")
        return result
    except ProtocolError:
        raise
    except (ValueError, UnicodeError, RecursionError):
        raise ProtocolError("JSON_INVALID") from None
