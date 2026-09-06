"""A fixed reservation is rechecked without trying to reserve its own capacity twice."""

import copy

import pytest
from karajan.routing import RoutingError, evaluate_reserved_profile, evaluate_route

from .test_cash_and_sort import add_candidate, cash_sample
from .test_routing import sample

REF = {"id": "fixture-profile", "revision": 1}


def test_held_slots_and_pool_balance_do_not_reject_the_fixed_profile() -> None:
    task, policy, capacity = sample()
    ref = {"id": "fixture-profile", "revision": 1}
    capacity["accounts"][0]["active_attempts"] = 2
    capacity["pools"][0]["reported_remaining"] = "0"
    capacity["pools"][0]["future_reserved"] = "1"
    before = copy.deepcopy((task, policy, capacity, ref))

    assert evaluate_route(task, policy, capacity)["selected_profile"] is None
    report = evaluate_reserved_profile(task, policy, capacity, ref)

    assert report["selected_profile"] == ref
    assert report["requested_profile"] == ref
    assert [row["profile"] for row in report["candidates"]] == [ref]
    assert report["candidates"][0]["reason_codes"] == []
    assert report["scope"] == "reserved_profile_validation"
    assert report["quota_revalidation_required"] is True
    assert report["activation_allowed"] is False
    assert report["live_qualification"] == "not_run"
    assert report["snapshots"]["capacity"]["pools"][0]["reported_remaining"] == "0"
    assert report["cash_sort"] == {"mode": "not_evaluated"}
    assert (task, policy, capacity, ref) == before


def test_different_eligible_profile_cannot_replace_the_reserved_profile() -> None:
    task, policy, capacity = sample()
    add_candidate(task, policy, capacity, "other")
    policy["resources"]["profiles"][0]["enabled"] = False
    assert evaluate_route(task, policy, capacity)["selected_profile"] == {
        "id": "other",
        "revision": 1,
    }

    report = evaluate_reserved_profile(task, policy, capacity, REF)

    assert report["selected_profile"] is None
    assert report["reason_codes"] == ["NO_ELIGIBLE_PROFILE"]
    assert len(report["candidates"]) == 1
    assert report["candidates"][0]["profile"] == REF
    assert report["candidates"][0]["reason_codes"] == ["PROFILE_DISABLED"]
    assert report["candidates"][0]["sort_inputs"] == {}
    assert "rank" not in report["candidates"][0]


@pytest.mark.parametrize(
    "case,code",
    [
        ("group", "GROUP_PROFILE_NOT_APPROVED"),
        ("class", "PROFILE_CLASS_INSUFFICIENT"),
        ("facts", "PROFILE_FACTS_MISSING"),
        ("expired_facts", "PROFILE_FACTS_STALE_OR_MISMATCHED"),
        ("role", "ROLE_NOT_SUPPORTED"),
        ("context", "CONTEXT_CAPACITY_INSUFFICIENT"),
        ("credential", "CHANNEL_ACCOUNT_BINDING_INVALID"),
        ("capability", "CAPABILITY_UNQUALIFIED:bounded_implementation"),
        ("tools", "TOOL_NOT_AUTHORIZED"),
        ("destination", "DATA_DESTINATION_NOT_AUTHORIZED"),
        ("ceiling", "RUN_CEILING_PROFILE_DENIED"),
    ],
)
def test_non_quota_checks_match_normal_routing(case: str, code: str) -> None:
    task, policy, capacity = sample()
    if case == "group":
        task["authorization"]["approved_groups"]["standard_qualified"] = []
    elif case == "class":
        policy["resources"]["profiles"][0]["max_class"] = "T1"
    elif case == "facts":
        policy["profile_facts"] = []
    elif case == "expired_facts":
        policy["profile_facts"][0]["valid_until"] = capacity["as_of"]
    elif case == "role":
        policy["profile_facts"][0]["roles"] = ["commander"]
    elif case == "context":
        policy["profile_facts"][0]["context_tokens"] = None
    elif case == "credential":
        policy["resources"]["accounts"][0]["secret_ref"] = "different-auth-generation"
    elif case == "capability":
        next(
            e
            for e in policy["resources"]["profiles"][0]["capability_evidence"]
            if e["capability"] == "bounded_implementation"
        )["status"] = "not_run"
    elif case == "tools":
        policy["constraints"]["tools"] = []
    elif case == "destination":
        task["authorization"]["data_destinations"] = []
    else:
        task["authorization"]["ceiling_profile_refs"] = []

    ordinary = evaluate_route(task, policy, capacity)
    report = evaluate_reserved_profile(task, policy, capacity, REF)

    assert report["selected_profile"] is None
    assert report["candidates"][0]["reason_codes"] == ordinary["candidates"][0]["reason_codes"]
    assert code in report["candidates"][0]["reason_codes"]


@pytest.mark.parametrize(
    "case,code",
    [
        ("account", "CAPACITY_ACCOUNT_MISSING"),
        ("policy", "CAPACITY_POLICY_NOT_CURRENT"),
        ("policy_account", "CAPACITY_POLICY_NOT_CURRENT"),
        ("estimate", "RESOURCE_ESTIMATE_MISSING"),
        ("vector", "DEMAND_VECTOR_INCOMPLETE"),
        ("window", "DEMAND_WINDOW_MISMATCH:service-fixture"),
        ("unit", "DEMAND_WINDOW_MISMATCH:service-fixture"),
        ("pool", "POOL_SNAPSHOT_MISSING:service-fixture"),
        ("pool_account", "POOL_BINDING_MISMATCH:service-fixture"),
        ("pool_catalog", "POOL_BINDING_MISMATCH:service-fixture"),
        ("zero", "DEMAND_ESTIMATE_INVALID:service-fixture"),
        ("duration", "DURATION_LIMIT_EXCEEDED"),
        ("approved_duration", "DURATION_LIMIT_EXCEEDED"),
    ],
)
def test_bound_resource_inputs_are_required_even_when_quota_is_deferred(
    case: str, code: str
) -> None:
    task, policy, capacity = sample()
    if case == "account":
        capacity["accounts"] = []
    elif case == "policy":
        capacity["accounts"][0]["current_policy_revision"] += 1
    elif case == "policy_account":
        capacity["accounts"][0]["policy"]["account_id"] = "other"
    elif case == "estimate":
        capacity["estimates"] = []
    elif case == "vector":
        capacity["estimates"][0]["demand"] = []
    elif case == "window":
        capacity["estimates"][0]["demand"][0]["window_id"] = "other"
    elif case == "unit":
        capacity["estimates"][0]["demand"][0]["unit"] = "tokens"
    elif case == "pool":
        capacity["pools"] = []
    elif case == "pool_account":
        capacity["pools"][0]["account_id"] = "other"
    elif case == "pool_catalog":
        policy["resources"]["quota_pools"] = []
    elif case == "zero":
        capacity["estimates"][0]["demand"][0]["amount"] = "0"
    elif case == "duration":
        capacity["accounts"][0]["policy"]["max_attempt_duration_seconds"] = 1
    else:
        task["authorization"]["max_attempt_duration_seconds"] = 1

    report = evaluate_reserved_profile(task, policy, capacity, REF)

    assert report["selected_profile"] is None
    assert code in report["candidates"][0]["reason_codes"]


@pytest.mark.parametrize(
    "case,code",
    [
        ("unbounded", "BOUNDED_CALLS_REQUIRED"),
        ("unqualified", "BOUNDED_CALLS_UNQUALIFIED"),
        ("price_missing", "PRICE_MISSING"),
        ("price_stale", "PRICE_STALE_OR_MISMATCHED"),
        ("price_unknown", "CASH_UPPER_BOUND_UNVERIFIED"),
        ("budget", "CASH_BUDGET_INSUFFICIENT"),
        ("account", "CASH_BUDGET_INSUFFICIENT"),
    ],
)
def test_cash_checks_are_not_deferred(case: str, code: str) -> None:
    task, policy, capacity = cash_sample()
    assert evaluate_reserved_profile(task, policy, capacity, REF)["selected_profile"] == REF
    if case == "unbounded":
        policy["profile_facts"][0]["budget_enforcement"] = "unknown"
    elif case == "unqualified":
        policy["resources"]["profiles"][0]["capability_evidence"][-1]["status"] = "not_run"
    elif case == "price_missing":
        capacity["estimates"][0]["price"] = None
    elif case == "price_stale":
        capacity["estimates"][0]["price"]["valid_until"] = capacity["as_of"]
    elif case == "price_unknown":
        capacity["estimates"][0]["price"]["upper_bound"] = None
    elif case == "budget":
        capacity["budget_remaining"]["run"]["USD"] = "0"
    else:
        capacity["accounts"][0]["cash_remaining"]["USD"] = "0"

    report = evaluate_reserved_profile(task, policy, capacity, REF)

    assert report["selected_profile"] is None
    assert code in report["candidates"][0]["reason_codes"]


def test_quality_profile_must_belong_to_the_current_approved_stage() -> None:
    task, policy, capacity = sample()
    add_candidate(task, policy, capacity, "quality-one")
    add_candidate(task, policy, capacity, "quality-two")
    first = {"id": "quality-one", "revision": 1}
    second = {"id": "quality-two", "revision": 1}
    rule = next(r for r in policy["rulebook"]["rules"] if r["id"] == "bounded-worker")
    rule["quality_escalation_groups"] = ["first_upgrade", "second_upgrade"]
    policy["rulebook"]["profile_groups"].update(
        standard_qualified=[REF], first_upgrade=[first], second_upgrade=[second]
    )
    task["authorization"]["approved_groups"] = copy.deepcopy(policy["rulebook"]["profile_groups"])
    task["authorization"]["approved_quality_stage_indices"] = [0, 1]
    task.update(stage="quality", failure_reason="QUALITY_FAILED", previous_profile=REF)

    assert evaluate_reserved_profile(task, policy, capacity, first)["selected_profile"] == first
    for excluded in (REF, second, {"id": "quality-one", "revision": 2}):
        report = evaluate_reserved_profile(task, policy, capacity, excluded)
        assert report["selected_profile"] is None
        assert report["reason_codes"] == ["RESERVED_PROFILE_NOT_STAGE_CANDIDATE"]
        assert report["candidates"] == []
    task["authorization"]["approved_quality_stage_indices"] = []
    assert evaluate_reserved_profile(task, policy, capacity, first)["reason_codes"] == [
        "QUALITY_STAGE_NOT_AUTHORIZED"
    ]


def test_review_independence_and_author_risk_floor_remain_mandatory() -> None:
    task, policy, capacity = sample()
    task.update(
        role="reviewer",
        complexity="T1",
        authors=[
            {
                "profile": REF,
                "model_family": "fixture-family",
                "attempt_id": "author-attempt",
                "context_id": "author-context",
                "complexity": "T1",
                "risk": "standard",
                "paths": ["src/code.py"],
            }
        ],
    )
    assert evaluate_reserved_profile(task, policy, capacity, REF)["selected_profile"] == REF
    task["planned_context_id"] = "author-context"
    report = evaluate_reserved_profile(task, policy, capacity, REF)
    assert report["selected_profile"] is None
    assert "REVIEW_NOT_INDEPENDENT" in report["candidates"][0]["reason_codes"]
    task["planned_context_id"] = "fresh"
    task["authors"][0]["risk"] = "critical"
    report = evaluate_reserved_profile(task, policy, capacity, REF)
    assert report["effective_class"] == "T3"
    assert report["selected_profile"] is None
    assert "REVIEW_FAMILY_NOT_INDEPENDENT" in report["candidates"][0]["reason_codes"]


def test_live_quota_observations_are_explicitly_left_for_activation() -> None:
    task, policy, capacity = sample()
    account = capacity["accounts"][0]
    account.update(cooldown_until=2000.0, exhaustion_observation_required=True)
    capacity["pools"][0].update(observed_at=800.0, reset_at=999.0)
    assert evaluate_route(task, policy, capacity)["selected_profile"] is None

    report = evaluate_reserved_profile(task, policy, capacity, REF)

    assert report["selected_profile"] == REF
    assert report["activation_allowed"] is False
    assert report["quota_revalidation_required"] is True
    assert "concurrency" not in report["candidates"][0]
    for pool in report["candidates"][0]["pool_evaluations"]:
        assert pool["quota_revalidation_required"] is True
        assert "available_before_demand" not in pool
        assert "pressure" not in pool


@pytest.mark.parametrize(
    "profile", [{"id": "fixture-profile", "revision": True}, REF | {"skip": True}]
)
def test_profile_reference_is_strict(profile: dict) -> None:
    with pytest.raises(RoutingError, match="RESERVED_PROFILE_REFERENCE_INVALID"):
        evaluate_reserved_profile(*sample(), profile)


def test_rule_selection_and_snapshot_validation_are_not_bypassed() -> None:
    task, policy, capacity = sample()
    task["readiness"] = "T0"
    report = evaluate_reserved_profile(task, policy, capacity, REF)
    assert report["reason_codes"] == ["TASK_NOT_READY"]
    assert report["selected_profile"] is None
    assert report["quota_revalidation_required"] is True
    assert report["snapshots"]["task"]["readiness"] == "T0"

    task, policy, capacity = sample()
    capacity["estimates"].append(copy.deepcopy(capacity["estimates"][0]))
    with pytest.raises(RoutingError, match="SNAPSHOT_IDENTITY_CONFLICT"):
        evaluate_reserved_profile(task, policy, capacity, REF)
