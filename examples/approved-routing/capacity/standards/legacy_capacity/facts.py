"""Immutable, source-bound quota facts for the trusted routing snapshot builder.

This is a capacity fragment. It carries no execution qualification, task authority,
cash balance or task-specific estimate, and cannot authorize an Attempt.
"""

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import TypeAdapter, ValidationError

from karajan.resources.broker import money, units

from .models import Identifier

if TYPE_CHECKING:
    from .store import CapacityStore

ALGORITHM_VERSION = "karajan.capacity-facts.v1"
_MAX_QUANTITY = units("9223372036854.775807")


def _encoded(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True, slots=True)
class CapacityFacts:
    """An immutable captured value; decoded dictionaries never modify its content."""

    canonical_json: str
    sha256: str

    def as_dict(self) -> dict[str, Any]:
        return dict(json.loads(self.canonical_json))


def _selection(account_ids: tuple[str, ...] | None) -> tuple[str, ...] | None:
    from .store import CapacityError

    if account_ids is None:
        return None
    try:
        selected = TypeAdapter(tuple[Identifier, ...]).validate_python(account_ids, strict=True)
        if len(selected) > 10000 or len(set(selected)) != len(selected):
            raise ValueError
        # SQLite and source hashes require actual Unicode scalar values.
        for identity in selected:
            identity.encode("utf-8")
    except (ValidationError, ValueError, TypeError, UnicodeError):
        raise CapacityError("CAPACITY_INPUT_INVALID") from None
    return tuple(sorted(selected))


def _source_rows(db: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    return {
        "profiles": [
            {"id": r[0], "revision": r[1], "profile": json.loads(r[2])}
            for r in db.execute("SELECT id,revision,data FROM profiles ORDER BY id,revision")
        ],
        "policies": [
            {"account_id": r[0], "revision": r[1], "policy": json.loads(r[2])}
            for r in db.execute(
                "SELECT account_id,revision,data FROM policies ORDER BY account_id,revision"
            )
        ],
        "observations": [
            {
                "sequence": r[0],
                "observation": json.loads(r[1]),
                "received_at": r[2],
                "applied": bool(r[3]),
            }
            for r in db.execute(
                "SELECT sequence,data,received_at,applied FROM observations ORDER BY sequence"
            )
        ],
        "reservations": [
            {"id": r[0], "attempt_id": r[1], "account_id": r[2], "reservation": json.loads(r[3])}
            for r in db.execute(
                "SELECT id,attempt_id,account_id,data FROM reservations ORDER BY id"
            )
        ],
        "lifecycle": [
            {"sequence": r[0], "admission_id": r[1], "event": json.loads(r[2])}
            for r in db.execute(
                "SELECT sequence,admission_id,data FROM lifecycle ORDER BY sequence"
            )
        ],
        "usage": [
            {"id": r[0], "admission_id": r[1], "account_id": r[2], "usage": json.loads(r[3])}
            for r in db.execute("SELECT id,admission_id,account_id,data FROM usage ORDER BY id")
        ],
        "failures": [
            {"sequence": r[0], "account_id": r[1], "failure": json.loads(r[2])}
            for r in db.execute("SELECT sequence,account_id,data FROM failures ORDER BY sequence")
        ],
    }


def _scoped_sources(
    sources: dict[str, list[dict[str, Any]]], selected: set[str]
) -> dict[str, list[dict[str, Any]]]:
    pools = [p for p in sources["pools"] if p["account_id"] in selected]
    pool_ids = {p["id"] for p in pools}
    admissions = [r for r in sources["reservations"] if r["account_id"] in selected]
    admission_ids = {r["id"] for r in admissions}
    return {
        "pools": pools,
        "profiles": [p for p in sources["profiles"] if p["profile"]["account_id"] in selected],
        "policies": [p for p in sources["policies"] if p["account_id"] in selected],
        "observations": [
            o for o in sources["observations"] if o["observation"]["pool_id"] in pool_ids
        ],
        "reservations": admissions,
        "lifecycle": [e for e in sources["lifecycle"] if e["admission_id"] in admission_ids],
        "usage": [u for u in sources["usage"] if u["account_id"] in selected],
        "failures": [f for f in sources["failures"] if f["account_id"] in selected],
    }


def _pool_facts(
    store: "CapacityStore",
    db: sqlite3.Connection,
    pool: dict[str, Any],
    sources: dict[str, list[dict[str, Any]]],
    policy: dict[str, Any] | None,
    held: list[dict[str, Any]],
    failures: list[dict[str, Any]],
    now: float,
) -> dict[str, Any]:
    applied = [
        item
        for item in sources["observations"]
        if item["applied"] and item["observation"]["pool_id"] == pool["id"]
    ]
    envelope = applied[-1] if applied else None
    observed = envelope["observation"] if envelope else None
    numeric = [o for o in applied if store._numeric_remaining(o["observation"]) is not None]
    remaining = store._numeric_remaining(observed) if observed is not None else None
    uncovered, future = store._pool_usage(
        db,
        pool["id"],
        observed or {"covered_usage_ids": [], "window_id": None, "observed_at": float("-inf")},
        held,
    )
    exhausted = store._exhausted(db, pool, observed, failures)
    diagnostics = []
    if policy is None:
        diagnostics.append("CAPACITY_POLICY_REQUIRED")
    if observed is None:
        diagnostics.append("OBSERVATION_REQUIRED")
    else:
        diagnostics.append("OBSERVATION_CONFIDENCE_UNAVAILABLE")
        if observed["observed_at"] > now:
            diagnostics.append("OBSERVATION_TIME_IN_FUTURE")
        if (
            policy is not None
            and now - observed["observed_at"] > policy["observation_max_age_seconds"]
        ):
            diagnostics.append("OBSERVATION_STALE")
        if observed["reset_at"] is not None and observed["reset_at"] <= now:
            diagnostics.append("WINDOW_EXPIRED")
        if (
            policy is not None
            and pool["kind"] == "service"
            and policy["require_official_observation"]
            and observed["source"] != "official"
        ):
            diagnostics.append("OFFICIAL_OBSERVATION_REQUIRED")
    if remaining is None:
        diagnostics.append("QUOTA_UNKNOWN")
    if remaining is not None and remaining < 0:
        diagnostics.append("REPORTED_USAGE_EXCEEDS_LIMIT")
    if exhausted:
        diagnostics.append("EXHAUSTION_REQUIRES_NEW_OBSERVATION")
    if any(v is not None and not 0 <= v <= _MAX_QUANTITY for v in (remaining, uncovered, future)):
        diagnostics.append("ROUTING_QUANTITY_OUT_OF_RANGE")
    return {
        **pool,
        "observation": envelope,
        "latest_numeric_observation": numeric[-1] if numeric else None,
        "reported_remaining": money(remaining) if remaining is not None else None,
        "local_uncovered": money(uncovered),
        "future_reserved": money(future),
        "safety_margin": policy["safety_margin"].get(pool["id"], "0") if policy else None,
        "lead_reserve": policy["lead_reserve"].get(pool["id"], "0") if policy else None,
        "exhaustion_requires_new_observation": exhausted,
        "diagnostics": diagnostics,
    }


def _account_facts(
    store: "CapacityStore",
    db: sqlite3.Connection,
    identity: str,
    sources: dict[str, list[dict[str, Any]]],
    now: float,
) -> dict[str, Any]:
    policies = [p for p in sources["policies"] if p["account_id"] == identity]
    current = policies[-1] if policies else None
    policy = current["policy"] if current else None
    admissions = []
    held = []
    for row in sources["reservations"]:
        if row["account_id"] != identity:
            continue
        item = row["reservation"]
        expired_unsent = item["state"] == "reserved" and item["expires_at"] <= now
        effective_held = item["state"] in ("active", "unknown") or (
            item["state"] == "reserved" and not expired_unsent
        )
        if effective_held:
            held.append(item)
        admissions.append(
            {
                "admission_id": item["id"],
                "attempt_id": item["request"]["attempt_id"],
                "run_id": item["request"]["run_id"],
                "stored_state": item["state"],
                "effective_held": effective_held,
                "exclusion_reason": "RESERVATION_EXPIRED_UNSENT" if expired_unsent else None,
                "reservation": item,
                "lifecycle": [e for e in sources["lifecycle"] if e["admission_id"] == item["id"]],
            }
        )
    failure_rows = [f for f in sources["failures"] if f["account_id"] == identity]
    failures = [f["failure"] for f in failure_rows]
    cooling = [f["until"] for f in failures if f["until"] > now]
    pools = [
        _pool_facts(store, db, pool, sources, policy, held, failures, now)
        for pool in sources["pools"]
        if pool["account_id"] == identity
    ]
    exhausted = any(p["exhaustion_requires_new_observation"] for p in pools) or (
        not any(p["kind"] == "service" for p in pools)
        and any(f["failure"]["reason"] == "QUOTA_EXHAUSTED" for f in failures)
    )
    return {
        "id": identity,
        "policy_revision": current["revision"] if current else None,
        "policy": policy,
        "profiles": [
            p["profile"] for p in sources["profiles"] if p["profile"]["account_id"] == identity
        ],
        "held_attempts": len(held),
        "unknown_attempts": sum(item["state"] == "unknown" for item in held),
        "held_admission_ids": sorted(item["id"] for item in held),
        "admissions": admissions,
        "usage": [u["usage"] for u in sources["usage"] if u["account_id"] == identity],
        "failures": failure_rows,
        "cooldown_until": max(cooling) if cooling else None,
        "exhaustion_requires_new_observation": exhausted,
        "pools": pools,
    }


def capture_routing_facts(
    store: "CapacityStore",
    db: sqlite3.Connection,
    *,
    account_ids: tuple[str, ...] | None,
) -> CapacityFacts:
    from .store import CapacityError

    selected = _selection(account_ids)
    # The first SELECT anchors the SQLite snapshot before obtaining the one trusted time.
    pools = [json.loads(row[0]) for row in db.execute("SELECT data FROM pools ORDER BY id")]
    now = store._now()
    sources = {"pools": pools, **_source_rows(db)}
    known = {p["account_id"] for p in pools} | {p["account_id"] for p in sources["policies"]}
    if selected is not None and set(selected) - known:
        raise CapacityError("CAPACITY_ACCOUNT_UNKNOWN")
    selected = tuple(sorted(known)) if selected is None else selected
    sources = _scoped_sources(sources, set(selected))
    payload = {
        "schema_version": "karajan.capacity-facts.v1",
        "algorithm_version": ALGORITHM_VERSION,
        "scope": "capacity_fragment",
        "captured_at": now,
        "account_ids": list(selected),
        "accounts": [_account_facts(store, db, identity, sources, now) for identity in selected],
        "source_summary": {
            table: {
                "row_count": len(records),
                "sha256": hashlib.sha256(_encoded(records).encode()).hexdigest(),
            }
            for table, records in sorted(sources.items())
        },
        "missing_facts": [
            "TASK_AUTHORIZATION",
            "EXECUTION_QUALIFICATION",
            "TASK_PROFILE_ESTIMATES",
            "CASH_ACCOUNTING_AND_PRICE_BOUNDS",
            "CURRENCY_CONVERSION",
        ],
        "activation_allowed": False,
    }
    canonical = _encoded(payload)
    return CapacityFacts(canonical, hashlib.sha256(canonical.encode()).hexdigest())
