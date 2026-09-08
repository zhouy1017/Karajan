"""Owner-facing preparation of one durable PlanningExecution.

This is deliberately only the workbench-to-controller start boundary.  It has
no model adapter, output authority, prompt, or admission shortcut: an execution
created here remains a durable identity for the later #112 transport.
"""

import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from pydantic import BaseModel, ConfigDict

from karajan.orchestration.planning_execution import PlanningExecution
from karajan.orchestration.planning_transport import PlanningTransport
from karajan.runs import RunError, RunPlanner
from karajan.runs.planning import identifier

from .projects import command_key


class PlanningStartInput(BaseModel):
    """The browser supplies no planning authority or model material."""

    model_config = ConfigDict(extra="forbid", strict=True)


class PlanningExecuteInput(BaseModel):
    """The browser can request execution but cannot select its inputs."""

    model_config = ConfigDict(extra="forbid", strict=True)


class PlanningWorkbench:
    """Persist one owner request before using the Commander and execution ledgers."""

    def __init__(
        self,
        database: Path,
        planner: RunPlanner,
        execution: PlanningExecution,
        transport: PlanningTransport | None = None,
    ) -> None:
        self.database = database
        self.planner = planner
        self.execution, self.transport = execution, transport
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS planning_start_commands ("
                "principal TEXT NOT NULL, key TEXT NOT NULL, run_id TEXT NOT NULL, "
                "term INTEGER NOT NULL, commander TEXT NOT NULL, intent_key TEXT NOT NULL, "
                "execution_key TEXT NOT NULL, intent_id TEXT, execution_id TEXT, "
                "created_at REAL NOT NULL, PRIMARY KEY(principal, key))"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS planning_start_commands_run "
                "ON planning_start_commands(run_id, created_at DESC)"
            )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.database, isolation_level=None, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _child_key(kind: str, run_id: str, key: str) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"karajan:{kind}:{run_id}:{key}"))

    def _record(self, principal: str, key: str) -> dict[str, Any] | None:
        with self._transaction() as db:
            row = db.execute(
                "SELECT * FROM planning_start_commands WHERE principal=? AND key=?",
                (principal, key),
            ).fetchone()
        return None if row is None else dict(row)

    def _claim(self, run: dict[str, Any], principal: str, key: str) -> dict[str, Any]:
        commander = run["commander"]
        record = {
            "principal": principal,
            "key": key,
            "run_id": run["id"],
            "term": commander["term"],
            "commander": commander["principal"],
            "intent_key": self._child_key("planning-intent", run["id"], key),
            "execution_key": self._child_key("planning-execution", run["id"], key),
            "intent_id": None,
            "execution_id": None,
            "created_at": self.planner.clock(),
        }
        with self._transaction() as db:
            prior = db.execute(
                "SELECT * FROM planning_start_commands WHERE principal=? AND key=?",
                (principal, key),
            ).fetchone()
            if prior is not None:
                existing = dict(prior)
                if existing["run_id"] != run["id"]:
                    raise RunError("IDEMPOTENCY_CONFLICT")
                return existing
            db.execute(
                "INSERT INTO planning_start_commands VALUES "
                "(:principal,:key,:run_id,:term,:commander,:intent_key,:execution_key,"
                ":intent_id,:execution_id,:created_at)",
                record,
            )
        return record

    def _save(
        self,
        record: dict[str, Any],
        *,
        intent_id: str | None = None,
        execution_id: str | None = None,
    ) -> dict[str, Any]:
        updated = dict(record)
        if intent_id is not None:
            updated["intent_id"] = intent_id
        if execution_id is not None:
            updated["execution_id"] = execution_id
        with self._transaction() as db:
            db.execute(
                "UPDATE planning_start_commands SET intent_id=?, execution_id=? "
                "WHERE principal=? AND key=?",
                (
                    updated["intent_id"],
                    updated["execution_id"],
                    updated["principal"],
                    updated["key"],
                ),
            )
        return updated

    @staticmethod
    def _intent_view(intent: dict[str, Any]) -> dict[str, Any]:
        profile = intent["profile"]
        return {
            "id": intent["id"],
            "term": intent["term"],
            "principal": intent["principal"],
            "profile": {"id": profile["id"], "revision": profile["revision"]},
            "budget_ref": intent["budget_ref"],
            "state": intent["state"],
        }

    @staticmethod
    def _execution_view(execution: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": execution["id"],
            "binding_sha256": execution["binding_sha256"],
            "state": execution["state"],
            "cancel_requested": execution["cancel_requested"],
            "reason_codes": execution["reason_codes"],
        }

    def _availability(
        self, run: dict[str, Any], execution: dict[str, Any] | None
    ) -> dict[str, str]:
        if execution is not None and execution["state"] == "submitted":
            return {"state": "completed"}
        if execution is not None and execution["cancel_requested"]:
            return {"state": "blocked", "reason_code": "PLANNING_EXECUTION_CANCELLED"}
        if execution is not None and execution["binding"]["term"] != run["commander"]["term"]:
            return {"state": "blocked", "reason_code": "PLANNING_EXECUTION_BINDING_STALE"}
        if self.execution.outputs is None:
            return {"state": "blocked", "reason_code": "PLANNING_TRANSPORT_UNAVAILABLE"}
        return {"state": "awaiting"}

    def _project(self, run: dict[str, Any], record: dict[str, Any] | None) -> dict[str, Any]:
        planning: dict[str, Any] | None = None
        if record is not None:
            intent = next(
                (item for item in run["planning_intents"] if item["id"] == record["intent_id"]),
                None,
            )
            execution = (
                None
                if record["execution_id"] is None
                else self.execution.get(record["execution_id"], principal=run["owner"])
            )
            planning = {
                "intent": None if intent is None else self._intent_view(intent),
                "execution": None if execution is None else self._execution_view(execution),
                "availability": self._availability(run, execution),
            }
        return {
            "schema_version": "karajan.workbench-planning.v1",
            "run": run,
            "planning": planning,
        }

    def read(self, run_id: str, *, principal: str) -> dict[str, Any]:
        run = self.planner.get(run_id, principal=principal)
        with self._transaction() as db:
            row = db.execute(
                "SELECT * FROM planning_start_commands WHERE run_id=? AND principal=? "
                "ORDER BY created_at DESC LIMIT 1",
                (run_id, principal),
            ).fetchone()
        return self._project(run, None if row is None else dict(row))

    def start(self, run_id: str, *, principal: str, command_key: str) -> dict[str, Any]:
        for value in (run_id, principal, command_key):
            identifier(value)
        run = self.planner.get(run_id, principal=principal)
        record = self._record(principal, command_key)
        if record is not None and record["run_id"] != run_id:
            raise RunError("IDEMPOTENCY_CONFLICT")
        if record is None:
            if run["plans"]:
                raise RunError("PLANNING_PLAN_ALREADY_PRESENT")
            record = self._claim(run, principal, command_key)
        intent = self.planner.planning_intent(
            run_id,
            term=record["term"],
            principal=record["commander"],
            command_key=record["intent_key"],
        )
        record = self._save(record, intent_id=intent["id"])
        execution = self.execution.begin(
            run_id,
            intent["id"],
            principal=principal,
            command_key=record["execution_key"],
        )
        record = self._save(record, execution_id=execution["id"])
        return self._project(self.planner.get(run_id, principal=principal), record)

    def execute(self, run_id: str, *, principal: str, command_key: str) -> dict[str, Any]:
        """Recover one original identity, then advance only its fixed transport."""
        started = self.start(run_id, principal=principal, command_key=command_key)
        planning = started["planning"]
        if planning is None or planning["execution"] is None or self.transport is None:
            return started
        self.transport.execute(
            planning["execution"]["id"], principal=principal, command_key=command_key
        )
        return self.read(run_id, principal=principal)


def register_planning_routes(app: FastAPI, workbench: PlanningWorkbench) -> None:
    @app.get("/v1/runs/{run_id}/planning")
    def get_planning(run_id: str) -> dict[str, Any]:
        return workbench.read(run_id, principal="owner")

    @app.post("/v1/runs/{run_id}/planning-start")
    def start_planning(run_id: str, request: Request, data: PlanningStartInput) -> dict[str, Any]:
        del data
        return workbench.start(run_id, principal="owner", command_key=command_key(request))

    @app.post("/v1/runs/{run_id}/planning-execute")
    def execute_planning(
        run_id: str, request: Request, data: PlanningExecuteInput
    ) -> dict[str, Any]:
        del data
        return workbench.execute(run_id, principal="owner", command_key=command_key(request))
