"""Deliberately synthetic facts for offline fixture configurations, never live inputs."""

import copy
from typing import Any

from karajan.contracts.credentials import contains_credential
from karajan.projects.models import ConfigurationDraft

from .compiler import RoutingError, digest, parse


def fixture_from_configuration(
    configuration: dict[str, Any], *, as_of: float
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    config = parse(ConfigurationDraft, configuration, "FIXTURE_CONFIGURATION_INVALID")
    if config["resources"] is None or config["rulebook"] is None:
        raise RoutingError("FIXTURE_CONFIGURATION_INVALID")
    resources = config["resources"]
    for registered in resources["profiles"]:
        profile = registered["profile"]
        binding = profile["binding"] if profile else {}
        if contains_credential(binding.get("native_settings", {})):
            raise RoutingError("CREDENTIAL_VALUE_FORBIDDEN")
        if (
            binding.get("runtime_kind") != "fixture-runtime"
            or binding.get("model_id") != "fixture-model"
            or binding.get("auth_mode") != "none"
        ):
            raise RoutingError("FIXTURE_CONFIGURATION_REQUIRED")
    refs = config["approved_profile_refs"]
    tools = sorted(
        {tool for p in resources["profiles"] for tool in p["profile"]["required_permissions"]}
    )
    hard = {
        "profile_refs": refs,
        "channel_ids": [c["id"] for c in resources["channels"]],
        "tools": tools,
        "data_destinations": [c["id"] for c in resources["channels"]],
        "required_capabilities": [],
        "min_isolation": "tool_sandboxed",
    }
    authorization = {
        **copy.deepcopy(hard),
        "ceiling_profile_refs": refs,
        "allowed_stages": ["normal", "quality"],
        "approved_groups": copy.deepcopy(config["rulebook"]["profile_groups"]),
        "approved_quality_stage_indices": [0],
        "budget_ref": "run",
        "currency_limits": {"USD": "0", "CNY": "0"},
        "max_attempt_duration_seconds": 30,
        "max_quality_repair_rounds": 2,
    }
    task = {
        "schema_version": "karajan.routing.task.v1",
        "task_id": "fixture-task",
        "task_revision": 1,
        "root_task_id": "fixture-root",
        "plan_revision": 1,
        "authorization_digest": digest(authorization),
        "role": "worker",
        "purpose": None,
        "readiness": "ready",
        "complexity": "T2",
        "risk": "standard",
        "domains": ["code"],
        "paths": ["src/example.py"],
        "required_capabilities": [],
        "tools": tools,
        "context_tokens": 1000,
        "duration_seconds": 30,
        "stage": "normal",
        "quality_stage_index": 0,
        "failure_reason": None,
        "previous_profile": None,
        "quality_repair_rounds_used": 0,
        "planned_attempt_id": "fixture-next",
        "planned_context_id": "fixture-next-context",
        "authors": [],
        "authorization": authorization,
    }
    policy = {
        "schema_version": "karajan.routing.policy.v1",
        "rulebook": config["rulebook"],
        "resources": resources,
        "approved_profile_refs": refs,
        "constraints": hard,
        "risk_policy": {
            "id": "fixture-risk",
            "revision": 1,
            "mapping": {"standard": "T1", "critical": "T3"},
            "path_floors": [],
        },
        "profile_facts": [],
    }
    capacity: dict[str, Any] = {
        "schema_version": "karajan.routing.capacity.v1",
        "id": "fixture-capacity",
        "revision": 1,
        "as_of": as_of,
        "accounts": [],
        "pools": [],
        "estimates": [],
        "budget_remaining": {b["id"]: b["currency_limits"] for b in resources["budgets"]},
        "fx": None,
    }
    for account in resources["accounts"]:
        capacity["accounts"].append(
            {
                "id": account["id"],
                "policy_revision": 1,
                "current_policy_revision": 1,
                "policy": {
                    "account_id": account["id"],
                    "max_active_attempts": 3,
                    "max_attempt_duration_seconds": 30,
                    "observation_max_age_seconds": 60,
                    "require_official_observation": False,
                    "safety_margin": {},
                    "lead_reserve": {},
                    "lead_reserved_slots": 1,
                    "conservative_mode": None,
                },
                "active_attempts": 0,
                "cash_remaining": {"USD": "0", "CNY": "0"},
                "cooldown_until": None,
                "exhaustion_observation_required": False,
            }
        )
    for pool in resources["quota_pools"]:
        capacity["pools"].append(
            {
                "id": pool["id"],
                "account_id": pool["account_id"],
                "kind": pool["kind"],
                "unit": pool["unit"],
                "window_kind": "fixed",
                "window_id": "fixture-window",
                "reported_remaining": pool["limit"],
                "reported_limit": pool["limit"],
                "local_uncovered": "0",
                "future_reserved": "0",
                "observed_at": as_of,
                "reset_at": as_of + 3600,
                "source": "fixture",
                "confidence": "known",
                "evidence_ref": "fixture:quota",
                "coverage_ref": "fixture:coverage",
            }
        )
    for profile in resources["profiles"]:
        ref = {"id": profile["id"], "revision": profile["revision"]}
        policy["profile_facts"].append(
            {
                "profile": ref,
                "profile_digest": digest(profile["profile"]),
                "runtime_version": profile["profile"]["binding"]["runtime_version"],
                "roles": ["commander", "worker", "reviewer"],
                "tools": tools,
                "context_tokens": 100000,
                "data_destination": profile["profile"]["binding"]["channel_id"],
                "budget_enforcement": "unknown",
                "provenance": "fixture",
                "evidence_ref": "fixture:qualification",
                "observed_at": as_of,
                "valid_until": as_of + 3600,
            }
        )
        capacity["estimates"].append(
            {
                "profile": ref,
                "demand": [
                    {
                        "pool_id": pool["id"],
                        "unit": pool["unit"],
                        "window_id": "fixture-window",
                        "amount": "1",
                    }
                    for pool in resources["quota_pools"]
                    if pool["id"] in profile["quota_pool_refs"]
                ],
                "confidence": "known",
                "completion_seconds": 20.0,
                "price": None,
                "evidence_ref": "fixture:estimate",
            }
        )
    # No aliases between approvals and editable configuration, even in synthetic fixtures.
    return copy.deepcopy(task), copy.deepcopy(policy), copy.deepcopy(capacity)
