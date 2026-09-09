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
from threading import Thread
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
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
            db.execute(
                "CREATE TABLE IF NOT EXISTS planning_execute_commands ("
                "principal TEXT NOT NULL, key TEXT NOT NULL, run_id TEXT NOT NULL, "
                "command_id TEXT NOT NULL, execution_id TEXT, binding_sha256 TEXT, "
                "state TEXT NOT NULL, reason_code TEXT, created_at REAL NOT NULL, "
                "PRIMARY KEY(principal, key))"
            )
            columns = {
                row[1] for row in db.execute("PRAGMA table_info(planning_execute_commands)")
            }
            if "reason_code" not in columns:
                db.execute("ALTER TABLE planning_execute_commands ADD COLUMN reason_code TEXT")

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

    def _claim_execute(
        self, run_id: str, *, principal: str, command_key: str
    ) -> dict[str, Any]:
        record = {
            "principal": principal,
            "key": command_key,
            "run_id": run_id,
            "command_id": self._child_key("planning-execute", run_id, command_key),
            "execution_id": None,
            "binding_sha256": None,
            "state": "accepted",
            "reason_code": None,
            "created_at": self.planner.clock(),
        }
        with self._transaction() as db:
            row = db.execute(
                "SELECT * FROM planning_execute_commands WHERE principal=? AND key=?",
                (principal, command_key),
            ).fetchone()
            if row is None:
                db.execute(
                "INSERT INTO planning_execute_commands ("
                "principal,key,run_id,command_id,execution_id,binding_sha256,state,reason_code,"
                "created_at) VALUES ("
                ":principal,:key,:run_id,:command_id,:execution_id,:binding_sha256,:state,"
                ":reason_code,:created_at)",
                    record,
                )
                return record
            existing = dict(row)
            if existing["run_id"] != run_id:
                raise RunError("IDEMPOTENCY_CONFLICT")
            return existing

    def _bind_execute(
        self, record: dict[str, Any], execution: dict[str, Any]
    ) -> dict[str, Any]:
        expected = (execution["id"], execution["binding_sha256"])
        with self._transaction() as db:
            row = db.execute(
                "SELECT execution_id,binding_sha256 FROM planning_execute_commands "
                "WHERE principal=? AND key=?",
                (record["principal"], record["key"]),
            ).fetchone()
            if row is None:
                raise RunError("PLANNING_EXECUTE_COMMAND_UNAVAILABLE")
            existing = tuple(row)
            if existing not in {(None, None), expected}:
                raise RunError("IDEMPOTENCY_CONFLICT")
            if existing == (None, None):
                db.execute(
                    "UPDATE planning_execute_commands SET execution_id=?, binding_sha256=? "
                    "WHERE principal=? AND key=?",
                    (*expected, record["principal"], record["key"]),
                )
        return {**record, "execution_id": expected[0], "binding_sha256": expected[1]}

    def _finish_execute(
        self,
        record: dict[str, Any],
        *,
        state: str,
        reason_code: str | None = None,
    ) -> bool:
        # A replay can only report an uncertain in-flight outcome.  The owner
        # that later persists the Run receipt must be able to resolve that
        # uncertainty; no other terminal state may replace it.
        prior_states = ("accepted", "unknown") if state == "completed" else ("accepted",)
        placeholders = ",".join("?" for _ in prior_states)
        with self._transaction() as db:
            updated = db.execute(
                "UPDATE planning_execute_commands SET state=?, reason_code=? "
                "WHERE principal=? AND key=? AND command_id=? AND state IN (" + placeholders + ")",
                (
                    state,
                    reason_code,
                    record["principal"],
                    record["key"],
                    record["command_id"],
                    *prior_states,
                ),
            )
        return updated.rowcount == 1

    def _execute_command_for_run(self, run_id: str, principal: str) -> dict[str, Any] | None:
        with self._transaction() as db:
            row = db.execute(
                "SELECT * FROM planning_execute_commands WHERE run_id=? AND principal=? "
                "ORDER BY created_at DESC LIMIT 1",
                (run_id, principal),
            ).fetchone()
        return None if row is None else dict(row)

    @staticmethod
    def _command_view(record: dict[str, Any]) -> dict[str, str]:
        view = {"id": record["command_id"], "state": record["state"]}
        if isinstance(record.get("reason_code"), str):
            view["reason_code"] = record["reason_code"]
        return view

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
        view: dict[str, Any] = {
            "id": execution["id"],
            "binding_sha256": execution["binding_sha256"],
            "state": execution["state"],
            "cancel_requested": execution["cancel_requested"],
            "reason_codes": execution["reason_codes"],
        }
        if isinstance(execution.get("native_cleanup"), dict):
            view["native_cleanup"] = execution["native_cleanup"]
        if execution.get("provider_remote_stop") in {"confirmed", "unknown"}:
            view["provider_remote_stop"] = execution["provider_remote_stop"]
        return view

    def _availability(
        self, run: dict[str, Any], execution: dict[str, Any] | None
    ) -> dict[str, str]:
        if execution is not None and execution["state"] == "submitted":
            return {"state": "completed"}
        if execution is not None and execution["cancel_requested"]:
            return {"state": "blocked", "reason_code": "PLANNING_EXECUTION_CANCELLED"}
        if execution is not None and execution["binding"]["term"] != run["commander"]["term"]:
            return {"state": "blocked", "reason_code": "PLANNING_EXECUTION_BINDING_STALE"}
        if execution is not None and execution["state"] in {
            "blocked",
            "admission_unknown",
            "submission_unknown",
        }:
            reason_codes = execution.get("reason_codes")
            reason = (
                reason_codes[0]
                if isinstance(reason_codes, list)
                and reason_codes
                and isinstance(reason_codes[0], str)
                else "PLANNING_EXECUTION_UNKNOWN"
            )
            return {"state": "blocked", "reason_code": reason}
        if execution is not None and execution["state"] == "awaiting_output":
            # A durable dispatch claim may be pending after an interrupted
            # local/native request.  It cannot safely be sent again.
            return {"state": "blocked", "reason_code": "PLANNING_OUTPUT_PENDING"}
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
        # Read a command first: a completed command is written only after the
        # Run store commits its Plan, so the following fresh Run read cannot
        # report a terminal receipt alongside an older plan snapshot.
        command = self._execute_command_for_run(run_id, principal)
        run = self.planner.get(run_id, principal=principal)
        with self._transaction() as db:
            row = db.execute(
                "SELECT * FROM planning_start_commands WHERE run_id=? AND principal=? "
                "ORDER BY created_at DESC LIMIT 1",
                (run_id, principal),
            ).fetchone()
        result = self._project(run, None if row is None else dict(row))
        return result if command is None else {**result, "command": self._command_view(command)}

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
        """Persist and acknowledge the command before the native transport runs."""
        for value in (run_id, principal, command_key):
            identifier(value)
        run = self.planner.get(run_id, principal=principal)
        command = self._claim_execute(run_id, principal=principal, command_key=command_key)
        with self._transaction() as db:
            row = db.execute(
                "SELECT * FROM planning_start_commands WHERE run_id=? AND principal=? "
                "ORDER BY created_at DESC LIMIT 1",
                (run_id, principal),
            ).fetchone()
        started = (
            self.start(run_id, principal=principal, command_key=command_key)
            if row is None
            else self._project(run, dict(row))
        )
        planning = started["planning"]
        transport = self.transport
        if planning is None or planning["execution"] is None or transport is None:
            return {**started, "command": self._command_view(command)}
        execution = self.execution.get(planning["execution"]["id"], principal=principal)
        command = self._bind_execute(command, execution)

        def advance() -> None:
            try:
                # Retrying this durable command may overlap a prior process,
                # but PlanningTransport's pre-effect receipt and dispatch
                # claim ensure it cannot create another native session/send.
                outcome = transport.execute(
                    execution["id"], principal=principal, command_key=command_key
                )
                outcome_state = outcome.get("state")
                reason_codes = outcome.get("reason_codes")
                reason = (
                    reason_codes[0]
                    if isinstance(reason_codes, list)
                    and reason_codes
                    and isinstance(reason_codes[0], str)
                    else None
                )
                if outcome_state == "submitted":
                    self._finish_execute(command, state="completed")
                elif outcome_state == "blocked":
                    self._finish_execute(command, state="failed", reason_code=reason)
                else:
                    self._finish_execute(
                        command,
                        state="unknown",
                        reason_code=reason or "PLANNING_EXECUTE_COMMAND_UNKNOWN",
                    )
            except RunError as error:
                self._finish_execute(command, state="failed", reason_code=error.code)
            except Exception:
                self._finish_execute(
                    command,
                    state="unknown",
                    reason_code="PLANNING_EXECUTE_COMMAND_UNKNOWN",
                )

        Thread(target=advance, daemon=True).start()
        return {**self.read(run_id, principal=principal), "command": self._command_view(command)}


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
    ) -> JSONResponse:
        del data
        return JSONResponse(
            workbench.execute(run_id, principal="owner", command_key=command_key(request)),
            status_code=202,
        )
