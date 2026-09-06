"""Explicit owner-fixed execution constraints; these records are not qualifications."""

from typing import Annotated, Any, Literal

from pydantic import Field, ValidationError

from karajan.contracts.probe import Contract
from karajan.routing.compiler import RoutingError, digest, parse
from karajan.routing.models import Count, Digest, HardConstraints, Names, Positive, RiskPolicy

from .models import Identifier, ProfileRef


class ToolPolicy(ProfileRef):
    tool_permissions: dict[Identifier, Names]


class ContextPolicy(ProfileRef):
    input_accounting: Literal["explicit_approved_upper_bound"]
    reserved_output_tokens: Count


class ContextMeasurement(Contract):
    method: Literal["reference_tokenizer_estimate"]
    source_sha256: Digest
    fixed_margin: Count
    ratio_margin_basis_points: Annotated[Count, Field(le=10_000)]


class MeasuredContextPolicy(ContextPolicy):
    measurement: ContextMeasurement


Argument = Annotated[str, Field(max_length=8192, pattern=r"^[^\x00]*$")]
EnvironmentName = Annotated[
    str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
]


class ValidationCheck(ProfileRef):
    argv: Annotated[list[Argument], Field(min_length=1, max_length=128)]
    environment_ref: ProfileRef
    timeout_seconds: Positive


class ValidationEnvironment(ProfileRef):
    runtime_kind: Identifier
    platform: Literal["linux_x64", "windows_x64"]
    source_sha256: Digest
    filesystem: Literal["candidate_copy"]
    network: Literal["none"]
    env: Annotated[dict[EnvironmentName, Argument], Field(max_length=128)]
    max_log_bytes: Positive


class ValidationReview(ProfileRef):
    id: Literal["independent_review"]
    environment_ref: ProfileRef
    context_policy: Literal["candidate_and_acceptance_only"]
    independence_policy: Literal["existing_candidate_independence_v1"]


class ValidationPolicy(ProfileRef):
    checks: Annotated[list[ValidationCheck], Field(min_length=1, max_length=100)]
    environments: Annotated[list[ValidationEnvironment], Field(min_length=1, max_length=100)]
    review: ValidationReview


class _ExecutionConstraints(Contract):
    id: Identifier
    revision: Positive
    configuration_digest: Digest
    constraints: HardConstraints
    risk_policy: RiskPolicy
    channel_destinations: dict[Identifier, Identifier]
    tool_policy: ToolPolicy
    context_policy: ContextPolicy
    max_context_tokens: Positive


class ExecutionPolicy(_ExecutionConstraints):
    schema_version: Literal["karajan.execution-policy.v1"]


class ExecutionPolicyV2(_ExecutionConstraints):
    schema_version: Literal["karajan.execution-policy.v2"]
    context_policy: MeasuredContextPolicy
    validation: ValidationPolicy


def validate_policy(request: dict[str, Any], configuration: dict[str, Any]) -> dict[str, Any]:
    model = (
        ExecutionPolicyV2
        if isinstance(request, dict)
        and request.get("schema_version") == "karajan.execution-policy.v2"
        else ExecutionPolicy
    )
    policy = parse(model, request, "EXECUTION_POLICY_INVALID")
    if policy["configuration_digest"] != digest(configuration):
        raise RoutingError("EXECUTION_POLICY_CONFIGURATION_CHANGED")
    hard = policy["constraints"]
    refs = {(r["id"], r["revision"]) for r in configuration["approved_profile_refs"]}
    selected = [(r["id"], r["revision"]) for r in hard["profile_refs"]]
    if len(set(selected)) != len(selected) or not set(selected) <= refs:
        raise RoutingError("EXECUTION_POLICY_PROFILE_DENIED")
    for key in ("channel_ids", "tools", "data_destinations", "required_capabilities"):
        if len(set(hard[key])) != len(hard[key]):
            raise RoutingError("EXECUTION_POLICY_DUPLICATE")
    channels = {c["id"]: c for c in configuration["resources"]["channels"]}
    if (
        set(policy["channel_destinations"]) != set(hard["channel_ids"])
        or not set(hard["channel_ids"]) <= channels.keys()
        or not set(policy["channel_destinations"].values()) <= set(hard["data_destinations"])
        or hard["min_isolation"] != "tool_sandboxed"
        or policy["risk_policy"]["mapping"].get("critical") != "T3"
        or "standard" not in policy["risk_policy"]["mapping"]
        or set(policy["tool_policy"]["tool_permissions"]) != set(hard["tools"])
        or policy["context_policy"]["reserved_output_tokens"] >= policy["max_context_tokens"]
    ):
        raise RoutingError("EXECUTION_POLICY_CONSTRAINT_INVALID")
    for permissions in policy["tool_policy"]["tool_permissions"].values():
        if not permissions or len(set(permissions)) != len(permissions):
            raise RoutingError("EXECUTION_POLICY_TOOL_DEFINITION_INVALID")
    # Use the same strict path syntax as approved plans, without importing their service.
    from karajan.runs.validation import path_parts

    try:
        for floor in policy["risk_policy"]["path_floors"]:
            path_parts(floor["prefix"])
    except (ValueError, ValidationError):
        raise RoutingError("EXECUTION_POLICY_RISK_PATH_INVALID") from None
    if policy["schema_version"] == "karajan.execution-policy.v2":
        _validate_validation(policy["validation"])
    return policy


def _validate_validation(validation: dict[str, Any]) -> None:
    checks = validation["checks"]
    environments = validation["environments"]
    names = [row["id"] for row in checks]
    environment_names = [row["id"] for row in environments]
    references = {(row["id"], row["revision"]) for row in environments}
    consumers = [*checks, validation["review"]]
    if (
        len(set(names)) != len(names)
        or "independent_review" in names
        or len(set(environment_names)) != len(environment_names)
        or any(not check["argv"][0].strip() for check in checks)
        or any(
            (row["environment_ref"]["id"], row["environment_ref"]["revision"]) not in references
            for row in consumers
        )
        or any(
            len({name.casefold() for name in row["env"]}) != len(row["env"]) for row in environments
        )
    ):
        raise RoutingError("EXECUTION_POLICY_VALIDATION_INVALID")


def policy_components(policy: dict[str, Any]) -> dict[tuple[str, str, int], dict[str, Any]]:
    """Versioned component identities share the existing project policy ledger."""
    rows = [(kind, policy[kind]) for kind in ("risk_policy", "tool_policy", "context_policy")]
    if policy["schema_version"] == "karajan.execution-policy.v2":
        validation = policy["validation"]
        rows += [("validation", validation), ("validation.review", validation["review"])]
        rows += [("validation.check", row) for row in validation["checks"]]
        rows += [("validation.environment", row) for row in validation["environments"]]
    return {(kind, row["id"], row["revision"]): row for kind, row in rows}
