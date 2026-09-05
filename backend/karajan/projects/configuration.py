"""Pure checks of fixed M1 inputs. No quota reservation or executor activation."""

import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter, ValidationError

from karajan.resources.broker import units

from .models import ConfigurationDraft, ProfileRef

VALIDATOR_REVISION = "karajan.m1-fixed.v1"


def validator_identity() -> str:
    baseline = json.loads(
        Path(__file__).with_name("fixed-rulebook.v1.json").read_text(encoding="utf-8")
    )
    return hashlib.sha256(
        json.dumps(baseline, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def positive(value: object) -> bool:
    return type(value) is int and value > 0


def contains_credential(value: object) -> bool:
    if isinstance(value, dict):
        forbidden = {
            "api_key",
            "apikey",
            "access_token",
            "refresh_token",
            "authorization",
            "password",
            "client_secret",
            "secret",
            "token",
            "env",
            "environment",
            "headers",
        }
        return any(
            str(key).lower().replace("-", "_") in forbidden
            or str(key)
            .lower()
            .replace("-", "_")
            .endswith(
                ("_api_key", "_auth_token", "_access_token", "_refresh_token", "_client_secret")
            )
            or contains_credential(item)
            for key, item in value.items()
        )
    return isinstance(value, list) and any(contains_credential(item) for item in value)


def fixed_rulebook(document: dict[str, Any]) -> bool:
    baseline = json.loads(
        Path(__file__).with_name("fixed-rulebook.v1.json").read_text(encoding="utf-8")
    )
    try:
        candidate = json.loads(json.dumps(document))
        if set(candidate) != set(baseline) or not positive(candidate["revision"]):
            return False
        if (
            not isinstance(candidate["id"], str)
            or not candidate["id"]
            or candidate["status"] not in {"draft", "configured", "example_unbound"}
        ):
            return False
        groups = candidate["profile_groups"]
        if set(groups) != set(baseline["profile_groups"]):
            return False
        for refs in groups.values():
            TypeAdapter(list[ProfileRef]).validate_python(refs)
        for key in ("id", "revision", "status", "description", "profile_groups"):
            candidate[key] = baseline[key]
        for key in ("planning_budget_ref", "run_budget_ref"):
            reference = candidate["resource_policy"].get(key)
            if reference is not None and not isinstance(reference, str):
                return False
            candidate["resource_policy"][key] = None
        for key in (
            "max_parallel_writers_per_project",
            "max_quality_repair_rounds",
            "max_infrastructure_retries_per_root_task",
        ):
            if not positive(candidate["collaboration"][key]):
                return False
            candidate["collaboration"][key] = baseline["collaboration"][key]
        return bool(candidate == baseline)
    except (KeyError, TypeError, AttributeError, ValidationError):
        return False


def resource_references(resources: dict[str, Any]) -> bool:
    directories = {}
    for kind in ("accounts", "channels", "quota_pools", "budgets"):
        directory = {item["id"]: item for item in resources[kind]}
        if len(directory) != len(resources[kind]):
            return False
        directories[kind] = directory
    accounts = directories["accounts"]
    channels = directories["channels"]
    pools = directories["quota_pools"]
    policies = {item["account_id"] for item in resources["capacity_policies"]}
    if len(policies) != len(resources["capacity_policies"]) or not policies <= accounts.keys():
        return False
    for channel in channels.values():
        if channel["account_id"] not in accounts:
            return False
    for pool in pools.values():
        if pool["account_id"] not in accounts:
            return False
    seen = set()
    for registration in resources["profiles"]:
        identity = (registration["id"], registration["revision"])
        if identity in seen:
            return False
        seen.add(identity)
        profile = registration["profile"]
        if profile is None:
            continue
        binding = profile["binding"]
        channel = channels.get(binding["channel_id"])
        if (
            channel is None
            or channel["account_id"] != binding["account_id"]
            or channel["billing_path"] != binding["billing_path"]
            or identity != (profile["id"], profile["revision"])
        ):
            return False
        if binding["account_id"] not in policies:
            return False
        if not registration["quota_pool_refs"] or len(set(registration["quota_pool_refs"])) != len(
            registration["quota_pool_refs"]
        ):
            return False
        if any(
            ref not in pools or pools[ref]["account_id"] != binding["account_id"]
            for ref in registration["quota_pool_refs"]
        ):
            return False
        if not any(pools[ref]["kind"] == "service" for ref in registration["quota_pool_refs"]):
            return False
    return True


def profile_requirements(document: dict[str, Any]) -> list[dict[str, str]]:
    resources, rulebook = document["resources"], document["rulebook"]
    registered = {(item["id"], item["revision"]): item for item in resources["profiles"]}
    channels = {item["id"]: item for item in resources["channels"]}
    accounts = {item["id"]: item for item in resources["accounts"]}
    approved = {(item["id"], item["revision"]) for item in document["approved_profile_refs"]}
    issues = []
    for rule in rulebook["rules"]:
        required_class = max(
            rule["when"].get("effective_class_in", [rule["when"].get("effective_class", "T1")])
        )
        for group in rule["eligible_groups"] + rule.get("quality_escalation_groups", []):
            refs = rulebook["profile_groups"][group]
            if not refs:
                issues.append(
                    {"code": "PROFILE_GROUP_EMPTY", "path": "rulebook.profile_groups." + group}
                )
            for ref in refs:
                identity = (ref["id"], ref["revision"])
                path = "rulebook.profile_groups." + group
                registration = registered.get(identity)
                if identity not in approved:
                    issues.append({"code": "PROFILE_NOT_APPROVED", "path": path})
                if (
                    registration is None
                    or registration["profile"] is None
                    or not registration["enabled"]
                ):
                    issues.append({"code": "PROFILE_UNAVAILABLE", "path": path})
                    continue
                profile = registration["profile"]
                channel = channels.get(profile["binding"]["channel_id"], {})
                account = accounts.get(profile["binding"]["account_id"], {})
                if (
                    not channel.get("approved_data_destination")
                    or account.get("secret_ref") != profile["auth_ref"]
                    or registration["required_isolation"] != "tool_sandboxed"
                    or not registration["model_family"]
                ):
                    issues.append({"code": "PROFILE_PERMISSION_UNVERIFIED", "path": path})
                digest = hashlib.sha256(
                    json.dumps(profile, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                if registration["max_class"] is None or registration["max_class"] < required_class:
                    issues.append({"code": "PROFILE_CLASS_INSUFFICIENT", "path": path})
                for capability in rule["capabilities_all"]:
                    evidence = [
                        item
                        for item in registration["capability_evidence"]
                        if item["capability"] == capability
                    ]
                    if len(evidence) != 1 or any(
                        item["status"] != "passed"
                        or item["profile_digest"] != digest
                        or item["runtime_version"] != profile["binding"]["runtime_version"]
                        or not item["evidence_ref"]
                        or item["provenance"] is None
                        for item in evidence
                    ):
                        issues.append(
                            {"code": "CAPABILITY_NOT_PASSED", "path": path + "." + capability}
                        )
    return issues


def validate_configuration(document: dict[str, Any]) -> list[dict[str, str]]:
    if contains_credential(document):
        return [{"code": "CREDENTIAL_VALUE_FORBIDDEN", "path": "configuration"}]
    try:
        document = ConfigurationDraft.model_validate(document).model_dump()
    except ValidationError:
        return [{"code": "CONFIGURATION_SCHEMA_INVALID", "path": "configuration"}]
    issues = []
    rulebook = document.get("rulebook")
    resources = document.get("resources")
    if not rulebook:
        issues.append({"code": "RULEBOOK_REQUIRED", "path": "rulebook"})
    if not resources:
        issues.append({"code": "RESOURCES_REQUIRED", "path": "resources"})
    if rulebook and not fixed_rulebook(rulebook):
        issues.append({"code": "RULEBOOK_HARD_CONSTRAINT_INVALID", "path": "rulebook"})
        return issues
    if rulebook and resources:
        if not resource_references(resources):
            issues.append({"code": "RESOURCE_REFERENCE_INVALID", "path": "resources"})
        issues.extend(profile_requirements(document))
        budgets = {item["id"]: item for item in resources.get("budgets", [])}
        for index, budget in enumerate(resources.get("budgets", [])):
            try:
                limits = budget.get("currency_limits")
                if not isinstance(limits, dict) or not limits:
                    raise ValueError("limits")
                for currency, amount in limits.items():
                    if (
                        len(currency) != 3
                        or not currency.isascii()
                        or not currency.isalpha()
                        or not currency.isupper()
                    ):
                        raise ValueError("currency")
                    units(amount)
                if not positive(budget.get("max_total_attempts")) or not positive(
                    budget.get("max_duration_seconds")
                ):
                    raise ValueError("bounds")
            except ValueError:
                issues.append({"code": "BUDGET_INVALID", "path": f"resources.budgets.{index}"})
        for field, scope in (("planning_budget_ref", "planning"), ("run_budget_ref", "run")):
            reference = rulebook.get("resource_policy", {}).get(field)
            if reference not in budgets or budgets[reference].get("scope") != scope:
                issues.append(
                    {"code": "BUDGET_REQUIRED", "path": "rulebook.resource_policy." + field}
                )
        for index, policy in enumerate(resources.get("capacity_policies", [])):
            mode = policy.get("conservative_mode") or {}
            if mode.get("enabled") is not True or any(
                not positive(mode.get(field))
                for field in (
                    "max_local_active_attempts",
                    "max_attempt_duration_seconds",
                    "observation_max_age_seconds",
                    "cooldown_seconds",
                )
            ):
                issues.append(
                    {
                        "code": "UNKNOWN_QUOTA_UNBOUNDED",
                        "path": f"resources.capacity_policies.{index}.conservative_mode",
                    }
                )
    return issues
