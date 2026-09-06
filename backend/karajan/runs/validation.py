"""Deterministic proposal checks, without tool execution or model authority."""

from pathlib import PurePosixPath, PureWindowsPath
from typing import Any


def validate_creation(
    request: dict[str, Any], project: dict[str, Any], config: dict[str, Any], owner: str
) -> None:
    people = request["participants"]
    names = [item["principal"] for item in people]
    if len(set(names)) != len(names) or owner in names:
        raise ValueError("PARTICIPANT_IDENTITY_INVALID")
    approved = {(ref["id"], ref["revision"]) for ref in config["approved_profile_refs"]}
    authorization = request["authorization"]
    if any((ref["id"], ref["revision"]) not in approved for ref in authorization["profile_refs"]):
        raise ValueError("PROFILE_NOT_APPROVED")
    for person in people:
        rulebook = config["rulebook"]
        if request.get("schema_version") == "karajan.create-run.v2":
            purpose = "advice" if person["purpose"] == "advice" else "lead"
            groups = {
                group
                for rule in rulebook["rules"]
                if rule["when"]["role"] == "commander"
                and rule["when"].get("purpose") in (None, purpose)
                for group in rule["eligible_groups"]
            }
        else:
            groups = {
                "adviser_qualified" if person["purpose"] == "advice" else "commander_qualified"
            }
        # Membership only permits this planning participant to be proposed. Its
        # actual planning intent still needs a separate trusted admission receipt.
        candidates = {
            (ref["id"], ref["revision"])
            for group in groups
            for ref in rulebook["profile_groups"].get(group, [])
        }
        if (person["profile"]["id"], person["profile"]["revision"]) not in candidates & approved:
            raise ValueError("PLANNER_PROFILE_NOT_APPROVED")
    authorize(authorization, authorization)
    if "independent_review" not in authorization["checks"]:
        raise ValueError("INDEPENDENT_REVIEW_REQUIRED")
    if authorization["target_branch"] != project["target_branch"]:
        raise ValueError("TARGET_BRANCH_NOT_APPROVED")
    if authorization["budget_ref"] != config["rulebook"]["resource_policy"]["run_budget_ref"]:
        raise ValueError("RUN_BUDGET_NOT_APPROVED")


def path_parts(path: str) -> tuple[str, ...]:
    parsed = PurePosixPath(path)
    if (
        parsed.is_absolute()
        or path != parsed.as_posix()
        or not parsed.parts
        or any(part in {".", ".."} for part in parsed.parts)
        or any(
            part.rstrip(". ") != part or PureWindowsPath(part).is_reserved()
            for part in parsed.parts
        )
        or any(character in path for character in '\\:*?[]<>|"\x00')
        or not path.isprintable()
    ):
        raise ValueError("PLAN_PATH_INVALID")
    return parsed.parts


def covered(path: str, roots: list[str]) -> bool:
    parts = path_parts(path)
    return any(parts[: len(path_parts(root))] == path_parts(root) for root in roots)


def authorize(proposed: dict[str, Any], ceiling: dict[str, Any]) -> None:
    for kind in ("read_paths", "write_paths"):
        if not all(covered(path, ceiling[kind]) for path in proposed[kind]):
            raise ValueError("PLAN_SCOPE_EXCEEDED")
    if any(
        any(part.casefold() == ".git" for part in path_parts(path))
        for path in proposed["write_paths"]
    ):
        raise ValueError("PLAN_SCOPE_EXCEEDED")
    references = {(ref["id"], ref["revision"]) for ref in ceiling["profile_refs"]}
    if (
        any((ref["id"], ref["revision"]) not in references for ref in proposed["profile_refs"])
        or proposed["budget_ref"] != ceiling["budget_ref"]
        or proposed["target_branch"] != ceiling["target_branch"]
        or proposed["delivery"] not in {"none", ceiling["delivery"]}
    ):
        raise ValueError("PLAN_SCOPE_EXCEEDED")
    if not set(ceiling["checks"]) <= set(proposed["checks"]):
        raise ValueError("REQUIRED_CHECKS_REMOVED")
    if not set(proposed["checks"]) <= set(ceiling["checks"]):
        raise ValueError("PLAN_SCOPE_EXCEEDED")


def validate_plan(plan: dict[str, Any], ceiling: dict[str, Any]) -> None:
    authorize(plan["authorization"], ceiling)
    tasks = {task["id"]: task for task in plan["tasks"]}
    if len(tasks) != len(plan["tasks"]):
        raise ValueError("PLAN_GRAPH_INVALID")
    for task in tasks.values():
        dependencies = task["depends_on"]
        if len(set(dependencies)) != len(dependencies) or not set(dependencies) <= tasks.keys():
            raise ValueError("PLAN_GRAPH_INVALID")
        roots = plan["authorization"]["write_paths" if task["role"] == "worker" else "read_paths"]
        if task["role"] == "worker" and any(
            any(part.casefold() == ".git" for part in path_parts(path)) for path in task["paths"]
        ):
            raise ValueError("PLAN_SCOPE_EXCEEDED")
        if not all(covered(path, roots) for path in task["paths"]):
            raise ValueError("PLAN_SCOPE_EXCEEDED")
    visited: set[str] = set()
    while len(visited) < len(tasks):
        available = {key for key, task in tasks.items() if set(task["depends_on"]) <= visited}
        if not available - visited:
            raise ValueError("PLAN_GRAPH_INVALID")
        visited |= available


def plan_impact(plan: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    proposed = {task["id"]: task for task in plan["tasks"]}
    for historical in run["plans"]:
        for task in historical["plan"]["tasks"]:
            current = proposed.get(task["id"])
            if current and current["revision"] == task["revision"] and current != task:
                raise ValueError("TASK_REVISION_REUSED")
    active = next(
        (
            item["plan"]
            for item in run["plans"]
            if item["plan_revision"] == run["active_plan_revision"]
        ),
        None,
    )
    prior = {task["id"]: task for task in active["tasks"]} if active else {}
    added = set(proposed) - prior.keys()
    removed = set(prior) - proposed.keys()
    changed = {key for key in proposed.keys() & prior.keys() if proposed[key] != prior[key]}
    authorization_changed = active is not None and active["authorization"] != plan["authorization"]
    affected = added | removed | changed
    if authorization_changed:
        affected |= proposed.keys() | prior.keys()
    while True:
        following = {
            key for key, task in {**prior, **proposed}.items() if set(task["depends_on"]) & affected
        }
        if following <= affected:
            break
        affected |= following
    return {
        "added": sorted(added),
        "removed": sorted(removed),
        "changed": sorted(changed),
        "affected": sorted(affected),
        "reusable": sorted(proposed.keys() - affected),
        "authorization_changed": authorization_changed,
        "attempt_reconciliation": "not_run",
    }
