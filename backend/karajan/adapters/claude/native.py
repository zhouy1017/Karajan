"""Observed wire subset; this is not a generated CLI schema or a control transport."""

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from .models import Label

Count = Annotated[int, Field(ge=0, le=2**63 - 1)]


class NativeRecord(BaseModel):
    # Native additive metadata is accepted, but never copied into public reports.
    model_config = ConfigDict(extra="ignore", strict=True, allow_inf_nan=False)


class Tokens(NativeRecord):
    input_tokens: Count | None = None
    output_tokens: Count | None = None
    cache_creation_input_tokens: Count | None = None
    cache_read_input_tokens: Count | None = None


class ModelUsage(NativeRecord):
    inputTokens: Count | None = None
    outputTokens: Count | None = None
    cacheReadInputTokens: Count | None = None
    cacheCreationInputTokens: Count | None = None
    costUSD: Annotated[float, Field(ge=0)] | None = None


class AssistantMessage(NativeRecord):
    id: Label
    model: Label
    content: list[dict[str, JsonValue]]
    usage: Tokens | None = None


class Assistant(NativeRecord):
    type: Literal["assistant"]
    uuid: Label
    session_id: Label
    parent_tool_use_id: Label | None = None
    message: AssistantMessage
    error: str | None = None


class UserMessage(NativeRecord):
    role: Literal["user"]
    content: str | list[dict[str, JsonValue]]


class User(NativeRecord):
    type: Literal["user"]
    uuid: Label
    session_id: Label
    parent_tool_use_id: Label | None = None
    message: UserMessage


class ToolUse(NativeRecord):
    type: Literal["tool_use"]
    id: Label
    name: Label
    input: dict[str, JsonValue]


class ToolResult(NativeRecord):
    type: Literal["tool_result"]
    tool_use_id: Label
    content: JsonValue = None
    is_error: bool = False


class StreamPayload(NativeRecord):
    type: Literal[
        "message_start",
        "message_delta",
        "message_stop",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
        "ping",
    ]
    index: Count | None = None
    message: AssistantMessage | None = None
    delta: dict[str, JsonValue] | None = None
    content_block: dict[str, JsonValue] | None = None
    usage: Tokens | None = None

    @model_validator(mode="after")
    def required_payload(self) -> Self:
        if self.type == "message_start" and self.message is None:
            raise ValueError("missing message")
        if self.type.startswith("content_block_") and self.index is None:
            raise ValueError("missing block index")
        if self.type == "content_block_start" and self.content_block is None:
            raise ValueError("missing block")
        if self.type in ("content_block_delta", "message_delta") and self.delta is None:
            raise ValueError("missing delta")
        if self.type == "content_block_delta" and self.delta is not None:
            fields = {
                "text_delta": "text",
                "input_json_delta": "partial_json",
                "thinking_delta": "thinking",
                "signature_delta": "signature",
            }
            subtype = self.delta.get("type")
            if not isinstance(subtype, str) or subtype not in fields:
                raise ValueError("unsupported delta")
            if not isinstance(self.delta.get(fields[subtype]), str):
                raise ValueError("invalid delta")
        return self


class Stream(NativeRecord):
    type: Literal["stream_event"]
    uuid: Label
    session_id: Label
    parent_tool_use_id: Label | None = None
    event: StreamPayload


class Retry(NativeRecord):
    type: Literal["system"]
    subtype: Literal["api_retry"]
    uuid: Label
    session_id: Label
    attempt: Annotated[int, Field(ge=1)]
    max_retries: Count
    retry_delay_ms: Count
    error_status: Annotated[int, Field(ge=100, le=599)] | None
    error: str


class RateLimitInfo(NativeRecord):
    status: Literal["allowed", "allowed_warning", "rejected"]
    resetsAt: Count | None = None
    rateLimitType: (
        Literal["five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet", "overage"] | None
    ) = None
    utilization: Annotated[float, Field(ge=0, le=1)] | None = None
    overageStatus: Literal["allowed", "allowed_warning", "rejected"] | None = None
    overageResetsAt: Count | None = None


class RateLimit(NativeRecord):
    type: Literal["rate_limit_event"]
    uuid: Label
    session_id: Label
    rate_limit_info: RateLimitInfo


class Result(NativeRecord):
    type: Literal["result"]
    subtype: Literal[
        "success",
        "error_during_execution",
        "error_max_turns",
        "error_max_budget_usd",
        "error_max_structured_output_retries",
    ]
    uuid: Label
    session_id: Label
    duration_ms: Count
    duration_api_ms: Count
    num_turns: Count
    is_error: bool
    result: str | None = None
    api_error_status: Annotated[int, Field(ge=100, le=599)] | None = None
    usage: Tokens
    modelUsage: dict[str, ModelUsage] = Field(default_factory=dict)
    total_cost_usd: Annotated[float, Field(ge=0)] | None = None
    permission_denials: list[dict[str, JsonValue]] = Field(default_factory=list)

    @model_validator(mode="after")
    def consistent_outcome(self) -> Self:
        if self.subtype == "success":
            if self.is_error or self.result is None or self.api_error_status is not None:
                raise ValueError("inconsistent success")
        elif not self.is_error:
            raise ValueError("inconsistent error")
        return self

    def category(self) -> str:
        if self.api_error_status in (401, 403):
            return "authentication_error"
        if self.api_error_status == 429:
            return "rate_limited"
        return {
            "success": "completed",
            "error_during_execution": "execution_error",
            "error_max_turns": "turn_limit",
            "error_max_budget_usd": "runtime_budget_limit",
            "error_max_structured_output_retries": "structured_output_limit",
        }[self.subtype]
