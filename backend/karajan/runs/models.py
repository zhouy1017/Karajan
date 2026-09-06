"""Strict proposal inputs; identity is supplied by the trusted controller."""

from typing import Annotated, Literal

from pydantic import Field

from karajan.contracts.probe import Contract, Identifier, PositiveInteger
from karajan.projects.models import ProfileRef

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Text = Annotated[str, Field(min_length=1, max_length=8000, pattern=r"\S")]


class Requirement(Contract):
    goal: Text
    acceptance: Annotated[list[Text], Field(min_length=1, max_length=100)]


class Participant(Contract):
    principal: Identifier
    profile: ProfileRef
    purpose: Literal["lead", "advice", "candidate"]


class Authorization(Contract):
    profile_refs: Annotated[list[ProfileRef], Field(min_length=1, max_length=100)]
    read_paths: Annotated[list[Identifier], Field(min_length=1, max_length=100)]
    write_paths: Annotated[list[Identifier], Field(max_length=100)]
    budget_ref: Identifier
    checks: Annotated[list[Identifier], Field(min_length=1, max_length=100)]
    delivery: Literal["none", "pull_request"]
    target_branch: Identifier


class CreateRun(Contract):
    project_id: Identifier
    project_revision: PositiveInteger
    configuration_digest: Digest
    requirement: Requirement
    participants: Annotated[list[Participant], Field(min_length=1, max_length=30)]
    authorization: Authorization


class PlanningReceipt(Contract):
    receipt_ref: Identifier
    authority_revision: Identifier
    run_id: Identifier
    intent_id: Identifier
    term: PositiveInteger
    principal: Identifier
    profile: ProfileRef
    budget_ref: Identifier
    state: Literal["admitted", "denied", "unknown"]
    provenance: Literal["fixture", "imported_observation"]


class PlanTask(Contract):
    id: Identifier
    revision: PositiveInteger
    role: Literal["commander", "worker", "reviewer"]
    readiness: Literal["T0", "ready"]
    complexity: Literal["T1", "T2", "T3"]
    risk: Literal["standard", "critical"]
    paths: Annotated[list[Identifier], Field(max_length=100)]
    depends_on: Annotated[list[Identifier], Field(max_length=100)]
    acceptance: Annotated[list[Text], Field(min_length=1, max_length=100)]
    required: bool


class Plan(Contract):
    summary: Text
    authorization: Authorization
    tasks: Annotated[list[PlanTask], Field(min_length=1, max_length=100)]


class SubmitPlan(Contract):
    term: PositiveInteger
    intent_id: Identifier
    expected_plan_revision: Annotated[int, Field(ge=0)]
    plan: Plan


class ApprovePlan(Contract):
    term: PositiveInteger
    plan_revision: PositiveInteger
    plan_digest: Digest
    authorization_digest: Digest
    configuration_digest: Digest


class ArtifactRef(Contract):
    ref: Identifier
    sha256: Digest


class Checkpoint(Contract):
    summary: Text
    artifacts: Annotated[list[ArtifactRef], Field(max_length=100)]


class ResourceImpact(Contract):
    budget_ref: Identifier
    summary: Text


class ProposeHandoff(Contract):
    term: PositiveInteger
    expected_plan_revision: Annotated[int, Field(ge=0)]
    candidate: Identifier
    checkpoint: Checkpoint
    resource_impact: ResourceImpact
    expires_at: Annotated[float, Field(gt=0, allow_inf_nan=False)]


class DecideHandoff(Contract):
    handoff_id: Identifier
    handoff_digest: Digest
    term: PositiveInteger
    decision: Literal["approve", "reject"]
