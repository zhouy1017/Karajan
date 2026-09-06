"""Inputs supplied only by the trusted controller, never directly by an agent."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

Identifier = Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[^\s\x00-\x1f]+$")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Actor(Contract):
    attempt_id: Identifier
    fence: Annotated[int, Field(gt=0)]
    profile_id: Identifier
    profile_revision: Annotated[int, Field(gt=0)]
    model_family: Identifier | None
    context_id: Identifier
    provenance_ref: Identifier


class Writer(Contract):
    attempt_id: Identifier
    fence: Annotated[int, Field(gt=0)]
    stopped: bool
    observation_ref: Identifier


class Check(Contract):
    id: Identifier
    revision: Annotated[int, Field(gt=0)]
    argv: Annotated[list[str], Field(min_length=1)]
    environment_sha256: Digest


class Reviewer(Contract):
    profile_id: Identifier
    profile_revision: Annotated[int, Field(gt=0)]
    model_family: Identifier | None
    qualification_ref: Identifier


class ReviewPolicy(Contract):
    revision: Annotated[int, Field(gt=0)]
    environment_sha256: Digest
    approved_reviewers: list[Reviewer]


class Policy(Contract):
    id: Identifier
    revision: Annotated[int, Field(gt=0)]
    checks: Annotated[list[Check], Field(min_length=1)]
    review: ReviewPolicy


class Freeze(Contract):
    series_id: Identifier
    baseline_id: Identifier
    input_sha256: Digest
    allowed_paths: Annotated[list[str], Field(min_length=1)]
    task_class: Literal["T1", "T2", "T3"]
    writer: Writer
    authors: Annotated[list[Actor], Field(min_length=1)]
    policy: Policy


class CurrentContext(Contract):
    repository_identity: Identifier
    base_sha: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    input_sha256: Digest
    policy_sha256: Digest


class EvidenceInput(Contract):
    evidence_key: Identifier
    candidate_id: Identifier
    policy_sha256: Digest
    input_sha256: Digest
    environment_sha256: Digest
    observation_ref: Identifier
    provenance: Literal["fixture", "trusted_observation"]


class CheckResult(EvidenceInput):
    check_id: Identifier
    check_revision: Annotated[int, Field(gt=0)]
    executor_ref: Identifier
    exit_code: int | None
    outcome: Literal["completed", "timed_out", "cancelled", "unknown"]


class Finding(Contract):
    severity: Literal["critical", "high", "medium", "low"]
    file: str
    line: Annotated[int, Field(gt=0)]
    behavior: Annotated[str, Field(min_length=1)]
    trigger: Annotated[str, Field(min_length=1)]
    acceptance_ref: Identifier
    blocking: bool


class ReviewResult(EvidenceInput):
    review_revision: Annotated[int, Field(gt=0)]
    check_evidence_ids: list[Identifier]
    actor: Actor
    author_reasoning_included: bool
    verdict: Literal["passed", "failed", "inconclusive"]
    findings: list[Finding]
