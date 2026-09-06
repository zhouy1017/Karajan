"""Durable Go execution identity in the original admission operation.

This controller port records intent and observations, never process or provider
effects. A committed claim is consumed even if its response is lost. Its caller
must revalidate all business guards and the current Host runner before effects.
"""

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from karajan.execution import ProcessIdentity, RunnerHost, Snapshot
from karajan.routing.compiler import digest
from karajan.runs import RunError
from karajan.runs.planning import encoded, identifier

from .admission import ApprovedTaskAdmission
from .workspace import _approved_task


@dataclass(frozen=True)
class GoExecutionSource:
    """Actual source digests supplied by the controller's fixed source compiler."""

    runner_source_sha256: str
    native_source_sha256: str

    def __post_init__(self) -> None:
        for value in asdict(self).values():
            if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise RunError("TASK_EXECUTION_SOURCE_INVALID")


@contextmanager
def _connection(path: Path, *, readonly: bool) -> Iterator[sqlite3.Connection]:
    # Neither status nor a reconstructed controller may create a missing ledger.
    db = sqlite3.connect(
        path.resolve().as_uri() + ("?mode=ro" if readonly else "?mode=rw"),
        uri=True,
        timeout=10,
        isolation_level=None,
    )
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA query_only=ON" if readonly else "PRAGMA synchronous=FULL")
        db.execute("BEGIN" if readonly else "BEGIN IMMEDIATE")
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


class GoExecutionIntents:
    """One execution lifecycle within one already reserved Task operation.

    Public inputs identify controller-owned records. No method accepts a caller's
    Profile, prompt, budget, credential or replacement grant. Read/reconcile are
    historical reads, not proof that an external resource remains available.
    """

    def __init__(
        self, admissions: ApprovedTaskAdmission, *, source: GoExecutionSource, host: RunnerHost
    ) -> None:
        if not isinstance(source, GoExecutionSource):
            raise RunError("TASK_EXECUTION_SOURCE_INVALID")
        self.admissions, self.source, self.host = admissions, source, host

    def _owner(self, run_id: str, operation_id: str, principal: str) -> None:
        for value in (run_id, operation_id, principal):
            identifier(value)
        planner = self.admissions.routing.planner
        with _connection(planner.database, readonly=True) as db:
            planner._owner(planner._get(db, run_id), principal)

    def read(self, run_id: str, operation_id: str, *, principal: str) -> dict[str, Any]:
        """Detached persisted status, with no clock, refresh, writes or effects."""
        self._owner(run_id, operation_id, principal)
        with _connection(self.admissions.database, readonly=True) as db:
            return self.admissions._load(db, run_id, operation_id)

    def reconcile(self, run_id: str, operation_id: str, *, principal: str) -> dict[str, Any]:
        """Recover the same intent; external reconciliation belongs to its owner."""
        return self.read(run_id, operation_id, principal=principal)

    def prepare_intent(
        self, run_id: str, operation_id: str, *, principal: str, command_key: str
    ) -> dict[str, Any]:
        self._owner(run_id, operation_id, principal)
        identifier(command_key)
        payload = encoded(["prepare_go_execution", run_id, operation_id, asdict(self.source)])
        with _connection(self.admissions.database, readonly=False) as db:
            operation = self.admissions._load(db, run_id, operation_id)
            prior = db.execute(
                "SELECT payload FROM commands WHERE principal=? AND key=?",
                (principal, command_key),
            ).fetchone()
            if prior is not None and prior["payload"] != payload:
                raise RunError("IDEMPOTENCY_CONFLICT")
            if "execution" in operation:
                self._execution(operation)
            else:
                if operation["state"] != "reserved" or operation["cancel_requested"]:
                    raise RunError("TASK_EXECUTION_RESERVATION_REQUIRED")
                with self.admissions.routing.planner.activation_guard(run_id) as run:
                    _, task = _approved_task(run, operation, principal)
                    intent = self._prepare(run, operation, task)
                operation["execution"] = {
                    "schema_version": "karajan.go-task-execution-intent.v1",
                    "intent": intent,
                    "intent_digest": digest(intent),
                    "phase": "prepared",
                    "cancel_requested": False,
                    "capacity_activation": None,
                    "host_prepared_id": None,
                    "host_observation": None,
                    "effect_claim": None,
                }
                operation["state"] = "execution_pending"
                self.admissions._save(db, operation)
            if prior is None:
                db.execute(
                    "INSERT INTO commands VALUES (?,?,?,?)",
                    (principal, command_key, payload, encoded(operation)),
                )
            # A replay never brings back a pre-cancellation/pre-claim UI state.
            return operation

    def _prepare(
        self, run: dict[str, Any], operation: dict[str, Any], task: dict[str, Any]
    ) -> dict[str, Any]:
        workspace = operation.get("workspace")
        if not isinstance(workspace, dict):
            raise RunError("TASK_WORKSPACE_NOT_PREPARED")
        body = deepcopy(workspace)
        supplied_digest = body.pop("digest", None)
        if digest(body) != supplied_digest:
            raise RunError("TASK_WORKSPACE_BINDING_MISMATCH")
        input_digest = body.pop("input_sha256", None)
        if (
            digest(body) != input_digest
            or any(
                workspace[key] != operation[key]
                for key in ("run_id", "task_id", "planned_attempt_id", "planned_context_id")
            )
            or workspace["operation_id"] != operation["id"]
        ):
            raise RunError("TASK_WORKSPACE_BINDING_MISMATCH")
        source = workspace["source_binding"]
        assessment = operation["assessment"]
        selected = assessment["route"]["selected_profile"]
        profile_source = next(
            (row for row in assessment["sources"]["profiles"] if row["profile"] == selected),
            None,
        )
        if (
            source["assessment_id"] != assessment["id"]
            or source["assessment_digest"] != assessment["digest"]
            or source["approval"] != assessment["sources"]["approval"]
            or source["configuration_digest"] != run["configuration_snapshot"]["digest"]
            or source["execution_policy"] != run["execution_policy_snapshot"]
            or source["selected_profile"] != selected
            or source["profile_source"] != profile_source
        ):
            raise RunError("TASK_WORKSPACE_BINDING_MISMATCH")
        if profile_source is None or not isinstance(profile_source.get("execution_context"), dict):
            raise RunError("TASK_EXECUTION_GO_SCOPE_REQUIRED")
        request = operation["request"]
        receipt = operation["capacity_receipt"]
        if (
            receipt["decision"] != "admitted"
            or request["attempt_id"] != operation["planned_attempt_id"]
        ):
            raise RunError("TASK_EXECUTION_RESERVATION_REQUIRED")
        return {
            "run_id": run["id"],
            "operation_id": operation["id"],
            "project_id": run["project_id"],
            "owner": run["owner"],
            "task_id": task["id"],
            "attempt_id": operation["planned_attempt_id"],
            "context_id": operation["planned_context_id"],
            "fence": 1,
            "admission_id": receipt["admission_id"],
            "admission_request_digest": digest(request),
            "workspace_digest": workspace["digest"],
            "input_sha256": workspace["input_sha256"],
            "assessment_digest": assessment["digest"],
            "authorization_ref": request["authorization_ref"],
            "budget_ref": assessment["route"]["snapshots"]["task"]["authorization"]["budget_ref"],
            "execution_context": deepcopy(profile_source["execution_context"]),
            **asdict(self.source),
            "activation_key": "go-task-activate:" + operation["id"],
            "start_key": "go-task-start:" + operation["id"],
            "grant_id": "go-task-grant:" + operation["id"],
            "cancel_key": "go-task-cancel:" + operation["id"],
        }

    def _execution(self, operation: dict[str, Any]) -> dict[str, Any]:
        execution = operation.get("execution")
        if not isinstance(execution, dict):
            raise RunError("TASK_EXECUTION_NOT_PREPARED")
        intent = execution["intent"]
        if (
            execution["schema_version"] != "karajan.go-task-execution-intent.v1"
            or digest(intent) != execution["intent_digest"]
            or any(intent[key] != value for key, value in asdict(self.source).items())
        ):
            raise RunError("TASK_EXECUTION_SOURCE_CHANGED")
        if (
            intent["run_id"] != operation["run_id"]
            or intent["operation_id"] != operation["id"]
            or intent["attempt_id"] != operation["planned_attempt_id"]
            or intent["context_id"] != operation["planned_context_id"]
            or intent["workspace_digest"] != operation["workspace"]["digest"]
            or intent["admission_request_digest"] != digest(operation["request"])
        ):
            raise RunError("TASK_EXECUTION_BINDING_MISMATCH")
        return execution

    @contextmanager
    def _edit(
        self, run_id: str, operation_id: str, principal: str
    ) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
        self._owner(run_id, operation_id, principal)
        with _connection(self.admissions.database, readonly=False) as db:
            operation = self.admissions._load(db, run_id, operation_id)
            execution = self._execution(operation)
            before = encoded(operation)
            yield operation, execution
            if encoded(operation) != before:
                self.admissions._save(db, operation)

    def activation_recorded(
        self, run_id: str, operation_id: str, *, principal: str
    ) -> dict[str, Any]:
        """Read the original Capacity command after success or a lost response."""
        with self._edit(run_id, operation_id, principal) as (operation, execution):
            intent = execution["intent"]
            receipt = self.admissions.routing.capacity.command_receipt(
                "activate",
                {"admission_id": intent["admission_id"]},
                command_key=intent["activation_key"],
            )
            if receipt is None:
                return operation
            if execution["capacity_activation"] is not None:
                if execution["capacity_activation"] != receipt:
                    raise RunError("TASK_EXECUTION_ACTIVATION_CONFLICT")
                return operation
            execution["capacity_activation"] = receipt
            if receipt["decision"] != "capacity_revalidated":
                execution["phase"] = "activation_rejected"
                if not operation["cancel_requested"]:
                    operation["state"] = "execution_unknown"
                operation["reason_codes"] = receipt["reason_codes"]
            elif execution["phase"] == "prepared":
                execution["phase"] = "activated"
            return operation

    def record_host_prepared(
        self, run_id: str, operation_id: str, *, principal: str
    ) -> dict[str, Any]:
        with self._edit(run_id, operation_id, principal) as (operation, execution):
            snapshot = self.host.inspect(execution["intent"]["attempt_id"])
            self._host_identity(execution, snapshot)
            if operation["cancel_requested"]:
                raise RunError("TASK_EXECUTION_CANCEL_REQUESTED")
            activation = execution["capacity_activation"]
            if activation is None or activation["decision"] != "capacity_revalidated":
                raise RunError("TASK_EXECUTION_ACTIVATION_REQUIRED")
            execution["host_prepared_id"] = snapshot.prepared_id
            return operation

    def mark_start_unknown(
        self, run_id: str, operation_id: str, *, principal: str
    ) -> dict[str, Any]:
        """Commit the one Host launch intent before calling Host.start."""
        with self._edit(run_id, operation_id, principal) as (operation, execution):
            if operation["cancel_requested"]:
                raise RunError("TASK_EXECUTION_CANCEL_REQUESTED")
            if execution["host_prepared_id"] is None:
                raise RunError("TASK_EXECUTION_HOST_PREPARE_REQUIRED")
            if execution["phase"] == "activated":
                execution["phase"] = "start_unknown"
                operation["state"] = "execution_unknown"
            return operation

    @staticmethod
    def _host_identity(execution: dict[str, Any], snapshot: Snapshot) -> None:
        if not isinstance(snapshot, Snapshot) or (
            snapshot.prepared_id != execution["intent"]["start_key"]
            or snapshot.attempt_id != execution["intent"]["attempt_id"]
        ):
            raise RunError("TASK_EXECUTION_HOST_BINDING_MISMATCH")

    def host_started(self, run_id: str, operation_id: str, *, principal: str) -> dict[str, Any]:
        """A late Host reply is observation only, never a new launch permission."""
        with self._edit(run_id, operation_id, principal) as (operation, execution):
            snapshot = self.host.inspect(execution["intent"]["attempt_id"])
            self._host_identity(execution, snapshot)
            if execution["host_prepared_id"] is None:
                raise RunError("TASK_EXECUTION_HOST_PREPARE_REQUIRED")
            execution["host_observation"] = {
                "prepared_id": snapshot.prepared_id,
                "attempt_id": snapshot.attempt_id,
                "state": snapshot.state,
                "launch_phase": snapshot.launch_phase,
                "remote_stop": snapshot.remote_stop,
            }
            return operation

    def effect_start_claim(
        self, run_id: str, operation_id: str, *, principal: str, runner: ProcessIdentity
    ) -> dict[str, Any]:
        """Commit once, after reading/releasing Host's actual runner guard.

        A true return exists only on the first live call. Every replay, including
        the same PID/birth, returns false. Recheck the identity under the complete
        business guards before native.start; this claim alone permits no effect.
        """
        _runner(runner)
        with self._edit(run_id, operation_id, principal) as (operation, execution):
            if operation["cancel_requested"]:
                raise RunError("TASK_EXECUTION_CANCEL_REQUESTED")
            prior = execution["effect_claim"]
            if prior is not None:
                result = deepcopy(operation)
                result["claim_allowed"] = False
                return result
            if execution["phase"] != "start_unknown":
                raise RunError("TASK_EXECUTION_START_INTENT_REQUIRED")
            execution["effect_claim"] = {
                "intent_digest": execution["intent_digest"],
                "runner": asdict(runner),
            }
            execution["phase"] = "effect_claimed"
            operation["state"] = "executing"
            result = deepcopy(operation)
            result["claim_allowed"] = True
            return result

    @contextmanager
    def _guard(
        self, run_id: str, operation_id: str, principal: str
    ) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
        self._owner(run_id, operation_id, principal)
        with _connection(self.admissions.database, readonly=False) as db:
            db.execute("PRAGMA query_only=ON")
            operation = self.admissions._load(db, run_id, operation_id)
            execution = self._execution(operation)
            if operation["cancel_requested"]:
                raise RunError("TASK_EXECUTION_CANCEL_REQUESTED")
            yield operation, execution

    @contextmanager
    def startup_guard(
        self, run_id: str, operation_id: str, *, principal: str
    ) -> Iterator[dict[str, Any]]:
        """Hold original launch intent before Run/Project/Capacity/Host guards.

        This is not fresh capacity or Host authority and must not surround a wait
        for child registration. It does not permit replaying a native start.
        """
        with self._guard(run_id, operation_id, principal) as (operation, execution):
            activation = execution["capacity_activation"]
            if (
                execution["phase"] != "start_unknown"
                or execution["effect_claim"] is not None
                or execution["host_prepared_id"] != execution["intent"]["start_key"]
                or activation is None
                or activation["decision"] != "capacity_revalidated"
            ):
                raise RunError("TASK_EXECUTION_START_INTENT_REQUIRED")
            yield operation

    @contextmanager
    def effect_claim_guard(
        self, run_id: str, operation_id: str, *, principal: str, runner: ProcessIdentity
    ) -> Iterator[dict[str, Any]]:
        """Hold operation before Run/Project/Capacity/Host, without ledger writes."""
        _runner(runner)
        with self._guard(run_id, operation_id, principal) as (operation, execution):
            if execution["phase"] != "effect_claimed" or execution["effect_claim"] != {
                "intent_digest": execution["intent_digest"],
                "runner": asdict(runner),
            }:
                raise RunError("TASK_EXECUTION_CLAIM_NOT_CURRENT")
            yield operation

    def cancel_intent(self, run_id: str, operation_id: str, *, principal: str) -> dict[str, Any]:
        """Persist through the common cancellation path; cleanup is separate."""
        self._owner(run_id, operation_id, principal)
        return self.admissions.cancel(run_id, operation_id, principal=principal)


def _runner(runner: ProcessIdentity) -> None:
    if (
        not isinstance(runner, ProcessIdentity)
        or type(runner.pid) is not int
        or runner.pid <= 0
        or not isinstance(runner.birth, str)
        or not runner.birth
        or len(runner.birth) > 256
    ):
        raise RunError("TASK_EXECUTION_RUNNER_INVALID")
