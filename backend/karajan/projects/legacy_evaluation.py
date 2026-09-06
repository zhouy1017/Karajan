"""Legacy fixed-template qualification only; current resources are checked separately."""

import hashlib
import json
from typing import Any

from .configuration import fixed_rulebook, validate_configuration
from .models import TaskPreview


def encoded(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def evaluate_fixed_task(
    configuration: dict[str, Any],
    request: TaskPreview,
    *,
    project_revision: int,
    configuration_digest: str | None,
) -> dict[str, Any]:
    effective = max(request.complexity, "T3" if request.risk == "critical" else "T1")
    result: dict[str, Any] = {
        "schema_version": "karajan.task-preview.v1",
        "project_revision": project_revision,
        "configuration_digest": configuration_digest,
        "effective_class": effective,
        "rule_id": None,
        "qualified_candidates": [],
        "reason_codes": [],
        "dispatch_eligible": False,
        "qualification_scope": "offline_configuration",
    }
    if request.role == "worker" and request.readiness != "ready":
        result["reason_codes"] = ["TASK_NOT_READY"]
        return result
    if not fixed_rulebook(configuration.get("rulebook") or {}):
        result["reason_codes"] = ["ROUTING_SNAPSHOT_REQUIRED"]
        return result
    if validate_configuration(configuration):
        result["reason_codes"] = ["CONFIGURATION_NOT_READY"]
        return result
    rules = []
    for rule in configuration["rulebook"]["rules"]:
        when = rule["when"]
        if when["role"] != request.role or (
            "purpose" in when and when["purpose"] != request.purpose
        ):
            continue
        if when.get("effective_class", effective) != effective or effective not in when.get(
            "effective_class_in", [effective]
        ):
            continue
        rules.append(rule)
    if len(rules) != 1:
        result["reason_codes"] = ["RULE_NOT_UNIQUE"]
        return result
    rule = rules[0]
    result["rule_id"] = rule["id"]
    result["required_independence"] = rule.get("independence", [])
    approved = {(item.id, item.revision) for item in request.approved_profile_refs}
    candidates = {
        (ref["id"], ref["revision"])
        for group in rule["eligible_groups"]
        for ref in configuration["rulebook"]["profile_groups"][group]
        if (ref["id"], ref["revision"]) in approved
    }
    profiles = {
        (item["id"], item["revision"]): item for item in configuration["resources"]["profiles"]
    }
    authors = {(item.id, item.revision) for item in request.author_profile_refs}
    author_families = set(request.author_model_families) | {
        profiles[author]["model_family"] for author in authors if author in profiles
    }
    for candidate in tuple(candidates):
        registration = profiles[candidate]
        profile_digest = hashlib.sha256(encoded(registration["profile"]).encode()).hexdigest()
        evidence = registration["capability_evidence"]
        capabilities = {
            item["capability"]
            for item in evidence
            if item["status"] == "passed"
            and item["evidence_ref"]
            and item["profile_digest"] == profile_digest
            and item["runtime_version"] == registration["profile"]["binding"]["runtime_version"]
            and item.get("provenance") in {"fixture", "imported_observation"}
            and sum(other["capability"] == item["capability"] for other in evidence) == 1
        }
        if not set(request.required_capabilities) <= capabilities:
            candidates.remove(candidate)
        elif (
            request.role == "reviewer"
            and effective == "T3"
            and registration["model_family"] in author_families
        ):
            candidates.remove(candidate)
    result["qualified_candidates"] = [
        {"id": identity, "revision": revision} for identity, revision in sorted(candidates)
    ]
    if not candidates:
        result["reason_codes"] = ["NO_APPROVED_CANDIDATE"]
    return result
