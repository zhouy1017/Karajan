"""Durable command outcome receipts for the asynchronous planning boundary."""

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
