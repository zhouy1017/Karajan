"""Supplied qualification facts define members without quota, selection or effects."""

from copy import deepcopy
from decimal import Decimal

import pytest
from karajan.routing import (
    RoutingError,
    evaluate_profile_membership,
    evaluate_reserved_profile,
    evaluate_route,
)

from .test_cash_and_sort import add_candidate, cash_sample
from .test_routing import sample


def test_membership_needs_no_capacity_and_does_not_select_or_authorize() -> None:
    task, policy, capacity = sample()
    capacity["pools"][0]["reported_remaining"] = "0"
    before = deepcopy((task, policy))
    result = evaluate_profile_membership(task, policy, as_of=1000)
    assert result["eligible_profiles"] == [{"id": "fixture-profile", "revision": 1}]
    assert result["reason_codes"] == []
    assert result["selected_profile"] is None
    assert result["activation_allowed"] is result["dispatch_enabled"] is False
    assert result["live_qualification"] == "not_run"
    assert result["scope"] == "supplied_profile_facts_membership"
    assert set(result["snapshots"]) == {"task", "policy"}
    assert not {"pool_evaluations", "sort_inputs", "rank"} & result["candidates"][0].keys()
    assert evaluate_route(task, policy, capacity)["selected_profile"] is None
    result["snapshots"]["task"]["authors"].append({"untrusted": "result mutation"})
    assert (task, policy) == before


@pytest.mark.parametrize(
    "as_of",
    [True, "1000", None, float("nan"), float("inf"), -float("inf"), 10**1000, Decimal("1000")],
    ids=[
        "boolean",
        "string",
        "none",
        "nan",
        "infinity",
        "negative-infinity",
        "overflow",
        "decimal",
    ],
)
def test_time_requires_a_finite_native_number(as_of) -> None:
    task, policy, _ = sample()
    with pytest.raises(RoutingError, match="MEMBERSHIP_AS_OF_INVALID"):
        evaluate_profile_membership(task, policy, as_of=as_of)


@pytest.mark.parametrize("as_of", [999.99, 4600.0])
def test_future_or_expired_facts_are_not_members(as_of: float) -> None:
    task, policy, _ = sample()
    result = evaluate_profile_membership(task, policy, as_of=as_of)
    assert result["eligible_profiles"] == []
    assert result["candidates"][0]["reason_codes"] == ["PROFILE_FACTS_STALE_OR_MISMATCHED"]
    assert evaluate_profile_membership(task, policy, as_of=4599.99)["eligible_profiles"]


def reviewer_sample() -> tuple[dict, dict, dict]:
    task, policy, capacity = sample()
    task.update(role="reviewer", complexity="T1")
    author = {
        "profile": {"id": "writer", "revision": 1},
        "model_family": "writer-family",
        "attempt_id": "author-attempt",
        "context_id": "author-context",
        "complexity": "T2",
        "risk": "standard",
        "paths": ["src/main.py"],
    }
    task["authors"] = [author, author | {"attempt_id": "second", "context_id": "second-context"}]
    return task, policy, capacity


@pytest.mark.parametrize(
    "case,code",
    [
        ("attempt", "REVIEW_NOT_INDEPENDENT"),
        ("context", "REVIEW_NOT_INDEPENDENT"),
        ("critical-same-family", "REVIEW_FAMILY_NOT_INDEPENDENT"),
        ("critical-unknown-family", "REVIEW_FAMILY_NOT_INDEPENDENT"),
        ("missing-authors", "AUTHOR_SCOPE_REQUIRED"),
    ],
)
def test_every_recorded_author_participates_in_reviewer_independence(case: str, code: str) -> None:
    task, policy, capacity = reviewer_sample()
    positive = evaluate_profile_membership(task, policy, as_of=1000)
    assert positive["effective_class"] == "T2"
    assert positive["rule_id"] == "standard-review"
    assert positive["eligible_profiles"] == [{"id": "fixture-profile", "revision": 1}]
    if case == "attempt":
        task["authors"][1]["attempt_id"] = task["planned_attempt_id"]
    elif case == "context":
        task["authors"][1]["context_id"] = task["planned_context_id"]
    elif case.startswith("critical"):
        task["authors"][1]["risk"] = "critical"
        task["authors"][1]["model_family"] = (
            "fixture-family" if case == "critical-same-family" else None
        )
    else:
        task["authors"] = []
    result = evaluate_profile_membership(task, policy, as_of=1000)
    assert result["eligible_profiles"] == []
    assert result["candidates"][0]["reason_codes"] == [code]
    if case.startswith("critical"):
        assert result["effective_class"] == "T3"
        assert result["rule_id"] == "critical-review"
    ref = {"id": "fixture-profile", "revision": 1}
    assert evaluate_route(task, policy, capacity)["candidates"][0]["reason_codes"] == [code]
    assert evaluate_reserved_profile(task, policy, capacity, ref)["candidates"][0][
        "reason_codes"
    ] == [code]


@pytest.mark.parametrize(
    "case,code",
    [
        ("role", "ROLE_NOT_SUPPORTED"),
        ("capability", "CAPABILITY_UNQUALIFIED:structured_findings"),
        ("context", "CONTEXT_CAPACITY_INSUFFICIENT"),
        ("tool", "TOOLS_NOT_SUPPORTED"),
        ("group", "GROUP_PROFILE_NOT_APPROVED"),
        ("ceiling", "RUN_CEILING_PROFILE_DENIED"),
        ("channel", "CHANNEL_NOT_AUTHORIZED"),
        ("isolation", "ISOLATION_INSUFFICIENT"),
    ],
)
def test_members_require_all_static_approval_and_qualification_facts(case: str, code: str) -> None:
    task, policy, capacity = reviewer_sample()
    registered = policy["resources"]["profiles"][0]
    facts = policy["profile_facts"][0]
    if case == "role":
        facts["roles"] = ["worker"]
    elif case == "capability":
        next(
            e for e in registered["capability_evidence"] if e["capability"] == "structured_findings"
        )["status"] = "not_run"
    elif case == "context":
        task["reserved_output_tokens"] = 300
        facts["context_tokens"] = 1299
    elif case == "tool":
        facts["tools"] = []
    elif case == "group":
        task["authorization"]["approved_groups"]["review_standard_qualified"] = []
    elif case == "ceiling":
        task["authorization"]["ceiling_profile_refs"] = []
    elif case == "channel":
        task["authorization"]["channel_ids"] = []
    else:
        registered["required_isolation"] = "attempt_isolated"
    result = evaluate_profile_membership(task, policy, as_of=1000)
    assert result["eligible_profiles"] == []
    assert result["candidates"][0]["reason_codes"] == [code]
    assert evaluate_route(task, policy, capacity)["candidates"][0]["reason_codes"] == [code]


@pytest.mark.parametrize(
    "case,code",
    [
        ("stage-denied", "STAGE_NOT_AUTHORIZED"),
        ("no-failure", "QUALITY_FAILURE_REQUIRED"),
        ("unapproved-index", "QUALITY_STAGE_NOT_AUTHORIZED"),
        ("not-reached", "QUALITY_STAGE_NOT_REACHED"),
        ("ambiguous", "RULE_AMBIGUOUS"),
    ],
)
def test_rule_and_quality_gates_precede_membership(case: str, code: str) -> None:
    task, policy, capacity = sample()
    task.update(
        stage="quality",
        failure_reason="QUALITY_FAILED",
        previous_profile={"id": "fixture-profile", "revision": 1},
    )
    if case == "stage-denied":
        task["authorization"]["allowed_stages"] = ["normal"]
    elif case == "no-failure":
        task["failure_reason"] = None
    elif case == "unapproved-index":
        task["authorization"]["approved_quality_stage_indices"] = []
    elif case == "not-reached":
        task["quality_stage_index"] = 1
        task["authorization"]["approved_quality_stage_indices"] = [1]
    else:
        rule = deepcopy(next(r for r in policy["rulebook"]["rules"] if r["id"] == "bounded-worker"))
        rule["id"] = "equal-priority"
        policy["rulebook"]["rules"].append(rule)
    result = evaluate_profile_membership(task, policy, as_of=1000)
    assert result["reason_codes"] == [code]
    assert result["candidates"] == result["eligible_profiles"] == []
    assert evaluate_route(task, policy, capacity)["reason_codes"] == [code]


def test_quality_members_require_the_exact_current_group_not_another_approved_group() -> None:
    task, policy, _ = sample()
    task.update(
        stage="quality",
        failure_reason="QUALITY_FAILED",
        previous_profile={"id": "fixture-profile", "revision": 1},
    )
    result = evaluate_profile_membership(task, policy, as_of=1000)
    assert [row["id"] for row in result["resolved_groups"]] == ["critical_qualified"]
    assert result["eligible_profiles"] == [{"id": "fixture-profile", "revision": 1}]
    task["authorization"]["approved_groups"]["critical_qualified"] = []
    assert evaluate_profile_membership(task, policy, as_of=1000)["eligible_profiles"] == []


def test_membership_has_identity_order_and_never_performs_cash_admission() -> None:
    task, policy, capacity = cash_sample()
    add_candidate(task, policy, capacity, "aaa")
    capacity["accounts"][0]["cash_remaining"]["USD"] = "0"
    result = evaluate_profile_membership(task, policy, as_of=1000)
    assert result["eligible_profiles"] == [
        {"id": "aaa", "revision": 1},
        {"id": "fixture-profile", "revision": 1},
    ]
    assert evaluate_route(task, policy, capacity)["selected_profile"] is None
    assert all("rank" not in row for row in result["candidates"])
    assert evaluate_profile_membership(task, policy, as_of=1000) == result


def test_duplicate_supplied_facts_and_invalid_shapes_keep_validation_errors() -> None:
    task, policy, _ = sample()
    policy["profile_facts"].append(deepcopy(policy["profile_facts"][0]))
    with pytest.raises(RoutingError, match="SNAPSHOT_IDENTITY_CONFLICT"):
        evaluate_profile_membership(task, policy, as_of=1000)
    with pytest.raises(RoutingError, match="TASK_SNAPSHOT_INVALID"):
        evaluate_profile_membership({}, policy, as_of=1000)


def test_credential_fields_are_rejected_before_any_snapshot_can_be_returned() -> None:
    task, policy, _ = sample()
    synthetic = "membership-synthetic-secret-canary"
    policy["resources"]["profiles"][0]["profile"]["binding"]["native_settings"] = {
        "nested": {"api_key": synthetic}
    }
    with pytest.raises(RoutingError, match="CREDENTIAL_VALUE_FORBIDDEN") as caught:
        evaluate_profile_membership(task, policy, as_of=1000)
    assert synthetic not in str(caught.value.issues)
