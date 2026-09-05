"""Read approved planning material; selection remains explicit and role constrained."""

import hashlib
import json
from typing import Any

from pydantic import ValidationError

from karajan.projects.models import ProfileRef
from karajan.runs import RunPlanner


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def material(
    planner: RunPlanner, run: dict[str, Any], task_id: str, profile_ref: dict[str, Any]
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
    preview = planner.projects.evaluate_task(
        run["project_id"],
        {key: task[key] for key in ("role", "readiness", "complexity", "risk")}
        | {"approved_profile_refs": authorization["profile_refs"]},
    )
    snapshot = run["configuration_snapshot"]
    if preview["configuration_digest"] != snapshot["digest"]:
        return None, "PROJECT_CONFIGURATION_CHANGED"
    if profile_ref not in preview["qualified_candidates"]:
        return None, "PROFILE_ROLE_NOT_APPROVED"
    config = snapshot["configuration"]
    registration = next(
        row
        for row in config["resources"]["profiles"]
        if {"id": row["id"], "revision": row["revision"]} == profile_ref
    )
    reviewers = []
    if task["role"] == "worker":
        review_preview = planner.projects.evaluate_task(
            run["project_id"],
            {
                "role": "reviewer",
                "readiness": "ready",
                "complexity": preview["effective_class"],
                "risk": task["risk"],
                "approved_profile_refs": authorization["profile_refs"],
                "author_model_families": [registration["model_family"]],
            },
        )
        reviewers = [
            row
            for row in config["resources"]["profiles"]
            if {"id": row["id"], "revision": row["revision"]}
            in review_preview["qualified_candidates"]
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
