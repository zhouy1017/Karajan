"""Recoverable quota reservations for approved tasks; no process or model effects."""

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from karajan.capacity import CapacityError
from karajan.routing.compiler import digest
from karajan.runs import RunError
from karajan.runs.planning import encoded, identifier
from karajan.storage import open_database, require_schema

from .routing import ApprovedRunRouting


class ApprovedTaskAdmission:
    """Record intent before Capacity writes, then recover through immutable receipts.

    No Host or activation interface is exposed. A reservation remains expiring
    until a separately qualified execution consumer is implemented.
    """

    def __init__(
        self, database: Path, routing: ApprovedRunRouting, *, existing_only: bool = False
    ) -> None:
        if existing_only and not (
            routing.planner.existing_only
            and routing.planner.projects.existing_only
            and routing.capacity.existing_only
        ):
            raise RunError("EXISTING_STORE_PARENT_MODE_REQUIRED")
        self.database, self.routing = database.resolve(), routing
        self.existing_only = existing_only
        if not existing_only:
            self.database.parent.mkdir(parents=True, exist_ok=True)
        if self.database in {
            routing.planner.database.resolve(),
            routing.planner.projects.database.resolve(),
            routing.capacity.path.resolve(),
        }:
            raise RunError("ADMISSION_DATABASE_MUST_BE_SEPARATE")
        if existing_only:
            require_schema(
                self.database,
                {
                    "operations": ["id", "run_id", "task_id", "state", "data"],
                    "commands": ["principal", "key", "payload", "result"],
                },
            )
            return
        with self._transaction() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, run_id TEXT NOT NULL, "
                "task_id TEXT NOT NULL, state TEXT NOT NULL, data TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS commands (principal TEXT NOT NULL, key TEXT NOT NULL, "
                "payload TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(principal,key))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS run_execution_budgets "
                "(run_id TEXT PRIMARY KEY, data TEXT NOT NULL)"
            )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        db = open_database(self.database, existing_only=self.existing_only, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _owner(self, run_id: str, principal: str) -> None:
        self.routing.planner.get(run_id, principal=principal)

    @staticmethod
    def _load(db: sqlite3.Connection, run_id: str, operation_id: str) -> dict[str, Any]:
        row = db.execute(
            "SELECT data FROM operations WHERE id=? AND run_id=?", (operation_id, run_id)
        ).fetchone()
        if row is None:
            raise RunError("TASK_ADMISSION_NOT_FOUND")
        return dict(json.loads(row["data"]))

    @staticmethod
    def _save(db: sqlite3.Connection, operation: dict[str, Any]) -> None:
        db.execute(
            "INSERT INTO operations VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE "
            "SET state=excluded.state,data=excluded.data",
            (
                operation["id"],
                operation["run_id"],
                operation["task_id"],
                operation["state"],
                encoded(operation),
            ),
        )

    def _refresh(self, db: sqlite3.Connection, operation: dict[str, Any]) -> dict[str, Any]:
        if "execution" in operation or operation["state"] != "reserved":
            return operation
        facts = self.routing.capacity.routing_facts()
        admission_id = operation["capacity_receipt"]["admission_id"]
        current = next(
            (
                row
                for account in facts.as_dict()["accounts"]
                for row in account["admissions"]
                if row["admission_id"] == admission_id
            ),
            None,
        )
        reason = None
        if current is None:
            operation["state"], reason = "reconciliation_required", "CAPACITY_ADMISSION_MISSING"
        elif current["stored_state"] == "expired" or current["exclusion_reason"] == (
            "RESERVATION_EXPIRED_UNSENT"
        ):
            operation["state"], reason = "expired", "RESERVATION_EXPIRED_UNSENT"
        elif current["stored_state"] == "released":
            operation["state"], reason = "released", "RESERVATION_RELEASED"
        elif current["stored_state"] != "reserved":
            operation["state"], reason = (
                "reconciliation_required",
                "EXECUTION_RECONCILIATION_REQUIRED",
            )
        if reason:
            operation["reason_codes"] = [reason]
            operation["capacity_status"] = {
                "facts_sha256": facts.sha256,
                "admission": current,
            }
            self._save(db, operation)
        return operation

    def enqueue(
        self, run_id: str, task_id: str, *, principal: str, command_key: str
    ) -> dict[str, Any]:
        for value in (run_id, task_id, principal, command_key):
            identifier(value)
        self._owner(run_id, principal)
        payload = encoded([run_id, task_id])
        with self._transaction() as db:
            prior = db.execute(
                "SELECT payload,result FROM commands WHERE principal=? AND key=?",
                (principal, command_key),
            ).fetchone()
            if prior:
                if prior["payload"] != payload:
                    raise RunError("IDEMPOTENCY_CONFLICT")
                return dict(json.loads(prior["result"]))
            for row in db.execute(
                "SELECT data FROM operations WHERE run_id=? AND task_id=? AND state='reserved'",
                (run_id, task_id),
            ).fetchall():
                self._refresh(db, dict(json.loads(row["data"])))
            if db.execute(
                "SELECT 1 FROM operations WHERE run_id=? AND task_id=? "
                "AND state IN ('queued','reserved','cancellation_pending',"
                "'reconciliation_required','execution_pending','executing','execution_unknown')",
                (run_id, task_id),
            ).fetchone():
                raise RunError("TASK_ADMISSION_PENDING")
            identity = str(uuid.uuid4())
            assessment = self.routing.assess(
                run_id, task_id, principal=principal, command_key="admission-assess:" + identity
            )
            request = _request(assessment)
            operation = {
                "schema_version": "karajan.approved-task-admission.v1",
                "id": identity,
                "run_id": run_id,
                "task_id": task_id,
                "planned_attempt_id": assessment["planned_attempt_id"],
                "planned_context_id": assessment["planned_context_id"],
                "state": "queued" if request else "blocked",
                "reason_codes": assessment["reason_codes"],
                "assessment": assessment,
                "request": request,
                "capacity_receipt": None,
                "revalidation": None,
                "cancel_requested": False,
                "cancellation_receipt": None,
                "activation_allowed": False,
                "dispatch_enabled": False,
            }
            self._save(db, operation)
            db.execute(
                "INSERT INTO commands VALUES (?,?,?,?)",
                (principal, command_key, payload, encoded(operation)),
            )
            return operation

    def get(self, run_id: str, operation_id: str, *, principal: str) -> dict[str, Any]:
        for value in (run_id, operation_id, principal):
            identifier(value)
        self._owner(run_id, principal)
        with self._transaction() as db:
            return self._refresh(db, self._load(db, run_id, operation_id))

    def advance(self, run_id: str, operation_id: str, *, principal: str) -> dict[str, Any]:
        for value in (run_id, operation_id, principal):
            identifier(value)
        self._owner(run_id, principal)
        with self._transaction() as db:
            operation = self._refresh(db, self._load(db, run_id, operation_id))
            if operation["state"] != "queued" or operation["cancel_requested"]:
                return operation
            key = "task-admit:" + operation_id
            request = operation["request"]
            # A lost response is recovered without re-authorizing or re-sending.
            receipt = self.routing.capacity.command_receipt("admit", request, command_key=key)
            if receipt is None:
                with self.routing.admission_guard(
                    run_id,
                    operation["task_id"],
                    principal=principal,
                    attempt_id=operation["planned_attempt_id"],
                    context_id=operation["planned_context_id"],
                ) as current:
                    operation["revalidation"] = current
                    current_request = _request(current)
                    if current_request is None or current_request != request:
                        operation["state"] = "blocked"
                        operation["reason_codes"] = current["reason_codes"] or [
                            "APPROVED_ADMISSION_INPUT_CHANGED"
                        ]
                        self._save(db, operation)
                        return operation
                    receipt = self.routing.capacity.admit(request, command_key=key)
            operation["capacity_receipt"] = receipt
            operation["state"] = "reserved" if receipt["decision"] == "admitted" else "blocked"
            operation["reason_codes"] = receipt["reason_codes"]
            self._refresh(db, operation)
            self._save(db, operation)
            return operation

    def cancel(self, run_id: str, operation_id: str, *, principal: str) -> dict[str, Any]:
        for value in (run_id, operation_id, principal):
            identifier(value)
        self._owner(run_id, principal)
        # This intent must survive a Capacity commit followed by a lost response.
        with self._transaction() as db:
            operation = self._load(db, run_id, operation_id)
            if operation["state"] == "cancelled":
                return operation
            operation["cancel_requested"] = True
            operation["state"] = "cancellation_pending"
            if "execution" in operation:
                # The execution controller must stop its owned grant and Host.
                # Unactivated-only cancellation cannot prove those effects ended.
                operation["execution"]["cancel_requested"] = True
                self._save(db, operation)
                return operation
            self._save(db, operation)
        with self._transaction() as db:
            operation = self._load(db, run_id, operation_id)
            if operation["state"] == "cancelled":
                return operation
            request = operation["request"]
            receipt = (
                self.routing.capacity.command_receipt(
                    "admit", request, command_key="task-admit:" + operation_id
                )
                if request
                else None
            )
            if receipt and receipt["decision"] == "admitted":
                operation["capacity_receipt"] = receipt
                cancellation = {
                    "admission_id": receipt["admission_id"],
                    "evidence_ref": "task-cancel-intent:" + operation_id,
                }
                key = "task-cancel:" + operation_id
                recorded = self.routing.capacity.command_receipt(
                    "cancel_unactivated", cancellation, command_key=key
                )
                try:
                    recorded = recorded or self.routing.capacity.cancel_unactivated(
                        **cancellation, command_key=key
                    )
                except CapacityError as error:
                    # An independently activated/unknown reservation needs real
                    # execution reconciliation; cancellation cannot invent it.
                    operation["state"] = "reconciliation_required"
                    operation["reason_codes"] = [str(error)]
                    self._save(db, operation)
                    return operation
                operation["cancellation_receipt"] = recorded
            operation["state"] = "cancelled"
            operation["reason_codes"] = []
            self._save(db, operation)
            return operation


def _request(assessment: dict[str, Any]) -> dict[str, Any] | None:
    if assessment["state"] != "selected":
        return None
    route = assessment["route"]
    profile = route["selected_profile"]
    expectation = next(e for e in assessment["admission_expectations"] if e["profile"] == profile)
    estimate = next(
        e for e in route["snapshots"]["capacity"]["estimates"] if e["profile"] == profile
    )
    rulebook = route["snapshots"]["policy"]["rulebook"]
    task = route["snapshots"]["task"]
    return {
        "attempt_id": assessment["planned_attempt_id"],
        "run_id": assessment["run_id"],
        "profile_id": profile["id"],
        "profile_revision": profile["revision"],
        "role": task["role"],
        "purpose": task["purpose"],
        "authorization_ref": digest(assessment["sources"]["approval"]),
        "rulebook_revision": digest(rulebook),
        "duration_seconds": task["duration_seconds"],
        "demand": {d["pool_id"]: d["amount"] for d in estimate["demand"]},
        "expected_capacity": expectation["expected_capacity"],
    }
