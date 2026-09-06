"""Bounded replay input tied to an exact runtime and immutable attempt."""

from typing import Annotated, Literal

from karajan.contracts.probe import AttemptManifest, Contract, PositiveInteger, Profile, Provenance
from pydantic import AwareDatetime, Field, JsonValue

RUNTIME_VERSION = "2.1.260"
PROTOCOL_SHA256 = "febb1aee19e47e03433d89cd4b1f8c5636950e206df77e4f4ca2738b0c900393"
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Label = Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[^\x00-\x1f\x7f]+$")]


class NativeStep(Contract):
    kind: Literal["native"]
    at: AwareDatetime
    message: dict[str, JsonValue]


class ControllerStep(Contract):
    kind: Literal["controller"]
    at: AwareDatetime
    event_id: Label
    action: Literal["cancel_requested", "authorization_revoked", "fence_replaced"]
    attempt_id: Label
    fence: PositiveInteger


Step = Annotated[NativeStep | ControllerStep, Field(discriminator="kind")]


class ReplayDocument(Contract):
    schema_version: Literal["karajan.claude-replay.v1"]
    case_id: Label
    runtime_version: Literal["2.1.260"]
    protocol_reference_sha256: Literal[
        "febb1aee19e47e03433d89cd4b1f8c5636950e206df77e4f4ca2738b0c900393"
    ]
    profile: Profile
    profile_digest: Digest
    attempt: AttemptManifest
    session_id: Annotated[
        str,
        Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"),
    ]
    cwd: Label
    started_at: AwareDatetime
    max_attempt_duration_seconds: Annotated[int, Field(gt=0, le=3600)]
    configuration_source_sha256: Digest
    provenance: Provenance
    steps: Annotated[list[Step], Field(max_length=10000)]
