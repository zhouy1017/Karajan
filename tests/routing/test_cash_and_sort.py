"""Pricing constraints precede preference; FX affects sorting, never cash admission."""

import copy
import hashlib
import json

import pytest
from karajan.routing import evaluate_route

from .test_resources import blocked
from .test_routing import sample


def requalify(policy: dict, capacity: dict, index: int = 0) -> str:
    profile = policy["resources"]["profiles"][index]
    hashed = hashlib.sha256(
        json.dumps(
            profile["profile"], sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()
    for evidence in profile["capability_evidence"]:
        evidence["profile_digest"] = hashed
    policy["profile_facts"][index]["profile_digest"] = hashed
    if capacity["estimates"][index]["price"] is not None:
        capacity["estimates"][index]["price"]["profile_digest"] = hashed
    return hashed


def cash_sample() -> tuple[dict, dict, dict]:
    task, policy, capacity = sample()
    registered = policy["resources"]["profiles"][0]
    registered["profile"]["binding"]["billing_path"] = "api_cash"
    registered["profile"].update(admission_granularity="model_call", usage_coverage="model_call")
    policy["resources"]["channels"][0]["billing_path"] = "api_cash"
    policy["profile_facts"][0]["budget_enforcement"] = "bounded_calls"
    evidence = copy.deepcopy(registered["capability_evidence"][0])
    evidence["capability"] = "bounded_calls"
    registered["capability_evidence"].append(evidence)
    capacity["estimates"][0]["price"] = {
        "price_revision": "fixture-price-1",
        "profile_digest": "0" * 64,
        "runtime_version": "1",
        "currency": "USD",
        "estimated_cash": "0.5",
        "upper_bound": "1",
        "coverage": "all_calls",
        "observed_at": 1000.0,
        "valid_until": 2000.0,
        "evidence_ref": "fixture:price",
    }
    requalify(policy, capacity)
    task["authorization"]["currency_limits"]["USD"] = "2"
    policy["resources"]["budgets"][1]["currency_limits"]["USD"] = "3"
    capacity["budget_remaining"]["run"]["USD"] = "2"
    capacity["accounts"][0]["cash_remaining"]["USD"] = "4"
    return task, policy, capacity


@pytest.mark.parametrize(
    "case,code",
    [
        ("estimated_stop", "BOUNDED_CALLS_REQUIRED"),
        ("unobserved", "BOUNDED_CALLS_UNQUALIFIED"),
        ("partial", "CASH_UPPER_BOUND_UNVERIFIED"),
        ("unknown", "CASH_UPPER_BOUND_UNVERIFIED"),
        ("expired", "PRICE_STALE_OR_MISMATCHED"),
        ("run", "CASH_BUDGET_INSUFFICIENT"),
        ("account", "CASH_BUDGET_INSUFFICIENT"),
        ("currency", "CASH_BUDGET_MISSING"),
    ],
)
def test_cash_needs_all_call_evidence_and_every_original_currency_ceiling(
    case: str, code: str
) -> None:
    task, policy, capacity = cash_sample()
    assert evaluate_route(task, policy, capacity)["selected_profile"] is not None
    if case == "estimated_stop":
        policy["profile_facts"][0]["budget_enforcement"] = "estimated_stop"
    elif case == "unobserved":
        policy["resources"]["profiles"][0]["capability_evidence"][-1]["status"] = "not_run"
    elif case == "partial":
        capacity["estimates"][0]["price"]["coverage"] = "partial"
    elif case == "unknown":
        capacity["estimates"][0]["price"]["upper_bound"] = None
    elif case == "expired":
        capacity["estimates"][0]["price"]["valid_until"] = 999.0
    elif case == "run":
        capacity["budget_remaining"]["run"]["USD"] = "0.9"
    elif case == "account":
        capacity["accounts"][0]["cash_remaining"]["USD"] = "0.9"
    else:
        capacity["estimates"][0]["price"]["currency"] = "JPY"
    blocked(task, policy, capacity, code)


def add_candidate(task: dict, policy: dict, capacity: dict, name: str) -> None:
    ref = {"id": name, "revision": 1}
    registered = copy.deepcopy(policy["resources"]["profiles"][0])
    registered["id"] = name
    registered["profile"]["id"] = name
    policy["resources"]["profiles"].append(registered)
    for refs in policy["rulebook"]["profile_groups"].values():
        refs.append(ref)
    for refs in (
        policy["approved_profile_refs"],
        policy["constraints"]["profile_refs"],
        task["authorization"]["profile_refs"],
        task["authorization"]["ceiling_profile_refs"],
    ):
        if ref not in refs:
            refs.append(ref)
    for refs in task["authorization"].get("approved_groups", {}).values():
        if ref not in refs:
            refs.append(ref)
    facts = copy.deepcopy(policy["profile_facts"][0])
    facts["profile"] = ref
    policy["profile_facts"].append(facts)
    estimate = copy.deepcopy(capacity["estimates"][0])
    estimate["profile"] = ref
    capacity["estimates"].append(estimate)
    requalify(policy, capacity, len(policy["resources"]["profiles"]) - 1)


def test_unknown_latency_loses_to_known_and_hard_constraints_override_preference() -> None:
    task, policy, capacity = sample()
    add_candidate(task, policy, capacity, "zz-fast")
    capacity["estimates"][0]["completion_seconds"] = None
    result = evaluate_route(task, policy, capacity)
    assert result["selected_profile"]["id"] == "zz-fast"
    rule = next(r for r in policy["rulebook"]["rules"] if r["id"] == "bounded-worker")
    rule["profile_preferences"] = [
        {"profile": {"id": "fixture-profile", "revision": 1}, "band": -1}
    ]
    assert evaluate_route(task, policy, capacity)["selected_profile"]["id"] == "fixture-profile"
    policy["resources"]["profiles"][0]["enabled"] = False
    assert evaluate_route(task, policy, capacity)["selected_profile"]["id"] == "zz-fast"


def test_missing_fx_skips_cash_for_entire_set_and_never_converts_budget() -> None:
    task, policy, capacity = cash_sample()
    add_candidate(task, policy, capacity, "b-usd")
    add_candidate(task, policy, capacity, "c-cny")
    capacity["estimates"][0].update(completion_seconds=30.0)
    capacity["estimates"][0]["price"]["estimated_cash"] = "0.1"
    capacity["estimates"][1].update(completion_seconds=10.0)
    capacity["estimates"][2]["price"].update(currency="CNY", estimated_cash="1", upper_bound="1")
    task["authorization"]["currency_limits"]["CNY"] = "2"
    policy["resources"]["budgets"][1]["currency_limits"]["CNY"] = "3"
    capacity["budget_remaining"]["run"]["CNY"] = "2"
    capacity["accounts"][0]["cash_remaining"]["CNY"] = "4"
    result = evaluate_route(task, policy, capacity)
    assert result["cash_sort"]["mode"] == "skipped"
    assert result["cash_sort"]["reason"] == "FX_UNAVAILABLE_FOR_CANDIDATE_SET"
    assert result["selected_profile"]["id"] == "b-usd"
    capacity["fx"] = {
        "id": "fixture-fx",
        "revision": 1,
        "reference_currency": "USD",
        "rates": {"USD": "1.000000", "CNY": "0.2"},
        "observed_at": 1000.0,
        "valid_until": 2000.0,
        "evidence_ref": "fixture:fx",
    }
    assert evaluate_route(task, policy, capacity)["selected_profile"]["id"] == "fixture-profile"
    capacity["budget_remaining"]["run"]["CNY"] = "0.5"
    result = evaluate_route(task, policy, capacity)
    assert (
        "CASH_BUDGET_INSUFFICIENT"
        in next(c for c in result["candidates"] if c["profile"]["id"] == "c-cny")["reason_codes"]
    )
