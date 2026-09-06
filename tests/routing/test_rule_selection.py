"""Classification chooses a rule before its own stage grants are resolved."""

from copy import deepcopy

import pytest
from karajan.routing import RoutingError, evaluate_route, select_rule

from .test_routing import sample


def classification(task: dict) -> dict:
    return {
        key: task[key]
        for key in (
            "role",
            "purpose",
            "readiness",
            "complexity",
            "risk",
            "domains",
            "paths",
            "authors",
        )
    }


def test_rule_can_be_selected_without_capacity_or_stage_authorization() -> None:
    task, policy, _ = sample()
    task.update(complexity="T1", paths=["Auth/login.py"])
    policy["risk_policy"]["path_floors"] = [{"prefix": "auth", "minimum_class": "T3"}]
    result = select_rule(classification(task), policy["rulebook"], policy["risk_policy"])
    assert result["reason_codes"] == []
    assert result["effective_class"] == "T3"
    assert result["rule_id"] == "critical-worker"
    assert result["rule"]["id"] == "critical-worker"
    assert result["activation_allowed"] is False


def test_reviewer_uses_recorded_author_risk_even_if_review_is_declared_simple() -> None:
    task, policy, capacity = sample()
    task.update(role="reviewer", complexity="T1", paths=["src/main.py"])
    task["authors"] = [
        {
            "profile": {"id": "writer", "revision": 1},
            "model_family": "writer-family",
            "attempt_id": "author-attempt",
            "context_id": "author-context",
            "complexity": "T2",
            "risk": "critical",
            "paths": ["src/main.py"],
        }
    ]
    selected = select_rule(classification(task), policy["rulebook"], policy["risk_policy"])
    assert selected["effective_class"] == "T3"
    assert selected["rule_id"] == "critical-review"
    route = evaluate_route(task, policy, capacity)
    assert route["effective_class"] == "T3"
    assert route["rule_id"] == "critical-review"


def test_priority_tie_and_missing_rule_never_supply_a_rule_to_authorize() -> None:
    task, policy, _ = sample()
    rulebook = policy["rulebook"]
    first = next(r for r in rulebook["rules"] if r["id"] == "bounded-worker")
    alternate = deepcopy(first) | {"id": "alternative"}
    rulebook["rules"].append(alternate)
    result = select_rule(classification(task), rulebook, policy["risk_policy"])
    assert result["reason_codes"] == ["RULE_AMBIGUOUS"]
    assert result["rule"] is None
    alternate["priority"] += 1
    assert (
        select_rule(classification(task), rulebook, policy["risk_policy"])["rule_id"]
        == "alternative"
    )
    rulebook["rules"] = [r for r in rulebook["rules"] if r not in (first, alternate)]
    result = select_rule(classification(task), rulebook, policy["risk_policy"])
    assert result["reason_codes"] == ["NO_RULE"]
    assert result["rule"] is None


def test_not_ready_and_unknown_risk_remain_distinct_from_rule_matching() -> None:
    task, policy, _ = sample()
    task.update(readiness="T0", risk="unmapped")
    result = select_rule(classification(task), policy["rulebook"], policy["risk_policy"])
    assert result["reason_codes"] == ["TASK_NOT_READY"]
    task["readiness"] = "ready"
    result = select_rule(classification(task), policy["rulebook"], policy["risk_policy"])
    assert result["reason_codes"] == ["RISK_MAPPING_REQUIRED"]


def test_selector_rejects_path_aliases_and_permission_fields() -> None:
    task, policy, _ = sample()
    task["paths"] = ["src/../auth/password.py"]
    with pytest.raises(RoutingError, match="ROUTING_PATH_INVALID"):
        select_rule(classification(task), policy["rulebook"], policy["risk_policy"])
    task["paths"] = ["src/main.py"]
    injected = classification(task) | {"allowed_stages": ["quality"]}
    with pytest.raises(RoutingError, match="TASK_CLASSIFICATION_INVALID"):
        select_rule(injected, policy["rulebook"], policy["risk_policy"])


def test_output_reserve_must_fit_real_profile_context_with_approved_input() -> None:
    task, policy, capacity = sample()
    task.update(context_tokens=4096, reserved_output_tokens=1024)
    policy["profile_facts"][0]["context_tokens"] = 5000
    rejected = evaluate_route(task, policy, capacity)
    assert rejected["selected_profile"] is None
    assert "CONTEXT_CAPACITY_INSUFFICIENT" in rejected["candidates"][0]["reason_codes"]
    policy["profile_facts"][0]["context_tokens"] = 5120
    assert evaluate_route(task, policy, capacity)["selected_profile"] == {
        "id": "fixture-profile",
        "revision": 1,
    }
