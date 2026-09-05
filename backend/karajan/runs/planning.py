"""Durable user-approved plan revisions, not a second scheduler or budget ledger."""

import builtins
import hashlib
import json
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter, ValidationError

from karajan.contracts.probe import Contract, Identifier
from karajan.projects import ProjectRegistry

from .models import (
    ApprovePlan,
    CreateRun,
    DecideHandoff,
    PlanningReceipt,
    ProposeHandoff,
    SubmitPlan,
)
from .validation import plan_impact, validate_creation, validate_plan


class RunError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def encoded(value: object) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise RunError("INPUT_NOT_JSON") from None


def digest(value: object) -> str:
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def parse(model: type[Contract], request: dict[str, Any]) -> dict[str, Any]:
    try:
        return model.model_validate(request).model_dump()
    except ValidationError:
        raise RunError("PLANNING_INPUT_INVALID") from None


def identifier(value: str) -> None:
    try:
        TypeAdapter(Identifier).validate_python(value, strict=True)
        if not value.isprintable():
            raise ValueError("unprintable")
    except (ValidationError, ValueError):
        raise RunError("COMMAND_IDENTITY_INVALID") from None


class RunPlanner:
    def __init__(
        self,
        database: Path,
        projects: ProjectRegistry,
        *,
        admissions: Callable[[str], dict[str, Any]] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.database = database
        self.projects = projects
        self.clock = clock
        self.admissions = admissions
        with self._transaction() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, snapshot TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS run_commands (principal TEXT NOT NULL, "
                "key TEXT NOT NULL, digest TEXT NOT NULL, result TEXT, error TEXT, "
                "PRIMARY KEY(principal, key))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS run_events (sequence INTEGER PRIMARY KEY, "
                "run_id TEXT, kind TEXT NOT NULL, principal TEXT NOT NULL, "
                "command_key TEXT NOT NULL, at REAL NOT NULL, result TEXT NOT NULL)"
            )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.database, timeout=10, isolation_level=None)
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

    def create(
        self, request: dict[str, Any], *, command_key: str, principal: str
    ) -> dict[str, Any]:
        try:
            request = CreateRun.model_validate(request).model_dump()
        except ValidationError:
            raise RunError("RUN_INPUT_INVALID") from None
        identity = digest(["create", request])
        with self._transaction() as db:
            previous = self._replay(db, principal, command_key, identity)
            if previous is not None:
                return previous
        project = self.projects.get(request["project_id"])
        exported = self.projects.get_configuration(request["project_id"])
        if (
            project["revision"] != request["project_revision"]
            or exported["project_revision"] != project["revision"]
            or project["configuration"]["digest"] != request["configuration_digest"]
            or digest(exported["configuration"]) != request["configuration_digest"]
        ):
            raise RunError("PROJECT_SNAPSHOT_CHANGED")
        if project["configuration"]["status"] != "offline_valid":
            raise RunError("CONFIGURATION_NOT_READY")
        leads = [item for item in request["participants"] if item["purpose"] == "lead"]
        if len(leads) != 1:
            raise RunError("ONE_COMMANDER_REQUIRED")
        try:
            validate_creation(request, project, exported["configuration"], principal)
        except ValueError as rejected:
            raise RunError(str(rejected)) from None
        snapshot = {
            "schema_version": "karajan.run-planning.v1",
            "id": str(uuid.uuid4()),
            "revision": 1,
            "owner": principal,
            "project_id": project["id"],
            "requirement": request["requirement"],
            "state": "planning",
            "dispatch_enabled": False,
            "live_qualification": "not_run",
            "configuration_snapshot": {
                "project_revision": project["revision"],
                "revision": exported["configuration_revision"],
                "digest": request["configuration_digest"],
                "configuration": exported["configuration"],
                "repository": project["repository"],
                "target_branch": project["target_branch"],
            },
            "participants": request["participants"],
            "authorization_ceiling": request["authorization"],
            "commander": {"term": 1, **leads[0]},
            "plans": [],
            "approvals": [],
            "handoffs": [],
            "planning_intents": [],
            "active_plan_revision": None,
            "latest_plan_revision": 0,
        }

        def insert(db: sqlite3.Connection) -> dict[str, Any]:
            db.execute("INSERT INTO runs VALUES (?, ?)", (snapshot["id"], encoded(snapshot)))
            return snapshot

        return self._command("create", request, principal, command_key, insert)

    def _replay(
        self, db: sqlite3.Connection, principal: str, key: str, identity: str
    ) -> dict[str, Any] | None:
        try:
            for value in (principal, key):
                TypeAdapter(Identifier).validate_python(value, strict=True)
                if not value.isprintable():
                    raise ValueError("unprintable")
        except (ValidationError, ValueError):
            raise RunError("COMMAND_IDENTITY_INVALID") from None
        row = db.execute(
            "SELECT * FROM run_commands WHERE principal=? AND key=?", (principal, key)
        ).fetchone()
        if row is None:
            return None
        if row["digest"] != identity:
            raise RunError("IDEMPOTENCY_CONFLICT")
        if row["error"]:
            raise RunError(row["error"])
        result: dict[str, Any] = json.loads(row["result"])
        return result

    def _command(
        self,
        kind: str,
        request: dict[str, Any],
        principal: str,
        key: str,
        operation: Callable[[sqlite3.Connection], dict[str, Any]],
    ) -> dict[str, Any]:
        if "run_id" in request:
            identifier(request["run_id"])
        identity = digest([kind, request])
        error: str | None = None
        result: dict[str, Any] = {}
        with self._transaction() as db:
            previous = self._replay(db, principal, key, identity)
            if previous is not None:
                return previous
            db.execute("SAVEPOINT mutation")
            try:
                result = operation(db)
            except RunError as rejected:
                db.execute("ROLLBACK TO mutation")
                error = rejected.code
            db.execute("RELEASE mutation")
            db.execute(
                "INSERT INTO run_commands VALUES (?, ?, ?, ?, ?)",
                (principal, key, identity, encoded(result) if error is None else None, error),
            )
            db.execute(
                "INSERT INTO run_events(run_id, kind, principal, command_key, at, result) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    request.get("run_id", result.get("id")),
                    kind,
                    principal,
                    key,
                    self.clock(),
                    encoded({"status": "rejected" if error else "accepted", "reason": error}),
                ),
            )
        if error:
            raise RunError(error)
        return result

    def get(self, run_id: str, *, principal: str | None = None) -> dict[str, Any]:
        identifier(run_id)
        if principal is not None:
            identifier(principal)
        with self._transaction() as db:
            run = self._get(db, run_id)
            if principal is not None and run["owner"] != principal:
                raise RunError("RUN_NOT_FOUND")
            return run

    def list(self, *, principal: str, project_id: str | None = None) -> list[dict[str, Any]]:
        identifier(principal)
        if project_id is not None:
            identifier(project_id)
        with self._transaction() as db:
            snapshots = [
                json.loads(row["snapshot"])
                for row in db.execute("SELECT snapshot FROM runs ORDER BY rowid")
            ]
        return [
            item
            for item in snapshots
            if item["owner"] == principal
            and (project_id is None or item["project_id"] == project_id)
        ]

    def events(self, run_id: str, *, principal: str) -> builtins.list[dict[str, Any]]:
        self.get(run_id, principal=principal)
        with self._transaction() as db:
            return [
                {**dict(row), "result": json.loads(row["result"])}
                for row in db.execute(
                    "SELECT e.*, c.digest AS request_digest FROM run_events e "
                    "JOIN run_commands c ON e.principal=c.principal AND e.command_key=c.key "
                    "WHERE e.run_id=? ORDER BY e.sequence",
                    (run_id,),
                )
            ]

    def _save(self, db: sqlite3.Connection, run: dict[str, Any]) -> None:
        run["revision"] += 1
        db.execute("UPDATE runs SET snapshot=? WHERE id=?", (encoded(run), run["id"]))

    def planning_intent(
        self, run_id: str, *, term: int, command_key: str, principal: str
    ) -> dict[str, Any]:
        request = {"run_id": run_id, "term": term}

        def apply(db: sqlite3.Connection) -> dict[str, Any]:
            run = self._get(db, run_id)
            self._term(run, term)
            participant = next(
                (item for item in run["participants"] if item["principal"] == principal), None
            )
            if participant is None or (
                principal != run["commander"]["principal"] and participant["purpose"] != "advice"
            ):
                raise RunError("PLANNING_ACTOR_NOT_ACTIVE")
            configuration = run["configuration_snapshot"]["configuration"]
            intent = {
                "id": str(uuid.uuid4()),
                "run_id": run_id,
                "term": term,
                "principal": principal,
                "profile": participant["profile"],
                "budget_ref": configuration["rulebook"]["resource_policy"]["planning_budget_ref"],
                "permissions": ["read"],
                "state": "awaiting_receipt",
                "receipt": None,
                "dispatch_enabled": False,
                "created_at": self.clock(),
            }
            run["planning_intents"].append(intent)
            self._save(db, run)
            return intent

        return self._command("planning_intent", request, principal, command_key, apply)

    def attach_planning_receipt(
        self,
        run_id: str,
        intent_id: str,
        *,
        receipt_ref: str,
        command_key: str,
        principal: str,
    ) -> dict[str, Any]:
        request = {"run_id": run_id, "intent_id": intent_id, "receipt_ref": receipt_ref}
        with self._transaction() as db:
            previous = self._replay(db, principal, command_key, digest(["attach_receipt", request]))
            if previous is not None:
                return previous
        if self.admissions is None:
            raise RunError("ADMISSION_AUTHORITY_UNAVAILABLE")
        try:
            receipt = PlanningReceipt.model_validate(self.admissions(receipt_ref)).model_dump()
        except (ValidationError, KeyError, ValueError):
            raise RunError("ADMISSION_RECEIPT_INVALID") from None

        def apply(db: sqlite3.Connection) -> dict[str, Any]:
            run = self._get(db, run_id)
            self._owner(run, principal)
            intent: dict[str, Any] | None = next(
                (item for item in run["planning_intents"] if item["id"] == intent_id), None
            )
            if intent is None:
                raise RunError("PLANNING_INTENT_NOT_FOUND")
            self._term(run, intent["term"])
            if (
                receipt["receipt_ref"] != receipt_ref
                or any(
                    receipt[key] != intent[key]
                    for key in ("run_id", "term", "principal", "profile", "budget_ref")
                )
                or receipt["intent_id"] != intent_id
            ):
                raise RunError("ADMISSION_BINDING_MISMATCH")
            if intent["receipt"] is not None:
                raise RunError("ADMISSION_ALREADY_ATTACHED")
            intent["receipt"] = receipt
            intent["state"] = receipt["state"]
            self._save(db, run)
            return intent

        return self._command("attach_receipt", request, principal, command_key, apply)

    def _owner(self, run: dict[str, Any], principal: str) -> None:
        if principal != run["owner"]:
            raise RunError("USER_DECISION_REQUIRED")

    def submit_plan(
        self, run_id: str, request: dict[str, Any], *, command_key: str, principal: str
    ) -> dict[str, Any]:
        request = {"run_id": run_id, **parse(SubmitPlan, request)}

        def apply(db: sqlite3.Connection) -> dict[str, Any]:
            run = self._get(db, run_id)
            self._term(run, request["term"])
            if principal != run["commander"]["principal"]:
                raise RunError("ONLY_CURRENT_COMMANDER_CAN_SUBMIT")
            intent = next(
                (item for item in run["planning_intents"] if item["id"] == request["intent_id"]),
                None,
            )
            if (
                intent is None
                or intent["principal"] != principal
                or intent["term"] != request["term"]
            ):
                raise RunError("PLANNING_INTENT_MISMATCH")
            if intent["state"] != "admitted":
                raise RunError("PLANNING_ADMISSION_REQUIRED")
            if request["expected_plan_revision"] != run["latest_plan_revision"]:
                raise RunError("PLAN_REVISION_STALE")
            try:
                validate_plan(request["plan"], run["authorization_ceiling"])
                impact = plan_impact(request["plan"], run)
            except ValueError as rejected:
                raise RunError(str(rejected)) from None
            configuration = run["configuration_snapshot"]["digest"]
            result = {
                "plan_revision": run["latest_plan_revision"] + 1,
                "term": request["term"],
                "submitted_by": principal,
                "intent_id": intent["id"],
                "plan": request["plan"],
                "configuration_digest": configuration,
                "authorization_digest": digest([configuration, request["plan"]["authorization"]]),
                "provenance": intent["receipt"]["provenance"],
                "impact": impact,
            }
            result["plan_digest"] = digest(result)
            run["plans"].append(result)
            run["latest_plan_revision"] = result["plan_revision"]
            if run["active_plan_revision"] is None:
                run["state"] = "awaiting_approval"
            self._save(db, run)
            return result

        return self._command("submit_plan", request, principal, command_key, apply)

    def approve_plan(
        self, run_id: str, request: dict[str, Any], *, command_key: str, principal: str
    ) -> dict[str, Any]:
        request = {"run_id": run_id, **parse(ApprovePlan, request)}

        def apply(db: sqlite3.Connection) -> dict[str, Any]:
            run = self._get(db, run_id)
            self._owner(run, principal)
            self._term(run, request["term"])
            if request["plan_revision"] != run["latest_plan_revision"] or not run["plans"]:
                raise RunError("PLAN_REVISION_STALE")
            plan = run["plans"][-1]
            if (
                any(
                    request[key] != plan[key]
                    for key in (
                        "term",
                        "plan_digest",
                        "authorization_digest",
                        "configuration_digest",
                    )
                )
                or request["configuration_digest"] != run["configuration_snapshot"]["digest"]
            ):
                raise RunError("APPROVAL_BINDING_MISMATCH")
            if run["active_plan_revision"] == plan["plan_revision"]:
                raise RunError("PLAN_ALREADY_APPROVED")
            receipt = {
                **request,
                "id": str(uuid.uuid4()),
                "approved_by": principal,
                "approved_at": self.clock(),
                "dispatch_enabled": False,
            }
            run["approvals"].append(receipt)
            run["active_plan_revision"] = plan["plan_revision"]
            run["state"] = "executing"
            self._save(db, run)
            return receipt

        return self._command("approve_plan", request, principal, command_key, apply)

    def task_gate(self, run_id: str, task_id: str) -> dict[str, Any]:
        """Approval-only view; dependency, resource and runtime gates remain mandatory."""
        run = self.get(run_id)
        plan = next(
            (item for item in run["plans"] if item["plan_revision"] == run["active_plan_revision"]),
            None,
        )
        task = (
            next((item for item in plan["plan"]["tasks"] if item["id"] == task_id), None)
            if plan
            else None
        )
        approved = task is not None and task["readiness"] == "ready"
        return {
            "run_id": run_id,
            "task_id": task_id,
            "plan_revision": run["active_plan_revision"],
            "scope_approved": approved,
            "dispatch_enabled": False,
            "reason_codes": ([] if approved else ["TASK_SCOPE_NOT_APPROVED"])
            + ["LIVE_QUALIFICATION_NOT_RUN", "RESOURCE_AND_DEPENDENCY_GATES_REQUIRED"],
        }

    def _term(self, run: dict[str, Any], term: int) -> None:
        if type(term) is not int or term != run["commander"]["term"]:
            raise RunError("COMMANDER_TERM_STALE")

    def _handoff_binding(self, run: dict[str, Any]) -> dict[str, Any]:
        return {
            "term": run["commander"]["term"],
            "plan_revision": run["latest_plan_revision"],
            "plan_digest": run["plans"][-1]["plan_digest"] if run["plans"] else None,
            "active_plan_revision": run["active_plan_revision"],
            "authorization_digest": run["approvals"][-1]["authorization_digest"]
            if run["approvals"]
            else digest(run["authorization_ceiling"]),
            "configuration_digest": run["configuration_snapshot"]["digest"],
        }

    def propose_handoff(
        self, run_id: str, request: dict[str, Any], *, command_key: str, principal: str
    ) -> dict[str, Any]:
        request = {"run_id": run_id, **parse(ProposeHandoff, request)}

        def apply(db: sqlite3.Connection) -> dict[str, Any]:
            run = self._get(db, run_id)
            self._term(run, request["term"])
            if principal not in {run["owner"], run["commander"]["principal"]}:
                raise RunError("HANDOFF_PROPOSER_INVALID")
            if request["expected_plan_revision"] != run["latest_plan_revision"]:
                raise RunError("PLAN_REVISION_STALE")
            candidate = next(
                (item for item in run["participants"] if item["principal"] == request["candidate"]),
                None,
            )
            if (
                candidate is None
                or candidate["purpose"] == "advice"
                or candidate["principal"] == run["commander"]["principal"]
            ):
                raise RunError("HANDOFF_CANDIDATE_NOT_APPROVED")
            budget_ref = run["configuration_snapshot"]["configuration"]["rulebook"][
                "resource_policy"
            ]["planning_budget_ref"]
            if request["resource_impact"]["budget_ref"] != budget_ref:
                raise RunError("HANDOFF_BUDGET_MISMATCH")
            if not self.clock() < request["expires_at"] <= self.clock() + 86400:
                raise RunError("HANDOFF_EXPIRY_INVALID")
            result = {
                "id": str(uuid.uuid4()),
                "run_id": run_id,
                "candidate": candidate,
                "checkpoint": request["checkpoint"],
                "resource_impact": request["resource_impact"],
                "binding": self._handoff_binding(run),
                "expires_at": request["expires_at"],
                "proposed_by": principal,
            }
            result["digest"] = digest(result)
            result["state"] = "pending"
            for old in run["handoffs"]:
                if old["state"] == "pending":
                    old["state"] = "superseded"
            run["handoffs"].append(result)
            self._save(db, run)
            return result

        return self._command("propose_handoff", request, principal, command_key, apply)

    def decide_handoff(
        self, run_id: str, request: dict[str, Any], *, command_key: str, principal: str
    ) -> dict[str, Any]:
        request = {"run_id": run_id, **parse(DecideHandoff, request)}

        def apply(db: sqlite3.Connection) -> dict[str, Any]:
            run = self._get(db, run_id)
            self._owner(run, principal)
            self._term(run, request["term"])
            handoff: dict[str, Any] | None = next(
                (item for item in run["handoffs"] if item["id"] == request["handoff_id"]), None
            )
            if handoff is None:
                raise RunError("HANDOFF_NOT_FOUND")
            if handoff["state"] != "pending" or handoff["expires_at"] <= self.clock():
                raise RunError("HANDOFF_STALE")
            if handoff["binding"] != self._handoff_binding(run):
                raise RunError("HANDOFF_CHECKPOINT_STALE")
            if request["handoff_digest"] != handoff["digest"]:
                raise RunError("HANDOFF_DIGEST_MISMATCH")
            handoff["state"] = "approved" if request["decision"] == "approve" else "rejected"
            handoff["decided_by"] = principal
            handoff["decided_at"] = self.clock()
            if request["decision"] == "approve":
                run["commander"] = {
                    **handoff["candidate"],
                    "term": request["term"] + 1,
                    "handoff_id": handoff["id"],
                }
            self._save(db, run)
            return handoff

        return self._command("decide_handoff", request, principal, command_key, apply)

    def _get(self, db: sqlite3.Connection, run_id: str) -> dict[str, Any]:
        identifier(run_id)
        row = db.execute("SELECT snapshot FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise RunError("RUN_NOT_FOUND")
        result: dict[str, Any] = json.loads(row["snapshot"])
        return result
