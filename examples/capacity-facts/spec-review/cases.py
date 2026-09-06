"""Independent public-API arrangements for the Capacity facts Spec review."""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from karajan.capacity import CapacityStore

MAXIMUM = "9223372036854.775807"


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def all_rows(path: Path) -> dict[str, Any]:
    """Read every logical table, including command rows omitted by snapshot()."""
    with closing(sqlite3.connect(path)) as db:
        tables = [
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        ]
        result = {}
        for table in tables:
            escaped = '"' + table.replace('"', '""') + '"'
            rows = list(db.execute("SELECT * FROM " + escaped))
            result[table] = sorted(rows, key=canonical)
        return result


def row_digests(rows: dict[str, Any]) -> dict[str, Any]:
    return {
        table: {
            "count": len(values),
            "sha256": hashlib.sha256(canonical(values).encode()).hexdigest(),
        }
        for table, values in rows.items()
    }


class Arrangement:
    def __init__(
        self,
        root: Path,
        *,
        windows: tuple[str, ...] = ("fixed",),
        initial: str = "80",
        limit: str = "100",
    ) -> None:
        self.now = 1000.0
        self.events: list[dict[str, Any]] = []
        self.store = CapacityStore(root / "capacity.sqlite", clock=lambda: self.now)
        self.pool_ids = ["pool-" + kind for kind in windows]
        for identity, window in zip(self.pool_ids, windows, strict=True):
            self.call(
                "register_pool",
                {
                    "id": identity,
                    "account_id": "account-a",
                    "kind": "service",
                    "unit": "requests",
                    "window_kind": window,
                },
            )
            self.observe(identity, amount=initial, limit=limit)
        for profile in ("profile-a", "profile-b"):
            self.call(
                "register_profile",
                {
                    "id": profile,
                    "revision": 1,
                    "account_id": "account-a",
                    "pool_ids": self.pool_ids,
                },
            )
        self.policy = {
            "account_id": "account-a",
            "max_active_attempts": 20,
            "max_attempt_duration_seconds": 60,
            "observation_max_age_seconds": 60,
            "require_official_observation": False,
            "safety_margin": {},
            "lead_reserve": {},
            "lead_reserved_slots": 0,
            "conservative_mode": None,
        }
        self.call("activate_policy", self.policy, expected_revision=0)

    def call(self, method: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        kwargs["command_key"] = f"spec-event-{len(self.events)}"
        result = getattr(self.store, method)(*args, **kwargs)
        self.events.append(
            copy.deepcopy(
                {
                    "clock": self.now,
                    "method": method,
                    "args": args,
                    "kwargs": kwargs,
                    "result": result,
                }
            )
        )
        return result

    def observe(
        self,
        pool: str,
        *,
        amount: str | None = "80",
        limit: str | None = "100",
        metric: str = "remaining",
        at: float | None = None,
        window: str = "window-old",
        reset: float | None = 1010.0,
        covered: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        return self.call(
            "observe",
            {
                "pool_id": pool,
                "window_id": window,
                "observed_at": self.now if at is None else at,
                "reset_at": reset,
                "source": "fixture",
                "source_ref": f"spec-observation-{len(self.events)}",
                "metric": metric,
                "amount": amount,
                "limit": limit,
                "covered_usage_ids": list(covered),
                "coverage_ref": "spec-explicit-coverage" if covered else None,
            },
        )

    def admit(
        self, name: str, *, amount: str = "8", duration: int = 9, other_run: bool = False
    ) -> str:
        result = self.call(
            "admit",
            {
                "attempt_id": "attempt-" + name,
                "run_id": "run-b" if other_run else "run-a",
                "profile_id": "profile-b" if other_run else "profile-a",
                "profile_revision": 1,
                "role": "worker",
                "purpose": None,
                "authorization_ref": "spec-approved-scope",
                "rulebook_revision": "rules-run-b" if other_run else "rules-run-a",
                "duration_seconds": duration,
                "demand": dict.fromkeys(self.pool_ids, amount),
            },
        )
        assert result["decision"] == "admitted", result
        return str(result["admission_id"])

    def start(self, admission: str) -> None:
        result = self.call("activate", admission)
        assert result["decision"] == "capacity_revalidated", result

    def usage(self, admission: str, identity: str, amount: str, *, attributed: bool = True) -> None:
        self.call(
            "record_usage",
            {
                "id": identity,
                "admission_id": admission,
                "amounts": dict.fromkeys(self.pool_ids, amount),
                "window_ids": dict.fromkeys(self.pool_ids, "window-old" if attributed else None),
                "evidence_ref": "spec-usage-evidence-" + identity,
                "attribution_ref": "spec-window-evidence" if attributed else None,
            },
        )

    def finish(self, admission: str, *, remote_ended: bool = True) -> None:
        self.call(
            "reconcile",
            admission,
            local_ended=True,
            remote_ended=remote_ended,
            usage_complete=remote_ended,
            not_sent=False,
            evidence_ref="spec-end-evidence",
        )


def mixed_lifecycle(root: Path) -> Arrangement:
    case = Arrangement(root, windows=("fixed", "rolling"))
    active = case.admit("active")
    case.start(active)
    case.usage(active, "active-use", "3")
    unknown = case.admit("unknown", other_run=True)
    case.start(unknown)
    case.usage(unknown, "unknown-use", "1")
    case.finish(unknown, remote_ended=False)
    case.admit("expired-reserved", duration=2)
    ended = case.admit("ended", amount="2")
    case.start(ended)
    case.usage(ended, "ended-use", "2")
    case.finish(ended)
    released = case.admit("released", amount="2")
    case.call(
        "reconcile",
        released,
        local_ended=True,
        remote_ended=True,
        usage_complete=False,
        not_sent=True,
        evidence_ref="spec-unsent-evidence",
    )
    case.now = 1012.0
    return case


def aggregate_beyond_scalar_limit(root: Path) -> Arrangement:
    case = Arrangement(root, initial=MAXIMUM, limit=MAXIMUM)
    first = case.admit("first", amount="1")
    second = case.admit("second", amount="1", other_run=True)
    case.start(first)
    case.start(second)
    case.usage(first, "large-first", MAXIMUM)
    case.usage(second, "large-second", MAXIMUM)
    case.finish(first)
    case.finish(second)
    return case


def reset_with_and_without_attribution(root: Path) -> Arrangement:
    case = Arrangement(root, windows=("fixed", "rolling", "balance", "unknown"))
    first = case.admit("attributed", amount="3")
    second = case.admit("unattributed", amount="3", other_run=True)
    for identity in (first, second):
        case.start(identity)
    case.usage(first, "attributed-use", "3")
    case.usage(second, "unattributed-use", "3", attributed=False)
    case.finish(first)
    case.finish(second)
    case.now = 1011.0
    for pool in case.pool_ids:
        case.observe(pool, window="window-new", reset=1020.0)
    return case


def exhausted_then_unknown_and_unapplied(root: Path) -> Arrangement:
    case = Arrangement(root)
    case.now = 1001.0
    case.observe("pool-fixed", amount="0")
    case.call(
        "record_failure",
        "account-a",
        reason="QUOTA_EXHAUSTED",
        retry_after_seconds=2,
        evidence_ref="spec-exhausted-error",
    )
    case.now = 1004.0
    case.observe("pool-fixed", amount=None, metric="unknown")
    case.now = 1005.0
    latest = case.observe(
        "pool-fixed",
        amount="100",
        window="window-premature",
        reset=1020.0,
    )
    assert latest["applied"] is False
    return case


def partial_coverage(root: Path) -> Arrangement:
    case = Arrangement(root)
    active = case.admit("active")
    case.start(active)
    case.usage(active, "covered-first", "1")
    case.usage(active, "uncovered-second", "2")
    case.now = 1001.0
    case.observe("pool-fixed", covered=("covered-first",))
    return case


def used_above_limit(root: Path) -> Arrangement:
    case = Arrangement(root)
    case.now = 1001.0
    case.observe("pool-fixed", metric="used", amount="101", limit="100")
    return case
