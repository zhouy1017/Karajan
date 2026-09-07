"""Durable, ID-only consumption of a planning admission and model output.

This controller deliberately has no transport, prompt, profile, or artifact-path
entry point.  Those effects belong to a separately qualified planning runtime.
The two authorities below are read-only ports: an unavailable or incomplete port
cannot create a plan.
"""

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import Field, ValidationError

from karajan.capacity import CapacityStore
from karajan.contracts.probe import Contract
from karajan.runs import RunError, RunPlanner
from karajan.runs.planning import digest, encoded, identifier
from karajan.storage import open_database, require_schema


class PlanningAdmissionEvidence(Contract):
    """A sealed, read-only observation of the original planning admission."""

    schema_version: Literal["karajan.planning-admission-evidence.v1"]
    binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authority_kind: Literal["fixture", "production"]
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    budget_ref: str
    capacity_request: dict[str, Any]
    capacity_command_key: str
    capacity_receipt: dict[str, Any] | None
    state: Literal["admitted", "denied", "unknown"]


class PlanningOutputEvidence(Contract):
    """A sealed artifact observed by the trusted planning executor."""

    schema_version: Literal["karajan.planning-output-evidence.v1"]
    execution_id: str
    binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authority_kind: Literal["fixture", "production"]
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    completed: bool
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_size: int = Field(ge=0, le=1_000_000)
    content: bytes


class PlanningOutputSource(Contract):
    """Source identity frozen before an output is consumed."""

    schema_version: Literal["karajan.planning-output-source.v1"]
    binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authority_kind: Literal["fixture", "production"]
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class PlanningAdmissionAuthority(Protocol):
    def read_admission(self, binding: dict[str, Any]) -> object: ...


class PlanningOutputAuthority(Protocol):
    def read_source(self, binding: dict[str, Any]) -> object: ...

    def read_output(self, execution_id: str, binding: dict[str, Any]) -> object: ...


class PlanningExecution:
    """Persist controller-side planning stages without granting runtime authority."""

    def __init__(
        self,
        database: Path,
        planner: RunPlanner,
        *,
        admissions: PlanningAdmissionAuthority | None = None,
        outputs: PlanningOutputAuthority | None = None,
        capacity: CapacityStore | None = None,
        allow_fixture_authorities: bool = False,
        existing_only: bool = False,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if existing_only and not planner.existing_only:
            raise RunError("EXISTING_STORE_PARENT_MODE_REQUIRED")
        self.database = database.resolve()
        self.planner = planner
        self.admissions = admissions
        self.outputs = outputs
        self.capacity = capacity
        self.allow_fixture_authorities = allow_fixture_authorities
        self.existing_only = existing_only
        self.clock = planner.clock if clock is None else clock
        if not existing_only:
            self.database.parent.mkdir(parents=True, exist_ok=True)
        if self.database == planner.database.resolve():
            raise RunError("PLANNING_EXECUTION_DATABASE_MUST_BE_SEPARATE")
        if existing_only:
            require_schema(
                self.database,
                {
                    "executions": ["id", "run_id", "intent_id", "state", "data"],
                    "commands": ["principal", "key", "payload", "result"],
                },
            )
            return
        with self._transaction() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS executions (id TEXT PRIMARY KEY, run_id TEXT NOT NULL, "
                "intent_id TEXT NOT NULL, state TEXT NOT NULL, data TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS commands (principal TEXT NOT NULL, key TEXT NOT NULL, "
                "payload TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(principal,key))"
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

    @staticmethod
    def _load(db: sqlite3.Connection, execution_id: str) -> dict[str, Any]:
        row = db.execute("SELECT data FROM executions WHERE id=?", (execution_id,)).fetchone()
        if row is None:
            raise RunError("PLANNING_EXECUTION_NOT_FOUND")
        return dict(json.loads(row["data"]))

    @staticmethod
    def _save(db: sqlite3.Connection, execution: dict[str, Any]) -> None:
        db.execute(
            "INSERT INTO executions VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE "
            "SET state=excluded.state,data=excluded.data",
            (
                execution["id"],
                execution["run_id"],
                execution["intent_id"],
                execution["state"],
                encoded(execution),
            ),
        )

    @staticmethod
    def _binding(run: dict[str, Any], intent: dict[str, Any], execution_id: str) -> dict[str, Any]:
        participant = run["commander"]
        configuration = run["configuration_snapshot"]
        return {
            "schema_version": "karajan.planning-execution-binding.v1",
            "execution_id": execution_id,
            "run_id": run["id"],
            "owner": run["owner"],
            "project_id": run["project_id"],
            "intent_id": intent["id"],
            "term": participant["term"],
            "principal": participant["principal"],
            "profile": participant["profile"],
            "profile_sha256": digest(participant["profile"]),
            "budget_ref": intent["budget_ref"],
            "requirement_sha256": digest(run["requirement"]),
            "configuration_sha256": configuration["digest"],
            "authorization_ceiling_sha256": digest(run["authorization_ceiling"]),
            "execution_policy_sha256": None
            if "execution_policy_snapshot" not in run
            else run["execution_policy_snapshot"]["digest"],
            "rulebook_sha256": digest(configuration["configuration"]["rulebook"]),
            "attempt_id": "planning:" + execution_id,
            "fence": 1,
        }

    def _owner_run(self, run_id: str, principal: str) -> dict[str, Any]:
        return self.planner.get(run_id, principal=principal)

    @staticmethod
    def _intent(run: dict[str, Any], intent_id: str) -> dict[str, Any]:
        intent = next((row for row in run["planning_intents"] if row["id"] == intent_id), None)
        if intent is None:
            raise RunError("PLANNING_INTENT_NOT_FOUND")
        if (
            intent["principal"] != run["commander"]["principal"]
            or intent["term"] != run["commander"]["term"]
            or intent["profile"] != run["commander"]["profile"]
            or intent["state"] != "awaiting_receipt"
        ):
            raise RunError("PLANNING_EXECUTION_BINDING_STALE")
        return dict(intent)

    def _command(
        self,
        db: sqlite3.Connection,
        principal: str,
        command_key: str,
        payload: object,
        operation: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        request = encoded(payload)
        prior = db.execute(
            "SELECT payload,result FROM commands WHERE principal=? AND key=?",
            (principal, command_key),
        ).fetchone()
        if prior is not None:
            if prior["payload"] != request:
                raise RunError("IDEMPOTENCY_CONFLICT")
            return dict(json.loads(prior["result"]))
        result = operation()
        db.execute(
            "INSERT INTO commands VALUES (?,?,?,?)",
            (principal, command_key, request, encoded(result)),
        )
        return result

    def begin(
        self, run_id: str, intent_id: str, *, principal: str, command_key: str
    ) -> dict[str, Any]:
        for value in (run_id, intent_id, principal, command_key):
            identifier(value)
        # Authenticate the owner before checking the command ledger.  A replay is
        # the original approved command, even after its intent has become admitted.
        run = self._owner_run(run_id, principal)
        replay_payload = ["begin", run_id, intent_id]
        with self._transaction() as db:
            prior = db.execute(
                "SELECT payload,result FROM commands WHERE principal=? AND key=?",
                (principal, command_key),
            ).fetchone()
            if prior is not None:
                if prior["payload"] != encoded(replay_payload):
                    raise RunError("IDEMPOTENCY_CONFLICT")
                return dict(json.loads(prior["result"]))
        intent = self._intent(run, intent_id)
        with self._transaction() as db:
            def create() -> dict[str, Any]:
                if db.execute(
                    "SELECT 1 FROM executions WHERE run_id=? AND intent_id=? "
                    "AND state NOT IN ('submitted')",
                    (run_id, intent_id),
                ).fetchone():
                    raise RunError("PLANNING_EXECUTION_PENDING")
                execution_id = str(uuid.uuid4())
                binding = self._binding(run, intent, execution_id)
                result: dict[str, Any] = {
                    "schema_version": "karajan.planning-execution.v1",
                    "id": execution_id,
                    "run_id": run_id,
                    "intent_id": intent_id,
                    "state": "awaiting_admission",
                    "binding": binding,
                    "binding_sha256": digest(binding),
                    "admission": None,
                    "output": None,
                    "submission": None,
                    "cancel_requested": False,
                    "reason_codes": [],
                    "activation_allowed": False,
                    "dispatch_enabled": False,
                    "created_at": self.clock(),
                }
                self._save(db, result)
                return result

            return self._command(
                db, principal, command_key, replay_payload, create
            )

    def get(self, execution_id: str, *, principal: str) -> dict[str, Any]:
        for value in (execution_id, principal):
            identifier(value)
        with self._transaction() as db:
            execution = self._load(db, execution_id)
        self._owner_run(execution["run_id"], principal)
        return execution

    def cancel(self, execution_id: str, *, principal: str, command_key: str) -> dict[str, Any]:
        for value in (execution_id, principal, command_key):
            identifier(value)
        with self._transaction() as db:
            execution = self._load(db, execution_id)
            self._owner_run(execution["run_id"], principal)

            def cancel() -> dict[str, Any]:
                if execution["submission"] is None and execution["state"] in {
                    "submit_claimed",
                    "submission_unknown",
                }:
                    # A durable claim may already have crossed into the Run store.
                    # Record the request, but never promise that it cancelled a plan.
                    execution["cancel_requested"] = True
                    execution["state"] = "submission_unknown"
                    execution["reason_codes"] = ["PLANNING_EXECUTION_SUBMISSION_UNKNOWN"]
                    self._save(db, execution)
                elif execution["submission"] is None:
                    execution["cancel_requested"] = True
                    execution["state"] = "cancelled"
                    execution["reason_codes"] = ["PLANNING_EXECUTION_CANCELLED"]
                    self._save(db, execution)
                return execution

            return self._command(db, principal, command_key, ["cancel", execution_id], cancel)

    def reconcile(self, execution_id: str, *, principal: str) -> dict[str, Any]:
        """Read only the original authority receipt; never acquire a new admission."""
        execution = self.get(execution_id, principal=principal)
        if execution["state"] not in {"awaiting_admission", "admission_unknown"}:
            return execution
        if self.admissions is None:
            return self._blocked(
                execution_id, principal, "PLANNING_ADMISSION_AUTHORITY_UNAVAILABLE"
            )
        try:
            evidence = PlanningAdmissionEvidence.model_validate(
                self.admissions.read_admission(execution["binding"])
            ).model_dump()
        except (ValidationError, TypeError, ValueError):
            return self._blocked(execution_id, principal, "PLANNING_ADMISSION_EVIDENCE_INVALID")
        if evidence["authority_kind"] == "fixture" and not self.allow_fixture_authorities:
            return self._blocked(execution_id, principal, "PLANNING_FIXTURE_AUTHORITY_FORBIDDEN")
        if evidence["authority_kind"] == "production":
            # This slice deliberately has no production factory.  A caller or
            # test double cannot promote itself by selecting a different tag.
            return self._blocked(
                execution_id, principal, "PLANNING_PRODUCTION_AUTHORITY_UNAVAILABLE"
            )
        if self.capacity is None:
            return self._blocked(execution_id, principal, "PLANNING_CAPACITY_AUTHORITY_UNAVAILABLE")
        if (
            evidence["binding_sha256"] != execution["binding_sha256"]
            or evidence["budget_ref"] != execution["binding"]["budget_ref"]
            or evidence["capacity_receipt"] is None
            or digest(evidence["capacity_request"]) != evidence["source_sha256"]
            or any(
                evidence["capacity_request"].get(key) != execution["binding"][key]
                for key in ("attempt_id", "run_id")
            )
            or evidence["capacity_request"].get("profile_id")
            != execution["binding"]["profile"]["id"]
            or evidence["capacity_request"].get("profile_revision")
            != execution["binding"]["profile"]["revision"]
            or evidence["capacity_request"].get("role") != "commander"
            or evidence["capacity_request"].get("purpose") != "lead"
        ):
            return self._blocked(execution_id, principal, "PLANNING_ADMISSION_BINDING_MISMATCH")
        try:
            receipt = self.capacity.command_receipt(
                "admit",
                evidence["capacity_request"],
                command_key=evidence["capacity_command_key"],
            )
        except ValueError:
            return self._blocked(execution_id, principal, "PLANNING_CAPACITY_RECEIPT_INVALID")
        if (
            receipt is None
            or receipt != evidence["capacity_receipt"]
            or receipt.get("decision") != "admitted"
        ):
            return self._blocked(execution_id, principal, "PLANNING_CAPACITY_RECEIPT_MISMATCH")
        if self.outputs is None:
            return self._blocked(execution_id, principal, "PLANNING_OUTPUT_AUTHORITY_UNAVAILABLE")
        try:
            source = PlanningOutputSource.model_validate(
                self.outputs.read_source(execution["binding"])
            ).model_dump()
        except (ValidationError, TypeError, ValueError):
            return self._blocked(execution_id, principal, "PLANNING_OUTPUT_SOURCE_INVALID")
        if source["authority_kind"] == "fixture" and not self.allow_fixture_authorities:
            return self._blocked(execution_id, principal, "PLANNING_FIXTURE_AUTHORITY_FORBIDDEN")
        if source["authority_kind"] == "production":
            return self._blocked(
                execution_id, principal, "PLANNING_PRODUCTION_AUTHORITY_UNAVAILABLE"
            )
        if source["binding_sha256"] != execution["binding_sha256"]:
            return self._blocked(execution_id, principal, "PLANNING_OUTPUT_SOURCE_BINDING_MISMATCH")
        with self._transaction() as db:
            current = self._load(db, execution_id)
            if current["cancel_requested"]:
                return current
            if current["admission"] not in (None, evidence):
                return self._blocked_locked(db, current, "PLANNING_ADMISSION_EVIDENCE_CHANGED")
            current["admission"] = evidence
            if current.get("output_source_sha256") not in (None, source["source_sha256"]):
                return self._blocked_locked(db, current, "PLANNING_OUTPUT_SOURCE_CHANGED")
            current["output_source_sha256"] = source["source_sha256"]
            current["state"] = (
                "awaiting_output" if evidence["state"] == "admitted" else "admission_unknown"
            )
            current["reason_codes"] = [] if evidence["state"] == "admitted" else [
                "PLANNING_ADMISSION_" + evidence["state"].upper()
            ]
            self._save(db, current)
            return current

    def _blocked(self, execution_id: str, principal: str, reason: str) -> dict[str, Any]:
        with self._transaction() as db:
            execution = self._load(db, execution_id)
            self._owner_run(execution["run_id"], principal)
            return self._blocked_locked(db, execution, reason)

    def _blocked_locked(
        self, db: sqlite3.Connection, execution: dict[str, Any], reason: str
    ) -> dict[str, Any]:
        if execution["submission"] is None and not execution["cancel_requested"]:
            execution["state"] = "blocked"
            execution["reason_codes"] = [reason]
            self._save(db, execution)
        return execution

    def submit(self, execution_id: str, *, principal: str, command_key: str) -> dict[str, Any]:
        """Consume a sealed output, then recover one fixed Run submission by receipt."""
        for value in (execution_id, principal, command_key):
            identifier(value)
        execution = self.reconcile(execution_id, principal=principal)
        if execution["state"] == "awaiting_output":
            execution = self._capture_output(execution, principal, command_key)
        if execution["state"] == "output_captured":
            execution = self._claim_submission(execution_id, principal, command_key)
        if execution["state"] in {"submit_claimed", "submission_unknown"}:
            return self._recover_or_submit(execution_id, principal)
        return execution

    def _capture_output(
        self, execution: dict[str, Any], principal: str, command_key: str
    ) -> dict[str, Any]:
        if self.outputs is None:
            return self._blocked(
                execution["id"], principal, "PLANNING_OUTPUT_AUTHORITY_UNAVAILABLE"
            )
        try:
            evidence = PlanningOutputEvidence.model_validate(
                self.outputs.read_output(execution["id"], execution["binding"])
            ).model_dump()
        except (ValidationError, TypeError, ValueError):
            return self._blocked(execution["id"], principal, "PLANNING_OUTPUT_EVIDENCE_INVALID")
        content: bytes = evidence.pop("content")
        actual_sha256 = hashlib.sha256(content).hexdigest()
        if evidence["authority_kind"] == "fixture" and not self.allow_fixture_authorities:
            return self._blocked(execution["id"], principal, "PLANNING_FIXTURE_AUTHORITY_FORBIDDEN")
        if evidence["authority_kind"] == "production":
            return self._blocked(
                execution["id"], principal, "PLANNING_PRODUCTION_AUTHORITY_UNAVAILABLE"
            )
        if (
            evidence["execution_id"] != execution["id"]
            or evidence["binding_sha256"] != execution["binding_sha256"]
            or evidence["source_sha256"] != execution.get("output_source_sha256")
            or not evidence["completed"]
            or evidence["artifact_size"] != len(content)
            or evidence["artifact_sha256"] != actual_sha256
        ):
            return self._blocked(execution["id"], principal, "PLANNING_OUTPUT_BINDING_MISMATCH")
        try:
            from karajan.runs.planning_output import parse_planning_output

            plan = (
                parse_planning_output(content, version="v2").model_dump()
                if execution["binding"]["execution_policy_sha256"]
                else parse_planning_output(content, version="v1").model_dump()
            )
        except (UnicodeError, ValueError, ValidationError):
            return self._blocked(execution["id"], principal, "PLANNING_OUTPUT_REJECTED")
        sealed = {**evidence, "content_sha256": actual_sha256}
        run = self._owner_run(execution["run_id"], principal)
        request: dict[str, Any] = {
            "run_id": execution["run_id"],
            "term": execution["binding"]["term"],
            "intent_id": execution["intent_id"],
            "expected_plan_revision": run["latest_plan_revision"],
            "plan": plan,
        }
        if run["schema_version"] == "karajan.run-planning.v2":
            request["schema_version"] = "karajan.submit-plan.v2"
        with self._transaction() as db:
            current = self._load(db, execution["id"])
            self._owner_run(current["run_id"], principal)

            def capture() -> dict[str, Any]:
                if current["cancel_requested"]:
                    return current
                if current["output"] not in (None, sealed):
                    return self._blocked_locked(db, current, "PLANNING_OUTPUT_EVIDENCE_CHANGED")
                current["output"] = sealed
                current["submission_request"] = request
                current["submission_command_key"] = "planning-execution-submit:" + current["id"]
                current["submission_principal"] = current["binding"]["principal"]
                current["state"] = "output_captured"
                self._save(db, current)
                return current

            return self._command(
                db, principal, command_key, ["submit", execution["id"], sealed], capture
            )

    def _claim_submission(
        self, execution_id: str, principal: str, command_key: str
    ) -> dict[str, Any]:
        with self._transaction() as db:
            current = self._load(db, execution_id)
            self._owner_run(current["run_id"], principal)
            if current["cancel_requested"]:
                return current
            if current["state"] == "output_captured":
                current["state"] = "submit_claimed"
                current["submission_started"] = False
                current["reason_codes"] = []
                self._save(db, current)
            return current

    def _recover_or_submit(self, execution_id: str, principal: str) -> dict[str, Any]:
        with self._transaction() as db:
            current = self._load(db, execution_id)
            self._owner_run(current["run_id"], principal)
            request = current.get("submission_request")
            key = current.get("submission_command_key")
            submission_principal = current.get("submission_principal")
            if (
                not isinstance(request, dict)
                or not isinstance(key, str)
                or not isinstance(submission_principal, str)
            ):
                return self._blocked_locked(db, current, "PLANNING_SUBMISSION_BINDING_MISSING")
        try:
            receipt = self.planner.command_receipt(
                "submit_plan", request, principal=submission_principal, command_key=key
            )
        except RunError as error:
            return self._blocked(execution_id, principal, error.code)
        if receipt is not None:
            return self._record_submission(execution_id, principal, receipt)
        with self._transaction() as db:
            current = self._load(db, execution_id)
            if current["state"] == "submission_unknown":
                return current
            if current["cancel_requested"]:
                return current
            if current.get("submission_started"):
                current["state"] = "submission_unknown"
                current["reason_codes"] = ["PLANNING_EXECUTION_SUBMISSION_UNKNOWN"]
                self._save(db, current)
                return current
            current["submission_started"] = True
            self._save(db, current)
        try:
            submission = self.planner._submit_planning_execution_plan(
                request["run_id"],
                current["intent_id"],
                request,
                execution_id=execution_id,
                binding_sha256=current["binding_sha256"],
                principal=principal,
                command_key=key,
                submission_guard=lambda: self._submission_allowed(execution_id, principal),
            )
        except RunError as error:
            return self._blocked(execution_id, principal, error.code)
        except Exception:
            with self._transaction() as db:
                current = self._load(db, execution_id)
                if current["submission"] is None:
                    current["state"] = "submission_unknown"
                    current["reason_codes"] = ["PLANNING_EXECUTION_SUBMISSION_UNKNOWN"]
                    self._save(db, current)
            raise
        return self._record_submission(execution_id, principal, submission)

    def _submission_allowed(self, execution_id: str, principal: str) -> None:
        with self._transaction() as db:
            current = self._load(db, execution_id)
            self._owner_run(current["run_id"], principal)
            if current["cancel_requested"]:
                raise RunError("PLANNING_EXECUTION_CANCELLED")

    def _record_submission(
        self, execution_id: str, principal: str, submission: dict[str, Any]
    ) -> dict[str, Any]:
        with self._transaction() as db:
            current = self._load(db, execution_id)
            self._owner_run(current["run_id"], principal)
            if current["submission"] is None:
                current["submission"] = submission
                current["state"] = "submitted"
                current["reason_codes"] = []
                self._save(db, current)
            elif current["submission"] != submission:
                return self._blocked_locked(db, current, "PLANNING_SUBMISSION_EVIDENCE_CHANGED")
            return current
