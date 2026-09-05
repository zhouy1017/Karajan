"""Explicit numeric quota facts; these quantities never represent cash ledgers."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, field_validator

from karajan.resources.broker import units

Identifier = Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[^\s\x00-\x1f\x7f]+$")]
Positive = Annotated[int, Field(gt=0, le=1_000_000)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Pool(Contract):
    id: Identifier
    account_id: Identifier
    kind: Literal["service", "platform_allowance"]
    unit: Literal["requests", "percent", "tokens"]
    window_kind: Literal["fixed", "rolling", "balance", "unknown"]


class Profile(Contract):
    id: Identifier
    revision: Positive
    account_id: Identifier
    pool_ids: Annotated[list[Identifier], Field(min_length=1, max_length=32)]

    @field_validator("pool_ids")
    @classmethod
    def unique_pools(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("Duplicate pool")
        return value


class Observation(Contract):
    pool_id: Identifier
    window_id: Identifier
    observed_at: FiniteFloat
    reset_at: FiniteFloat | None
    source: Literal["official", "fixture", "manual", "local_ledger"]
    source_ref: Identifier
    metric: Literal["remaining", "used", "unknown"]
    amount: str | None
    limit: str | None
    covered_usage_ids: Annotated[list[Identifier], Field(max_length=10000)]
    coverage_ref: Identifier | None = None
    adjustment_reason: Annotated[str, Field(min_length=1, max_length=1000)] | None = None

    @field_validator("amount", "limit")
    @classmethod
    def valid_quantity(cls, value: str | None) -> str | None:
        if value is not None:
            units(value)
        return value


class ConservativeMode(Contract):
    enabled: bool
    max_local_active_attempts: Positive | None = None
    max_attempt_duration_seconds: Positive | None = None
    observation_max_age_seconds: Positive | None = None
    cooldown_seconds: Positive | None = None


class Policy(Contract):
    account_id: Identifier
    max_active_attempts: Positive
    max_attempt_duration_seconds: Positive
    observation_max_age_seconds: Positive
    require_official_observation: bool
    safety_margin: dict[Identifier, str]
    lead_reserve: dict[Identifier, str]
    lead_reserved_slots: Annotated[int, Field(ge=0, le=1_000_000)]
    conservative_mode: ConservativeMode | None

    @field_validator("safety_margin", "lead_reserve")
    @classmethod
    def valid_quantities(cls, value: dict[str, str]) -> dict[str, str]:
        for quantity in value.values():
            units(quantity)
        return value


class AdmissionRequest(Contract):
    attempt_id: Identifier
    run_id: Identifier
    profile_id: Identifier
    profile_revision: Positive
    role: Literal["commander", "worker", "reviewer", "check"]
    purpose: Literal["lead", "advice"] | None
    authorization_ref: Identifier
    rulebook_revision: Identifier
    duration_seconds: Positive
    demand: dict[Identifier, str]

    @field_validator("demand")
    @classmethod
    def valid_quantities(cls, value: dict[str, str]) -> dict[str, str]:
        if not 1 <= len(value) <= 32:
            raise ValueError("Finite resource vector required")
        for quantity in value.values():
            if units(quantity) <= 0:
                raise ValueError("Positive estimate required")
        return value


class AdmissionRef(Contract):
    admission_id: Identifier


class Reconciliation(AdmissionRef):
    local_ended: bool
    remote_ended: bool
    usage_complete: bool
    not_sent: bool
    evidence_ref: Identifier


class UsageReceipt(AdmissionRef):
    id: Identifier
    amounts: dict[Identifier, str]
    window_ids: dict[Identifier, Identifier | None]
    evidence_ref: Identifier
    attribution_ref: Identifier | None

    @field_validator("amounts")
    @classmethod
    def valid_amounts(cls, value: dict[str, str]) -> dict[str, str]:
        if not 1 <= len(value) <= 32:
            raise ValueError("Finite resource vector required")
        for quantity in value.values():
            units(quantity)
        return value


class Failure(Contract):
    account_id: Identifier
    reason: Literal["RATE_LIMIT_TRANSIENT", "QUOTA_EXHAUSTED", "OBSERVATION_UNAVAILABLE"]
    retry_after_seconds: Positive
    evidence_ref: Identifier
