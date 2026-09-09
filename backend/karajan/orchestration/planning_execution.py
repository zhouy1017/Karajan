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
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
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
    # Denied and interrupted receipts have no estimate.  A production producer
    # requires this value only after the durable receipt is admitted.
    duration_seconds: int | None = Field(default=None, ge=1, le=300)
    state: Literal["admitted", "denied", "unknown"]
    reason_codes: list[str] = Field(default_factory=list, max_length=8)


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


@dataclass(frozen=True, slots=True, repr=False)
class _TrustedFactoryAuthority:
    """Private bootstrap and store identities captured by one factory."""

    control_directory: Path
    bootstrap_sha256: str
    settings_document: dict[str, Any]
    identities: tuple[tuple[Path, tuple[int, int]], ...]
    private_files: tuple[Path, ...]


def _path_identity(path: Path) -> tuple[int, int]:
    """Keep stable filesystem identity; SQLite writes do not change this."""
    info = path.lstat()
    return info.st_dev, info.st_ino


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
        _trusted_factory_authority: _TrustedFactoryAuthority | None = None,
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
        self._trusted_factory_authority = _trusted_factory_authority
        self.existing_only = existing_only
        self.clock = planner.clock if clock is None else clock
        self._native_stop_lock = Lock()
        self._native_stoppers: dict[str, Callable[[], dict[str, Any]]] = {}
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
        from .planning_bootstrap import (
            PLANNING_ADMISSION_BOOTSTRAP,
            _canonical_existing,
            assert_planning_bootstrap_current,
            read_planning_bootstrap,
        )
        from .planning_snapshot import PlanningRepositorySnapshotStore, snapshot_database
        from .planning_transport import PlanningOutputStore

        settings, bootstrap_sha256 = read_planning_bootstrap(control_directory)
        admissions = open_persistent_planning_admission(control_directory)
        # The admission factory has independently opened its read-only ports.
        # Re-read the descriptor before retaining their pathname identities so
        # construction itself cannot race a substitution.
        settings = assert_planning_bootstrap_current(control_directory, bootstrap_sha256)
        try:
            ledger = snapshot_database(control_directory)
        except Exception as error:
            raise RunError("PLANNING_REPOSITORY_SNAPSHOT_UNAVAILABLE") from error
        # Snapshot production was introduced after the original #110/#111
        # execution records.  An absent ledger therefore means this deployment
        # can only recover its pre-snapshot controller history; it does not
        # cause a reader to provision a new private store.  Once a ledger has a
        # filesystem spelling, however, it might contain evidence required by
        # a newer execution.  Open it strictly so a corrupt or aliased ledger
        # remains a fail-closed factory error rather than being mistaken for
        # historical absence.  ``is_symlink`` also catches a dangling alias.
        snapshots: object | None = None
        if ledger.exists() or ledger.is_symlink():
            try:
                snapshots = PlanningRepositorySnapshotStore(
                    ledger,
                    existing_only=True,
                    private_root=settings.state_directory,
                )
            except Exception as error:
                raise RunError("PLANNING_REPOSITORY_SNAPSHOT_UNAVAILABLE") from error
        output_database = settings.state_directory / "planning-output.sqlite"
        outputs = None
        if output_database.exists() or output_database.is_symlink():
            try:
                output_database = _canonical_existing(str(output_database), directory=False)
                from karajan.projects.credential_sources import _private

                from .planning_transport import observe_production_output_source

                _private(output_database)
                outputs = PlanningOutputStore(
                    output_database, authority_kind="production", existing_only=True
                )
                # This binding is owned by the execution factory itself. A
                # direct submit/recovery does not construct PlanningTransport,
                # yet it must re-observe the current Commander authority before
                # accepting an unclaimed persisted output.
                outputs.bind_source_reader(
                    lambda binding: observe_production_output_source(
                        settings.control_directory, admissions, binding
                    )
                )
            except (OSError, RunError, ValueError, sqlite3.Error) as error:
                raise RunError("PLANNING_OUTPUT_AUTHORITY_UNAVAILABLE") from error
        protected_paths = (
            settings.control_directory / PLANNING_ADMISSION_BOOTSTRAP,
            settings.state_directory,
            settings.planning_execution_database,
            settings.planning_admission_database,
            settings.capacity_database,
            settings.projects_database,
            settings.state_directory / "runs.sqlite",
            *((output_database,) if outputs is not None else ()),
        )
        try:
            trusted_factory_authority = _TrustedFactoryAuthority(
                settings.control_directory,
                bootstrap_sha256,
                settings.document(),
                tuple((path, _path_identity(path)) for path in protected_paths),
                (output_database,) if outputs is not None else (),
            )
        except OSError as error:
            raise RunError("PLANNING_ADMISSION_BOOTSTRAP_CHANGED") from error
        return cls(
            admissions.execution_database,
            admissions.planner,
            admissions=admissions,
            outputs=outputs,
            capacity=admissions.capacity,
            snapshots=snapshots,
            existing_only=True,
            _trusted_authority_ids=frozenset(
                {id(admissions)} if outputs is None else {id(admissions), id(outputs)}
            ),
            _trusted_factory_authority=trusted_factory_authority,
        )

    def _assert_trusted_factory_authority_current(self) -> None:
        """Reject retained aliases/replacements before reopening controller state."""
        trusted = self._trusted_factory_authority
        if trusted is None:
            return
        from karajan.projects.credential_sources import _private

        from .planning_bootstrap import (
            _canonical_existing,
            assert_planning_bootstrap_current,
        )

        try:
            settings = assert_planning_bootstrap_current(
                trusted.control_directory, trusted.bootstrap_sha256
            )
            if settings.document() != trusted.settings_document or any(
                _path_identity(path) != expected for path, expected in trusted.identities
            ):
                raise ValueError()
            for path in trusted.private_files:
                if _canonical_existing(str(path), directory=False) != path:
                    raise ValueError()
                _private(path)
        except (OSError, ValueError, RunError):
            raise RunError("PLANNING_ADMISSION_BOOTSTRAP_CHANGED") from None

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
        self._assert_trusted_factory_authority_current()
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
        row = db.execute(
            "SELECT run_id,intent_id,state,data FROM executions WHERE id=?", (execution_id,)
        ).fetchone()
        if row is None:
            raise RunError("PLANNING_EXECUTION_NOT_FOUND")
        try:
            execution = json.loads(row["data"])
        except (TypeError, json.JSONDecodeError):
            raise RunError("PLANNING_EXECUTION_BINDING_STALE") from None
        # The SQLite primary key is the public request identity.  Do not let a
        # substituted, internally consistent JSON row reconstruct authority
        # for another execution owned by the same principal.
        if (
            not isinstance(execution, dict)
            or execution.get("id") != execution_id
            or any(execution.get(key) != row[key] for key in ("run_id", "intent_id", "state"))
        ):
            raise RunError("PLANNING_EXECUTION_BINDING_STALE")
        return dict(execution)

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

        @contextmanager
        def current_authority() -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
            """Hold Execution then Run authority through manifest publication.

            Every operation that needs both locks takes Execution before Run
            (notably cancellation's owner check).  Git/CAS preparation occurs
            before this guard; only the reference commit is serialized here.
            """
            with self._transaction() as db:
                current = self._load(db, execution_id)
                if current["cancel_requested"]:
                    raise RunError("PLANNING_EXECUTION_CANCELLED")
                stored = current.get("binding")
                if (
                    not isinstance(stored, dict)
                    or current.get("binding_sha256") != digest(stored)
                    or stored != binding
                ):
                    raise RunError("PLANNING_EXECUTION_BINDING_STALE")
                with self.planner._transaction() as runs:
                    run = self.planner._get(runs, current["run_id"])
                    self.planner._owner(run, principal)
                    intent = self._intent(run, current["intent_id"])
                    if self._binding(run, intent, execution_id) != binding:
                        raise RunError("PLANNING_EXECUTION_BINDING_STALE")
                    # Project is the final source authority.  Keep its short
                    # writer transaction through the manifest/reference commit
                    # after Execution -> Run, matching the controller lock
                    # order used by the other planning boundaries.
                    with self.planner.projects._transaction() as projects:
                        self.planner.projects._current(
                            projects,
                            run["project_id"],
                            run["configuration_snapshot"]["project_revision"],
                        )
                        yield current, run

        def freeze_current() -> dict[str, Any]:
            try:
                return {key: value for key, value in read(binding).items() if key != "content"}
            except RunError as error:
                if str(error) != "PLANNING_REPOSITORY_SNAPSHOT_NOT_FOUND":
                    raise
            # This pre-prepare read rejects an already revoked identity without
            # holding writers during Git. The same guard is acquired again and
            # retained by the store for the final reference publication.
            with current_authority() as (_, run):
                prepared_run = run
            # Registry access precedes slow Git preparation and is deliberately
            # outside the held Execution/Run writer chain.
            project = self.planner.projects.get(prepared_run["project_id"])
            return cast(
                dict[str, Any], freeze(binding, prepared_run, project, guard=current_authority)
            )

        # Reserve the command identity before Git/CAS publication.  The
        # pending receipt intentionally survives a reply loss: only this exact
        # payload may resume it, while a different execution/key type is
        # rejected before it can publish another snapshot.
        payload = ["freeze_repository_snapshot", execution_id]
        replay: dict[str, Any] | None = None
        with self._transaction() as db:
            prior = db.execute(
                "SELECT payload,result FROM commands WHERE principal=? AND key=?",
                (principal, command_key),
            ).fetchone()
            if prior is not None:
                if prior["payload"] != encoded(payload):
                    raise RunError("IDEMPOTENCY_CONFLICT")
                recorded = dict(json.loads(prior["result"]))
                if recorded.get("state") != "freeze_repository_snapshot_pending":
                    replay = recorded
            else:
                db.execute(
                    "INSERT INTO commands VALUES (?,?,?,?)",
                    (
                        principal,
                        command_key,
                        encoded(payload),
                        encoded({"state": "freeze_repository_snapshot_pending"}),
                    ),
                )
        # Historical artifact verification deliberately happens after the
        # controller writer is gone: cancellation and other Run writers can
        # progress while a large, but bounded, snapshot is checked.
        if replay is not None:
            verified = read(binding)
            manifest = {key: value for key, value in verified.items() if key != "content"}
            if replay != manifest:
                raise RunError("PLANNING_REPOSITORY_SNAPSHOT_CHANGED")
            return manifest
        result = freeze_current()
        concurrent_receipt: dict[str, Any] | None = None
        with self._transaction() as db:
            prior = db.execute(
                "SELECT payload,result FROM commands WHERE principal=? AND key=?",
                (principal, command_key),
            ).fetchone()
            if prior is None or prior["payload"] != encoded(payload):
                raise RunError("IDEMPOTENCY_CONFLICT")
            recorded = dict(json.loads(prior["result"]))
            if recorded.get("state") == "freeze_repository_snapshot_pending":
                db.execute(
                    "UPDATE commands SET result=? WHERE principal=? AND key=?",
                    (encoded(result), principal, command_key),
                )
                return result
            concurrent_receipt = recorded
        # Another claimant can finish between our store read and this short
        # receipt transaction.  Verify outside the controller writer just as
        # the ordinary replay path does, and return the sealed manifest rather
        # than mutable command-ledger metadata.
        verified = read(binding)
        manifest = {key: value for key, value in verified.items() if key != "content"}
        if concurrent_receipt != manifest:
            raise RunError("PLANNING_REPOSITORY_SNAPSHOT_CHANGED")
        return manifest

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
        fixture_allowed = (
            getattr(authority, "authority_kind", None) == "fixture"
            and self.allow_fixture_authorities
        )
        if not callable(advance) or not (
            id(authority) in self._trusted_authority_ids or fixture_allowed
        ):
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

            cancelled = self._command(db, principal, command_key, ["cancel", execution_id], cancel)
        return self._stop_owned_native(execution_id, principal, cancelled)

    def register_native_stopper(
        self, execution_id: str, stopper: Callable[[], dict[str, Any]]
    ) -> Callable[[], None]:
        """Expose only this process's owned native stop operation to cancel."""
        identifier(execution_id)
        with self._native_stop_lock:
            if execution_id in self._native_stoppers:
                raise RunError("PLANNING_NATIVE_STOPPER_ALREADY_REGISTERED")
            self._native_stoppers[execution_id] = stopper

        def unregister() -> None:
            with self._native_stop_lock:
                if self._native_stoppers.get(execution_id) is stopper:
                    self._native_stoppers.pop(execution_id, None)

        return unregister

    def register_native_stop_proof(
        self, execution_id: str, binding_sha256: str, proof: dict[str, object]
    ) -> None:
        """Persist the native object's own stop proof for this exact execution."""
        identifier(execution_id)
        if len(binding_sha256) != 64 or not isinstance(proof, dict):
            raise RunError("PLANNING_NATIVE_STOP_PROOF_INVALID")
        with self._transaction() as db:
            current = self._load(db, execution_id)
            entry = {"binding_sha256": binding_sha256, "proof": proof}
            previous = current.get("native_stop_proof")
            if previous not in (None, entry):
                raise RunError("PLANNING_NATIVE_STOP_PROOF_INVALID")
            if current["binding_sha256"] != binding_sha256:
                raise RunError("PLANNING_NATIVE_STOP_PROOF_INVALID")
            current["native_stop_proof"] = entry
            self._save(db, current)

    def _stop_owned_native(
        self, execution_id: str, principal: str, execution: dict[str, Any]
    ) -> dict[str, Any]:
        with self._native_stop_lock:
            stopper = self._native_stoppers.get(execution_id)
        if stopper is None:
            proof = execution.get("native_stop_proof")
            if (
                not isinstance(proof, dict)
                or proof.get("binding_sha256") != execution.get("binding_sha256")
                or not isinstance(proof.get("proof"), dict)
            ):
                cleanup = {"local_stop": "unknown"}
            else:
                from karajan.isolation.opencode_runtime import stop_from_proof

                cleanup = stop_from_proof(proof["proof"])
        else:
            try:
                cleanup = stopper()
                if not isinstance(cleanup, dict):
                    raise ValueError
            except Exception:
                cleanup = {"local_stop": "unknown"}
        with self._transaction() as db:
            current = self._load(db, execution_id)
            self._owner_run(current["run_id"], principal)
            current["native_cleanup"] = cleanup
            # Native cancellation has no provider cancel protocol. Its remote
            # outcome remains deliberately unknown even when the local PID is
            # confirmed stopped.
            current["provider_remote_stop"] = "unknown"
            self._save(db, current)
            return current

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
        if evidence["state"] == "denied":
            reason_codes = evidence.get("reason_codes")
            reason = (
                reason_codes[0]
                if isinstance(reason_codes, list)
                and reason_codes
                and isinstance(reason_codes[0], str)
                else "PLANNING_ADMISSION_DENIED"
            )
            return self._blocked(execution_id, principal, reason)
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
                submission_fence=lambda: self._submission_fence(execution_id, principal),
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

    def _submission_source(self, execution: dict[str, Any]) -> None:
        """Read the sealed output source before taking the submission fence."""
        if self.outputs is None:
            raise RunError("PLANNING_OUTPUT_AUTHORITY_UNAVAILABLE")
        try:
            source = PlanningOutputSource.model_validate(
                self.outputs.read_source(execution["binding"])
            ).model_dump()
        except (ValidationError, TypeError, ValueError):
            raise RunError("PLANNING_OUTPUT_SOURCE_INVALID") from None
        if not self._authority_allowed(self.outputs, source["authority_kind"]):
            raise RunError(
                "PLANNING_FIXTURE_AUTHORITY_FORBIDDEN"
                if source["authority_kind"] == "fixture"
                else "PLANNING_PRODUCTION_AUTHORITY_UNAVAILABLE"
            )
        if source["binding_sha256"] != execution["binding_sha256"] or source[
            "source_sha256"
        ] != execution.get("output_source_sha256"):
            raise RunError("PLANNING_OUTPUT_SOURCE_CHANGED")

    @contextmanager
    def _submission_fence(
        self, execution_id: str, principal: str
    ) -> Iterator[Callable[[dict[str, Any]], Callable[[], None]]]:
        """Hold Execution before Run, then acquire source authority under Run.

        The initial source observation has no held controller writer, so a
        cancellation or source revocation may win before the fence.  Once the
        Execution writer is held, ``submit_plan`` acquires Run and calls the
        yielded callback for the final Project current-source guard.  That
        guard remains live until both Plan and its command receipt commit.
        """
        execution = self.get(execution_id, principal=principal)
        self._submission_source(execution)
        transaction = self._transaction()
        db = transaction.__enter__()
        try:
            current = self._load(db, execution_id)
            self._owner_run(current["run_id"], principal)
            if current["cancel_requested"]:
                raise RunError("PLANNING_EXECUTION_CANCELLED")

            def source_guard(run: dict[str, Any]) -> Callable[[], None]:
                return self._submission_source_guard(current, run)

            yield source_guard
        except BaseException as error:
            transaction.__exit__(type(error), error, error.__traceback__)
            raise
        else:
            transaction.__exit__(None, None, None)

    def _submission_source_guard(
        self, execution: dict[str, Any], run: dict[str, Any]
    ) -> Callable[[], None]:
        """Keep the production credential/qualification authority through Plan commit."""
        authority = self.admissions
        if getattr(authority, "authority_kind", None) != "production":
            return lambda: None
        reader = None if authority is None else getattr(authority, "qualifications", None)
        current_guard = None if reader is None else getattr(reader, "current_guard_locked", None)
        if not callable(current_guard):
            raise RunError("PLANNING_OUTPUT_AUTHORITY_UNAVAILABLE")
        from .planning_admission import (
            COMMANDER_QUALIFICATION_READER_VERSION,
            COMMANDER_QUALIFICATION_SCOPE,
        )

        guard = current_guard(
            execution["binding"],
            run,
            scope=COMMANDER_QUALIFICATION_SCOPE,
            reader_version=COMMANDER_QUALIFICATION_READER_VERSION,
        )
        try:
            current = guard.__enter__()
        except (RunError, ValueError, OSError, sqlite3.Error):
            raise RunError("PLANNING_OUTPUT_SOURCE_UNAVAILABLE") from None
        if current is None:
            guard.__exit__(None, None, None)
            raise RunError("PLANNING_OUTPUT_SOURCE_CHANGED")

        def release() -> None:
            guard.__exit__(None, None, None)

        return release

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
