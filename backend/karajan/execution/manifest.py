"""Versioned Host identities for model attempts and deterministic Checks.

This union belongs to local process supervision. The original model/probe
contract stays unchanged, and a Check carries its approved environment and
execution identity instead of a model Profile.
"""

from typing import Annotated, Literal

from pydantic import Field, TypeAdapter

from karajan.contracts.probe import AttemptManifest, Contract, Identifier, PositiveInteger

Sha256 = Annotated[str, Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")]


class CheckAttemptManifest(Contract):
    schema_version: Literal["karajan.check-attempt.v1"]
    id: Identifier
    fence: PositiveInteger
    role: Literal["check"]
    authorization_ref: Identifier
    budget_ref: Identifier
    permissions: list[Identifier]
    environment_id: Identifier
    environment_revision: PositiveInteger
    environment_source_sha256: Sha256
    execution_sha256: Sha256


HostManifest = AttemptManifest | CheckAttemptManifest
_manifest: TypeAdapter[HostManifest] = TypeAdapter(
    Annotated[HostManifest, Field(discriminator="role")]
)


def parse_host_manifest(value: object) -> HostManifest:
    """Select one exact kind; never infer a Check or add a model identity."""
    if isinstance(value, (AttemptManifest, CheckAttemptManifest)):
        value = value.model_dump(warnings=False)
    return _manifest.validate_python(value, strict=True)


def parse_host_manifest_json(value: str | bytes | bytearray) -> HostManifest:
    return _manifest.validate_json(value, strict=True)
