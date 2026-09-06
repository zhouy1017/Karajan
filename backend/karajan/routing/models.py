"""Strict, non-executable routing documents and explicit simulation facts."""

from typing import Annotated, Any, Literal

from pydantic import AfterValidator, Field, FiniteFloat, field_validator, model_validator

from karajan.capacity.models import Policy as CapacityPolicy
from karajan.contracts.probe import Contract
from karajan.projects.models import Identifier, ProfileRef, ResourceCatalog
from karajan.resources.broker import units

Class = Literal["T1", "T2", "T3"]
Role = Literal["commander", "worker", "reviewer"]
Stage = Literal["normal", "quality"]
Positive = Annotated[int, Field(gt=0, le=1_000_000_000)]
Count = Annotated[int, Field(ge=0, le=1_000_000_000)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def quantity(value: str) -> str:
    units(value)
    return value


Quantity = Annotated[str, Field(min_length=1, max_length=64), AfterValidator(quantity)]
Currency = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
References = Annotated[list[ProfileRef], Field(max_length=1000)]
Names = Annotated[list[Identifier], Field(max_length=1000)]


class Collaboration(Contract):
    command_mode: Literal["lead_with_advisers"]
    plan_approval: Literal["explicit_revision"]
    implementation_readiness: Literal["ready"]
    delivery_target: Literal["pull_request"]
    merge_authority: Literal["user"]
    max_parallel_writers_per_project: Positive
    max_quality_repair_rounds: Count
    max_infrastructure_retries_per_root_task: Count
    internal_delegation: Literal["disabled_unless_admitted_and_observable"]


class GlobalConstraints(Contract):
    @model_validator(mode="before")
    @classmethod
    def strict_flags(cls, value: Any) -> Any:
        if isinstance(value, dict):
            for key in (
                "require_enabled_profile",
                "require_passed_capabilities",
                "require_explicit_billing_path",
                "require_approved_data_destination",
                "profile_fixed_per_attempt",
                "no_silent_model_or_billing_fallback",
                "final_review_required",
            ):
                if key in value and type(value[key]) is not bool:
                    raise ValueError("Boolean permission required")
        return value

    require_enabled_profile: Literal[True]
    require_passed_capabilities: Literal[True]
    require_explicit_billing_path: Literal[True]
    require_approved_data_destination: Literal[True]
    autonomous_tool_execution_min_isolation: Literal["tool_sandboxed"]
    profile_fixed_per_attempt: Literal[True]
    no_silent_model_or_billing_fallback: Literal[True]
    review_context: Literal["fresh_non_author"]
    final_review_required: Literal[True]


class ResourcePolicy(Contract):
    id: Identifier
    queue_order: list[
        Literal[
            "lead_feedback",
            "required_review_repair_and_critical_path",
            "worker",
            "optional_adviser",
        ]
    ]
    fairness: Literal["aging_and_run_round_robin"]
    candidate_order: list[
        Literal[
            "preference_band",
            "uncertainty_band",
            "bottleneck_quota_pressure",
            "incremental_cash_estimate",
            "completion_time_estimate",
            "profile_id",
        ]
    ]
    reset_preference: Literal["only_among_equivalent_candidates_all_windows_checked"]
    account_capacity_policy_binding: Literal["current_global_revision"]
    unknown_quota: Literal["require_explicit_conservative_mode"]
    planning_budget_ref: Identifier | None
    run_budget_ref: Identifier | None
    missing_cash_budget: Literal["deny_cash_route"]
    cash_budget_enforcement: Literal["bounded_calls"]
    subscription_quota_enforcement: Literal["conservative_estimate"]
    call_reservations: Literal["slices_of_attempt_reservation"]


class When(Contract):
    role: Role
    purpose: Literal["lead", "advice"] | None = None
    readiness: Literal["ready", "T0"] | None = None
    effective_class: Class | None = None
    effective_class_in: list[Class] | None = None
    domains_all: Names = Field(default_factory=list)
    risks_in: Names = Field(default_factory=list)


class Preference(Contract):
    profile: ProfileRef
    band: Annotated[int, Field(ge=-1_000_000, le=1_000_000)]


class Rule(Contract):
    @field_validator("lead_reserve_access", mode="before")
    @classmethod
    def strict_access(cls, value: Any) -> Any:
        if value is not None and type(value) is not bool:
            raise ValueError("Boolean permission required")
        return value

    id: Identifier
    priority: Annotated[int, Field(ge=-1_000_000, le=1_000_000)]
    when: When
    eligible_groups: Names
    capabilities_all: Names
    quality_escalation_groups: Names = Field(default_factory=list)
    profile_preferences: list[Preference] = Field(default_factory=list)
    independence: list[
        Literal["fresh_context", "non_author_attempt", "different_model_family_from_authors"]
    ] = Field(default_factory=list)
    handoff: Literal["explicit_checkpoint_record"] | None = None
    reroute: Literal[
        "propose_checkpoint_handoff_require_user_decision",
        "within_approved_set",
        "within_approved_set_preserve_requirements",
    ]
    lead_reserve_access: Literal[False] | None = None


class Rulebook(Contract):
    schema_version: Literal["karajan.rulebook.v1"]
    id: Identifier
    revision: Positive
    status: Literal["draft", "configured", "example_unbound", "published"]
    description: Annotated[str, Field(max_length=8000)]
    collaboration: Collaboration
    profile_groups: dict[Identifier, References]
    global_constraints: GlobalConstraints
    resource_policy: ResourcePolicy
    rules: Annotated[list[Rule], Field(max_length=1000)]


class PathFloor(Contract):
    prefix: Identifier
    minimum_class: Class


class RiskPolicy(Contract):
    id: Identifier
    revision: Positive
    mapping: dict[Identifier, Class]
    path_floors: list[PathFloor]


class HardConstraints(Contract):
    profile_refs: References
    channel_ids: Names
    tools: Names
    data_destinations: Names
    required_capabilities: Names
    min_isolation: Literal["attempt_isolated", "tool_sandboxed"]


class Authorization(HardConstraints):
    ceiling_profile_refs: References
    allowed_stages: list[Stage]
    approved_groups: dict[Identifier, References]
    approved_quality_stage_indices: list[Count]
    budget_ref: Identifier
    currency_limits: dict[Currency, Quantity]
    max_attempt_duration_seconds: Positive
    max_quality_repair_rounds: Count

    @field_validator("currency_limits")
    @classmethod
    def amounts(cls, value: dict[str, str]) -> dict[str, str]:
        for amount in value.values():
            units(amount)
        return value


class Author(Contract):
    profile: ProfileRef
    model_family: Identifier | None
    attempt_id: Identifier
    context_id: Identifier
    complexity: Class
    risk: Identifier
    paths: Names


class TaskClassification(Contract):
    """Approved requirements and recorded authors, independent of route permission."""

    role: Role
    purpose: Literal["lead", "advice"] | None
    readiness: Literal["T0", "ready"]
    complexity: Class
    risk: Identifier
    domains: Names
    paths: Names
    authors: list[Author]


class TaskSnapshot(TaskClassification):
    schema_version: Literal["karajan.routing.task.v1"]
    task_id: Identifier
    task_revision: Positive
    root_task_id: Identifier
    plan_revision: Positive
    authorization_digest: Digest
    required_capabilities: Names
    tools: Names
    context_tokens: Positive
    reserved_output_tokens: Count = 0
    duration_seconds: Positive
    stage: Stage
    quality_stage_index: Count
    failure_reason: Identifier | None
    previous_profile: ProfileRef | None
    quality_repair_rounds_used: Count
    planned_attempt_id: Identifier
    planned_context_id: Identifier
    authorization: Authorization


class ProfileFacts(Contract):
    profile: ProfileRef
    profile_digest: Digest
    runtime_version: Identifier
    roles: list[Role]
    tools: Names
    context_tokens: Positive | None
    data_destination: Identifier
    budget_enforcement: Literal["bounded_calls", "estimated_stop", "unknown"]
    provenance: Literal["fixture", "imported_observation"]
    evidence_ref: Identifier
    observed_at: FiniteFloat
    valid_until: FiniteFloat


class PolicySnapshot(Contract):
    schema_version: Literal["karajan.routing.policy.v1"]
    rulebook: Rulebook
    resources: ResourceCatalog
    approved_profile_refs: References
    profile_facts: list[ProfileFacts]
    risk_policy: RiskPolicy
    constraints: HardConstraints

    @field_validator("resources")
    @classmethod
    def valid_catalog_budgets(cls, value: ResourceCatalog) -> ResourceCatalog:
        for budget in value.budgets:
            for amount in budget.currency_limits.values():
                if amount is not None:
                    units(amount)
        return value


class AccountState(Contract):
    id: Identifier
    policy_revision: Positive
    current_policy_revision: Positive
    policy: CapacityPolicy
    active_attempts: Count
    cash_remaining: dict[Currency, Quantity]
    cooldown_until: FiniteFloat | None
    exhaustion_observation_required: bool


class PoolState(Contract):
    id: Identifier
    account_id: Identifier
    kind: Literal["service", "platform_allowance"]
    unit: Literal["requests", "tokens", "percent"]
    window_kind: Literal["fixed", "rolling", "balance", "unknown"]
    window_id: Identifier
    reported_remaining: Quantity | None
    reported_limit: Quantity | None
    local_uncovered: Quantity
    future_reserved: Quantity
    observed_at: FiniteFloat
    reset_at: FiniteFloat | None
    source: Literal["official", "fixture", "manual", "local_ledger"]
    confidence: Literal["known", "calibrated", "unknown"]
    evidence_ref: Identifier
    coverage_ref: Identifier | None


class PriceQuote(Contract):
    price_revision: Identifier
    profile_digest: Digest
    runtime_version: Identifier
    currency: Currency
    estimated_cash: Quantity | None
    upper_bound: Quantity | None
    coverage: Literal["all_calls", "partial", "unknown"]
    observed_at: FiniteFloat
    valid_until: FiniteFloat
    evidence_ref: Identifier


class Demand(Contract):
    pool_id: Identifier
    unit: Literal["requests", "tokens", "percent"]
    window_id: Identifier
    amount: Quantity


class Estimate(Contract):
    profile: ProfileRef
    demand: list[Demand]
    confidence: Literal["known", "calibrated", "unknown"]
    completion_seconds: Annotated[FiniteFloat, Field(ge=0)] | None
    price: PriceQuote | None
    evidence_ref: Identifier


class FXSnapshot(Contract):
    id: Identifier
    revision: Positive
    reference_currency: Currency
    rates: dict[Currency, Quantity]
    observed_at: FiniteFloat
    valid_until: FiniteFloat
    evidence_ref: Identifier


class CapacitySnapshot(Contract):
    schema_version: Literal["karajan.routing.capacity.v1"]
    id: Identifier
    revision: Positive
    as_of: FiniteFloat
    accounts: list[AccountState]
    pools: list[PoolState]
    estimates: list[Estimate]
    budget_remaining: dict[Identifier, dict[Currency, Quantity]]
    fx: FXSnapshot | None
