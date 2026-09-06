"""Select one rule from requirements before resolving that rule's approval grants."""

from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from .compiler import RoutingError, compile_rulebook, parse
from .models import RiskPolicy, TaskClassification

CLASSES = {"T1": 1, "T2": 2, "T3": 3}


def _paths(value: str) -> tuple[str, ...]:
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or value != path.as_posix()
        or not path.parts
        or any(
            part in {".", ".."} or part.rstrip(". ") != part or PureWindowsPath(part).is_reserved()
            for part in path.parts
        )
        or any(char in value for char in '\\:*?[]<>|"\x00')
        or not value.isprintable()
    ):
        raise RoutingError("ROUTING_PATH_INVALID")
    return tuple(part.casefold() for part in path.parts)


def _floor(task: dict[str, Any], risks: dict[str, Any]) -> str:
    if task["risk"] not in risks["mapping"]:
        raise RoutingError("RISK_MAPPING_REQUIRED")
    values: list[str] = [task["complexity"], risks["mapping"][task["risk"]]]
    for value in task["paths"]:
        path = _paths(value)
        for floor in risks["path_floors"]:
            prefix = _paths(floor["prefix"])
            if path[: len(prefix)] == prefix:
                values.append(floor["minimum_class"])
    return max(values, key=CLASSES.__getitem__)


def _matches(rule: dict[str, Any], task: dict[str, Any], effective: str) -> bool:
    when = rule["when"]
    return (
        all(when[key] is None or when[key] == task[key] for key in ("role", "purpose", "readiness"))
        and (when["effective_class"] is None or when["effective_class"] == effective)
        and (when["effective_class_in"] is None or effective in when["effective_class_in"])
        and set(when["domains_all"]) <= set(task["domains"])
        and (not when["risks_in"] or task["risk"] in when["risks_in"])
    )


def validate_classification(task: dict[str, Any], risks: dict[str, Any]) -> None:
    for floor in risks["path_floors"]:
        _paths(floor["prefix"])
    if risks["mapping"].get("critical", "T3") != "T3":
        raise RoutingError("RISK_POLICY_FLOOR_INVALID")
    for current in [task, *task["authors"]]:
        for path in current["paths"]:
            _paths(path)
    if (task["role"] == "commander") != (task["purpose"] is not None):
        raise RoutingError("TASK_PURPOSE_INVALID")


def select_compiled_rule(
    task: dict[str, Any], compiled: dict[str, Any], risks: dict[str, Any]
) -> dict[str, Any]:
    """Internal shared decision over already parsed and validated documents."""
    report: dict[str, Any] = {
        "reason_codes": [],
        "effective_class": None,
        "rule_id": None,
        "matching_rules": [],
        "rule": None,
        "activation_allowed": False,
    }
    if task["readiness"] == "T0" and task["role"] == "worker":
        report["reason_codes"] = ["TASK_NOT_READY"]
        return report
    try:
        effective = max(
            (
                _floor(current, risks)
                for current in ([task, *task["authors"]] if task["role"] == "reviewer" else [task])
            ),
            key=CLASSES.__getitem__,
        )
    except RoutingError as error:
        report["reason_codes"] = [error.code]
        return report
    report["effective_class"] = effective
    matches = [r for r in compiled["document"]["rules"] if _matches(r, task, effective)]
    report["matching_rules"] = [
        {"id": r["id"], "priority": r["priority"]}
        for r in sorted(matches, key=lambda r: (-r["priority"], r["id"]))
    ]
    highest = [
        r for r in matches if r["priority"] == max((row["priority"] for row in matches), default=0)
    ]
    if len(highest) != 1:
        report["reason_codes"] = ["RULE_AMBIGUOUS" if highest else "NO_RULE"]
        return report
    rule = highest[0]
    report["rule_id"] = rule["id"]
    report["rule"] = rule
    if not rule["eligible_groups"] or not rule["capabilities_all"]:
        report["reason_codes"] = ["RULE_REQUIREMENT_EMPTY"]
        return report
    return report


def select_rule(
    task_classification: dict[str, Any], rulebook: dict[str, Any], risk_policy: dict[str, Any]
) -> dict[str, Any]:
    """Classify a task without accepting capacity, candidate facts or stage permission.

    Authors must come from recorded execution lineage in a controller consumer.
    This pure operation cannot approve a stage, select a Profile or admit work.
    """
    task = parse(TaskClassification, task_classification, "TASK_CLASSIFICATION_INVALID")
    risks = parse(RiskPolicy, risk_policy, "RISK_POLICY_INVALID")
    compiled = compile_rulebook(rulebook)
    validate_classification(task, risks)
    return select_compiled_rule(task, compiled, risks)
