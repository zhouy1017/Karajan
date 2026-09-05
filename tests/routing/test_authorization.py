"""Approved revisions and stages cannot be expanded by later rule changes."""

import copy

import pytest
from karajan.routing import evaluate_route

from .test_cash_and_sort import add_candidate
from .test_resources import blocked
from .test_routing import sample


def test_quality_selects_only_current_preapproved_stage_without_normal_group_intersection() -> None:
    task, policy, capacity = sample()
    add_candidate(task, policy, capacity, "quality-one")
    add_candidate(task, policy, capacity, "quality-two")
    normal = {"id": "fixture-profile", "revision": 1}
    first = {"id": "quality-one", "revision": 1}
    second = {"id": "quality-two", "revision": 1}
    rule = next(r for r in policy["rulebook"]["rules"] if r["id"] == "bounded-worker")
    rule["quality_escalation_groups"] = ["first_upgrade", "second_upgrade"]
    policy["rulebook"]["profile_groups"].update(
        standard_qualified=[normal], first_upgrade=[first], second_upgrade=[second]
    )
    task["authorization"]["approved_groups"] = copy.deepcopy(policy["rulebook"]["profile_groups"])
    task["authorization"]["approved_quality_stage_indices"] = [0, 1]
    task["quality_stage_index"] = 0
    assert evaluate_route(task, policy, capacity)["selected_profile"] == normal
    task.update(stage="quality", failure_reason="QUALITY_FAILED", previous_profile=normal)
    assert evaluate_route(task, policy, capacity)["selected_profile"] == first
    task.update(quality_stage_index=1, quality_repair_rounds_used=1)
    assert evaluate_route(task, policy, capacity)["selected_profile"] == second
    task["authorization"]["approved_quality_stage_indices"] = [0]
    assert evaluate_route(task, policy, capacity)["reason_codes"] == [
        "QUALITY_STAGE_NOT_AUTHORIZED"
    ]


def test_new_group_member_cannot_expand_old_approval_and_quality_needs_failure_and_rounds() -> None:
    task, policy, capacity = sample()
    task["authorization"]["approved_groups"]["standard_qualified"] = []
    blocked(task, policy, capacity, "GROUP_PROFILE_NOT_APPROVED")
    task, policy, capacity = sample()
    task.update(stage="quality", previous_profile={"id": "fixture-profile", "revision": 1})
    assert evaluate_route(task, policy, capacity)["reason_codes"] == ["QUALITY_FAILURE_REQUIRED"]
    task.update(failure_reason="QUALITY_FAILED", quality_repair_rounds_used=2)
    assert evaluate_route(task, policy, capacity)["reason_codes"] == [
        "QUALITY_REPAIR_LIMIT_REACHED"
    ]


@pytest.mark.parametrize(
    "case,code",
    [
        ("maxclass", "PROFILE_CLASS_INSUFFICIENT"),
        ("role", "ROLE_NOT_SUPPORTED"),
        ("context", "CONTEXT_CAPACITY_INSUFFICIENT"),
        ("isolation", "ISOLATION_INSUFFICIENT"),
        ("tools", "TOOL_NOT_AUTHORIZED"),
        ("destination", "DATA_DESTINATION_NOT_AUTHORIZED"),
        ("ceiling", "RUN_CEILING_PROFILE_DENIED"),
        ("capability", "CAPABILITY_UNQUALIFIED:rule_specific"),
    ],
)
def test_hard_constraints_remain_strict(case: str, code: str) -> None:
    task, policy, capacity = sample()
    if case == "maxclass":
        policy["resources"]["profiles"][0]["max_class"] = "T1"
    elif case == "role":
        policy["profile_facts"][0]["roles"] = ["commander"]
    elif case == "context":
        policy["profile_facts"][0]["context_tokens"] = None
    elif case == "isolation":
        policy["resources"]["profiles"][0]["required_isolation"] = "attempt_isolated"
    elif case == "tools":
        policy["constraints"]["tools"] = []
    elif case == "destination":
        task["authorization"]["data_destinations"] = []
    elif case == "ceiling":
        task["authorization"]["ceiling_profile_refs"] = []
    else:
        next(r for r in policy["rulebook"]["rules"] if r["id"] == "bounded-worker")[
            "capabilities_all"
        ].append("rule_specific")
    blocked(task, policy, capacity, code)


def test_review_fresh_context_and_highest_author_scope_require_independent_family() -> None:
    task, policy, capacity = sample()
    task.update(
        role="reviewer",
        complexity="T1",
        authors=[
            {
                "profile": {"id": "fixture-profile", "revision": 1},
                "model_family": "fixture-family",
                "attempt_id": "author-attempt",
                "context_id": "author-context",
                "complexity": "T1",
                "risk": "standard",
                "paths": ["src/code.py"],
            }
        ],
    )
    assert evaluate_route(task, policy, capacity)["selected_profile"] is not None
    task["planned_context_id"] = "author-context"
    blocked(task, policy, capacity, "REVIEW_NOT_INDEPENDENT")
    task["planned_context_id"] = "fresh"
    task["authors"][0]["risk"] = "critical"
    result = blocked(task, policy, capacity, "REVIEW_FAMILY_NOT_INDEPENDENT")
    assert result["effective_class"] == "T3"
    task["authors"][0]["model_family"] = "other-family"
    assert evaluate_route(task, policy, capacity)["selected_profile"] is not None
