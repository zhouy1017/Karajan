"""V2 execution proposals and server-resolved, approval-bound routing grants."""

from typing import Any, Literal

from pydantic import Field

from karajan.contracts.probe import Contract
from karajan.projects.models import Identifier, ProfileRef
from karajan.resources.broker import units
from karajan.routing.compiler import compile_rulebook, digest
from karajan.routing.models import Count, Currency, Names, Positive, Quantity

from .models import (
    ApprovePlan,
    Authorization,
    CreateRun,
    Digest,
    PlanTask,
    Text,
)


class PolicyRef(ProfileRef):
    revision: Positive
    digest: Digest


class StagePermission(Contract):
    normal: bool
    quality_indices: list[Count] = Field(max_length=1000)


class AuthorizationV2(Authorization):
    channel_ids: Names
    tools: Names
    data_destinations: Names
    required_capabilities: Names
    min_isolation: Literal["tool_sandboxed"]
    currency_limits: dict[Currency, Quantity]
    max_attempt_duration_seconds: Positive
    max_quality_repair_rounds: Count
    stage_permissions: dict[Identifier, StagePermission]


class CreateRunV2(CreateRun):
    schema_version: Literal["karajan.create-run.v2"]
    execution_policy: PolicyRef
    authorization: AuthorizationV2


class PlanTaskV2(PlanTask):
    purpose: Literal["lead", "advice"] | None
    domains: Names
    required_capabilities: Names
    tools: Names
    context_tokens: Positive
    duration_seconds: Positive


class PlanV2(Contract):
    summary: Text
    authorization: AuthorizationV2
    tasks: list[PlanTaskV2] = Field(min_length=1, max_length=100)


class SubmitPlanV2(Contract):
    schema_version: Literal["karajan.submit-plan.v2"]
    term: Positive
    intent_id: Identifier
    expected_plan_revision: Count
    plan: PlanV2


class ApprovePlanV2(ApprovePlan):
    schema_version: Literal["karajan.approve-plan.v2"]
    routing_digest: Digest


def validate_authorization(
    proposed: dict[str, Any],
    ceiling: dict[str, Any],
    policy: dict[str, Any],
    config: dict[str, Any],
) -> None:
    hard = policy["constraints"]
    for kind in ("channel_ids", "tools", "data_destinations"):
        values = proposed[kind]
        if len(set(values)) != len(values) or not set(values) <= set(ceiling[kind]) & set(
            hard[kind]
        ):
            raise ValueError("ROUTING_AUTHORIZATION_EXCEEDED")
    refs = {(r["id"], r["revision"]) for r in hard["profile_refs"]}
    proposed_refs = [(r["id"], r["revision"]) for r in proposed["profile_refs"]]
    if len(set(proposed_refs)) != len(proposed_refs) or not set(proposed_refs) <= refs:
        raise ValueError("ROUTING_AUTHORIZATION_EXCEEDED")
    if not set(ceiling["required_capabilities"] + hard["required_capabilities"]) <= set(
        proposed["required_capabilities"]
    ):
        raise ValueError("ROUTING_REQUIREMENTS_REMOVED")
    budget = next(b for b in config["resources"]["budgets"] if b["id"] == proposed["budget_ref"])
    for currency, amount in proposed["currency_limits"].items():
        limits = [ceiling["currency_limits"].get(currency), budget["currency_limits"].get(currency)]
        if any(limit is None or units(amount) > units(limit) for limit in limits):
            raise ValueError("ROUTING_CASH_CEILING_EXCEEDED")
    if proposed["max_attempt_duration_seconds"] > min(
        ceiling["max_attempt_duration_seconds"], budget["max_duration_seconds"]
    ) or proposed["max_quality_repair_rounds"] > min(
        ceiling["max_quality_repair_rounds"],
        config["rulebook"]["collaboration"]["max_quality_repair_rounds"],
    ):
        raise ValueError("ROUTING_LIMIT_EXCEEDED")
    rules = {rule["id"]: rule for rule in config["rulebook"]["rules"]}
    for rule_id, grant in proposed["stage_permissions"].items():
        prior = ceiling["stage_permissions"].get(rule_id)
        rule = rules.get(rule_id)
        indices = grant["quality_indices"]
        if (
            prior is None
            or rule is None
            or grant["normal"]
            and not prior["normal"]
            or len(set(indices)) != len(indices)
            or not set(indices) <= set(prior["quality_indices"])
            or any(index >= len(rule.get("quality_escalation_groups", [])) for index in indices)
        ):
            raise ValueError("ROUTING_STAGE_NOT_AUTHORIZED")


def resolve_binding(run: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    policy = run["execution_policy_snapshot"]
    config = run["configuration_snapshot"]["configuration"]
    authorization = plan["authorization"]
    validate_authorization(authorization, run["authorization_ceiling"], policy, config)
    compiled = compile_rulebook(config["rulebook"])
    if compiled["issues"]:
        raise ValueError("RULEBOOK_NOT_COMPILABLE")
    rulebook = compiled["document"]
    permitted = {(r["id"], r["revision"]) for r in authorization["profile_refs"]} & {
        (r["id"], r["revision"]) for r in run["authorization_ceiling"]["profile_refs"]
    }
    grants: dict[str, Any] = {}
    for rule in rulebook["rules"]:
        permission = authorization["stage_permissions"].get(rule["id"])
        if permission is None:
            continue

        def members(group: str) -> list[dict[str, Any]]:
            return [
                ref
                for ref in rulebook["profile_groups"][group]
                if (ref["id"], ref["revision"]) in permitted
            ]

        grants[rule["id"]] = {
            "normal": {group: members(group) for group in rule["eligible_groups"]}
            if permission["normal"]
            else {},
            "quality": [
                {"index": index, "group": group, "profiles": members(group)}
                for index, group in enumerate(rule["quality_escalation_groups"])
                if index in permission["quality_indices"]
            ],
        }
    for task in plan["tasks"]:
        if (
            (task["role"] == "commander") != (task["purpose"] is not None)
            or task["risk"] not in policy["risk_policy"]["mapping"]
            or not set(task["tools"]) <= set(authorization["tools"])
            or task["context_tokens"] + policy["context_policy"]["reserved_output_tokens"]
            > policy["max_context_tokens"]
            or task["duration_seconds"] > authorization["max_attempt_duration_seconds"]
        ):
            raise ValueError("TASK_EXECUTION_REQUIREMENTS_NOT_AUTHORIZED")
        for kind in ("tools", "domains", "required_capabilities"):
            if len(set(task[kind])) != len(task[kind]):
                raise ValueError("TASK_EXECUTION_REQUIREMENTS_INVALID")
    return {
        "schema_version": "karajan.approved-routing-binding.v1",
        "execution_policy": {
            "id": policy["id"],
            "revision": policy["revision"],
            "digest": policy["digest"],
            "project_id": policy["project_id"],
            "registered_by": policy["registered_by"],
        },
        "configuration_digest": run["configuration_snapshot"]["digest"],
        "rulebook": {
            "id": rulebook["id"],
            "revision": rulebook["revision"],
            "digest": compiled["rulebook_sha256"],
        },
        "stage_grants": grants,
        "authorization_ceiling_digest": digest(run["authorization_ceiling"]),
        "task_requirements": {
            task["id"]: {
                key: task[key]
                for key in (
                    "revision",
                    "role",
                    "purpose",
                    "readiness",
                    "complexity",
                    "risk",
                    "paths",
                    "domains",
                    "required_capabilities",
                    "tools",
                    "context_tokens",
                    "duration_seconds",
                )
            }
            for task in plan["tasks"]
        },
        "activation_allowed": False,
    }
