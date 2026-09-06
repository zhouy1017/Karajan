"""Compile an explicit routing DSL; no file, database, network or code evaluation."""

import hashlib
import json
from itertools import product
from typing import Any

from pydantic import BaseModel, ValidationError

from .models import Rulebook

COMPILER_REVISION = "karajan.routing.compiler.v1"


class RoutingError(ValueError):
    def __init__(self, code: str, issues: list[dict[str, str]] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.issues = issues or []


def canonical(value: Any) -> str:
    try:
        checked = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        # Reject invalid Unicode before a future storage/HTTP boundary sees it.
        checked.encode("utf-8")
        # Profile fingerprints must agree with the existing project and Run encoders.
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise RoutingError("ROUTING_INPUT_INVALID") from None


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def parse(model: type[BaseModel], value: Any, code: str) -> dict[str, Any]:
    canonical(value)
    try:
        return model.model_validate(value).model_dump()
    except (ValidationError, ValueError, TypeError) as error:
        issues = (
            [
                {"path": ".".join(map(str, item["loc"])), "code": item["type"]}
                for item in error.errors(
                    include_url=False, include_context=False, include_input=False
                )
            ]
            if isinstance(error, ValidationError)
            else []
        )
        raise RoutingError(code, issues) from None


def reference(value: dict[str, Any]) -> tuple[str, int]:
    return value["id"], value["revision"]


def compile_rulebook(document: dict[str, Any]) -> dict[str, Any]:
    result = parse(Rulebook, document, "RULEBOOK_INVALID")
    if not result["rules"]:
        raise RoutingError("RULES_REQUIRED")
    for name, value in result["global_constraints"].items():
        if type(value) is bool and type(document["global_constraints"][name]) is not bool:
            raise RoutingError("RULEBOOK_INVALID")
    rule_ids = [row["id"] for row in result["rules"]]
    if len(rule_ids) != len(set(rule_ids)):
        raise RoutingError("RULE_IDENTITY_CONFLICT")
    for refs in result["profile_groups"].values():
        if len(refs) != len({reference(ref) for ref in refs}):
            raise RoutingError("PROFILE_REFERENCE_DUPLICATED")
    for rule in result["rules"]:
        when = rule["when"]
        if (
            when["effective_class"] is not None
            and when["effective_class_in"] is not None
            or when["effective_class_in"] == []
        ):
            raise RoutingError("RULE_CONDITION_INVALID")
        if when["role"] != "commander" and when["purpose"] is not None:
            raise RoutingError("RULE_CONDITION_INVALID")
        if (
            when["role"] == "commander"
            and when["purpose"] != "advice"
            and rule["reroute"] != "propose_checkpoint_handoff_require_user_decision"
        ):
            raise RoutingError("COMMANDER_HANDOFF_REQUIRED")
        for key in (
            "eligible_groups",
            "capabilities_all",
            "quality_escalation_groups",
            "independence",
        ):
            if len(rule[key]) != len(set(rule[key])):
                raise RoutingError("RULE_SET_DUPLICATED")
        if set(rule["eligible_groups"]) & set(rule["quality_escalation_groups"]):
            raise RoutingError("QUALITY_STAGE_CYCLE")
        for key in ("domains_all", "risks_in", "effective_class_in"):
            if when[key] is not None and len(when[key]) != len(set(when[key])):
                raise RoutingError("RULE_CONDITION_INVALID")
        original = next(row for row in document["rules"] if row["id"] == rule["id"])
        if (
            original.get("lead_reserve_access") is not None
            and type(original["lead_reserve_access"]) is not bool
        ):
            raise RoutingError("RULEBOOK_INVALID")
        groups = rule["eligible_groups"] + rule["quality_escalation_groups"]
        if any(group not in result["profile_groups"] for group in groups):
            raise RoutingError("GROUP_REFERENCE_UNKNOWN")
        if len(rule["profile_preferences"]) != len(
            {reference(p["profile"]) for p in rule["profile_preferences"]}
        ):
            raise RoutingError("PROFILE_PREFERENCE_DUPLICATED")
        members = {reference(ref) for group in groups for ref in result["profile_groups"][group]}
        if any(reference(pref["profile"]) not in members for pref in rule["profile_preferences"]):
            raise RoutingError("PROFILE_PREFERENCE_UNKNOWN")
    queue = result["resource_policy"]["queue_order"]
    if len(queue) != 4 or set(queue) != {
        "lead_feedback",
        "required_review_repair_and_critical_path",
        "worker",
        "optional_adviser",
    }:
        raise RoutingError("QUEUE_ORDER_INVALID")
    order = result["resource_policy"]["candidate_order"]
    required = {
        "preference_band",
        "uncertainty_band",
        "bottleneck_quota_pressure",
        "incremental_cash_estimate",
        "completion_time_estimate",
        "profile_id",
    }
    if len(order) != len(required) or set(order) != required or order[-1] != "profile_id":
        raise RoutingError("CANDIDATE_ORDER_INVALID")
    warnings = [
        {"path": f"profile_groups.{key}", "code": "GROUP_EMPTY"}
        for key, refs in sorted(result["profile_groups"].items())
        if not refs
    ]
    issues = []
    for index, rule in enumerate(result["rules"]):
        for name in ("eligible_groups", "capabilities_all"):
            if not rule[name]:
                issues.append({"path": f"rules.{index}.{name}", "code": "RULE_REQUIREMENT_EMPTY"})
        higher = [
            row["when"]
            for row in result["rules"]
            if row["priority"] > rule["priority"] and row["when"]["role"] == rule["when"]["role"]
        ]
        for other in result["rules"][:index]:
            if rule["priority"] == other["priority"] and overlap(
                rule["when"], other["when"], higher
            ):
                issues.append(
                    {"path": f"rules.{index}", "code": "RULE_AMBIGUOUS", "other_rule": other["id"]}
                )
                break
    executable = {
        key: value
        for key, value in result.items()
        if key not in {"id", "revision", "status", "description"}
    }
    return {
        "schema_version": "karajan.routing.compiled.v1",
        "status": "compiled",
        "compiler_revision": COMPILER_REVISION,
        "rulebook_sha256": digest(executable),
        "document": result,
        "issues": issues,
        "warnings": warnings,
        "activation_allowed": False,
    }


def overlap(
    left: dict[str, Any], right: dict[str, Any], higher: list[dict[str, Any]] | None = None
) -> bool:
    for name in ("role", "purpose", "readiness"):
        if left[name] is not None and right[name] is not None and left[name] != right[name]:
            return False

    def classes(row: dict[str, Any]) -> set[str]:
        return (
            {row["effective_class"]}
            if row["effective_class"]
            else set(row["effective_class_in"] or ("T1", "T2", "T3"))
        )

    shared_classes = classes(left) & classes(right)
    if not shared_classes:
        return False
    left_risks, right_risks = set(left["risks_in"]), set(right["risks_in"])
    risks = left_risks & right_risks if left_risks and right_risks else left_risks | right_risks
    if left_risks and right_risks and not risks:
        return False
    if not higher:
        return True
    if not risks:
        # Both routes accept any risk. A value outside finite risk lists proves whether
        # unrestricted higher routes cover their intersection.
        sentinel = "_unlisted_risk"
        named = {risk for row in higher for risk in row["risks_in"]}
        while sentinel in named:
            sentinel += "_"
        risks = {sentinel}
    purposes = (
        {left["purpose"] or right["purpose"]}
        if left["purpose"] or right["purpose"]
        else ({"lead", "advice"} if left["role"] == "commander" else {None})
    )
    readiness = (
        {left["readiness"] or right["readiness"]}
        if left["readiness"] or right["readiness"]
        else {"ready", "T0"}
    )
    domains = set(left["domains_all"]) | set(right["domains_all"])
    for kind, risk, purpose, ready in product(shared_classes, risks, purposes, readiness):
        if not any(
            (row["purpose"] is None or row["purpose"] == purpose)
            and (row["readiness"] is None or row["readiness"] == ready)
            and kind in classes(row)
            and (not row["risks_in"] or risk in row["risks_in"])
            and set(row["domains_all"]) <= domains
            for row in higher
        ):
            return True
    return False
