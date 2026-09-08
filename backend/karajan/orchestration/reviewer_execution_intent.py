"""Durable Reviewer Host preparation, deliberately before native observation."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from karajan.candidates import CandidateStore
from karajan.execution import ProcessSpec, RunnerHost
from karajan.routing.compiler import digest
from karajan.runs import RunError

from .admission import ApprovedTaskAdmission
from .go_execution_intent import GoExecutionIntents
from .reviewer_execution_binding import compiler_binding, host_manifest, launch_document
from .reviewer_input import ReviewerInput, compile_reviewer_input


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
    ) -> None:
        self.database, self.admissions, self.candidates = Path(database), admissions, candidates
        self.source, self.host, self.launch_compiler = source, host, launch_compiler
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS reviewer_executions ("
                "execution_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, "
                "reviewer_operation_id TEXT NOT NULL, principal TEXT NOT NULL, "
                "command_key TEXT NOT NULL, intent TEXT NOT NULL, state TEXT NOT NULL, "
                "UNIQUE(principal, command_key), UNIQUE(run_id, reviewer_operation_id))"
            )

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.database, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _load(
        self, db: sqlite3.Connection, run_id: str, operation_id: str, principal: str
    ) -> dict[str, Any] | None:
        row = db.execute(
            "SELECT intent FROM reviewer_executions WHERE run_id=? AND reviewer_operation_id=?",
            (run_id, operation_id),
        ).fetchone()
        if row is None:
            return None
        value = json.loads(row["intent"])
        if not isinstance(value, dict):
            raise RunError("REVIEWER_EXECUTION_BINDING_INVALID")
        if value.get("principal") != principal or value.get("intent_digest") != digest(
            {k: v for k, v in value.items() if k != "intent_digest"}
        ):
            raise RunError("REVIEWER_EXECUTION_BINDING_INVALID")
        return value

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
        binding, _ = self._compiled(run_id, reviewer_operation_id, principal)
        with self._db() as db:
            prior = db.execute(
                "SELECT intent FROM reviewer_executions WHERE principal=? AND command_key=?",
                (principal, command_key),
            ).fetchone()
            existing = self._load(db, run_id, reviewer_operation_id, principal)
            if prior is not None:
                prior_value = json.loads(prior["intent"])
                if existing != prior_value:
                    raise RunError("IDEMPOTENCY_CONFLICT")
                if existing is None:
                    raise RunError("REVIEWER_EXECUTION_BINDING_INVALID")
                return existing
            if existing is not None:
                raise RunError("REVIEWER_EXECUTION_ALREADY_PREPARED")
            intent = (
                binding
                | asdict(self.source)
                | {
                    "schema_version": "karajan.reviewer-execution-intent.v1",
                    "execution_id": str(uuid.uuid4()),
                    "principal": principal,
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
            db.execute(
                "INSERT INTO reviewer_executions VALUES (?,?,?,?,?,?,?)",
                (
                    intent["execution_id"],
                    run_id,
                    reviewer_operation_id,
                    principal,
                    command_key,
                    json.dumps(intent, sort_keys=True),
                    "prepared",
                ),
            )
            return deepcopy(intent)

    def read(
        self, run_id: str, reviewer_operation_id: str, *, principal: str
    ) -> dict[str, Any] | None:
        with self._db() as db:
            return deepcopy(self._load(db, run_id, reviewer_operation_id, principal))

    def _save(self, db: sqlite3.Connection, value: dict[str, Any]) -> None:
        value["intent_digest"] = digest({k: v for k, v in value.items() if k != "intent_digest"})
        db.execute(
            "UPDATE reviewer_executions SET intent=?,state=? WHERE execution_id=?",
            (json.dumps(value, sort_keys=True), value["phase"], value["execution_id"]),
        )

    def _current(self, value: dict[str, Any]) -> None:
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

    def freeze_launch(
        self, run_id: str, reviewer_operation_id: str, *, principal: str
    ) -> dict[str, Any]:
        with self._db() as db:
            value = self._load(db, run_id, reviewer_operation_id, principal)
            if value is None:
                raise RunError("REVIEWER_EXECUTION_NOT_PREPARED")
            if value["cancel_requested"]:
                raise RunError("REVIEWER_EXECUTION_CANCELLED")
            self._current(value)
            if value.get("launch") is None:
                compiled_launch = self.launch_compiler(deepcopy(value))
                launch = launch_document(
                    value,
                    compiled_launch.process_spec,
                    compiled_launch.bootstrap_digest,
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
            )
            control = self.host.initialize_control_once(
                value["planned_attempt_id"],
                prepared_id=value["start_key"],
                fence=value["fence"],
                authorization_ref=value["authorization_ref"],
            )
            if not control["dispatch_enabled"]:
                raise RunError("REVIEWER_EXECUTION_CONTROL_REVOKED")
            value.update(
                launch=launch, host_prepared_id=snapshot.prepared_id, phase="host_prepared"
            )
            self._save(db, value)
            return deepcopy(value)

    def inspect_host(
        self, run_id: str, reviewer_operation_id: str, *, principal: str
    ) -> dict[str, Any]:
        with self._db() as db:
            value = self._load(db, run_id, reviewer_operation_id, principal)
            if value is None or value["host_prepared_id"] is None:
                raise RunError("REVIEWER_EXECUTION_HOST_PREPARE_REQUIRED")
            snapshot = self.host.inspect(value["planned_attempt_id"])
            if (
                snapshot.prepared_id != value["start_key"]
                or snapshot.attempt_id != value["planned_attempt_id"]
            ):
                raise RunError("REVIEWER_EXECUTION_HOST_BINDING_MISMATCH")
            value["host_observation"] = {
                "prepared_id": snapshot.prepared_id,
                "attempt_id": snapshot.attempt_id,
                "state": snapshot.state,
                "launch_phase": snapshot.launch_phase,
                "remote_stop": snapshot.remote_stop,
            }
            self._save(db, value)
            return deepcopy(value)

    def claim_registered_observer(
        self,
        run_id: str,
        reviewer_operation_id: str,
        *,
        principal: str,
        timeout_seconds: float = 5.0,
    ) -> dict[str, Any]:
        with self._db() as db:
            value = self._load(db, run_id, reviewer_operation_id, principal)
            if value is None or value["host_prepared_id"] is None:
                raise RunError("REVIEWER_EXECUTION_HOST_PREPARE_REQUIRED")
            if value["cancel_requested"]:
                raise RunError("REVIEWER_EXECUTION_CANCELLED")
            if value["effect_claim"] is not None:
                return deepcopy(value | {"claim_allowed": False})
            self._current(value)
            runner = self.host.wait_for_runner_registration(
                value["planned_attempt_id"], timeout_seconds=timeout_seconds
            )
            with self.host.current_runner_guard(
                value["planned_attempt_id"],
                fence=value["fence"],
                authorization_ref=value["authorization_ref"],
            ) as current:
                if current != runner:
                    raise RunError("REVIEWER_EXECUTION_RUNNER_CHANGED")
                value["effect_claim"] = {
                    "intent_digest": value["intent_digest"],
                    "runner": {"pid": runner.pid, "birth": runner.birth},
                }
                value["phase"] = "observer_claimed"
                self._save(db, value)
            return deepcopy(value | {"claim_allowed": True})
