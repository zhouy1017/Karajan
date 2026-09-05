"""Read-only vector checks and pressure components; no reservations are created."""

from fractions import Fraction
from typing import Any

from karajan.resources.broker import money, units

from .compiler import digest, reference


def ratio(value: Fraction | None) -> dict[str, int] | None:
    return (
        None if value is None else {"numerator": value.numerator, "denominator": value.denominator}
    )


def check_quota(
    row: dict[str, Any],
    task: dict[str, Any],
    policy: dict[str, Any],
    capacity: dict[str, Any],
    rule: dict[str, Any],
) -> None:
    reasons = row["reason_codes"]
    key = reference(row["profile"])
    registered = next((p for p in policy["resources"]["profiles"] if reference(p) == key), None)
    if registered is None or registered["profile"] is None:
        return
    account_id = registered["profile"]["binding"]["account_id"]
    account = next((a for a in capacity["accounts"] if a["id"] == account_id), None)
    estimate = next((e for e in capacity["estimates"] if reference(e["profile"]) == key), None)
    if account is None:
        reasons.append("CAPACITY_ACCOUNT_MISSING")
        return
    current = account["policy"]
    lead = (
        task["role"] == "commander"
        and task["purpose"] == "lead"
        and rule["lead_reserve_access"] is not False
    )
    row["capacity_policy"] = {
        "account_id": account_id,
        "revision": account["policy_revision"],
        "current_revision": account["current_policy_revision"],
        "sha256": digest(current),
    }
    # The full policy is in the immutable report snapshot; keep the exact revision at the decision.
    if (
        account["policy_revision"] != account["current_policy_revision"]
        or current["account_id"] != account_id
    ):
        reasons.append("CAPACITY_POLICY_NOT_CURRENT")
    if current["lead_reserved_slots"] > current["max_active_attempts"]:
        reasons.append("CAPACITY_POLICY_INVALID")
    slots = current["max_active_attempts"] - (0 if lead else current["lead_reserved_slots"])
    row["concurrency"] = {
        "active_attempts": account["active_attempts"],
        "maximum": current["max_active_attempts"],
        "role_reserved_slots": 0 if lead else current["lead_reserved_slots"],
        "available_slots": max(0, slots - account["active_attempts"]),
    }
    if account["active_attempts"] >= slots:
        reasons.append("CONCURRENCY_UNAVAILABLE")
    if task["duration_seconds"] > min(
        current["max_attempt_duration_seconds"],
        task["authorization"]["max_attempt_duration_seconds"],
    ):
        reasons.append("DURATION_LIMIT_EXCEEDED")
    if account["cooldown_until"] is not None and capacity["as_of"] < account["cooldown_until"]:
        reasons.append("ACCOUNT_COOLDOWN")
    if account["exhaustion_observation_required"]:
        reasons.append("EXHAUSTION_REQUIRES_NEW_OBSERVATION")
    if estimate is None:
        reasons.append("RESOURCE_ESTIMATE_MISSING")
        return
    row["estimate_evidence"] = estimate
    pool_ids = registered["quota_pool_refs"]
    demands = {d["pool_id"]: d for d in estimate["demand"]}
    if not pool_ids or set(pool_ids) != set(demands):
        reasons.append("DEMAND_VECTOR_INCOMPLETE")
    pressures: list[Fraction] = []
    uncertainty = {"known": 0, "calibrated": 1, "unknown": 2}[estimate["confidence"]]
    for pool_id in sorted(pool_ids):
        pool = next((p for p in capacity["pools"] if p["id"] == pool_id), None)
        catalog = next((p for p in policy["resources"]["quota_pools"] if p["id"] == pool_id), None)
        demand = demands.get(pool_id)
        if pool is None:
            reasons.append(f"POOL_SNAPSHOT_MISSING:{pool_id}")
            continue
        if (
            catalog is None
            or any(pool[k] != catalog[k] for k in ("account_id", "kind", "unit"))
            or pool["account_id"] != account_id
        ):
            reasons.append(f"POOL_BINDING_MISMATCH:{pool_id}")
        if demand is None:
            continue
        if demand["unit"] != pool["unit"] or demand["window_id"] != pool["window_id"]:
            reasons.append(f"DEMAND_WINDOW_MISMATCH:{pool_id}")
        amount = units(demand["amount"])
        if amount <= 0:
            reasons.append(f"DEMAND_ESTIMATE_INVALID:{pool_id}")
        remaining = (
            None if pool["reported_remaining"] is None else units(pool["reported_remaining"])
        )
        limit = None if pool["reported_limit"] is None else units(pool["reported_limit"])
        uncovered, reserved = units(pool["local_uncovered"]), units(pool["future_reserved"])
        safety = units(current["safety_margin"].get(pool_id, "0"))
        role_reserve = 0 if lead else units(current["lead_reserve"].get(pool_id, "0"))
        if limit is not None and (
            remaining is not None and remaining > limit or safety + role_reserve > limit
        ):
            reasons.append(f"POOL_LIMIT_INCONSISTENT:{pool_id}")
        available = (
            None if remaining is None else remaining - uncovered - reserved - safety - role_reserve
        )
        if remaining is not None and available is not None and available < amount:
            reasons.append(f"QUOTA_INSUFFICIENT:{pool_id}")
        age = capacity["as_of"] - pool["observed_at"]
        if (
            age < 0
            or age > current["observation_max_age_seconds"]
            or pool["reset_at"] is not None
            and pool["reset_at"] <= capacity["as_of"]
        ):
            reasons.append(f"OBSERVATION_STALE:{pool_id}")
        if (
            current["require_official_observation"]
            and pool["kind"] == "service"
            and pool["source"] != "official"
        ):
            reasons.append(f"OFFICIAL_OBSERVATION_REQUIRED:{pool_id}")
        unknown = (
            remaining is None
            or pool["confidence"] == "unknown"
            or estimate["confidence"] == "unknown"
            or pool["window_kind"] == "unknown"
        )
        mode = current["conservative_mode"]
        if unknown:
            uncertainty = 2
            finite = (
                mode is not None
                and mode["enabled"]
                and all(
                    mode[k] is not None
                    for k in (
                        "max_local_active_attempts",
                        "max_attempt_duration_seconds",
                        "observation_max_age_seconds",
                        "cooldown_seconds",
                    )
                )
            )
            if not finite or current["require_official_observation"] and pool["kind"] == "service":
                reasons.append(f"UNKNOWN_QUOTA_NOT_AUTHORIZED:{pool_id}")
            elif (
                account["active_attempts"]
                >= mode["max_local_active_attempts"]
                - (0 if lead else current["lead_reserved_slots"])
                or task["duration_seconds"] > mode["max_attempt_duration_seconds"]
                or age < 0
                or age > mode["observation_max_age_seconds"]
            ):
                reasons.append(f"CONSERVATIVE_LIMIT_EXCEEDED:{pool_id}")
        else:
            uncertainty = max(
                uncertainty, {"known": 0, "calibrated": 1, "unknown": 2}[pool["confidence"]]
            )
        pressure = None
        if limit is not None and limit > 0 and remaining is not None and not unknown:
            pressure = Fraction(
                limit - remaining + uncovered + reserved + safety + role_reserve + amount, limit
            )
            pressures.append(pressure)
        row["pool_evaluations"].append(
            {
                "pool_id": pool_id,
                "unit": pool["unit"],
                "window_id": pool["window_id"],
                "reported_limit": pool["reported_limit"],
                "reported_remaining": pool["reported_remaining"],
                "local_uncovered": pool["local_uncovered"],
                "future_reserved": pool["future_reserved"],
                "safety_margin": money(safety),
                "role_reserve": money(role_reserve),
                "demand": demand["amount"],
                "available_before_demand": None
                if available is None or unknown
                else money(available),
                "pressure": ratio(pressure),
                "observation": pool,
                "estimate_evidence_ref": estimate["evidence_ref"],
                "unknown_mode": unknown,
                "capacity_policy_revision": account["policy_revision"],
            }
        )
    row["sort_inputs"].update(
        uncertainty_band=uncertainty,
        bottleneck_quota_pressure=ratio(max(pressures) if pressures else None),
        completion_time_estimate=estimate["completion_seconds"],
    )
