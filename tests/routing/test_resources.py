"""Public decisions preserve every pool and every original-currency cash ceiling."""

import copy

import pytest
from karajan.routing import evaluate_route

from .test_routing import sample


def blocked(task: dict, policy: dict, capacity: dict, code: str) -> dict:
    result = evaluate_route(task, policy, capacity)
    assert result["selected_profile"] is None
    assert code in result["candidates"][0]["reason_codes"]
    return result


def test_short_window_cannot_hide_exhausted_long_window_and_pressure_keeps_components() -> None:
    task, policy, capacity = sample()
    pool = copy.deepcopy(capacity["pools"][0])
    pool.update(id="weekly", reported_remaining="0")
    capacity["pools"].append(pool)
    registered = policy["resources"]["profiles"][0]
    registered["quota_pool_refs"].append("weekly")
    catalog = copy.deepcopy(policy["resources"]["quota_pools"][0])
    catalog["id"] = "weekly"
    policy["resources"]["quota_pools"].append(catalog)
    demand = copy.deepcopy(capacity["estimates"][0]["demand"][0])
    demand["pool_id"] = "weekly"
    capacity["estimates"][0]["demand"].append(demand)
    result = blocked(task, policy, capacity, "QUOTA_INSUFFICIENT:weekly")
    assert len(result["candidates"][0]["pool_evaluations"]) == 2
    pool.update(reported_remaining="80", local_uncovered="4", future_reserved="6")
    capacity["accounts"][0]["policy"].update(
        safety_margin={"weekly": "2"}, lead_reserve={"weekly": "8"}
    )
    result = evaluate_route(task, policy, capacity)
    details = result["candidates"][0]["pool_evaluations"][1]
    assert details["available_before_demand"] == "60.000000"
    assert details["pressure"] == {"numerator": 41, "denominator": 100}


@pytest.mark.parametrize(
    "case,code",
    [
        ("policy", "CAPACITY_POLICY_NOT_CURRENT"),
        ("window", "DEMAND_WINDOW_MISMATCH:service-fixture"),
        ("pool", "POOL_SNAPSHOT_MISSING:service-fixture"),
        ("demand", "DEMAND_VECTOR_INCOMPLETE"),
        ("slots", "CONCURRENCY_UNAVAILABLE"),
        ("duration", "DURATION_LIMIT_EXCEEDED"),
        ("stale", "OBSERVATION_STALE:service-fixture"),
        ("exhaustion", "EXHAUSTION_REQUIRES_NEW_OBSERVATION"),
    ],
)
def test_capacity_hard_filters(case: str, code: str) -> None:
    task, policy, capacity = sample()
    account = capacity["accounts"][0]
    if case == "policy":
        account["current_policy_revision"] = 2
    elif case == "window":
        capacity["estimates"][0]["demand"][0]["window_id"] = "other"
    elif case == "pool":
        capacity["pools"] = []
    elif case == "demand":
        capacity["estimates"][0]["demand"] = []
    elif case == "slots":
        account["active_attempts"] = 2
    elif case == "duration":
        task["duration_seconds"] = 31
    elif case == "stale":
        capacity["pools"][0]["observed_at"] = 900.0
    else:
        account["exhaustion_observation_required"] = True
    blocked(task, policy, capacity, code)


def test_unknown_quota_needs_finite_conservative_policy_and_never_becomes_zero() -> None:
    task, policy, capacity = sample()
    capacity["pools"][0].update(reported_remaining=None, confidence="unknown")
    blocked(task, policy, capacity, "UNKNOWN_QUOTA_NOT_AUTHORIZED:service-fixture")
    capacity["accounts"][0]["policy"]["conservative_mode"] = {
        "enabled": True,
        "max_local_active_attempts": 2,
        "max_attempt_duration_seconds": 30,
        "observation_max_age_seconds": 60,
        "cooldown_seconds": 10,
    }
    result = evaluate_route(task, policy, capacity)
    assert result["selected_profile"] is not None
    assert result["candidates"][0]["sort_inputs"]["bottleneck_quota_pressure"] is None
    assert result["candidates"][0]["sort_inputs"]["uncertainty_band"] == 2
    capacity["pools"][0]["reported_remaining"] = "0"
    blocked(task, policy, capacity, "QUOTA_INSUFFICIENT:service-fixture")


def test_unknown_demand_confidence_does_not_bypass_conservative_authorization() -> None:
    task, policy, capacity = sample()
    capacity["estimates"][0]["confidence"] = "unknown"
    blocked(task, policy, capacity, "UNKNOWN_QUOTA_NOT_AUTHORIZED:service-fixture")


def test_conservative_mode_preserves_commander_slot_inside_its_smaller_ceiling() -> None:
    task, policy, capacity = sample()
    account = capacity["accounts"][0]
    account["policy"]["max_active_attempts"] = 4
    account["active_attempts"] = 1
    account["policy"]["conservative_mode"] = {
        "enabled": True,
        "max_local_active_attempts": 2,
        "max_attempt_duration_seconds": 30,
        "observation_max_age_seconds": 60,
        "cooldown_seconds": 10,
    }
    capacity["pools"][0].update(reported_remaining=None, confidence="unknown")
    blocked(task, policy, capacity, "CONSERVATIVE_LIMIT_EXCEEDED:service-fixture")
    task.update(role="commander", purpose="lead")
    assert evaluate_route(task, policy, capacity)["selected_profile"] is not None
    task["purpose"] = "advice"
    blocked(task, policy, capacity, "CONSERVATIVE_LIMIT_EXCEEDED:service-fixture")


def test_lead_rule_can_deny_access_to_commander_protection() -> None:
    task, policy, capacity = sample()
    task.update(role="commander", purpose="lead")
    capacity["pools"][0]["reported_remaining"] = "1"
    capacity["accounts"][0]["policy"]["lead_reserve"] = {"service-fixture": "1"}
    assert evaluate_route(task, policy, capacity)["selected_profile"] is not None
    rule = next(r for r in policy["rulebook"]["rules"] if r["id"] == "lead-planning")
    rule["lead_reserve_access"] = False
    blocked(task, policy, capacity, "QUOTA_INSUFFICIENT:service-fixture")
    capacity["pools"][0]["reported_remaining"] = "100"
    capacity["accounts"][0]["active_attempts"] = 2
    blocked(task, policy, capacity, "CONCURRENCY_UNAVAILABLE")


def test_mixed_known_and_unknown_pools_keep_known_pressure_and_unknown_uncertainty() -> None:
    task, policy, capacity = sample()
    pool = copy.deepcopy(capacity["pools"][0])
    pool.update(
        id="local-allowance",
        kind="platform_allowance",
        reported_remaining=None,
        confidence="unknown",
    )
    capacity["pools"].append(pool)
    catalog = copy.deepcopy(policy["resources"]["quota_pools"][0])
    catalog.update(id="local-allowance", kind="platform_allowance")
    policy["resources"]["quota_pools"].append(catalog)
    policy["resources"]["profiles"][0]["quota_pool_refs"].append("local-allowance")
    demand = copy.deepcopy(capacity["estimates"][0]["demand"][0])
    demand["pool_id"] = "local-allowance"
    capacity["estimates"][0]["demand"].append(demand)
    capacity["accounts"][0]["policy"]["conservative_mode"] = {
        "enabled": True,
        "max_local_active_attempts": 2,
        "max_attempt_duration_seconds": 30,
        "observation_max_age_seconds": 60,
        "cooldown_seconds": 10,
    }
    result = evaluate_route(task, policy, capacity)
    assert result["selected_profile"] is not None
    sort = result["candidates"][0]["sort_inputs"]
    assert sort["bottleneck_quota_pressure"] == {"numerator": 1, "denominator": 100}
    assert sort["uncertainty_band"] == 2
