"""Explicit project inputs; unknown fields never become an authorization."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from karajan.contracts.probe import Contract, Profile

Identifier = Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[^\s\x00-\x1f\x7f]+$")]


class ProjectUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: Annotated[str, Field(min_length=1, max_length=120, pattern=r"\S")]
    base_ref: Identifier
    target_branch: Identifier
    allowed_target_branches: Annotated[list[Identifier], Field(min_length=1, max_length=100)]


class ProjectCreate(ProjectUpdate):
    repository_path: Annotated[str, Field(min_length=1, max_length=4096, pattern=r"^[^\x00]+$")]


class ProfileRef(Contract):
    id: Identifier
    revision: Annotated[int, Field(gt=0)]


class Account(Contract):
    id: Identifier
    provider_id: Identifier | None = None
    secret_ref: Identifier | None = None


class Channel(Contract):
    id: Identifier
    account_id: Identifier | None = None
    billing_path: Literal["subscription_only", "api_cash"] | None = None
    approved_data_destination: bool = False


class CapabilityEvidence(Contract):
    capability: Identifier
    status: Literal["passed", "failed", "not_run", "unsupported"]
    profile_digest: str | None = None
    runtime_version: Identifier | None = None
    evidence_ref: Identifier | None = None
    provenance: Literal["fixture", "imported_observation"] | None = None


class RegisteredProfile(ProfileRef):
    profile: Profile | None = None
    model_family: Identifier | None = None
    max_class: Literal["T1", "T2", "T3"] | None = None
    required_isolation: Literal["attempt_isolated", "tool_sandboxed"] | None = None
    enabled: bool = False
    quota_pool_refs: list[Identifier] = Field(default_factory=list)
    capability_evidence: list[CapabilityEvidence] = Field(default_factory=list)


class QuotaPool(Contract):
    id: Identifier
    account_id: Identifier | None = None
    kind: Literal["service", "platform_allowance"]
    unit: Literal["percent", "tokens", "requests"]
    limit: str | None = None
    observation_state: Literal["unknown", "observed"] = "unknown"


class ConservativeMode(Contract):
    enabled: bool | None = None
    max_local_active_attempts: int | None = None
    max_attempt_duration_seconds: int | None = None
    observation_max_age_seconds: int | None = None
    cooldown_seconds: int | None = None


class CapacityPolicy(Contract):
    account_id: Identifier
    conservative_mode: ConservativeMode | None = None


class Budget(Contract):
    id: Identifier
    scope: Literal["planning", "run"]
    currency_limits: dict[str, str | None] = Field(default_factory=dict)
    max_total_attempts: int | None = None
    max_duration_seconds: int | None = None


class ResourceCatalog(Contract):
    accounts: list[Account] = Field(default_factory=list)
    channels: list[Channel] = Field(default_factory=list)
    profiles: list[RegisteredProfile] = Field(default_factory=list)
    quota_pools: list[QuotaPool] = Field(default_factory=list)
    capacity_policies: list[CapacityPolicy] = Field(default_factory=list)
    budgets: list[Budget] = Field(default_factory=list)


class ConfigurationDraft(Contract):
    schema_version: Literal["karajan.project-config.v1"]
    rulebook: dict[str, JsonValue] | None = None
    resources: ResourceCatalog | None = None
    approved_profile_refs: list[ProfileRef] = Field(default_factory=list)


class TaskPreview(Contract):
    role: Literal["commander", "worker", "reviewer"]
    readiness: Literal["T0", "ready"]
    complexity: Literal["T1", "T2", "T3"]
    risk: Literal["standard", "critical"]
    purpose: Literal["lead", "advice"] | None = None
    approved_profile_refs: list[ProfileRef]
    required_capabilities: list[Identifier] = Field(default_factory=list)
    author_profile_refs: list[ProfileRef] = Field(default_factory=list)
    author_model_families: list[Identifier] = Field(default_factory=list)
