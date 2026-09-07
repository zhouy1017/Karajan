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

from .execution_budget import admission_allowed, current_process
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
        # Installed after ApprovedReviewerBindings is constructed.  Keeping this
        # controller-owned avoids a public Reviewer payload or a second source
        # of Candidate authority.
        self.reviewer_bindings: Any | None = None
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

    def set_reviewer_bindings(self, bindings: Any) -> None:
        """Attach the current-subject authority after the cyclic services exist."""
        if getattr(bindings, "admissions", None) is not self:
            raise RunError("REVIEW_BINDING_ADMISSION_MISMATCH")
        self.reviewer_bindings = bindings

    def _reviewer_context(
        self, db: sqlite3.Connection, run_id: str, task_id: str, principal: str
    ) -> dict[str, Any] | None:
        """Locate the one persisted Worker operation for an approved Reviewer."""
        with self.routing.planner.activation_guard(run_id) as run:
            self.routing.planner._owner(run, principal)
            plan = next(
                (
                    row
                    for row in run["plans"]
                    if row["plan_revision"] == run["active_plan_revision"]
                ),
                None,
            )
            task = (
                next((row for row in plan["plan"]["tasks"] if row["id"] == task_id), None)
                if plan is not None
                else None
            )
            if task is None or task["role"] != "reviewer":
                return None
            bindings = self.reviewer_bindings
            if bindings is None:
                # Preserve the established public admission result for an
                # approved reviewer-shaped task before #115's controller is
                # installed: the generic assessor records its immutable
                # EXECUTION_LINEAGE_REQUIRED blocker and never reserves.
                return None
            dependencies = task.get("depends_on")
            if not isinstance(dependencies, list) or len(dependencies) != 1:
                raise RunError("UNIQUE_APPROVED_REVIEWER_DEPENDENCY_REQUIRED")
            rows = [
                dict(json.loads(row["data"]))
                for row in db.execute(
                    "SELECT data FROM operations WHERE run_id=? AND task_id=?",
                    (run_id, dependencies[0]),
                ).fetchall()
            ]
            rows = [
                row
                for row in rows
                if row.get("workspace") is not None
                and row.get("execution", {}).get("collection") is not None
                and not row.get("cancel_requested")
            ]
            if len(rows) != 1:
                raise RunError("REVIEW_WORKER_LINEAGE_REQUIRED")
            return rows[0]

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

    def _reviewer_activation_identity(
        self, db: sqlite3.Connection, operation: dict[str, Any]
    ) -> None:
        """Require the persisted Reviewer lineage before making an activation intent."""
        request = operation.get("request")
        assessment = operation.get("assessment")
        lineage = assessment.get("reviewer_lineage") if isinstance(assessment, dict) else None
        worker_operation_id = operation.get("depends_on_operation_id")
        if (
            not isinstance(request, dict)
            or request.get("role") != "reviewer"
            or not isinstance(lineage, dict)
            or not isinstance(worker_operation_id, str)
            or lineage.get("worker_operation_id") != worker_operation_id
        ):
            raise RunError("REVIEWER_OPERATION_REQUIRED")
        worker = self._load(db, operation["run_id"], worker_operation_id)
        if (
            worker["id"] == operation["id"]
            or worker.get("task_id") != lineage.get("worker_task_id")
        ):
            raise RunError("REVIEWER_OPERATION_REQUIRED")

    def _refresh(self, db: sqlite3.Connection, operation: dict[str, Any]) -> dict[str, Any]:
        if "execution" in operation or operation["state"] != "reserved":
            return operation
        activation = operation.get("reviewer_activation")
        if isinstance(activation, dict):
            receipt = self.routing.capacity.command_receipt(
                "activate",
                {"admission_id": activation["admission_id"]},
                command_key=activation["command_key"],
            )
            if receipt is not None:
                activation["receipt"] = receipt
                if receipt.get("decision") == "capacity_revalidated":
                    operation["reason_codes"] = []
                    self._save(db, operation)
                    return operation
                # Keep the immutable rejected receipt, then derive the current
                # reservation state below. A past rejection cannot mask expiry.
                operation["reason_codes"] = list(receipt.get("reason_codes", []))
                self._save(db, operation)
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
            reviewer_worker = self._reviewer_context(db, run_id, task_id, principal)
            if reviewer_worker is None:
                assessment = self.routing.assess(
                    run_id, task_id, principal=principal, command_key="admission-assess:" + identity
                )
            else:
                bindings = self.reviewer_bindings
                if bindings is None:
                    raise RunError("REVIEWER_BINDING_CONTROLLER_REQUIRED")
                assessment = self.routing.assess_reviewer(
                    run_id,
                    task_id,
                    principal=principal,
                    command_key="admission-assess:" + identity,
                    worker_operation=reviewer_worker,
                    candidates=bindings.candidates,
                    reviewer_validator=bindings,
                )
            request = _request(assessment)
            operation = {
                "schema_version": "karajan.approved-task-admission.v1",
                "id": identity,
                "run_id": run_id,
                "task_id": task_id,
                "depends_on_operation_id": assessment.get("reviewer_lineage", {}).get(
                    "worker_operation_id"
                ),
                "planned_attempt_id": assessment["planned_attempt_id"],
                "planned_context_id": assessment["planned_context_id"],
                "state": "queued" if request else "blocked",
                "reason_codes": assessment["reason_codes"],
                "assessment": assessment,
                "request": request,
                "capacity_receipt": None,
                "reviewer_activation": (
                    None if assessment.get("reviewer_lineage") is not None else "not_applicable"
                ),
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
                reviewer_worker = self._reviewer_context(
                    db, run_id, operation["task_id"], principal
                )
                bindings = self.reviewer_bindings
                if reviewer_worker is not None:
                    if bindings is None:
                        raise RunError("REVIEWER_BINDING_CONTROLLER_REQUIRED")
                    # Keep the original Run lock while Project facts and the
                    # Capacity transaction are acquired.  The callback runs
                    # after Capacity has its actual write lock, so time spent
                    # waiting for either lock cannot admit past this Run's
                    # existing cumulative deadline or mint a new claim.
                    with self.routing.planner.activation_guard(run_id) as run:
                        self.routing.planner._owner(run, principal)
                        with self.routing.reviewer_admission_guard(
                            run_id,
                            operation["task_id"],
                            principal=principal,
                            attempt_id=operation["planned_attempt_id"],
                            context_id=operation["planned_context_id"],
                            worker_operation=reviewer_worker,
                            candidates=bindings.candidates,
                            reviewer_validator=bindings,
                            _held_run=run,
                        ) as current:
                            operation["revalidation"] = current
                            current_request = _request(current)
                            provenance_changed = (
                                current["state"] == "selected"
                                and operation["assessment"].get("reviewer_lineage")
                                != current.get("reviewer_lineage")
                            )
                            if (
                                current_request is None
                                or current_request != request
                                or provenance_changed
                            ):
                                operation["state"] = "blocked"
                                operation["reason_codes"] = (
                                    ["REVIEWER_ADMISSION_PROVENANCE_CHANGED"]
                                    if provenance_changed
                                    else current["reason_codes"]
                                    or ["APPROVED_ADMISSION_INPUT_CHANGED"]
                                )
                                self._save(db, operation)
                                return operation

                            def check_budget_at_reservation() -> None:
                                self.routing.reviewer_boundary_guard(
                                    current,
                                    worker_operation=reviewer_worker,
                                    candidates=bindings.candidates,
                                    clock=lambda: self.routing.capacity.clock(),
                                )
                                admission_allowed(db, run, now=self.routing.planner.clock())

                            capacity_boundary: Any | None = None

                            def check_reviewer_capacity_route(boundary: Any) -> None:
                                nonlocal capacity_boundary
                                capacity_boundary = boundary
                                self.routing.reviewer_capacity_boundary_guard(
                                    current, request=request, boundary=boundary
                                )

                            def check_reviewer_final_reservation_boundary() -> None:
                                if capacity_boundary is None:
                                    raise RunError("REVIEWER_CAPACITY_BOUNDARY_INVALID")
                                self.routing.reviewer_elapsed_boundary_guard(
                                    current, clock=lambda: self.routing.capacity.clock()
                                )
                                self.routing.reviewer_capacity_boundary_guard(
                                    current,
                                    request=request,
                                    boundary=capacity_boundary,
                                    as_of=self.routing.capacity.clock(),
                                )
                                admission_allowed(db, run, now=self.routing.planner.clock())

                            try:
                                receipt = self.routing.capacity.admit(
                                    request,
                                    command_key=key,
                                    before_reserve=check_budget_at_reservation,
                                    after_capacity_facts=check_reviewer_capacity_route,
                                    before_reservation_write=check_reviewer_final_reservation_boundary,
                                )
                            except RunError as error:
                                operation["state"] = "blocked"
                                operation["reason_codes"] = [error.code]
                                self._save(db, operation)
                                return operation
                else:
                    guard = self.routing.admission_guard(
                        run_id,
                        operation["task_id"],
                        principal=principal,
                        attempt_id=operation["planned_attempt_id"],
                        context_id=operation["planned_context_id"],
                    )
                    with guard as current:
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

    def activate_reviewer(
        self, run_id: str, operation_id: str, *, principal: str
    ) -> dict[str, Any]:
        """Record and recover the one Reviewer Capacity activation by operation ID.

        A lost response leaves the durable intent in place. Later ``get`` and
        ``reconcile_reviewer`` only read this exact command receipt; they never
        nominate an activation key or issue a replacement Capacity effect.
        """
        for value in (run_id, operation_id, principal):
            identifier(value)
        self._owner(run_id, principal)
        with self._transaction() as db:
            operation = self._load(db, run_id, operation_id)
            self._reviewer_activation_identity(db, operation)
            operation = self._refresh(db, operation)
            if operation.get("cancel_requested"):
                raise RunError("REVIEWER_OPERATION_CANCELLED")
            if operation.get("state") != "reserved" or not operation.get("capacity_receipt"):
                raise RunError("RESERVED_REVIEWER_OPERATION_REQUIRED")
            activation = operation.get("reviewer_activation")
            if activation is None:
                activation = {
                    "admission_id": operation["capacity_receipt"]["admission_id"],
                    "command_key": "reviewer-activate:" + operation_id,
                    "receipt": None,
                }
                operation["reviewer_activation"] = activation
                self._save(db, operation)
            receipt = self.routing.capacity.command_receipt(
                "activate",
                {"admission_id": activation["admission_id"]},
                command_key=activation["command_key"],
            )
            if receipt is not None:
                activation["receipt"] = receipt
                self._save(db, operation)
                return self._refresh(db, operation)
        # The intent is already durable. A retry uses this same key and
        # Capacity's idempotent receipt; it cannot create another activation.
        receipt = self.routing.capacity.activate(
            activation["admission_id"], command_key=activation["command_key"]
        )
        with self._transaction() as db:
            operation = self._load(db, run_id, operation_id)
            current = operation.get("reviewer_activation")
            if current != activation:
                raise RunError("REVIEWER_ACTIVATION_INTENT_CHANGED")
            current["receipt"] = receipt
            self._save(db, operation)
            return self._refresh(db, operation)

    def reconcile_reviewer(
        self, run_id: str, operation_id: str, *, principal: str
    ) -> dict[str, Any]:
        """Read the persisted Reviewer activation receipt without an effect."""
        return self.get(run_id, operation_id, principal=principal)

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

    @contextmanager
    def reviewer_reserved_effect_guard(
        self, run_id: str, operation_id: str, *, principal: str
    ) -> Iterator[dict[str, Any]]:
        """Fence a future Reviewer effect to its original operation and Capacity hold.

        This exposes no native start or send capability.  A #116 consumer uses
        this ID-only port at its real boundary; it receives the Reviewer record
        only after the stored Worker lineage, current binding, exact request,
        cancellation state, Project facts, and already-active Capacity hold all
        agree.  The guard never creates or activates a reservation.
        """
        for value in (run_id, operation_id, principal):
            identifier(value)
        self._owner(run_id, principal)
        with self._transaction() as db:
            operation = self._refresh(db, self._load(db, run_id, operation_id))
            if operation.get("cancel_requested"):
                raise RunError("REVIEWER_OPERATION_CANCELLED")
            worker_operation_id = operation.get("depends_on_operation_id")
            if not isinstance(worker_operation_id, str):
                raise RunError("REVIEW_WORKER_LINEAGE_REQUIRED")
            worker_operation = self._load(db, run_id, worker_operation_id)
            bindings = self.reviewer_bindings
            if bindings is None:
                raise RunError("REVIEWER_BINDING_CONTROLLER_REQUIRED")
            request = operation.get("request")
            capacity_receipt = operation.get("capacity_receipt")
            activation = operation.get("reviewer_activation")
            if (
                operation.get("state") != "reserved"
                or not isinstance(request, dict)
                or not isinstance(capacity_receipt, dict)
                or not isinstance(capacity_receipt.get("admission_id"), str)
                or not isinstance(activation, dict)
                or not isinstance(activation.get("receipt"), dict)
                or activation["receipt"].get("decision") != "capacity_revalidated"
                or activation["receipt"].get("admission_id") != capacity_receipt["admission_id"]
            ):
                raise RunError("RESERVED_REVIEWER_OPERATION_REQUIRED")
            with self.routing.planner.activation_guard(run_id) as run:
                self.routing.planner._owner(run, principal)
                with self.routing._reviewer_reserved_execution_guard(
                    run_id,
                    operation,
                    worker_operation,
                    principal=principal,
                    candidates=bindings.candidates,
                    reviewer_validator=bindings,
                    _held_run=run,
                ) as current:
                    if (
                        current["state"] != "selected"
                        or current["reviewer_operation_id"] != operation_id
                        or current["original_assessment_digest"]
                        != operation["assessment"]["digest"]
                        or current["route"]["selected_profile"]
                        != operation["assessment"]["route"]["selected_profile"]
                        or current["planned_attempt_id"] != operation["planned_attempt_id"]
                        or current["planned_context_id"] != operation["planned_context_id"]
                        or _request(current) != request
                    ):
                        raise RunError("REVIEWER_RESERVED_ROUTE_NOT_CURRENT")
                    # This is the existing Reviewer admission's Capacity transaction;
                    # it excludes only its own hold and cannot issue another claim.
                    def check_reviewer_effect_boundary() -> None:
                        self.routing.reviewer_boundary_guard(
                            current,
                            worker_operation=worker_operation,
                            candidates=bindings.candidates,
                            clock=lambda: self.routing.capacity.clock(),
                        )

                    capacity_boundary: Any | None = None

                    def check_reviewer_capacity_route(boundary: Any) -> None:
                        nonlocal capacity_boundary
                        capacity_boundary = boundary
                        self.routing.reviewer_capacity_boundary_guard(
                            current, request=request, boundary=boundary
                        )

                    def check_reviewer_final_effect_boundary() -> None:
                        if capacity_boundary is None:
                            raise RunError("REVIEWER_CAPACITY_BOUNDARY_INVALID")
                        self.routing.reviewer_elapsed_boundary_guard(
                            current, clock=lambda: self.routing.capacity.clock()
                        )
                        self.routing.reviewer_capacity_boundary_guard(
                            current,
                            request=request,
                            boundary=capacity_boundary,
                            as_of=self.routing.capacity.clock(),
                        )
                        now = self.routing.planner.clock()
                        try:
                            current_process(
                                db,
                                run,
                                operation,
                                attempt_id=operation["planned_attempt_id"],
                                now=now,
                            )
                        except RunError as error:
                            if error.code != "RUN_EXECUTION_CLAIM_REQUIRED":
                                raise
                            # #115 does not claim a native Reviewer process.
                            # Before that later #116 claim exists, this is a
                            # fresh-admission check; after it exists, the
                            # exact original process remains valid even at the
                            # final legal Run slot.
                            admission_allowed(db, run, now=now)

                    with self.routing.capacity.pre_effect_guard(
                        capacity_receipt["admission_id"],
                        expected_request=request,
                        before_effect=check_reviewer_effect_boundary,
                        after_capacity_facts=check_reviewer_capacity_route,
                        before_effect_yield=check_reviewer_final_effect_boundary,
                    ) as capacity:
                        now = self.routing.planner.clock()
                        try:
                            current_process(
                                db,
                                run,
                                operation,
                                attempt_id=operation["planned_attempt_id"],
                                now=now,
                            )
                        except RunError as error:
                            if error.code != "RUN_EXECUTION_CLAIM_REQUIRED":
                                raise
                            # #115 has not claimed a native Reviewer process;
                            # it can only prove that a future claim still fits.
                            admission_allowed(db, run, now=now)
                        yield {
                            "operation": operation,
                            "revalidation": current,
                            "capacity": capacity,
                        }


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
