"""Durable Reviewer Host preparation, deliberately before native observation."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast

from karajan.candidates import CandidateStore
from karajan.execution import LaunchDenied, ProcessSpec, RunnerHost
from karajan.routing.compiler import digest
from karajan.runs import RunError
from karajan.storage import ExistingStoreError, open_database, require_schema

from .admission import (
    ApprovedTaskAdmission,
    PreparedReviewerFinalEffect,
    ReviewerFinalEffectCapability,
)
from .go_execution_intent import GoExecutionIntents
from .reviewer_execution_binding import compiler_binding, host_manifest, launch_document
from .reviewer_input import ReviewerInput, compile_reviewer_input


def _validate_existing_ledger(database: Path) -> None:
    """Reject an absent or aliased fixed ledger before SQLite resolves it."""
    try:
        info = database.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError
    except FileNotFoundError:
        raise RunError("REVIEWER_EXECUTION_LEDGER_MISSING") from None
    except (OSError, ValueError):
        raise RunError("REVIEWER_EXECUTION_LEDGER_UNAVAILABLE") from None


def _require_existing_schema(database: Path) -> None:
    try:
        require_schema(
            database,
            {
                "reviewer_executions": [
                    "execution_id",
                    "run_id",
                    "reviewer_operation_id",
                    "principal",
                    "command_key",
                    "intent",
                    "state",
                ]
            },
        )
    except ExistingStoreError:
        raise RunError("REVIEWER_EXECUTION_LEDGER_UNAVAILABLE") from None


_LEDGER_COLUMNS = (
    "execution_id,run_id,reviewer_operation_id,principal,command_key,intent,state"
)


def _decode_intent_row(
    row: sqlite3.Row,
    *,
    run_id: str | None = None,
    reviewer_operation_id: str | None = None,
    principal: str | None = None,
) -> dict[str, Any]:
    """Bind every persisted index column to the digest-protected intent body."""
    try:
        value = json.loads(row["intent"])
        if not isinstance(value, dict) or value.get("intent_digest") != digest(
            {key: item for key, item in value.items() if key != "intent_digest"}
        ):
            raise ValueError
        identities = (
            "execution_id",
            "run_id",
            "reviewer_operation_id",
            "principal",
            "command_key",
        )
        if any(value.get(key) != row[key] for key in identities) or (
            value.get("phase") != row["state"]
        ):
            raise ValueError
        if (
            (run_id is not None and value["run_id"] != run_id)
            or (
                reviewer_operation_id is not None
                and value["reviewer_operation_id"] != reviewer_operation_id
            )
            or (principal is not None and value["principal"] != principal)
        ):
            raise ValueError
        return value
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise RunError("REVIEWER_EXECUTION_BINDING_INVALID") from None


def _find_intent_row(
    db: sqlite3.Connection, run_id: str, reviewer_operation_id: str, principal: str
) -> sqlite3.Row | None:
    """Find indexed identity, then a digest-protected embedded match.

    The normal indexed lookup is the fast path. If a persistent index column
    was changed, an original request must find the embedded identity so the
    common body/column binding check rejects it rather than creating a second
    intent. This exact JSON query is a narrow lookup, not a ledger rebuild.
    """
    row = cast(
        sqlite3.Row | None,
        db.execute(
            f"SELECT {_LEDGER_COLUMNS} FROM reviewer_executions "
            "WHERE run_id=? AND reviewer_operation_id=?",
            (run_id, reviewer_operation_id),
        ).fetchone(),
    )
    if row is not None:
        return row
    try:
        return cast(
            sqlite3.Row | None,
            db.execute(
                f"SELECT {_LEDGER_COLUMNS} FROM reviewer_executions "
                "WHERE json_valid(intent) "
                "AND json_extract(intent, '$.run_id')=? "
                "AND json_extract(intent, '$.reviewer_operation_id')=? "
                "AND json_extract(intent, '$.principal')=?",
                (run_id, reviewer_operation_id, principal),
            ).fetchone(),
        )
    except sqlite3.Error:
        raise RunError("REVIEWER_EXECUTION_BINDING_INVALID") from None


def _reject_repository_ledger(database: Path, projects: object) -> None:
    """Reject direct, alias, and hard-linked ledgers under registered sources."""
    try:
        candidate = database.resolve(strict=False)
        roots = [
            Path(row["repository"]["root"]).resolve(strict=True)
            for row in projects.list()  # type: ignore[attr-defined]
        ]
        if any(candidate.is_relative_to(root) for root in roots):
            raise RunError("REVIEWER_EXECUTION_LEDGER_IN_REPOSITORY")
        if not database.exists():
            return
        info = database.stat()
        for root in roots:
            for parent, directories, names in os.walk(root, followlinks=False):
                directories[:] = [
                    name for name in directories if not (Path(parent) / name).is_symlink()
                ]
                for name in names:
                    path = Path(parent) / name
                    try:
                        other = path.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if stat.S_ISREG(other.st_mode) and (other.st_dev, other.st_ino) == (
                        info.st_dev,
                        info.st_ino,
                    ):
                        raise RunError("REVIEWER_EXECUTION_LEDGER_IN_REPOSITORY")
    except RunError:
        raise
    except (KeyError, OSError, TypeError, ValueError):
        raise RunError("REVIEWER_EXECUTION_LEDGER_UNAVAILABLE") from None


class ReviewerExecutionHistory:
    """Read only a fixed existing ledger; it deliberately cannot produce effects."""

    def __init__(self, database: Path, *, host: RunnerHost | None = None) -> None:
        self.database = Path(database)
        self.host = host
        _validate_existing_ledger(self.database)
        _require_existing_schema(self.database)

    def read(
        self, run_id: str, reviewer_operation_id: str, *, principal: str
    ) -> dict[str, Any] | None:
        _validate_existing_ledger(self.database)
        _require_existing_schema(self.database)
        try:
            db = open_database(self.database, existing_only=True, isolation_level=None)
        except ExistingStoreError:
            raise RunError("REVIEWER_EXECUTION_LEDGER_UNAVAILABLE") from None
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN")
            row = _find_intent_row(db, run_id, reviewer_operation_id, principal)
            if row is None:
                return None
            return deepcopy(
                _decode_intent_row(
                    row,
                    run_id=run_id,
                    reviewer_operation_id=reviewer_operation_id,
                    principal=principal,
                )
            )
        finally:
            db.close()

    def inspect_host(
        self, run_id: str, reviewer_operation_id: str, *, principal: str
    ) -> dict[str, Any]:
        """Correlate only an already-persisted original Host preparation.

        Historical mode cannot prepare, initialize control, register/claim a
        runner, or write either ledger.  It uses the immutable original intent
        identity and RunnerHost's existing-only inspection port exclusively.
        """
        value = self.read(run_id, reviewer_operation_id, principal=principal)
        if value is None or self.host is None:
            raise RunError("REVIEWER_EXECUTION_HOST_PREPARE_REQUIRED")
        try:
            snapshot = self.host.inspect_original_preparation(
                host_manifest(value), value["start_key"]
            )
        except KeyError:
            raise RunError("REVIEWER_EXECUTION_HOST_PREPARE_REQUIRED") from None
        except (LaunchDenied, ValueError):
            raise RunError("REVIEWER_EXECUTION_HOST_BINDING_MISMATCH") from None
        if value.get("host_prepared_id") not in {None, snapshot.prepared_id}:
            raise RunError("REVIEWER_EXECUTION_HOST_BINDING_MISMATCH")
        return deepcopy(
            value
            | {
                "host_observation": {
                    "prepared_id": snapshot.prepared_id,
                    "attempt_id": snapshot.attempt_id,
                    "state": snapshot.state,
                    "launch_phase": snapshot.launch_phase,
                    "remote_stop": snapshot.remote_stop,
                }
            }
        )


@dataclass(frozen=True)
class ReviewerExecutionSource:
    runner_source_sha256: str
    native_source_sha256: str

    def __post_init__(self) -> None:
        if any(
            len(value) != 64 or any(c not in "0123456789abcdef" for c in value)
            for value in asdict(self).values()
        ):
            raise RunError("REVIEWER_EXECUTION_SOURCE_INVALID")


@dataclass(frozen=True)
class ReviewerLaunchSpec:
    process_spec: ProcessSpec
    bootstrap_digest: str


class ReviewerExecutionIntents:
    """Owns a separate ledger; its records never authorize Host.start or sends."""

    def __init__(
        self,
        database: Path,
        admissions: ApprovedTaskAdmission,
        candidates: CandidateStore,
        *,
        source: ReviewerExecutionSource,
        host: RunnerHost,
        launch_compiler: Callable[[dict[str, Any]], ReviewerLaunchSpec],
        current_source: Callable[[], ReviewerExecutionSource] | None = None,
        existing_only: bool = False,
    ) -> None:
        self.database, self.admissions, self.candidates = Path(database), admissions, candidates
        self.source, self.host, self.launch_compiler = source, host, launch_compiler
        self.current_source = current_source
        self.existing_only = existing_only
        _reject_repository_ledger(self.database, admissions.routing.planner.projects)
        if existing_only:
            self._validate_existing_ledger()
            self._require_existing_schema()
        if not existing_only:
            self.database.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            if not existing_only:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS reviewer_executions ("
                    "execution_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, "
                    "reviewer_operation_id TEXT NOT NULL, principal TEXT NOT NULL, "
                    "command_key TEXT NOT NULL, intent TEXT NOT NULL, state TEXT NOT NULL, "
                    "UNIQUE(principal, command_key), UNIQUE(run_id, reviewer_operation_id))"
                )

    def _validate_existing_ledger(self) -> None:
        _validate_existing_ledger(self.database)

    def _require_existing_schema(self) -> None:
        _require_existing_schema(self.database)

    @contextmanager
    def _db(self, *, write: bool = True) -> Iterator[sqlite3.Connection]:
        if self.existing_only:
            self._validate_existing_ledger()
            self._require_existing_schema()
        try:
            db = open_database(
                self.database, existing_only=self.existing_only, isolation_level=None
            )
        except ExistingStoreError:
            raise RunError("REVIEWER_EXECUTION_LEDGER_UNAVAILABLE") from None
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            # This private ledger is a local SQLite store: enable WAL once on
            # every writable open, and do not acquire its sole writer for a
            # read.  In particular, compiler/Host work must never sit inside
            # this transaction.
            if write:
                db.execute("PRAGMA journal_mode=WAL")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except BaseException:
            try:
                db.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            db.close()

    def _load(
        self, db: sqlite3.Connection, run_id: str, operation_id: str, principal: str
    ) -> dict[str, Any] | None:
        row = _find_intent_row(db, run_id, operation_id, principal)
        if row is None:
            return None
        return _decode_intent_row(
            row, run_id=run_id, reviewer_operation_id=operation_id, principal=principal
        )

    def _compiled(
        self, run_id: str, reviewer_operation_id: str, principal: str
    ) -> tuple[dict[str, Any], ReviewerInput]:
        run = self.admissions.routing.planner.get(run_id, principal=principal)
        reviewer = GoExecutionIntents.read_operation(
            self.admissions, run_id, reviewer_operation_id, principal=principal
        )
        worker = reviewer.get("depends_on_operation_id")
        if not isinstance(worker, str):
            raise RunError("REVIEW_WORKER_LINEAGE_REQUIRED")
        worker_record = GoExecutionIntents.read_operation(
            self.admissions, run_id, worker, principal=principal
        )
        try:
            checks = [
                row["evidence"]["id"] for row in worker_record["validation"]["checks"]["runs"]
            ]
        except (KeyError, TypeError):
            raise RunError("REVIEWER_INPUT_CHECKS_INCOMPLETE") from None
        compiled = compile_reviewer_input(
            self.admissions,
            self.candidates,
            run_id=run_id,
            operation_id=worker,
            principal=principal,
            final_check_evidence_ids=checks,
        )
        # The compiler owns independent Run/CAS reads.  Its records cannot be
        # read through the admission lock, so re-enter the current guard after
        # compilation and before persisting/using this otherwise inert binding.
        with self.admissions.reviewer_reserved_effect_guard(
            run_id, reviewer_operation_id, principal=principal
        ) as held:
            return compiler_binding(held, compiled, project_id=run["project_id"]), compiled

    def prepare(
        self, run_id: str, reviewer_operation_id: str, *, principal: str, command_key: str
    ) -> dict[str, Any]:
        with self._db(write=False) as db:
            prior = db.execute(
                f"SELECT {_LEDGER_COLUMNS} FROM reviewer_executions "
                "WHERE principal=? AND command_key=?",
                (principal, command_key),
            ).fetchone()
            existing = self._load(db, run_id, reviewer_operation_id, principal)
            if prior is not None:
                prior_value = _decode_intent_row(prior)
                if existing != prior_value:
                    raise RunError("IDEMPOTENCY_CONFLICT")
                if existing is None:
                    raise RunError("REVIEWER_EXECUTION_BINDING_INVALID")
                return existing
            if existing is not None:
                raise RunError("REVIEWER_EXECUTION_ALREADY_PREPARED")
        binding, compiled = self._compiled(run_id, reviewer_operation_id, principal)
        # Re-enter the real admission authority at the sole new-ledger effect.
        # Compilation uses independent controller stores, so its earlier guard
        # cannot authorize a stale insert.
        with self.admissions.reviewer_reserved_effect_guard(
            run_id, reviewer_operation_id, principal=principal
        ) as held:
            if compiler_binding(held, compiled, project_id=binding["project_id"]) != binding:
                raise RunError("REVIEWER_EXECUTION_INPUT_CHANGED")
            prepared_effect = self._prepare_current_effect_boundary(
                held, binding["reviewer_input"], compiled
            )
            try:
                with self._db() as db:
                    # The private-ledger writer may have waited after the
                    # Admission guard was entered.  Re-read deployment source
                    # before taking the producer's final scalar time sample.
                    intent = (
                        binding
                        | asdict(self.source)
                        | {
                            "schema_version": "karajan.reviewer-execution-intent.v1",
                            "execution_id": str(uuid.uuid4()),
                            "principal": principal,
                            "command_key": command_key,
                            "fence": 1,
                            "start_key": "reviewer-host-start:" + reviewer_operation_id,
                            "phase": "prepared",
                            "host_prepared_id": None,
                            "host_observation": None,
                            "effect_claim": None,
                            "cancel_requested": False,
                            "delivery": {"eligible": False, "state": "not_run"},
                        }
                    )
                    intent["binding_digest"] = digest(intent)
                    intent["intent_digest"] = digest(intent)
                    serialized = json.dumps(intent, sort_keys=True)
                    # All UUID/digest/JSON work is complete before this final
                    # scalar authority check and the immediately following SQL.
                    prepared_effect.assert_current()
                    db.execute(
                        "INSERT INTO reviewer_executions VALUES (?,?,?,?,?,?,?)",
                        (
                            intent["execution_id"], run_id, reviewer_operation_id, principal,
                            command_key, serialized, "prepared",
                        ),
                    )
                    return deepcopy(intent)
            except sqlite3.IntegrityError:
                # A concurrent exact replay wins by observing its canonical
                # record; a different key remains a conflict, never a second
                # compilation-derived identity.
                with self._db(write=False) as db:
                    existing = self._load(db, run_id, reviewer_operation_id, principal)
                    prior = db.execute(
                        f"SELECT {_LEDGER_COLUMNS} FROM reviewer_executions "
                        "WHERE principal=? AND command_key=?",
                        (principal, command_key),
                    ).fetchone()
                    if (
                        existing is not None
                        and prior is not None
                        and _decode_intent_row(prior) == existing
                    ):
                        return deepcopy(existing)
                raise RunError("REVIEWER_EXECUTION_ALREADY_PREPARED") from None

    def cancel(
        self, run_id: str, reviewer_operation_id: str, *, principal: str
    ) -> dict[str, Any] | None:
        """Persist cancellation; it never infers Host/native/remote completion."""
        # Validate a matching execution identity before this request can alter
        # its producer admission. In particular, an index-column tamper must
        # not turn a later cancellation into a different Run/operation error
        # or change the original admission before the common ledger boundary
        # rejects the record.
        with self._db(write=False) as db:
            self._load(db, run_id, reviewer_operation_id, principal)
        self.admissions.cancel(run_id, reviewer_operation_id, principal=principal)
        # Take the ledger writer before loading so this update cannot promote
        # an old WAL snapshot after inspect_host has committed.
        with self._db() as db:
            value = self._load(db, run_id, reviewer_operation_id, principal)
            if value is None:
                return None
            value["cancel_requested"] = True
            value["phase"] = "cancellation_pending"
            self._save(db, value)
            return deepcopy(value)

    def read(
        self, run_id: str, reviewer_operation_id: str, *, principal: str
    ) -> dict[str, Any] | None:
        with self._db(write=False) as db:
            return deepcopy(self._load(db, run_id, reviewer_operation_id, principal))

    @staticmethod
    def _serialized_save(value: dict[str, Any]) -> tuple[str, str, str]:
        value["intent_digest"] = digest({k: v for k, v in value.items() if k != "intent_digest"})
        return json.dumps(value, sort_keys=True), value["phase"], value["execution_id"]

    @staticmethod
    def _save(
        db: sqlite3.Connection,
        value: dict[str, Any],
        serialized: tuple[str, str, str] | None = None,
    ) -> None:
        payload = serialized
        if payload is None:
            payload = ReviewerExecutionIntents._serialized_save(value)
        db.execute(
            "UPDATE reviewer_executions SET intent=?,state=? WHERE execution_id=?",
            payload,
        )

    def _prepare_current_effect_boundary(
        self,
        held: dict[str, Any],
        expected_input: dict[str, Any],
        prepared_input: ReviewerInput,
    ) -> PreparedReviewerFinalEffect:
        """Revalidate a prebuilt immutable input at one producer effect guard.

        Deployment-source reads can block, so they must complete before the
        producer samples its Capacity/Reviewer/Run clocks.  The retained
        callable is valid only inside ``reviewer_reserved_effect_guard``.
        Complete Candidate snapshot/diff work happened in ``_compiled``
        before this shared producer guard was acquired.
        """
        self._assert_current_source()
        capability = held.get("final_effect_capability")
        if not isinstance(capability, ReviewerFinalEffectCapability):
            raise RunError("REVIEWER_EXECUTION_BOUNDARY_INVALID")
        return capability.prepare_current(
            expected_input, prepared_input, self._assert_current_source
        )

    def _assert_current_source(self) -> None:
        if self.current_source is not None and self.current_source() != self.source:
            raise RunError("REVIEWER_EXECUTION_SOURCE_CHANGED")

    @contextmanager
    def _current_guard(self, value: dict[str, Any]) -> Iterator[PreparedReviewerFinalEffect]:
        self._assert_current_source()
        binding, compiled = self._compiled(
            value["run_id"], value["reviewer_operation_id"], value["principal"]
        )
        expected = binding | asdict(self.source)
        if (
            any(value.get(key) != current for key, current in expected.items())
            or value["reviewer_input"]["sha256"] != compiled.content_sha256
            or value["reviewer_input"]["size"] != compiled.size
            or value["reviewer_input"]["check_evidence_ids"] != list(compiled.check_evidence_ids)
        ):
            raise RunError("REVIEWER_EXECUTION_INPUT_CHANGED")
        # The existing admission guard is deliberately held through the Host
        # boundary.  The compiler's independent reads happen first, then this
        # guard rechecks all current Reviewer/CAS/Capacity facts.
        with self.admissions.reviewer_reserved_effect_guard(
            value["run_id"], value["reviewer_operation_id"], principal=value["principal"]
        ) as held:
            prepared_effect = self._prepare_current_effect_boundary(
                held, value["reviewer_input"], compiled
            )

            yield prepared_effect

    def freeze_launch(
        self, run_id: str, reviewer_operation_id: str, *, principal: str
    ) -> dict[str, Any]:
        value = self.read(run_id, reviewer_operation_id, principal=principal)
        if value is None:
            raise RunError("REVIEWER_EXECUTION_NOT_PREPARED")
        if value["cancel_requested"]:
            raise RunError("REVIEWER_EXECUTION_CANCELLED")
        with self._current_guard(value) as prepared_effect:
            if value.get("launch") is None:
                compiled_launch = self.launch_compiler(deepcopy(value))
                launch = launch_document(
                    value, compiled_launch.process_spec, compiled_launch.bootstrap_digest
                )
            else:
                launch = value["launch"]
            snapshot = self.host.prepare(
                host_manifest(value),
                value["start_key"],
                ProcessSpec(
                    tuple(launch["process_spec"]["argv"]),
                    Path(launch["process_spec"]["cwd"]),
                    float(launch["process_spec"]["timeout_seconds"]),
                ),
                before_nonce=prepared_effect.assert_source_current,
                before_write=prepared_effect.assert_current,
            )
            control = self.host.initialize_control_once(
                value["planned_attempt_id"], prepared_id=value["start_key"], fence=value["fence"],
                authorization_ref=value["authorization_ref"],
                before_write=prepared_effect.assert_current,
            )
        if not control["dispatch_enabled"]:
            raise RunError("REVIEWER_EXECUTION_CONTROL_REVOKED")
        with self._db() as db:
            current = self._load(db, run_id, reviewer_operation_id, principal)
            if current is None or current["execution_id"] != value["execution_id"]:
                raise RunError("REVIEWER_EXECUTION_BINDING_INVALID")
            current.update(launch=launch, host_prepared_id=snapshot.prepared_id)
            if not current["cancel_requested"]:
                current["phase"] = "host_prepared"
            self._save(db, current)
            return deepcopy(current)

    def inspect_host(
        self, run_id: str, reviewer_operation_id: str, *, principal: str
    ) -> dict[str, Any]:
        with self._db(write=False) as db:
            value = self._load(db, run_id, reviewer_operation_id, principal)
            if value is None:
                raise RunError("REVIEWER_EXECUTION_HOST_PREPARE_REQUIRED")
            try:
                snapshot = self.host.inspect_original_preparation(
                    host_manifest(value), value["start_key"]
                )
            except KeyError:
                raise RunError("REVIEWER_EXECUTION_HOST_PREPARE_REQUIRED") from None
            except (LaunchDenied, ValueError):
                raise RunError("REVIEWER_EXECUTION_HOST_BINDING_MISMATCH") from None
            if value["host_prepared_id"] not in {None, snapshot.prepared_id}:
                raise RunError("REVIEWER_EXECUTION_HOST_BINDING_MISMATCH")
            observation = {
                "prepared_id": snapshot.prepared_id,
                "attempt_id": snapshot.attempt_id,
                "state": snapshot.state,
                "launch_phase": snapshot.launch_phase,
                "remote_stop": snapshot.remote_stop,
            }
            # Observations are correlation-only, including a reply lost after
            # Host preparation. They must not create a ledger receipt or
            # require a ledger writer.
            return deepcopy(value | {"host_observation": observation})

    def claim_registered_observer(
        self,
        run_id: str,
        reviewer_operation_id: str,
        *,
        principal: str,
        timeout_seconds: float = 5.0,
    ) -> dict[str, Any]:
        value = self.read(run_id, reviewer_operation_id, principal=principal)
        if value is None or value["host_prepared_id"] is None:
            raise RunError("REVIEWER_EXECUTION_HOST_PREPARE_REQUIRED")
        if value["cancel_requested"]:
            raise RunError("REVIEWER_EXECUTION_CANCELLED")
        if value["effect_claim"] is not None:
            return deepcopy(value | {"claim_allowed": False})
        # Waiting for a child never holds a ledger/business writer.  The
        # current guards are acquired again at the one-shot ledger mutation.
        runner = self.host.wait_for_runner_registration(
            value["planned_attempt_id"], timeout_seconds=timeout_seconds
        )
        with self._current_guard(value) as prepared_effect:
            launch = value.get("launch")
            if not isinstance(launch, dict):
                raise RunError("REVIEWER_EXECUTION_BINDING_INVALID")
            try:
                spec = ProcessSpec(
                    tuple(launch["process_spec"]["argv"]),
                    Path(launch["process_spec"]["cwd"]),
                    float(launch["process_spec"]["timeout_seconds"]),
                )
            except (KeyError, TypeError, ValueError):
                raise RunError("REVIEWER_EXECUTION_BINDING_INVALID") from None
            with self.host.current_runner_guard(
                value["planned_attempt_id"],
                fence=value["fence"],
                authorization_ref=value["authorization_ref"],
                expected_manifest=host_manifest(value),
                expected_spec=spec,
            ) as current:
                if current != runner:
                    raise RunError("REVIEWER_EXECUTION_RUNNER_CHANGED")
                with self._db() as db:
                    current_value = self._load(db, run_id, reviewer_operation_id, principal)
                    if (
                        current_value is None
                        or current_value["execution_id"] != value["execution_id"]
                    ):
                        raise RunError("REVIEWER_EXECUTION_BINDING_INVALID")
                    if current_value["cancel_requested"]:
                        raise RunError("REVIEWER_EXECUTION_CANCELLED")
                    if current_value["effect_claim"] is not None:
                        return deepcopy(current_value | {"claim_allowed": False})
                    current_value["effect_claim"] = {
                        "intent_digest": current_value["intent_digest"],
                        "runner": {"pid": runner.pid, "birth": runner.birth},
                    }
                    current_value["phase"] = "observer_claimed"
                    serialized = self._serialized_save(current_value)
                    # The complete persisted claim payload exists before the
                    # final scalar authority check and adjacent UPDATE.
                    prepared_effect.assert_current()
                    self._save(db, current_value, serialized)
                    return deepcopy(current_value | {"claim_allowed": True})
