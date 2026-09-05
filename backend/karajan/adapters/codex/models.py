"""Karajan replay envelope, separate from native JSON-RPC wire messages."""

from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue

Identifier = Annotated[str, Field(min_length=1, max_length=256, pattern=r"^\S+$")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
PositiveInteger = Annotated[int, Field(gt=0)]
RequestId = Identifier | int


class Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class AttemptContext(Envelope):
    attempt_id: Identifier
    fence: PositiveInteger
    profile_id: Identifier
    profile_revision: PositiveInteger
    profile_digest: Digest
    thread_id: Identifier
    turn_id: Identifier


class Authorization(Envelope):
    hash: Digest
    expires_at: AwareDatetime
    allowed_request_digests: list[Digest]


class ReadOnlySandbox(Envelope):
    type: Literal["readOnly"]
    networkAccess: Literal[False]


class RequestedConfiguration(Envelope):
    model: Identifier
    model_provider: Literal["openai"]
    cwd: Annotated[str, Field(min_length=1)]
    approval_policy: Literal["on-request"]
    approvals_reviewer: Literal["user"]
    sandbox: ReadOnlySandbox


class PermissionDecision(AttemptContext):
    authorization_hash: Digest
    request_id: RequestId
    request_digest: Digest
    decision: JsonValue


class NativeStep(Envelope):
    kind: Literal["native"]
    at: AwareDatetime
    expires_at: AwareDatetime | None = None
    message: dict[str, JsonValue]


class DecisionStep(Envelope):
    kind: Literal["decision"]
    at: AwareDatetime
    decision: PermissionDecision


class CancelStep(Envelope):
    kind: Literal["cancel", "invalidate"]
    at: AwareDatetime


class Provenance(Envelope):
    kind: Literal["fixture", "imported_observation"]
    observed_at: AwareDatetime
    evidence_refs: list[Identifier]
    limitations: list[str]


class ReplayDocument(Envelope):
    schema_version: Literal["karajan.codex-replay.v1"]
    case_id: Identifier
    runtime_version: Literal["0.153.2"]
    schema_sha256: Digest
    config_source_sha256: Digest
    attempt: AttemptContext
    authorization: Authorization
    requested: RequestedConfiguration
    thread_start_request_id: RequestId
    steps: list[Annotated[NativeStep | DecisionStep | CancelStep, Field(discriminator="kind")]]
    provenance: Provenance


class NativeCommandParams(Envelope):
    itemId: Identifier
    threadId: Identifier
    turnId: Identifier
    startedAtMs: int
    command: Annotated[str, Field(min_length=1)]
    cwd: Annotated[str, Field(min_length=1)]
    kind: Literal["command", "writeStdin"] = "command"
    approvalId: Identifier | None = None
    environmentId: Identifier | None = None
    commandActions: JsonValue = None
    reason: str | None = None
    networkApprovalContext: JsonValue = None
    proposedExecpolicyAmendment: JsonValue = None
    proposedNetworkPolicyAmendments: JsonValue = None


class NativeCommandRequest(Envelope):
    id: RequestId
    method: Literal["item/commandExecution/requestApproval"]
    params: NativeCommandParams
    jsonrpc: Literal["2.0"] | None = None


class NativeObservation(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)


class TokenBreakdown(NativeObservation):
    inputTokens: Annotated[int, Field(ge=0)]
    outputTokens: Annotated[int, Field(ge=0)]
    cachedInputTokens: Annotated[int, Field(ge=0)]
    reasoningOutputTokens: Annotated[int, Field(ge=0)]
    totalTokens: Annotated[int, Field(ge=0)]
    cacheWriteInputTokens: Annotated[int, Field(ge=0)] | None = None


class ThreadUsage(NativeObservation):
    total: TokenBreakdown
    last: TokenBreakdown
    modelContextWindow: int | None = None


class UsageNotification(NativeObservation):
    threadId: Identifier
    turnId: Identifier
    tokenUsage: ThreadUsage


class RateWindow(NativeObservation):
    usedPercent: int
    resetsAt: int | None = None
    windowDurationMins: int | None = None


class RateSnapshot(NativeObservation):
    limitId: str | None = None
    primary: RateWindow | None = None
    secondary: RateWindow | None = None


class ResolvedNotification(NativeObservation):
    threadId: Identifier
    requestId: RequestId
