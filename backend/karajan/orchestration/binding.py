"""Read approved planning material; selection remains explicit and role constrained."""

import hashlib
import json
from typing import Any

from pydantic import ValidationError

from karajan.projects.legacy_evaluation import evaluate_fixed_task
from karajan.projects.models import ProfileRef, TaskPreview
from karajan.runs import RunPlanner


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _current_profile_allowed(
    frozen: dict[str, Any],
    current: dict[str, Any],
    ref: dict[str, Any],
    *,
    required_class: str,
    capabilities: list[str],
) -> bool:
    resources = current.get("resources")
    if resources is None or ref not in current.get("approved_profile_refs", []):
        return False
    original = next(
        (
            p
            for p in frozen["resources"]["profiles"]
            if {"id": p["id"], "revision": p["revision"]} == ref
        ),
        None,
    )
    registered = next(
        (p for p in resources["profiles"] if {"id": p["id"], "revision": p["revision"]} == ref),
        None,
    )
    if original is None or registered is None or not registered["enabled"]:
        return False
    if (
        registered["profile"] is None
        or digest(registered["profile"]) != digest(original["profile"])
        or registered["max_class"] is None
        or registered["max_class"] < required_class
        or registered["required_isolation"] != "tool_sandboxed"
        or registered["model_family"] != original["model_family"]
        or set(registered["quota_pool_refs"]) != set(original["quota_pool_refs"])
    ):
        return False
    profile = registered["profile"]
    binding = profile["binding"]
    account = next((a for a in resources["accounts"] if a["id"] == binding["account_id"]), None)
    old_account = next(
        (a for a in frozen["resources"]["accounts"] if a["id"] == binding["account_id"]),
        None,
    )
    channel = next((c for c in resources["channels"] if c["id"] == binding["channel_id"]), None)
    if (
        account is None
        or old_account is None
        or channel is None
        or account["provider_id"] != old_account["provider_id"]
        or account["secret_ref"] != profile["auth_ref"]
        or channel["account_id"] != binding["account_id"]
        or channel["billing_path"] != binding["billing_path"]
        or not channel["approved_data_destination"]
    ):
        return False
    profile_hash = digest(profile)
    for capability in capabilities:
        evidence = [e for e in registered["capability_evidence"] if e["capability"] == capability]
        if len(evidence) != 1 or any(
            e["status"] != "passed"
            or e["profile_digest"] != profile_hash
            or e["runtime_version"] != binding["runtime_version"]
            or not e["evidence_ref"]
            or e["provenance"] not in {"fixture", "imported_observation"}
            for e in evidence
        ):
            return False
    return True


def material(
    planner: RunPlanner,
    run: dict[str, Any],
    task_id: str,
    profile_ref: dict[str, Any],
    *,
    current_resources: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    try:
        profile_ref = ProfileRef.model_validate(profile_ref).model_dump()
    except ValidationError:
        return None, "PROFILE_REFERENCE_INVALID"
    plan = next(
        (row for row in run["plans"] if row["plan_revision"] == run["active_plan_revision"]), None
    )
    task = (
        next((row for row in plan["plan"]["tasks"] if row["id"] == task_id), None) if plan else None
    )
    if not task or task["readiness"] != "ready":
        return None, "TASK_SCOPE_NOT_APPROVED"
    if task["role"] not in {"worker", "reviewer"}:
        return None, "ROLE_NOT_IMPLEMENTED"
    assert plan is not None
    authorization = plan["plan"]["authorization"]
    if profile_ref not in authorization["profile_refs"]:
        return None, "PROFILE_NOT_APPROVED"
    snapshot = run["configuration_snapshot"]
    config = snapshot["configuration"]
    preview = evaluate_fixed_task(
        config,
        TaskPreview.model_validate(
            {key: task[key] for key in ("role", "readiness", "complexity", "risk")}
            | {"approved_profile_refs": authorization["profile_refs"]}
        ),
        project_revision=snapshot["project_revision"],
        configuration_digest=snapshot["digest"],
    )
    if "ROUTING_SNAPSHOT_REQUIRED" in preview["reason_codes"]:
        return None, "ROUTING_SNAPSHOT_REQUIRED"
    if profile_ref not in preview["qualified_candidates"]:
        return None, "PROFILE_ROLE_NOT_APPROVED"
    current_resources = (
        planner.projects.get_effective_resources(run["project_id"])
        if current_resources is None
        else current_resources
    )
    rule = next(r for r in config["rulebook"]["rules"] if r["id"] == preview["rule_id"])
    if current_resources["project_id"] != run["project_id"] or not _current_profile_allowed(
        config,
        current_resources,
        profile_ref,
        required_class=preview["effective_class"],
        capabilities=rule["capabilities_all"],
    ):
        return None, "CURRENT_PROFILE_RESTRICTED"
    registration = next(
        row
        for row in config["resources"]["profiles"]
        if {"id": row["id"], "revision": row["revision"]} == profile_ref
    )
    reviewers = []
    if task["role"] == "worker":
        review_preview = evaluate_fixed_task(
            config,
            TaskPreview.model_validate(
                {
                    "role": "reviewer",
                    "readiness": "ready",
                    "complexity": preview["effective_class"],
                    "risk": task["risk"],
                    "approved_profile_refs": authorization["profile_refs"],
                    "author_model_families": [registration["model_family"]],
                }
            ),
            project_revision=snapshot["project_revision"],
            configuration_digest=snapshot["digest"],
        )
        review_rule = next(
            r for r in config["rulebook"]["rules"] if r["id"] == review_preview["rule_id"]
        )
        reviewers = [
            row
            for row in config["resources"]["profiles"]
            if {"id": row["id"], "revision": row["revision"]}
            in review_preview["qualified_candidates"]
            and _current_profile_allowed(
                config,
                current_resources,
                {"id": row["id"], "revision": row["revision"]},
                required_class=review_preview["effective_class"],
                capabilities=review_rule["capabilities_all"],
            )
        ]
        if not reviewers:
            return None, "QUALIFIED_REVIEWER_UNAVAILABLE"
    budget = next(
        row for row in config["resources"]["budgets"] if row["id"] == authorization["budget_ref"]
    )
    inputs = {task_id}
    tasks = {row["id"]: row for row in plan["plan"]["tasks"]}
    while True:
        expanded = inputs | {
            dependency for key in inputs for dependency in tasks[key]["depends_on"]
        }
        if expanded == inputs:
            break
        inputs = expanded
    return {
        "task": task,
        "registration": registration,
        "profile": registration["profile"],
        "reviewers": reviewers,
        "plan_revision": plan["plan_revision"],
        "planning_term": plan["term"],
        "plan_digest": plan["plan_digest"],
        "authorization": authorization,
        "authorization_digest": plan["authorization_digest"],
        "configuration_digest": snapshot["digest"],
        "current_resources": {
            "revision": current_resources["revision"],
            "digest": current_resources["digest"],
        },
        "repository": snapshot["repository"],
        "budget": budget,
        "limits": config["rulebook"]["collaboration"],
        "effective_class": preview["effective_class"],
        "input_sha256": digest(
            {
                "requirement": run["requirement"],
                "tasks": [tasks[key] for key in sorted(inputs)],
                "authorization": authorization,
                "configuration": snapshot["digest"],
            }
        ),
    }, None
