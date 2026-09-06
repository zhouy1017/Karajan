"""Explicit owner-fixed execution constraints; these records are not qualifications."""

from typing import Any, Literal

from pydantic import ValidationError

from karajan.contracts.probe import Contract
from karajan.routing.compiler import RoutingError, digest, parse
from karajan.routing.models import Count, Digest, HardConstraints, Names, Positive, RiskPolicy

from .models import Identifier, ProfileRef


class ToolPolicy(ProfileRef):
    tool_permissions: dict[Identifier, Names]


class ContextPolicy(ProfileRef):
    input_accounting: Literal["explicit_approved_upper_bound"]
    reserved_output_tokens: Count


class ExecutionPolicy(Contract):
    schema_version: Literal["karajan.execution-policy.v1"]
    id: Identifier
    revision: Positive
    configuration_digest: Digest
    constraints: HardConstraints
    risk_policy: RiskPolicy
    channel_destinations: dict[Identifier, Identifier]
    tool_policy: ToolPolicy
    context_policy: ContextPolicy
    max_context_tokens: Positive


def validate_policy(request: dict[str, Any], configuration: dict[str, Any]) -> dict[str, Any]:
    policy = parse(ExecutionPolicy, request, "EXECUTION_POLICY_INVALID")
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
    return policy
