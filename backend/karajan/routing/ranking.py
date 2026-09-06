"""Cash hard ceilings remain in original currencies; reference FX only ranks."""

from fractions import Fraction
from typing import Any

from karajan.resources.broker import units

from .compiler import digest, reference
from .quotas import ratio


def check_cash(
    row: dict[str, Any], task: dict[str, Any], policy: dict[str, Any], capacity: dict[str, Any]
) -> None:
    key = reference(row["profile"])
    registered = next((p for p in policy["resources"]["profiles"] if reference(p) == key), None)
    if registered is None or registered["profile"] is None:
        return
    profile = registered["profile"]
    if profile["binding"]["billing_path"] == "subscription_only":
        row["cash"] = {
            "currency": None,
            "estimate": "0",
            "upper_bound": "0",
            "basis": "explicit_subscription_only_binding",
        }
        return
    reasons = row["reason_codes"]
    facts = row.get("qualification_evidence")
    if (
        facts is None
        or facts["budget_enforcement"] != "bounded_calls"
        or profile["admission_granularity"] != "model_call"
        or profile["usage_coverage"] != "model_call"
    ):
        reasons.append("BOUNDED_CALLS_REQUIRED")
    evidence = next(
        (e for e in registered["capability_evidence"] if e["capability"] == "bounded_calls"), None
    )
    if (
        evidence is None
        or evidence["status"] != "passed"
        or evidence["profile_digest"] != digest(profile)
        or evidence["runtime_version"] != profile["binding"]["runtime_version"]
        or not evidence["evidence_ref"]
        or evidence["provenance"] is None
    ):
        reasons.append("BOUNDED_CALLS_UNQUALIFIED")
    estimate = next((e for e in capacity["estimates"] if reference(e["profile"]) == key), None)
    price = estimate["price"] if estimate else None
    if price is None:
        reasons.append("PRICE_MISSING")
        return
    row["cash"] = {
        "currency": price["currency"],
        "estimate": price["estimated_cash"],
        "upper_bound": price["upper_bound"],
        "price": price,
    }
    if (
        not price["observed_at"] <= capacity["as_of"] < price["valid_until"]
        or price["profile_digest"] != digest(profile)
        or price["runtime_version"] != profile["binding"]["runtime_version"]
    ):
        reasons.append("PRICE_STALE_OR_MISMATCHED")
    if price["coverage"] != "all_calls" or price["upper_bound"] is None:
        reasons.append("CASH_UPPER_BOUND_UNVERIFIED")
        return
    upper = units(price["upper_bound"])
    if price["estimated_cash"] is not None and units(price["estimated_cash"]) > upper:
        reasons.append("PRICE_ESTIMATE_EXCEEDS_UPPER_BOUND")
    budget_id = task["authorization"]["budget_ref"]
    planning = task["role"] == "commander"
    configured_id = policy["rulebook"]["resource_policy"][
        "planning_budget_ref" if planning else "run_budget_ref"
    ]
    budget = next((b for b in policy["resources"]["budgets"] if b["id"] == budget_id), None)
    account = next(
        (a for a in capacity["accounts"] if a["id"] == profile["binding"]["account_id"]), None
    )
    if (
        budget is None
        or budget_id != configured_id
        or budget["scope"] != ("planning" if planning else "run")
    ):
        reasons.append("CASH_BUDGET_NOT_AUTHORIZED")
        return
    currency = price["currency"]
    bounds = {
        "authorization": task["authorization"]["currency_limits"].get(currency),
        "configured_budget": budget["currency_limits"].get(currency),
        "remaining_budget": capacity["budget_remaining"].get(budget_id, {}).get(currency),
        "remaining_account": None if account is None else account["cash_remaining"].get(currency),
    }
    row["cash"]["original_currency_bounds"] = bounds
    if any(value is None for value in bounds.values()):
        reasons.append("CASH_BUDGET_MISSING")
    elif any(value is not None and units(value) < upper for value in bounds.values()):
        reasons.append("CASH_BUDGET_INSUFFICIENT")


def _known(value: Any) -> tuple[int, Any]:
    return (1, 0) if value is None else (0, value)


def rank(
    candidates: list[dict[str, Any]],
    rule: dict[str, Any],
    policy: dict[str, Any],
    capacity: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    eligible = [row for row in candidates if not row["reason_codes"]]
    currencies = {
        row["cash"]["currency"]
        for row in eligible
        if row.get("cash", {}).get("currency") is not None
    }
    fx = capacity["fx"]
    comparable = len(currencies) <= 1 or (
        fx is not None
        and fx["observed_at"] <= capacity["as_of"] < fx["valid_until"]
        and all(
            currency in fx["rates"] and units(fx["rates"][currency]) > 0 for currency in currencies
        )
        and units(fx["rates"].get(fx["reference_currency"], "0")) == 1_000_000
    )
    mode = (
        "original_currency" if len(currencies) <= 1 else "reference_fx" if comparable else "skipped"
    )
    cash_sort: dict[str, Any] = {
        "mode": mode,
        "currencies": sorted(currencies),
        "reason": None if comparable else "FX_UNAVAILABLE_FOR_CANDIDATE_SET",
        "fx": fx if mode == "reference_fx" else None,
    }
    order = [
        name
        for name in policy["rulebook"]["resource_policy"]["candidate_order"]
        if comparable or name != "incremental_cash_estimate"
    ]
    cash_sort["effective_candidate_order"] = order
    preferences = {reference(p["profile"]): p["band"] for p in rule["profile_preferences"]}
    keys: dict[tuple[str, int], tuple[Any, ...]] = {}
    for row in candidates:
        key = reference(row["profile"])
        values = row["sort_inputs"]
        values["preference_band"] = preferences.get(key, 0)
        values["profile_id"] = row["profile"]
        cash = row.get("cash")
        converted = None
        if cash is not None and cash["estimate"] is not None:
            converted = Fraction(units(cash["estimate"]), 1_000_000)
            if mode == "reference_fx" and cash["currency"] is not None:
                if cash["currency"] in fx["rates"]:
                    converted *= Fraction(units(fx["rates"][cash["currency"]]), 1_000_000)
                else:
                    converted = None
        values["incremental_cash_estimate"] = ratio(converted) if comparable else None
        keys_for_row: list[Any] = []
        for name in order:
            value = values.get(name)
            if name == "profile_id":
                keys_for_row.append(key)
            elif name in {"bottleneck_quota_pressure", "incremental_cash_estimate"}:
                keys_for_row.append(
                    _known(
                        None
                        if value is None
                        else Fraction(value["numerator"], value["denominator"])
                    )
                )
            else:
                keys_for_row.append(_known(value))
        keys[key] = tuple(keys_for_row)
    eligible.sort(key=lambda row: keys[reference(row["profile"])])
    for index, row in enumerate(eligible, 1):
        row["rank"] = index
    return eligible, cash_sort
