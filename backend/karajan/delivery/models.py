"""Frozen content and permission identities for a delivery revision."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

Identifier = Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[^\s\x00-\x1f]+$")]
Sha = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class DeliveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    run_id: Identifier
    delivery_revision: Annotated[int, Field(gt=0)]
    repository_id: Identifier
    managed_branch: Identifier
    base_branch: Identifier
    tested_base_sha: Sha
    candidate_id: Identifier
    content_sha256: Digest
    tree_sha: Sha
    commit_sha: Sha
    authorization_sha256: Digest
    evidence_sha256: Digest
    verification_ref: Identifier
    expected_old_sha: Sha | None
    require_ci: bool


class VerificationReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    receipt_ref: Identifier
    binding_sha256: Digest
    authority_revision: Identifier
    decision: Literal["allow", "deny", "unknown"]
    provenance: Literal["fixture", "imported_observation"]


class PullRequestObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: Identifier
    repository_id: Identifier
    managed_branch: Identifier
    base_branch: Identifier
    run_id: Identifier
    head_sha: Sha
    state: Literal["open", "closed"]
    merged: bool
    ci_sha: Sha | None
    ci_status: Literal[
        "unknown",
        "pending",
        "success",
        "failure",
        "cancelled",
        "skipped",
        "neutral",
        "timed_out",
        "action_required",
        "queued",
        "in_progress",
    ]
