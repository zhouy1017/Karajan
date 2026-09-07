"""One Run's supervised process budget in its existing admission ledger.

Writer, deterministic check and Reviewer processes share the original total.
These counters are not model calls or provider settlement observations.
"""

import json
import math
import sqlite3
from copy import deepcopy
from typing import TYPE_CHECKING, Any, Literal

from karajan.runs import RunError
from karajan.runs.planning import encoded

if TYPE_CHECKING:
    from .admission import ApprovedTaskAdmission


class RunExecutionBudget:
    def __init__(self, admissions: "ApprovedTaskAdmission") -> None:
        self.admissions = admissions

    def get(self, run_id: str, *, principal: str) -> dict[str, Any] | None:
        from .go_execution_intent import GoExecutionIntents, _connection

        GoExecutionIntents._check_owner(self.admissions, run_id, run_id, principal)
        with _connection(self.admissions.database, readonly=True) as db:
            if not _has_table(db):
                return None
            return _read(db, run_id)


def _has_table(db: sqlite3.Connection) -> bool:
    return (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='run_execution_budgets'"
        ).fetchone()
        is not None
    )


def _read(db: sqlite3.Connection, run_id: str) -> dict[str, Any] | None:
    row = db.execute("SELECT data FROM run_execution_budgets WHERE run_id=?", (run_id,)).fetchone()
    return dict(json.loads(row[0])) if row else None


def claim_process(
    db: sqlite3.Connection,
    run: dict[str, Any],
    operation: dict[str, Any],
    *,
    attempt_id: str,
    scope: Literal["writer", "check", "reviewer"],
    now: float,
) -> dict[str, Any]:
    """Internal same-transaction claim; caller already holds operation and Run.

    An old execution without this ledger cannot acquire a fabricated new start
    time. Default schema creation does not migrate or infer execution history.
    """
    if not _has_table(db):
        raise RunError("RUN_EXECUTION_HISTORY_RECONCILIATION_REQUIRED")
    if type(now) not in (int, float) or not math.isfinite(now):
        raise RunError("RUN_EXECUTION_CLOCK_INVALID")
    plan = next(row for row in run["plans"] if row["plan_revision"] == run["active_plan_revision"])
    authorization = plan["plan"]["authorization"]
    resource = next(
        row
        for row in run["configuration_snapshot"]["configuration"]["resources"]["budgets"]
        if row["id"] == authorization["budget_ref"]
    )
    identity = {
        "attempt_id": attempt_id,
        "scope": scope,
        "operation_id": operation["id"],
        "root_task_id": operation["assessment"]["route"]["snapshots"]["task"]["root_task_id"],
        "plan_revision": plan["plan_revision"],
        "budget_ref": authorization["budget_ref"],
    }
    budget = _read(db, run["id"])
    if budget is None:
        histories = db.execute(
            "SELECT data FROM operations WHERE run_id=?", (run["id"],)
        ).fetchall()
        if scope != "writer" or any("execution" in json.loads(row[0]) for row in histories):
            raise RunError("RUN_EXECUTION_HISTORY_RECONCILIATION_REQUIRED")
        budget = {
            "schema_version": "karajan.run-execution-budget.v1",
            "run_id": run["id"],
            "started_at": now,
            "max_total_attempts": resource["max_total_attempts"],
            "max_duration_seconds": resource["max_duration_seconds"],
            "claims": [],
        }
    previous = next((row for row in budget["claims"] if row["attempt_id"] == attempt_id), None)
    if previous is not None:
        if {key: previous[key] for key in identity} != identity:
            raise RunError("RUN_EXECUTION_CLAIM_CONFLICT")
        return deepcopy(budget)
    # A later approval may narrow the original boundary but cannot enlarge it.
    budget["max_total_attempts"] = min(budget["max_total_attempts"], resource["max_total_attempts"])
    budget["max_duration_seconds"] = min(
        budget["max_duration_seconds"], resource["max_duration_seconds"]
    )
    if now < budget["started_at"]:
        raise RunError("RUN_EXECUTION_CLOCK_REGRESSED")
    if len(budget["claims"]) >= budget["max_total_attempts"]:
        raise RunError("RUN_ATTEMPT_LIMIT")
    if now >= budget["started_at"] + budget["max_duration_seconds"]:
        raise RunError("RUN_DURATION_LIMIT")
    budget["claims"].append({**identity, "claimed_at": now})
    db.execute(
        "INSERT INTO run_execution_budgets VALUES (?,?) "
        "ON CONFLICT(run_id) DO UPDATE SET data=excluded.data",
        (run["id"], encoded(budget)),
    )
    return deepcopy(budget)


def current_process(
    db: sqlite3.Connection,
    run: dict[str, Any],
    operation: dict[str, Any],
    *,
    attempt_id: str,
    now: float,
) -> float:
    """Recheck a claimed process without refunding or writing counter history."""
    if not _has_table(db) or (budget := _read(db, run["id"])) is None:
        raise RunError("RUN_EXECUTION_HISTORY_RECONCILIATION_REQUIRED")
    if type(now) not in (int, float) or not math.isfinite(now) or now < budget["started_at"]:
        raise RunError("RUN_EXECUTION_CLOCK_REGRESSED")
    if not any(
        row["attempt_id"] == attempt_id and row["operation_id"] == operation["id"]
        for row in budget["claims"]
    ):
        raise RunError("RUN_EXECUTION_CLAIM_REQUIRED")
    plan = next(row for row in run["plans"] if row["plan_revision"] == run["active_plan_revision"])
    resource = next(
        row
        for row in run["configuration_snapshot"]["configuration"]["resources"]["budgets"]
        if row["id"] == plan["plan"]["authorization"]["budget_ref"]
    )
    maximum = min(budget["max_total_attempts"], resource["max_total_attempts"])
    deadline = budget["started_at"] + min(
        budget["max_duration_seconds"], resource["max_duration_seconds"]
    )
    if len(budget["claims"]) > maximum:
        raise RunError("RUN_ATTEMPT_LIMIT")
    if now >= deadline:
        raise RunError("RUN_DURATION_LIMIT")
    return float(deadline)
