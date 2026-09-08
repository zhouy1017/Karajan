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
from typing import Any, Literal, Protocol, cast

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
    capacity_activation_request: dict[str, Any]
    capacity_activation_command_key: str
    capacity_activation_receipt: dict[str, Any] | None
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
        snapshots: object | None = None,
        allow_fixture_authorities: bool = False,
        _trusted_authority_ids: frozenset[int] = frozenset(),
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
        self.snapshots = snapshots
        self.allow_fixture_authorities = allow_fixture_authorities
        # Production tags are evidence fields, never a caller-controlled grant.
        # Only the controller factory below can bind the exact authority objects
        # it rebuilt from fixed persistent configuration.
        self._trusted_authority_ids = _trusted_authority_ids
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

    @classmethod
    def from_trusted_factory(
        cls,
        control_directory: Path,
    ) -> "PlanningExecution":
        """Rebuild the admission port from the protected persistent bootstrap."""
        from .planning_admission import open_persistent_planning_admission
        from .planning_snapshot import PlanningRepositorySnapshotStore, snapshot_database

        admissions = open_persistent_planning_admission(control_directory)
        try:
            snapshots = PlanningRepositorySnapshotStore(
                snapshot_database(control_directory), existing_only=True
            )
        except Exception as error:
            raise RunError("PLANNING_REPOSITORY_SNAPSHOT_UNAVAILABLE") from error
        return cls(
            admissions.execution_database,
            admissions.planner,
            admissions=admissions,
            capacity=admissions.capacity,
            snapshots=snapshots,
            existing_only=True,
            _trusted_authority_ids=frozenset({id(admissions)}),
        )

    def _authority_allowed(self, authority: object, kind: str) -> bool:
        if kind == "fixture":
            return self.allow_fixture_authorities
        return kind == "production" and id(authority) in self._trusted_authority_ids

    @staticmethod
    def _monotonic_admission(previous: object, fresh: dict[str, Any]) -> bool:
        """Allow exact completion or one receipt-proven unknown enrichment."""
        if previous is None:
            return True
        if not isinstance(previous, dict) or previous == fresh:
            return previous == fresh
        if previous.get("state") != "unknown" or fresh.get("state") not in {
            "unknown",
            "admitted",
            "denied",
        }:
            return False
        immutable = all(
            previous.get(key) == fresh.get(key)
            for key in (
                "schema_version",
                "binding_sha256",
                "authority_kind",
                "source_sha256",
                "budget_ref",
                "capacity_request",
                "capacity_command_key",
                "capacity_activation_command_key",
            )
        )
        if not immutable:
            return False
        if fresh.get("state") in {"admitted", "denied"}:
            return previous.get("capacity_activation_request") == fresh.get(
                "capacity_activation_request"
            )
        # A lost admit reply has no receipt or activation request in the first
        # read-only evidence. It may be enriched only with the receipt returned
        # by that exact persisted command and its derived activation identity;
        # it remains unknown and cannot activate, claim, refund or rebind.
        receipt = fresh.get("capacity_receipt")
        return (
            previous.get("capacity_receipt") is None
            and previous.get("capacity_activation_request") == {}
            and previous.get("capacity_activation_receipt") is None
            and isinstance(receipt, dict)
            and receipt.get("decision") == "admitted"
            and isinstance(receipt.get("admission_id"), str)
            and fresh.get("capacity_activation_request")
            == {"admission_id": receipt["admission_id"]}
            and fresh.get("capacity_activation_receipt") is None
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
        # The intent is the controller-sealed record of the Commander that
        # created this execution.  ``run.commander`` is deliberately live: a
        # later approved handoff must not rewrite an existing execution's v1
        # identity merely because historical evidence is being read.
        participant = intent
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

    def _trusted_snapshot_binding(
        self, execution: dict[str, Any], principal: str
    ) -> dict[str, Any]:
        """Rebuild v1 identity from the durable Run, never from execution JSON.

        This intentionally does not require an intent to still be awaiting a
        receipt: a snapshot already committed before a later cancellation is
        historical evidence and remains readable.  Its identity fields must,
        however, still be exactly those recorded in the Run.
        """
        run = self._owner_run(execution["run_id"], principal)
        intent = next(
            (item for item in run["planning_intents"] if item["id"] == execution["intent_id"]),
            None,
        )
        if not isinstance(intent, dict):
            raise RunError("PLANNING_EXECUTION_BINDING_STALE")
        try:
            return self._binding(run, intent, execution["id"])
        except (KeyError, TypeError):
            raise RunError("PLANNING_EXECUTION_BINDING_STALE") from None

    def _snapshot_binding(self, execution: dict[str, Any], principal: str) -> dict[str, Any]:
        binding = execution.get("binding")
        if not isinstance(binding, dict) or execution.get("binding_sha256") != digest(binding):
            raise RunError("PLANNING_EXECUTION_BINDING_STALE")
        if self._trusted_snapshot_binding(execution, principal) != binding:
            raise RunError("PLANNING_EXECUTION_BINDING_STALE")
        return binding

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

            return self._command(db, principal, command_key, replay_payload, create)

    def get(self, execution_id: str, *, principal: str) -> dict[str, Any]:
        for value in (execution_id, principal):
            identifier(value)
        with self._transaction() as db:
            execution = self._load(db, execution_id)
        self._owner_run(execution["run_id"], principal)
        return execution

    def freeze_repository_snapshot(
        self, execution_id: str, *, principal: str, command_key: str
    ) -> dict[str, Any]:
        """Create or recover the one private base-tree snapshot for this execution.

        The caller supplies only durable IDs.  Missing production provisioning is
        fail-closed and never changes admission, capacity, or Run state.
        """
        for value in (execution_id, principal, command_key):
            identifier(value)
        execution = self.get(execution_id, principal=principal)
        binding = self._snapshot_binding(execution, principal)
        store = self.snapshots
        freeze = None if store is None else getattr(store, "freeze", None)
        read = None if store is None else getattr(store, "read", None)
        if not callable(freeze) or not callable(read):
            raise RunError("PLANNING_REPOSITORY_SNAPSHOT_UNAVAILABLE")

        def freeze_current() -> dict[str, Any]:
            try:
                return {key: value for key, value in read(binding).items() if key != "content"}
            except RunError as error:
                if str(error) != "PLANNING_REPOSITORY_SNAPSHOT_NOT_FOUND":
                    raise
            if execution.get("cancel_requested"):
                raise RunError("PLANNING_EXECUTION_CANCELLED")
            run = self._owner_run(execution["run_id"], principal)
            intent = self._intent(run, execution["intent_id"])
            if self._binding(run, intent, execution_id) != binding:
                raise RunError("PLANNING_EXECUTION_BINDING_STALE")
            project = self.planner.projects.get(run["project_id"])
            return cast(dict[str, Any], freeze(binding, run, project))

        with self._transaction() as db:
            return self._command(
                db,
                principal,
                command_key,
                ["freeze_repository_snapshot", execution_id],
                freeze_current,
            )

    def read_repository_snapshot(self, execution_id: str, *, principal: str) -> dict[str, Any]:
        execution = self.get(execution_id, principal=principal)
        binding = self._snapshot_binding(execution, principal)
        read = None if self.snapshots is None else getattr(self.snapshots, "read", None)
        if not callable(read):
            raise RunError("PLANNING_REPOSITORY_SNAPSHOT_UNAVAILABLE")
        return cast(dict[str, Any], read(binding))

    def admit(self, execution_id: str, *, principal: str, command_key: str) -> dict[str, Any]:
        """Advance only a trusted durable admission authority once.

        ``reconcile`` intentionally remains receipt-only, so a reconnect cannot
        turn an observation into a fresh capacity claim.
        """
        for value in (execution_id, principal, command_key):
            identifier(value)
        self.get(execution_id, principal=principal)
        authority = self.admissions
        advance = None if authority is None else getattr(authority, "advance", None)
        if not callable(advance) or id(authority) not in self._trusted_authority_ids:
            return self._blocked(
                execution_id, principal, "PLANNING_PRODUCTION_AUTHORITY_UNAVAILABLE"
            )
        try:
            advance(execution_id, principal, command_key)
        except (RunError, ValueError):
            # The immutable authority record is the only result consumers see.
            pass
        if self.outputs is None:
            # #112 owns the output transport. Admission remains observable via
            # its sealed receipt without treating missing output as a denial.
            return self.get(execution_id, principal=principal)
        return self.reconcile(execution_id, principal=principal)

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
        if not self._authority_allowed(self.admissions, evidence["authority_kind"]):
            return self._blocked(
                execution_id,
                principal,
                "PLANNING_FIXTURE_AUTHORITY_FORBIDDEN"
                if evidence["authority_kind"] == "fixture"
                else "PLANNING_PRODUCTION_AUTHORITY_UNAVAILABLE",
            )
        # An interrupted Capacity effect is neither an approval nor a denial.
        # Keep the controller resumable and let a later ID-only admission read
        # the original durable receipt; do not convert uncertainty to a
        # terminal blocked execution.
        if evidence["state"] == "unknown":
            with self._transaction() as db:
                current = self._load(db, execution_id)
                if current["state"] not in {"awaiting_admission", "admission_unknown"}:
                    return current
                if current["cancel_requested"]:
                    return current
                if not self._monotonic_admission(current["admission"], evidence):
                    return self._blocked_locked(db, current, "PLANNING_ADMISSION_EVIDENCE_CHANGED")
                current["admission"] = evidence
                current["state"] = "admission_unknown"
                current["reason_codes"] = ["PLANNING_ADMISSION_UNKNOWN"]
                self._save(db, current)
                return current
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
        if evidence["capacity_activation_request"] != {"admission_id": receipt["admission_id"]}:
            return self._blocked(execution_id, principal, "PLANNING_CAPACITY_ACTIVATION_MISMATCH")
        try:
            activation = self.capacity.command_receipt(
                "activate",
                evidence["capacity_activation_request"],
                command_key=evidence["capacity_activation_command_key"],
            )
        except ValueError:
            return self._blocked(execution_id, principal, "PLANNING_CAPACITY_ACTIVATION_INVALID")
        if (
            activation is None
            or activation != evidence["capacity_activation_receipt"]
            or activation.get("decision") != "capacity_revalidated"
            or activation.get("admission_id") != evidence["capacity_receipt"]["admission_id"]
        ):
            return self._blocked(execution_id, principal, "PLANNING_CAPACITY_ACTIVATION_MISMATCH")
        if self.outputs is None:
            return self._blocked(execution_id, principal, "PLANNING_OUTPUT_AUTHORITY_UNAVAILABLE")
        try:
            source = PlanningOutputSource.model_validate(
                self.outputs.read_source(execution["binding"])
            ).model_dump()
        except (ValidationError, TypeError, ValueError):
            return self._blocked(execution_id, principal, "PLANNING_OUTPUT_SOURCE_INVALID")
        if not self._authority_allowed(self.outputs, source["authority_kind"]):
            return self._blocked(
                execution_id,
                principal,
                "PLANNING_FIXTURE_AUTHORITY_FORBIDDEN"
                if source["authority_kind"] == "fixture"
                else "PLANNING_PRODUCTION_AUTHORITY_UNAVAILABLE",
            )
        if source["binding_sha256"] != execution["binding_sha256"]:
            return self._blocked(execution_id, principal, "PLANNING_OUTPUT_SOURCE_BINDING_MISMATCH")
        with self._transaction() as db:
            current = self._load(db, execution_id)
            if current["state"] not in {"awaiting_admission", "admission_unknown"}:
                return current
            if current["cancel_requested"]:
                return current
            if not self._monotonic_admission(current["admission"], evidence):
                return self._blocked_locked(db, current, "PLANNING_ADMISSION_EVIDENCE_CHANGED")
            current["admission"] = evidence
            if current.get("output_source_sha256") not in (None, source["source_sha256"]):
                return self._blocked_locked(db, current, "PLANNING_OUTPUT_SOURCE_CHANGED")
            current["output_source_sha256"] = source["source_sha256"]
            current["state"] = (
                "awaiting_output" if evidence["state"] == "admitted" else "admission_unknown"
            )
            current["reason_codes"] = (
                []
                if evidence["state"] == "admitted"
                else ["PLANNING_ADMISSION_" + evidence["state"].upper()]
            )
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
        claimed_here = False
        if execution["state"] == "awaiting_output":
            execution = self._capture_output(execution, principal, command_key)
        if execution["state"] == "output_captured":
            execution, claimed_here = self._claim_submission(execution_id, principal, command_key)
            # Only this invocation has just checked the live output authority.
            # A persisted claim belongs to a previous, possibly interrupted
            # invocation and may only be recovered through its Run receipt.
        if execution["state"] in {"submit_claimed", "submission_unknown"}:
            return self._recover_or_submit(execution_id, principal, claimed_here=claimed_here)
        return execution

    def _capture_output(
        self, execution: dict[str, Any], principal: str, command_key: str
    ) -> dict[str, Any]:
        if self.outputs is None:
            return self._blocked(
                execution["id"], principal, "PLANNING_OUTPUT_AUTHORITY_UNAVAILABLE"
            )
        try:
            current_source = PlanningOutputSource.model_validate(
                self.outputs.read_source(execution["binding"])
            ).model_dump()
        except (ValidationError, TypeError, ValueError):
            return self._blocked(execution["id"], principal, "PLANNING_OUTPUT_SOURCE_INVALID")
        if not self._authority_allowed(self.outputs, current_source["authority_kind"]):
            return self._blocked(
                execution["id"],
                principal,
                "PLANNING_FIXTURE_AUTHORITY_FORBIDDEN"
                if current_source["authority_kind"] == "fixture"
                else "PLANNING_PRODUCTION_AUTHORITY_UNAVAILABLE",
            )
        if current_source["binding_sha256"] != execution["binding_sha256"] or current_source[
            "source_sha256"
        ] != execution.get("output_source_sha256"):
            return self._blocked(execution["id"], principal, "PLANNING_OUTPUT_SOURCE_CHANGED")
        try:
            evidence = PlanningOutputEvidence.model_validate(
                self.outputs.read_output(execution["id"], execution["binding"])
            ).model_dump()
        except (ValidationError, TypeError, ValueError):
            return self._blocked(execution["id"], principal, "PLANNING_OUTPUT_EVIDENCE_INVALID")
        content: bytes = evidence.pop("content")
        actual_sha256 = hashlib.sha256(content).hexdigest()
        if not self._authority_allowed(self.outputs, evidence["authority_kind"]):
            return self._blocked(
                execution["id"],
                principal,
                "PLANNING_FIXTURE_AUTHORITY_FORBIDDEN"
                if evidence["authority_kind"] == "fixture"
                else "PLANNING_PRODUCTION_AUTHORITY_UNAVAILABLE",
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
                # Output reads happen outside the controller transaction.  A
                # slower submit with a different command key must not restore
                # this earlier stage after another caller advanced it.
                if current["state"] != "awaiting_output":
                    return current
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
    ) -> tuple[dict[str, Any], bool]:
        execution = self.get(execution_id, principal=principal)
        if self.outputs is None:
            return (
                self._blocked(execution_id, principal, "PLANNING_OUTPUT_AUTHORITY_UNAVAILABLE"),
                False,
            )
        try:
            source = PlanningOutputSource.model_validate(
                self.outputs.read_source(execution["binding"])
            ).model_dump()
        except (ValidationError, TypeError, ValueError):
            return self._blocked(execution_id, principal, "PLANNING_OUTPUT_SOURCE_INVALID"), False
        if not self._authority_allowed(self.outputs, source["authority_kind"]):
            return (
                self._blocked(
                    execution_id,
                    principal,
                    "PLANNING_FIXTURE_AUTHORITY_FORBIDDEN"
                    if source["authority_kind"] == "fixture"
                    else "PLANNING_PRODUCTION_AUTHORITY_UNAVAILABLE",
                ),
                False,
            )
        if source["binding_sha256"] != execution["binding_sha256"] or source[
            "source_sha256"
        ] != execution.get("output_source_sha256"):
            return self._blocked(execution_id, principal, "PLANNING_OUTPUT_SOURCE_CHANGED"), False
        with self._transaction() as db:
            current = self._load(db, execution_id)
            self._owner_run(current["run_id"], principal)
            if current["cancel_requested"]:
                return current, False
            if current["state"] == "output_captured":
                current["state"] = "submit_claimed"
                current["submission_started"] = False
                current["reason_codes"] = []
                self._save(db, current)
                return current, True
            return current, False

    def _recover_or_submit(
        self, execution_id: str, principal: str, *, claimed_here: bool
    ) -> dict[str, Any]:
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
            if not claimed_here:
                # Receipt-only recovery cannot tell a crashed claimant from a
                # live one that has not entered the Run store yet.  Report its
                # uncertainty without changing the durable claim, so it cannot
                # stop the claimant that holds the one first-submit right.
                return {
                    **current,
                    "state": "submission_unknown",
                    "reason_codes": ["PLANNING_EXECUTION_SUBMISSION_UNKNOWN"],
                }
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
        if self.outputs is None:
            raise RunError("PLANNING_OUTPUT_AUTHORITY_UNAVAILABLE")
        try:
            source = PlanningOutputSource.model_validate(
                self.outputs.read_source(current["binding"])
            ).model_dump()
        except (ValidationError, TypeError, ValueError):
            raise RunError("PLANNING_OUTPUT_SOURCE_INVALID") from None
        if not self._authority_allowed(self.outputs, source["authority_kind"]):
            raise RunError(
                "PLANNING_FIXTURE_AUTHORITY_FORBIDDEN"
                if source["authority_kind"] == "fixture"
                else "PLANNING_PRODUCTION_AUTHORITY_UNAVAILABLE"
            )
        if source["binding_sha256"] != current["binding_sha256"] or source[
            "source_sha256"
        ] != current.get("output_source_sha256"):
            raise RunError("PLANNING_OUTPUT_SOURCE_CHANGED")
        # The authority read is deliberately outside the controller lock.  A
        # cancellation may therefore arrive while it is in progress, so make
        # the final decision from a fresh durable observation.
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
            elif current["state"] != "submitted" or current["reason_codes"]:
                # A receipt proves the recorded submission even if a prior
                # interrupted controller write left the state behind it.
                current["state"] = "submitted"
                current["reason_codes"] = []
                self._save(db, current)
            return current
