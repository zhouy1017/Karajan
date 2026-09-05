"""Route decisions from explicit fixture facts, with no execution or ledger access."""

import copy
import json
from pathlib import Path

import pytest
from karajan.routing import (
    RoutingError,
    compile_rulebook,
    evaluate_route,
    fixture_from_configuration,
)


def sample() -> tuple[dict, dict, dict]:
    configuration = json.loads(Path("examples/projects/offline-configuration.json").read_bytes())
    return fixture_from_configuration(configuration, as_of=1000.0)


def test_default_rulebook_compiles_and_fixed_snapshot_repeats_without_mutation() -> None:
    task, policy, capacity = sample()
    before = copy.deepcopy((task, policy, capacity))
    compiled = compile_rulebook(policy["rulebook"])
    assert compiled["compiler_revision"] == "karajan.routing.compiler.v1"
    result = evaluate_route(task, policy, capacity)
    assert result["selected_profile"] == {"id": "fixture-profile", "revision": 1}
    assert result["reason_codes"] == []
    assert result == evaluate_route(task, policy, capacity)
    assert (task, policy, capacity) == before
    assert result["activation_allowed"] is False
    assert result["live_qualification"] == "not_run"


def test_rule_priority_selects_highest_unique_match_and_equal_priority_is_ambiguous() -> None:
    task, policy, capacity = sample()
    first = next(row for row in policy["rulebook"]["rules"] if row["id"] == "bounded-worker")
    added = copy.deepcopy(first)
    added.update(id="special", priority=101)
    policy["rulebook"]["rules"].insert(0, added)
    assert evaluate_route(task, policy, capacity)["rule_id"] == "special"
    added["priority"] = 100
    assert evaluate_route(task, policy, capacity)["reason_codes"] == ["RULE_AMBIGUOUS"]
    policy["rulebook"]["rules"] = [
        row for row in policy["rulebook"]["rules"] if row not in (first, added)
    ]
    assert evaluate_route(task, policy, capacity)["reason_codes"] == ["NO_RULE"]


def test_risk_comes_from_trusted_mapping_and_paths_while_t0_never_becomes_complexity() -> None:
    task, policy, capacity = sample()
    task.update(complexity="T1", paths=["auth/login.py"])
    policy["risk_policy"]["path_floors"] = [{"prefix": "auth", "minimum_class": "T3"}]
    result = evaluate_route(task, policy, capacity)
    assert result["effective_class"] == "T3"
    assert result["rule_id"] == "critical-worker"
    task["readiness"] = "T0"
    assert evaluate_route(task, policy, capacity)["reason_codes"] == ["TASK_NOT_READY"]
    task["readiness"] = "ready"
    del policy["risk_policy"]["mapping"]["standard"]
    assert evaluate_route(task, policy, capacity)["reason_codes"] == ["RISK_MAPPING_REQUIRED"]


def test_compiler_refuses_unknown_expressions_and_unresolved_groups() -> None:
    _, policy, _ = sample()
    document = copy.deepcopy(policy["rulebook"])
    document["rules"][0]["expression"] = "__import__('os').system('anything')"
    with pytest.raises(RoutingError, match="RULEBOOK_INVALID"):
        compile_rulebook(document)
    document = copy.deepcopy(policy["rulebook"])
    document["rules"][0]["eligible_groups"] = ["missing"]
    with pytest.raises(RoutingError, match="GROUP_REFERENCE_UNKNOWN"):
        compile_rulebook(document)
