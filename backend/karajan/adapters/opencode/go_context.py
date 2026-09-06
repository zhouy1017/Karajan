"""Offline reference accounting for Go text/tool requests, not server-exact tokens.

Only pinned official tokenizer data and the upstream Transformers template engine
are used. No Hub loader, remote code, model weights or runtime downloads are used.
Declared capacity is separate from both this estimate and Profile qualification.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from importlib.metadata import version
from pathlib import Path
from typing import Annotated, Any, Final, Literal

from karajan.contracts.probe import Contract
from pydantic import Field, model_validator
from tokenizers import Tokenizer
from transformers.utils.chat_template_utils import render_jinja_template

_MODEL: Final = "glm-5.3-flash"
_REVISION = "690b705278a3a58e538fcb37c2ca8b5f9511213c"
_ARTIFACTS = {
    "tokenizer.json": (
        20_217_442,
        "19e773648cb4e65de8660ea6365e10acca112d42a854923df93db4a6f333a82d",
    ),
    "tokenizer_config.json": (
        761,
        "98b1271574f41abf89427ae2dda030d94dc9478f0edc5a8bd240db213c6fd5fc",
    ),
    "chat_template.jinja": (
        10_950,
        "0c4099f3382d6c92700dfb99725025360966fd73032f0ecf32377c0d9e6309c5",
    ),
}
_LIBRARIES = {"transformers": "5.16.1", "tokenizers": "0.23.2", "Jinja2": "3.1.6"}
_CONTEXT: Final = 1_000_000
_OUTPUT: Final = 131_072
_BODY_BYTES = 262_144
Count = Annotated[int, Field(ge=0, le=2_000_000)]
Positive = Annotated[int, Field(gt=0, le=_CONTEXT)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class GoContextError(ValueError):
    @property
    def code(self) -> str:
        return str(self)


class ContextMeasurement(Contract):
    """Allowlisted, content-free journal data, with internally consistent limits."""

    schema_version: Literal["karajan.go-context-measurement.v1"]
    model: Literal["glm-5.3-flash"]
    measurement_method: Literal["reference_tokenizer_estimate"]
    measurement_confidence: Literal["local_estimate"]
    request_digest: Digest
    source_sha256: Digest
    local_input_tokens: Positive
    fixed_margin: Count
    ratio_margin_basis_points: Annotated[int, Field(ge=0, le=10_000)]
    margin_tokens: Count
    accounted_input_tokens: Positive
    requested_output_tokens: Annotated[int, Field(gt=0, le=_OUTPUT)]
    approved_input_tokens: Positive
    reserved_output_tokens: Annotated[int, Field(gt=0, le=_OUTPUT)]
    operating_context_tokens: Positive
    declared_context_tokens: Literal[1_000_000]
    declared_max_output_tokens: Literal[131_072]
    template_reasoning_effort: Literal["low", "high", "max"]
    template_clear_thinking: Literal[False]

    @model_validator(mode="after")
    def consistent(self) -> ContextMeasurement:
        margin = (
            self.fixed_margin
            + (self.local_input_tokens * self.ratio_margin_basis_points + 9999) // 10_000
        )
        if (
            self.margin_tokens != margin
            or self.accounted_input_tokens != self.local_input_tokens + margin
            or self.accounted_input_tokens > self.approved_input_tokens
            or self.requested_output_tokens > self.reserved_output_tokens
            or self.accounted_input_tokens + self.reserved_output_tokens
            > self.operating_context_tokens
        ):
            raise ValueError("GO_CONTEXT_MEASUREMENT_INCONSISTENT")
        return self


def _encoded(value: Any, *, sort_keys: bool = False) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=sort_keys
    ).encode("utf-8")


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise GoContextError("GO_CONTEXT_INVALID_TOOL_ARGUMENTS")
        result[key] = value
    return result


def _unsupported() -> None:
    raise GoContextError("GO_CONTEXT_UNSUPPORTED_SHAPE")


def _json_value(value: Any, depth: int = 0) -> None:
    if depth > 32:
        _unsupported()
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                _unsupported()
            _json_value(item, depth + 1)
    elif type(value) is list:
        for item in value:
            _json_value(item, depth + 1)
    elif value is not None and type(value) not in (str, bool, int, float):
        _unsupported()
    elif type(value) is float and not math.isfinite(value):
        _unsupported()


def _keys(value: Any, allowed: set[str], required: set[str]) -> None:
    if type(value) is not dict or not required <= value.keys() or value.keys() - allowed:
        _unsupported()


def _name(value: Any) -> None:
    if type(value) is not str or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) is None:
        _unsupported()


def _content(value: Any, *, nullable: bool = False) -> None:
    if type(value) is str or value is None and nullable:
        return
    if type(value) is not list or not value:
        _unsupported()
    for part in value:
        _keys(part, {"type", "text"}, {"type", "text"})
        if part["type"] != "text" or type(part["text"]) is not str:
            _unsupported()


def _request_shape(request: Any) -> None:
    _keys(
        request,
        {
            "model",
            "stream",
            "max_tokens",
            "messages",
            "tools",
            "tool_choice",
            "parallel_tool_calls",
            "reasoning_effort",
            "clear_thinking",
            "stream_options",
            "temperature",
            "top_p",
            "presence_penalty",
            "frequency_penalty",
            "seed",
            "stop",
        },
        {"model", "stream", "max_tokens", "messages"},
    )
    if request["model"] != _MODEL:
        raise GoContextError("GO_CONTEXT_INVALID_MODEL")
    if request["stream"] is not True:
        _unsupported()
    if type(request["max_tokens"]) is not int or not 1 <= request["max_tokens"] <= _OUTPUT:
        raise GoContextError("GO_CONTEXT_OUTPUT_LIMIT_EXCEEDED")
    if request.get("reasoning_effort", "max") not in ("low", "high", "max"):
        _unsupported()
    # This accounting scope retains historical reasoning; clearing it needs a new policy/source.
    if request.get("clear_thinking", False) is not False:
        _unsupported()
    if "stream_options" in request:
        _keys(request["stream_options"], {"include_usage"}, {"include_usage"})
        if request["stream_options"]["include_usage"] is not True:
            _unsupported()
    if "parallel_tool_calls" in request and type(request["parallel_tool_calls"]) is not bool:
        _unsupported()
    if "tool_choice" in request and request["tool_choice"] not in ("auto", "none", "required"):
        _unsupported()
    for key, low, high in (
        ("temperature", 0, 2),
        ("top_p", 0, 1),
        ("presence_penalty", -2, 2),
        ("frequency_penalty", -2, 2),
    ):
        if key in request and (
            type(request[key]) not in (int, float) or not low <= request[key] <= high
        ):
            _unsupported()
    if "seed" in request and (
        type(request["seed"]) is not int or not 0 <= request["seed"] <= 2**31 - 1
    ):
        _unsupported()
    if "stop" in request:
        stop = request["stop"]
        if not (
            type(stop) is str
            or type(stop) is list
            and 1 <= len(stop) <= 4
            and all(type(s) is str for s in stop)
        ):
            _unsupported()
    if "tools" in request:
        if type(request["tools"]) is not list:
            _unsupported()
        names = set()
        for tool in request["tools"]:
            _keys(tool, {"type", "function"}, {"type", "function"})
            if tool["type"] != "function":
                _unsupported()
            function = tool["function"]
            _keys(function, {"name", "description", "parameters", "strict"}, {"name", "parameters"})
            _name(function["name"])
            if function["name"] in names or type(function["parameters"]) is not dict:
                _unsupported()
            names.add(function["name"])
            if "description" in function and type(function["description"]) is not str:
                _unsupported()
            if "strict" in function and type(function["strict"]) is not bool:
                _unsupported()
    messages = request["messages"]
    if type(messages) is not list or not messages or len(messages) > 1024:
        _unsupported()
    pending: set[str] = set()
    used: set[str] = set()
    for message in messages:
        _keys(
            message,
            {"role", "content", "reasoning_content", "tool_calls", "tool_call_id"},
            {"role"},
        )
        role = message["role"]
        if pending and role != "tool":
            _unsupported()
        if role in ("system", "user"):
            _keys(message, {"role", "content"}, {"role", "content"})
            _content(message["content"])
        elif role == "assistant":
            _keys(message, {"role", "content", "reasoning_content", "tool_calls"}, {"role"})
            if "content" not in message and not message.get("tool_calls"):
                _unsupported()
            _content(message.get("content"), nullable=True)
            if (
                message.get("reasoning_content") is not None
                and type(message["reasoning_content"]) is not str
            ):
                _unsupported()
            calls = message.get("tool_calls")
            if calls is not None:
                if type(calls) is not list:
                    _unsupported()
                for call in calls:
                    _keys(call, {"id", "type", "function"}, {"id", "type", "function"})
                    _name(call["id"])
                    if call["type"] != "function" or call["id"] in used:
                        _unsupported()
                    used.add(call["id"])
                    pending.add(call["id"])
                    _keys(call["function"], {"name", "arguments"}, {"name", "arguments"})
                    _name(call["function"]["name"])
        elif role == "tool":
            _keys(message, {"role", "content", "tool_call_id"}, {"role", "content", "tool_call_id"})
            _name(message["tool_call_id"])
            if message["tool_call_id"] not in pending:
                _unsupported()
            pending.remove(message["tool_call_id"])
            _content(message["content"])
        else:
            _unsupported()
    if pending:
        _unsupported()


def _normalize_arguments(request: dict[str, Any]) -> None:
    for message in request["messages"]:
        for call in message.get("tool_calls") or []:
            arguments = call["function"]["arguments"]
            if not isinstance(arguments, str):
                raise GoContextError("GO_CONTEXT_INVALID_TOOL_ARGUMENTS")
            try:
                parsed = json.loads(arguments, object_pairs_hook=_object)
                _json_value(parsed)
                _encoded(parsed)  # Reject nonfinite values inside the encoded arguments too.
            except (ValueError, UnicodeError, RecursionError):
                raise GoContextError("GO_CONTEXT_INVALID_TOOL_ARGUMENTS") from None
            if not isinstance(parsed, dict):
                raise GoContextError("GO_CONTEXT_INVALID_TOOL_ARGUMENTS")
            call["function"]["arguments"] = parsed


class GoRequestAccounting:
    def __init__(self, artifact_directory: Path | str) -> None:
        content: dict[str, bytes] = {}
        try:
            for name, (size, expected) in _ARTIFACTS.items():
                path = Path(artifact_directory) / name
                if not path.is_file():
                    raise GoContextError("GO_CONTEXT_ARTIFACT_MISSING")
                with path.open("rb") as stream:
                    raw = stream.read(size + 1)
                if len(raw) != size or _sha(raw) != expected:
                    raise GoContextError("GO_CONTEXT_ARTIFACT_CHANGED")
                content[name] = raw
            libraries = {name: version(name) for name in _LIBRARIES}
            if libraries != _LIBRARIES:
                raise GoContextError("GO_CONTEXT_LIBRARY_CHANGED")
            self._tokenizer = Tokenizer.from_str(content["tokenizer.json"].decode("utf-8"))
            self._template = content["chat_template.jinja"].decode("utf-8")
            implementation = _sha(Path(__file__).read_bytes())
        except GoContextError:
            raise
        except Exception:
            raise GoContextError("GO_CONTEXT_SOURCE_UNAVAILABLE") from None
        self._source = {
            "schema_version": "karajan.go-context-source.v1",
            "model": _MODEL,
            "channel": "opencode-go",
            "measurement_method": "reference_tokenizer_estimate",
            "libraries": libraries,
            "artifacts": {
                name: {
                    "sha256": sha,
                    "bytes": size,
                    "url": f"https://huggingface.co/zai-org/GLM-5.3-Flash/resolve/{_REVISION}/{name}",
                }
                for name, (size, sha) in _ARTIFACTS.items()
            },
            "normalizer": {
                "id": "openai-compatible-text-functions.v1",
                "implementation_sha256": implementation,
                "generation_prompt": True,
                "add_special_tokens": False,
                "default_reasoning_effort": "max",
                "default_clear_thinking": False,
                "tool_argument_normalization": "duplicate-rejecting-json-object",
                "request_digest_encoding": "ordered-json-utf8-compact",
            },
            "declared_capacity": {
                "basis": "provider_declared",
                "context_tokens": _CONTEXT,
                "max_output_tokens": _OUTPUT,
                "catalog_revision": "98383f3755693d8b173ec1a2ff8bd0ae851ef207",
                "channel_record_url": (
                    "https://github.com/anomalyco/models.dev/blob/"
                    "98383f3755693d8b173ec1a2ff8bd0ae851ef207/"
                    "providers/opencode-go/models/glm-5.3-flash.toml"
                ),
                "model_record_url": (
                    "https://github.com/anomalyco/models.dev/blob/"
                    "98383f3755693d8b173ec1a2ff8bd0ae851ef207/"
                    "models/zhipuai/glm-5.3-flash.toml"
                ),
                "channel_record_sha256": (
                    "04995d39482667fe746b81bb5bd2ad08817ee0a11ad2de0d93a9d774f54c68f0"
                ),
                "model_record_sha256": (
                    "541b4e2dc3b05cfae7e89766882eff3dc6e733b8309d9d11fe9037d5bd374b0a"
                ),
                "model_documentation": "https://docs.z.ai/guides/vlm/glm-5.3-flash",
                "channel_documentation": "https://opencode.ai/docs/go/",
            },
            "server_exact_accounting": False,
            "qualification_granted": False,
        }
        self._source_sha256 = _sha(_encoded(self._source, sort_keys=True))

    def source(self) -> dict[str, Any]:
        return copy.deepcopy(self._source)

    def measure(
        self,
        payload: dict[str, Any],
        *,
        approved_input_tokens: int,
        reserved_output_tokens: int,
        operating_context_tokens: int,
        fixed_margin: int,
        ratio_margin_basis_points: int,
    ) -> dict[str, Any]:
        try:
            for value, minimum, maximum in (
                (approved_input_tokens, 1, _CONTEXT),
                (reserved_output_tokens, 1, _OUTPUT),
                (operating_context_tokens, 1, _CONTEXT),
                (fixed_margin, 0, _CONTEXT),
                (ratio_margin_basis_points, 0, 10_000),
            ):
                if type(value) is not int or not minimum <= value <= maximum:
                    raise GoContextError("GO_CONTEXT_INVALID_LIMITS")
            _json_value(payload)
            _request_shape(payload)
            if payload["max_tokens"] > reserved_output_tokens:
                raise GoContextError("GO_CONTEXT_OUTPUT_LIMIT_EXCEEDED")
            raw = _encoded(payload)
            if len(raw) > _BODY_BYTES:
                raise GoContextError("GO_CONTEXT_REQUEST_TOO_LARGE")
            request = json.loads(raw)
            _normalize_arguments(request)
            # Use upstream template semantics, including role/control tokens and tools.
            result: Any = render_jinja_template(
                [request["messages"]],
                tools=request.get("tools"),
                chat_template=self._template,
                add_generation_prompt=True,
                reasoning_effort=request.get("reasoning_effort", "max"),
                clear_thinking=request.get("clear_thinking", False),
            )
            local = len(self._tokenizer.encode(result[0][0], add_special_tokens=False).ids)
            margin = fixed_margin + (local * ratio_margin_basis_points + 9999) // 10_000
            if local + margin > approved_input_tokens:
                raise GoContextError("GO_CONTEXT_INPUT_LIMIT_EXCEEDED")
            if local + margin + reserved_output_tokens > operating_context_tokens:
                raise GoContextError("GO_CONTEXT_WINDOW_EXCEEDED")
            return ContextMeasurement(
                schema_version="karajan.go-context-measurement.v1",
                model=_MODEL,
                measurement_method="reference_tokenizer_estimate",
                measurement_confidence="local_estimate",
                request_digest=_sha(raw),
                source_sha256=self._source_sha256,
                local_input_tokens=local,
                fixed_margin=fixed_margin,
                ratio_margin_basis_points=ratio_margin_basis_points,
                margin_tokens=margin,
                accounted_input_tokens=local + margin,
                requested_output_tokens=request["max_tokens"],
                approved_input_tokens=approved_input_tokens,
                reserved_output_tokens=reserved_output_tokens,
                operating_context_tokens=operating_context_tokens,
                declared_context_tokens=_CONTEXT,
                declared_max_output_tokens=_OUTPUT,
                template_reasoning_effort=request.get("reasoning_effort", "max"),
                template_clear_thinking=request.get("clear_thinking", False),
            ).model_dump()
        except GoContextError:
            raise
        except Exception:
            raise GoContextError("GO_CONTEXT_MEASUREMENT_FAILED") from None
