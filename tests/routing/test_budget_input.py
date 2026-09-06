"""Catalog compatibility must not leak arithmetic exceptions through the route API."""

import pytest
from karajan.routing import RoutingError, evaluate_route

from .test_cash_and_sort import cash_sample


@pytest.mark.parametrize("amount", ["NaN", "-1", "1.0000001", "not-a-number"])
def test_invalid_catalog_budget_has_a_stable_safe_routing_error(amount: str) -> None:
    task, policy, capacity = cash_sample()
    policy["resources"]["budgets"][1]["currency_limits"]["USD"] = amount
    with pytest.raises(RoutingError, match="POLICY_SNAPSHOT_INVALID") as caught:
        evaluate_route(task, policy, capacity)
    assert caught.value.code == "POLICY_SNAPSHOT_INVALID"
    assert all(set(issue) == {"path", "code"} for issue in caught.value.issues)


def test_unknown_catalog_budget_still_uses_missing_budget_decision() -> None:
    task, policy, capacity = cash_sample()
    policy["resources"]["budgets"][1]["currency_limits"]["USD"] = None
    result = evaluate_route(task, policy, capacity)
    assert result["selected_profile"] is None
    assert "CASH_BUDGET_MISSING" in result["candidates"][0]["reason_codes"]
