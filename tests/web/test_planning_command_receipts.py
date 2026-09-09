"""Durable command outcome receipts for the asynchronous planning boundary."""

import sqlite3
from pathlib import Path
from typing import Any

from karajan.web.planning import PlanningWorkbench


class _Planner:
    def clock(self) -> float:
        return 1000.0


def test_execute_command_persists_unknown_outcome_for_snapshot_recovery(tmp_path: Path) -> None:
    workbench = PlanningWorkbench(
        tmp_path / "workbench.sqlite", _Planner(), object()  # type: ignore[arg-type]
    )
    command: dict[str, Any] = workbench._claim_execute(
        "run", principal="owner", command_key="execute"
    )

    workbench._finish_execute(
        command, state="unknown", reason_code="PLANNING_EXECUTE_COMMAND_UNKNOWN"
    )

    saved = workbench._execute_command_for_run("run", "owner")
    assert saved is not None
    assert workbench._command_view(saved) == {
        "id": command["command_id"],
        "state": "unknown",
        "reason_code": "PLANNING_EXECUTE_COMMAND_UNKNOWN",
    }


def test_command_receipt_migration_names_columns_and_never_regresses_terminal(
    tmp_path: Path,
) -> None:
    database = tmp_path / "workbench.sqlite"
    with sqlite3.connect(database) as db:
        db.execute(
            "CREATE TABLE planning_execute_commands ("
            "principal TEXT NOT NULL, key TEXT NOT NULL, run_id TEXT NOT NULL, "
            "command_id TEXT NOT NULL, execution_id TEXT, binding_sha256 TEXT, "
            "state TEXT NOT NULL, created_at REAL NOT NULL, PRIMARY KEY(principal, key))"
        )
    workbench = PlanningWorkbench(database, _Planner(), object())  # type: ignore[arg-type]
    command = workbench._claim_execute("run", principal="owner", command_key="execute")

    assert workbench._finish_execute(command, state="completed")
    assert not workbench._finish_execute(
        command, state="unknown", reason_code="PLANNING_EXECUTE_COMMAND_UNKNOWN"
    )
    saved = workbench._execute_command_for_run("run", "owner")
    assert saved is not None
    assert workbench._command_view(saved) == {"id": command["command_id"], "state": "completed"}


def test_completion_resolves_an_overlapping_replay_unknown(tmp_path: Path) -> None:
    workbench = PlanningWorkbench(tmp_path / "workbench.sqlite", _Planner(), object())  # type: ignore[arg-type]
    command = workbench._claim_execute("run", principal="owner", command_key="execute")

    assert workbench._finish_execute(
        command, state="unknown", reason_code="PLANNING_EXECUTE_COMMAND_UNKNOWN"
    )
    assert workbench._finish_execute(command, state="completed")
    saved = workbench._execute_command_for_run("run", "owner")
    assert saved is not None
    assert workbench._command_view(saved) == {"id": command["command_id"], "state": "completed"}
