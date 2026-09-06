"""Approved Candidate checks use the original operation, never caller commands."""

import hashlib
import json
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from karajan.candidates import CandidateStore
from karajan.execution import (
    Activation,
    CheckAttemptManifest,
    ProcessIdentity,
    ProcessSpec,
    RunnerHost,
)
from karajan.routing.compiler import digest
from karajan.runs import RunError

from .admission import ApprovedTaskAdmission
from .candidate_subjects import (
    assert_cycle_quiescent,
    candidate_identity,
    check_is_current,
    current_subject,
    cycle_for_check,
    cycles,
    parse_transition,
    transition_pending,
)
from .execution_budget import claim_process, current_process
from .go_execution_intent import GoExecutionIntents, _connection
from .go_task_collector import _validation_policy
from .workspace import _approved_task


@dataclass(frozen=True)
class CheckLaunchSpec:
    process_spec: ProcessSpec
    bootstrap_digest: str


_LIFECYCLE = {
    "phase",
    "execution_digest",
    "launch",
    "host_prepared_id",
    "host_observation",
    "native_claim",
    "observation",
    "evidence_request",
    "evidence",
    "reason_codes",
    "evidence_submit_claim",
    "cleanup",
}


def execution_document(row: dict[str, Any]) -> dict[str, Any]:
    """Internal immutable runtime input, excluding the operation's changing state."""
    return deepcopy({key: value for key, value in row.items() if key not in _LIFECYCLE})


class ApprovedCandidateChecks:
    """Read and advance trusted checks without reopening native model execution.

    A historical reader needs only existing stores. The separately configured
    fixed runner is required for a new check effect, not to read old evidence.
    """

    def __init__(
        self,
        admissions: ApprovedTaskAdmission,
        candidates: CandidateStore,
        *,
        runner: Any = None,
        host: RunnerHost | None = None,
        launch_compiler: Callable[[dict[str, Any]], CheckLaunchSpec] | None = None,
        controller_source: Callable[[], dict[str, Any]] | None = None,
        subject_validator: Callable[..., None] | None = None,
    ) -> None:
        self.admissions, self.candidates, self.runner = admissions, candidates, runner
        self.host, self.launch_compiler = host, launch_compiler
        self.controller_source = controller_source
        self.subject_validator = subject_validator

    def get(self, run_id: str, operation_id: str, *, principal: str) -> dict[str, Any] | None:
        operation = GoExecutionIntents.read_operation(
            self.admissions, run_id, operation_id, principal=principal
        )
        return deepcopy(operation.get("validation"))

    def cancel(self, run_id: str, operation_id: str, *, principal: str) -> dict[str, Any] | None:
        # Share the operation's cancellation intent. It commits before optional
        # Host/runtime cleanup, and does not release unknown Worker usage.
        self.admissions.cancel(run_id, operation_id, principal=principal)
        return self.reconcile(run_id, operation_id, principal=principal)

    def reconcile(self, run_id: str, operation_id: str, *, principal: str) -> dict[str, Any] | None:
        original = GoExecutionIntents.read_operation(
            self.admissions, run_id, operation_id, principal=principal
        )
        if original.get("validation") is None:
            return None
        # Every external read/stop happens without the operation lock. A missing
        # optional runtime/Host does not prevent recovery of a committed Evidence.
        for row in [row for cycle in cycles(original) for row in cycle["checks"]["runs"]]:
            execution = execution_document(row)
            if (
                row.get("host_prepared_id") is not None
                and row.get("observation") is None
                and self.host is not None
            ):
                try:
                    snapshot = self.host.inspect(row["attempt_id"])
                    if (
                        snapshot.prepared_id != row["start_key"]
                        or snapshot.attempt_id != row["attempt_id"]
                    ):
                        raise RunError("CANDIDATE_CHECK_HOST_IDENTITY_CHANGED")
                    if snapshot.state == "exited" and row["phase"] in {
                        "host_start_claimed",
                        "host_started",
                        "native_claimed",
                    }:
                        self._history_reason(
                            run_id,
                            operation_id,
                            row["check_run_id"],
                            "CANDIDATE_CHECK_HOST_EXITED_WITHOUT_RESULT",
                        )
                except Exception:
                    self._history_reason(
                        run_id,
                        operation_id,
                        row["check_run_id"],
                        "CANDIDATE_CHECK_HOST_UNAVAILABLE",
                    )
            if (
                row.get("native_claim") is not None
                and row.get("observation") is None
                and self.runner is not None
            ):
                try:
                    observed = self.runner.inspect(execution)
                    if observed is not None:
                        self._save_observation(
                            run_id, operation_id, row["check_run_id"], execution, observed
                        )
                except Exception:
                    self._history_reason(
                        run_id,
                        operation_id,
                        row["check_run_id"],
                        "CANDIDATE_CHECK_RESULT_UNAVAILABLE",
                    )
            if row.get("evidence_submit_claim") is not None and row.get("evidence") is None:
                self._recover_evidence(run_id, operation_id, row)
            elif (
                row["phase"] == "observed"
                and self.runner is not None
                and not check_is_current(original, row["check_run_id"])
            ):
                self._submit_evidence(run_id, operation_id, row["check_run_id"])
            if original["cancel_requested"]:
                self._cleanup(run_id, operation_id, row)
        with _connection(self.admissions.database, readonly=False) as db:
            operation = self.admissions._load(db, run_id, operation_id)
            validation = operation.get("validation")
            if validation is None:
                return None
            before = json.dumps(validation, sort_keys=True)
            if operation["cancel_requested"]:
                for row in [row for cycle in cycles(operation) for row in cycle["checks"]["runs"]]:
                    if row["phase"] in {"prepared", "claimed"}:
                        row["phase"] = "cancelled"
            self._summarize(operation)
            if json.dumps(validation, sort_keys=True) != before:
                self.admissions._save(db, operation)
            return dict(deepcopy(validation))

    def advance(self, run_id: str, operation_id: str, *, principal: str) -> dict[str, Any]:
        GoExecutionIntents._check_owner(self.admissions, run_id, operation_id, principal)
        operation = GoExecutionIntents.read_operation(
            self.admissions, run_id, operation_id, principal=principal
        )
        if transition_pending(operation):
            transition = parse_transition(operation)
            if transition is not None and transition["phase"] == "ready":
                return self._install_subject(run_id, operation_id, principal)
            return self.reconcile(run_id, operation_id, principal=principal) or {}
        history = self.get(run_id, operation_id, principal=principal)
        if history is not None:
            observed = next(
                (row for row in history["checks"]["runs"] if row["phase"] == "observed"), None
            )
            if observed is not None:
                self._submit_evidence(run_id, operation_id, observed["check_run_id"])
                return self.get(run_id, operation_id, principal=principal) or history
            if history["checks"]["phase"] in {
                "checks_passed",
                "blocked",
                "cancelled",
                "cancellation_pending",
            }:
                return self.reconcile(run_id, operation_id, principal=principal) or history
        if history is not None and any(
            row["phase"]
            in {
                "host_start_claimed",
                "host_started",
                "native_claimed",
                "evidence_submit_claimed",
                "reconciliation_required",
            }
            for row in history["checks"]["runs"]
        ):
            return self.reconcile(run_id, operation_id, principal=principal) or history
        start_after = None
        with _connection(self.admissions.database, readonly=False) as db:
            operation = self.admissions._load(db, run_id, operation_id)
            if transition_pending(operation):
                return dict(deepcopy(operation["validation"]))
            with self._current_run(operation, principal) as run:
                fresh = self._prepare(run, operation, principal)
                if "validation" not in operation:
                    operation["validation"] = fresh
                else:
                    validation = operation["validation"]
                    if fresh["subject"] != validation["subject"] or any(
                        fresh_row[key] != old_row[key]
                        for fresh_row, old_row in zip(
                            fresh["checks"]["runs"], validation["checks"]["runs"], strict=True
                        )
                        for key in ("check", "environment", "source")
                    ):
                        raise RunError("CANDIDATE_CHECKS_BINDING_CHANGED")
                    pending = next(
                        (row for row in validation["checks"]["runs"] if row["phase"] != "recorded"),
                        None,
                    )
                    if pending is not None:
                        if pending["phase"] == "prepared":
                            self._claim(db, run, operation, pending)
                        elif pending["phase"] == "claimed":
                            self._check_budget(db, run, operation, pending)
                            self._prepare_host(pending)
                            validation["checks"]["phase"] = pending["phase"]
                        elif pending["phase"] == "host_prepared":
                            self._check_budget(db, run, operation, pending)
                            pending["phase"] = "host_start_claimed"
                            validation["checks"]["phase"] = pending["phase"]
                            start_after = pending["check_run_id"]
            self.admissions._save(db, operation)
            result = dict(deepcopy(operation["validation"]))
        # The effect claim is committed before RunnerHost can accept/start.
        # A lost response from this stage is recovered through inspect only.
        if start_after is not None:
            self._start_host(run_id, operation_id, start_after, principal)
            return self.get(run_id, operation_id, principal=principal) or result
        return result

    def _install_subject(self, run_id: str, operation_id: str, principal: str) -> dict[str, Any]:
        with _connection(self.admissions.database, readonly=False) as db:
            operation = self.admissions._load(db, run_id, operation_id)
            if operation["cancel_requested"] or operation.get("execution", {}).get(
                "cancel_requested"
            ):
                raise RunError("CANDIDATE_CHECKS_CANCELLED")
            transition = parse_transition(operation)
            if transition is None or transition["phase"] == "installed":
                return dict(deepcopy(operation["validation"]))
            if transition["phase"] != "ready":
                raise RunError("REVIEW_SUBJECT_TRANSITION_PENDING")
            with self._current_run(operation, principal) as run:
                previous = operation["validation"]
                current = current_subject(operation, self.candidates)
                if transition["expected_subject_digest"] != digest(
                    current["subject"]
                ) or transition["binding"]["source_candidate"] != candidate_identity(
                    current["candidate"]
                ):
                    raise RunError("REVIEW_SUBJECT_BINDING_MISMATCH")
                assert_cycle_quiescent(operation)
                candidate = self.candidates.lookup_review_rebind(
                    transition["binding"], command_key=transition["command_key"]
                )
                if candidate is None or candidate_identity(candidate) != transition["receipt"]:
                    raise RunError("REVIEW_SUBJECT_RECEIPT_MISMATCH")
                subject = deepcopy(previous["subject"])
                subject.update(
                    revision=subject["revision"] + 1, candidate=candidate_identity(candidate)
                )
                installed = deepcopy(transition)
                installed["phase"] = "installed"
                prospective = deepcopy(operation)
                prospective["validation"] = {
                    "subject": subject,
                    "review_binding": installed,
                    "subject_transition": installed,
                }
                fresh = self._prepare(run, prospective, principal)
                archive = {
                    key: deepcopy(value)
                    for key, value in previous.items()
                    if key not in {"history", "subject_transition", "intent_history"}
                }
                fresh.update(
                    history=[*deepcopy(previous.get("history", [])), archive],
                    review_binding=installed,
                    subject_transition=deepcopy(installed),
                )
                if "intent_history" in previous:
                    fresh["intent_history"] = deepcopy(previous["intent_history"])
                operation["validation"] = fresh
                self.admissions._save(db, operation)
                return dict(deepcopy(fresh))

    @staticmethod
    def _summarize(operation: dict[str, Any], validation: dict[str, Any] | None = None) -> None:
        if validation is None:
            validation = operation["validation"]
        rows = validation["checks"]["runs"]
        if operation["cancel_requested"]:
            stopped = all(
                row["phase"] == "cancelled"
                or (row.get("observation") or {}).get("local_stop") in {"confirmed", "not_started"}
                or (
                    row.get("cleanup", {}).get("native", {}).get("local_stop")
                    in {"confirmed", "not_started"}
                )
                or (
                    row.get("native_claim") is None
                    and row.get("cleanup", {}).get("host", {}).get("status") == "confirmed"
                )
                for row in rows
            )
            phase = "cancelled" if stopped else "cancellation_pending"
        elif any(
            row.get("evidence") and row["evidence"]["status"] not in {"passed", "failed"}
            for row in rows
        ):
            phase = "blocked"
        elif all(row["phase"] == "recorded" for row in rows):
            phase = (
                "checks_passed"
                if all(row["evidence"]["status"] == "passed" for row in rows)
                else "blocked"
            )
        else:
            phase = next(row["phase"] for row in rows if row["phase"] != "recorded")
            if phase in {
                "host_start_claimed",
                "host_started",
                "native_claimed",
                "evidence_submit_claimed",
            } and any(row.get("reason_codes") and row.get("observation") is None for row in rows):
                phase = "reconciliation_required"
            if phase == "evidence_submit_claimed" and any(
                row.get("reason_codes") and row.get("evidence") is None for row in rows
            ):
                phase = "reconciliation_required"
        validation["checks"]["phase"] = phase
        # #95 owns the independent Reviewer. Historical Check evidence cannot
        # turn itself into a complete local or delivery gate.
        validation["local_gate_passed"] = False
        validation["delivery_eligible"] = False

    def _history_reason(self, run_id: str, operation_id: str, check_run_id: str, code: str) -> None:
        with _connection(self.admissions.database, readonly=False) as db:
            operation = self.admissions._load(db, run_id, operation_id)
            row = self._row(operation, check_run_id)
            reasons = row.setdefault("reason_codes", [])
            if code not in reasons:
                reasons.append(code)
                self.admissions._save(db, operation)

    def _cleanup(self, run_id: str, operation_id: str, row: dict[str, Any]) -> None:
        cleanup = deepcopy(row.get("cleanup", {}))
        if row.get("native_claim") is not None and self.runner is not None:
            try:
                observed = self.runner.cancel(execution_document(row))
                cleanup["native"] = {
                    "local_stop": observed.local_stop if observed is not None else "unknown"
                }
                if observed is not None and row.get("observation") is None:
                    self._save_observation(
                        run_id, operation_id, row["check_run_id"], execution_document(row), observed
                    )
            except Exception:
                cleanup["native"] = {
                    "local_stop": "unknown",
                    "reason": "CANDIDATE_CHECK_CLEANUP_UNAVAILABLE",
                }
        if row.get("host_prepared_id") is not None and self.host is not None:
            try:
                stopped = self.host.cancel(
                    row["attempt_id"],
                    "cancel:" + row["check_run_id"],
                    expected_binding={
                        "prepared_id": row["start_key"],
                        "manifest": row["launch"]["manifest"],
                        "process_spec": row["launch"]["process_spec"],
                    },
                )
                cleanup["host"] = json.loads(json.dumps(asdict(stopped)))
            except Exception:
                cleanup["host"] = {
                    "local_stop": "unknown",
                    "reason": "CANDIDATE_CHECK_HOST_CLEANUP_UNAVAILABLE",
                }
        if cleanup:
            with _connection(self.admissions.database, readonly=False) as db:
                operation = self.admissions._load(db, run_id, operation_id)
                current = self._row(operation, row["check_run_id"])
                if current.get("cleanup") != cleanup:
                    current["cleanup"] = cleanup
                    self.admissions._save(db, operation)

    def _recover_evidence(self, run_id: str, operation_id: str, row: dict[str, Any]) -> None:
        claim = row["evidence_submit_claim"]
        try:
            evidence = self.candidates.lookup_evidence(
                row["evidence_request"],
                kind="check",
                log_sha256=claim["log_sha256"],
                log_size=claim["log_size"],
            )
        except Exception:
            self._history_reason(
                run_id, operation_id, row["check_run_id"], "CANDIDATE_CHECK_EVIDENCE_UNAVAILABLE"
            )
            return
        if evidence is not None:
            self._link_evidence(run_id, operation_id, row["check_run_id"], evidence)

    def _link_evidence(
        self, run_id: str, operation_id: str, check_run_id: str, evidence: dict[str, Any]
    ) -> None:
        with _connection(self.admissions.database, readonly=False) as db:
            operation = self.admissions._load(db, run_id, operation_id)
            row = self._row(operation, check_run_id)
            claim = row["evidence_submit_claim"]
            if evidence["input"] != row["evidence_request"] or evidence["kind"] != "check":
                raise RunError("CANDIDATE_CHECK_EVIDENCE_MISMATCH")
            log = evidence["log"]
            if (log["sha256"] if log else None) != claim["log_sha256"] or (
                log["size"] if log else None
            ) != claim["log_size"]:
                raise RunError("CANDIDATE_CHECK_EVIDENCE_MISMATCH")
            if row.get("evidence") is not None and row["evidence"] != evidence:
                raise RunError("CANDIDATE_CHECK_EVIDENCE_CONFLICT")
            row["evidence"] = deepcopy(evidence)
            row["phase"] = "recorded"
            self._summarize(operation, cycle_for_check(operation, check_run_id))
            self.admissions._save(db, operation)

    def _submit_evidence(self, run_id: str, operation_id: str, check_run_id: str) -> None:
        from karajan.candidates.models import CheckResult
        from karajan.isolation.check_runner import CheckObservation

        with _connection(self.admissions.database, readonly=True) as db:
            original = self.admissions._load(db, run_id, operation_id)
            row = deepcopy(self._row(original, check_run_id))
        if row.get("evidence_submit_claim") is not None:
            self._recover_evidence(run_id, operation_id, row)
            return
        observation = CheckObservation(**row["observation"])
        execution = execution_document(row)
        if observation.execution_digest != digest(execution):
            raise RunError("CANDIDATE_CHECK_OBSERVATION_MISMATCH")
        log = self.runner.read_log(execution, observation) if self.runner is not None else None
        if log is not None and (
            type(log) is not bytes
            or len(log) != observation.log_size
            or hashlib.sha256(log).hexdigest() != observation.log_sha256
        ):
            raise RunError("CANDIDATE_CHECK_LOG_MISMATCH")
        completed = (
            observation.local_stop == "confirmed" and observation.log_complete and log is not None
        )
        request = CheckResult(
            evidence_key=row["evidence_key"],
            candidate_id=row["candidate"]["id"],
            policy_sha256=row["candidate"]["policy_sha256"],
            input_sha256=row["candidate"]["input_sha256"],
            environment_sha256=row["environment"]["source_sha256"],
            observation_ref=observation.observation_ref,
            provenance="trusted_observation",
            check_id=row["check"]["id"],
            check_revision=row["check"]["revision"],
            executor_ref=observation.executor_ref,
            exit_code=observation.exit_code,
            outcome=observation.outcome if completed else "unknown",
        ).model_dump()
        claim = {
            "request_digest": digest(request),
            "log_sha256": hashlib.sha256(log).hexdigest() if log is not None else None,
            "log_size": len(log) if log is not None else None,
        }
        with _connection(self.admissions.database, readonly=False) as db:
            operation = self.admissions._load(db, run_id, operation_id)
            current = self._row(operation, check_run_id)
            if current.get("evidence_submit_claim") is not None:
                return
            if (
                current["observation"] != row["observation"]
                or current["execution_digest"] != row["execution_digest"]
            ):
                raise RunError("CANDIDATE_CHECK_OBSERVATION_CONFLICT")
            current["evidence_request"], current["evidence_submit_claim"] = request, claim
            current["phase"] = "evidence_submit_claimed"
            self._summarize(operation, cycle_for_check(operation, check_run_id))
            self.admissions._save(db, operation)
        # This sole submission follows a durable once-claim. Recovery reads the
        # complete request+log identity; it never retries an uncertain submission.
        try:
            evidence = self.candidates.record_check(request, log=log)
        except Exception:
            self._history_reason(
                run_id, operation_id, check_run_id, "CANDIDATE_CHECK_EVIDENCE_RESPONSE_UNKNOWN"
            )
            return
        self._link_evidence(run_id, operation_id, check_run_id, evidence)

    @contextmanager
    def _effect_guard(
        self, run_id: str, operation_id: str, check_run_id: str, principal: str
    ) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
        GoExecutionIntents._check_owner(self.admissions, run_id, operation_id, principal)
        with _connection(self.admissions.database, readonly=False) as db:
            db.execute("PRAGMA query_only=ON")
            operation = self.admissions._load(db, run_id, operation_id)
            if transition_pending(operation) or not check_is_current(operation, check_run_id):
                raise RunError("CANDIDATE_CHECK_SUBJECT_NOT_CURRENT")
            row = self._row(operation, check_run_id)
            with self._current_run(operation, principal) as run:
                fresh = self._prepare(run, operation, principal)
                original = operation["validation"]
                fresh_row = next(
                    item
                    for item in fresh["checks"]["runs"]
                    if item["check"]["id"] == row["check"]["id"]
                )
                if fresh["subject"] != original["subject"] or any(
                    fresh_row[key] != row[key] for key in ("check", "environment", "source")
                ):
                    raise RunError("CANDIDATE_CHECKS_BINDING_CHANGED")
                self._check_budget(db, run, operation, row)
                yield operation, row

    @staticmethod
    def _row(operation: dict[str, Any], check_run_id: str) -> dict[str, Any]:
        for row in cycle_for_check(operation, check_run_id)["checks"]["runs"]:
            if row["check_run_id"] == check_run_id:
                current: dict[str, Any] = row
                return current
        raise RunError("CANDIDATE_CHECK_NOT_FOUND")

    def _start_host(
        self, run_id: str, operation_id: str, check_run_id: str, principal: str
    ) -> None:
        if self.host is None:
            raise RunError("CANDIDATE_CHECK_HOST_REQUIRED")
        with self._effect_guard(run_id, operation_id, check_run_id, principal) as (_, row):
            if row["phase"] != "host_start_claimed" or row["native_claim"] is not None:
                raise RunError("CANDIDATE_CHECK_START_ALREADY_CLAIMED")
            snapshot = self.host.start(row["start_key"], Activation(**row["launch"]["activation"]))
        with _connection(self.admissions.database, readonly=False) as db:
            operation = self.admissions._load(db, run_id, operation_id)
            current = self._row(operation, check_run_id)
            current["host_observation"] = asdict(snapshot)
            if current["phase"] == "host_start_claimed":
                current["phase"] = "host_started"
                cycle_for_check(operation, check_run_id)["checks"]["phase"] = current["phase"]
            self.admissions._save(db, operation)

    def consume_check(
        self,
        run_id: str,
        operation_id: str,
        check_run_id: str,
        *,
        principal: str,
        runner_identity: ProcessIdentity,
    ) -> dict[str, Any] | None:
        """Internal fixed Host child entry; no report or execution JSON ingress."""
        original = GoExecutionIntents.read_operation(
            self.admissions, run_id, operation_id, principal=principal
        )
        row = self._row(original, check_run_id)
        if not check_is_current(original, check_run_id) or transition_pending(original):
            return self.reconcile(run_id, operation_id, principal=principal)
        if row.get("native_claim") is not None or row.get("observation") is not None:
            return self.reconcile(run_id, operation_id, principal=principal)
        if self.host is None or self.runner is None:
            raise RunError("CANDIDATE_CHECK_RUNNER_REQUIRED")
        # The supervisor's direct-child registration can lag this entry. Never
        # wait while holding operation/Run/Project transactions.
        actual = self.host.wait_for_runner_registration(row["attempt_id"], timeout_seconds=10)
        if actual != runner_identity:
            raise RunError("CANDIDATE_CHECK_RUNNER_CHANGED")
        with self._effect_guard(run_id, operation_id, check_run_id, principal) as (_, held):
            with self.host.current_runner_guard(
                held["attempt_id"], fence=held["fence"], authorization_ref=held["authorization_ref"]
            ) as current:
                if current != actual:
                    raise RunError("CANDIDATE_CHECK_RUNNER_CHANGED")
        with _connection(self.admissions.database, readonly=False) as db:
            operation = self.admissions._load(db, run_id, operation_id)
            if transition_pending(operation) or not check_is_current(operation, check_run_id):
                raise RunError("CANDIDATE_CHECK_SUBJECT_NOT_CURRENT")
            held = self._row(operation, check_run_id)
            if operation["cancel_requested"]:
                raise RunError("CANDIDATE_CHECKS_CANCELLED")
            if held.get("native_claim") is not None:
                return dict(deepcopy(operation["validation"]))
            if held["phase"] not in {"host_start_claimed", "host_started"}:
                raise RunError("CANDIDATE_CHECK_START_INTENT_REQUIRED")
            execution = execution_document(held)
            if digest(execution) != held["execution_digest"]:
                raise RunError("CANDIDATE_CHECK_EXECUTION_CHANGED")
            held["native_claim"] = {
                "execution_digest": held["execution_digest"],
                "runner": asdict(actual),
            }
            held["phase"] = "native_claimed"
            operation["validation"]["checks"]["phase"] = "native_claimed"
            self.admissions._save(db, operation)
        # An exception or lost reply after the preceding commit cannot turn into
        # another native start. A new child may only inspect the original result.
        try:
            observed = self.runner.run(
                execution,
                start_guard=lambda: self._native_guard(
                    run_id, operation_id, check_run_id, principal, actual
                ),
                cancelled=lambda: self._cancelled(run_id, operation_id, principal),
            )
            self._save_observation(run_id, operation_id, check_run_id, execution, observed)
        except Exception as error:
            with _connection(self.admissions.database, readonly=False) as db:
                operation = self.admissions._load(db, run_id, operation_id)
                failed = self._row(operation, check_run_id)
                failed["phase"] = "reconciliation_required"
                failed["reason_codes"] = [
                    error.code
                    if isinstance(error, RunError)
                    else "CANDIDATE_CHECK_EXECUTION_FAILED"
                ]
                cycle_for_check(operation, check_run_id)["checks"]["phase"] = (
                    "reconciliation_required"
                )
                self.admissions._save(db, operation)
            raise RunError("CANDIDATE_CHECK_EXECUTION_FAILED") from None
        return self.get(run_id, operation_id, principal=principal)

    @contextmanager
    def _native_guard(
        self,
        run_id: str,
        operation_id: str,
        check_run_id: str,
        principal: str,
        runner: ProcessIdentity,
    ) -> Iterator[None]:
        if self.host is None:
            raise RunError("CANDIDATE_CHECK_HOST_REQUIRED")
        with self._effect_guard(run_id, operation_id, check_run_id, principal) as (_, row):
            if row.get("native_claim") != {
                "execution_digest": row["execution_digest"],
                "runner": asdict(runner),
            }:
                raise RunError("CANDIDATE_CHECK_NATIVE_CLAIM_CHANGED")
            with self.host.current_runner_guard(
                row["attempt_id"], fence=row["fence"], authorization_ref=row["authorization_ref"]
            ) as current:
                if current != runner:
                    raise RunError("CANDIDATE_CHECK_RUNNER_CHANGED")
                now = self.admissions.routing.planner.clock()
                if now < row["claimed_at"] or now >= row["deadline"]:
                    raise RunError("CANDIDATE_CHECK_DEADLINE_EXPIRED")
                yield

    def _cancelled(self, run_id: str, operation_id: str, principal: str) -> bool:
        try:
            operation = GoExecutionIntents.read_operation(
                self.admissions, run_id, operation_id, principal=principal
            )
            if operation["cancel_requested"]:
                return True
            planner = self.admissions.routing.planner
            with _connection(planner.database, readonly=True) as db:
                run = planner._get(db, run_id)
                return bool(
                    run["state"] != "executing"
                    or run["active_plan_revision"]
                    != operation["workspace"]["source_binding"]["plan"]["plan_revision"]
                )
        except Exception:
            return True

    def _save_observation(
        self,
        run_id: str,
        operation_id: str,
        check_run_id: str,
        execution: dict[str, Any],
        observed: Any,
    ) -> None:
        from karajan.isolation.check_runner import CheckObservation

        if (
            type(observed) is not CheckObservation
            or observed.execution_digest != digest(execution)
            or observed.environment_sha256 != execution["environment"]["source_sha256"]
        ):
            raise RunError("CANDIDATE_CHECK_OBSERVATION_MISMATCH")
        document = json.loads(json.dumps(asdict(observed), allow_nan=False))
        if (
            observed.outcome not in {"completed", "timed_out", "cancelled", "unknown"}
            or observed.local_stop not in {"confirmed", "unknown", "not_started"}
            or type(observed.log_complete) is not bool
            or type(observed.log_size) is not int
            or not 0 <= observed.log_size <= execution["environment"]["max_log_bytes"]
            or (observed.exit_code is not None and type(observed.exit_code) is not int)
            or (
                observed.log_sha256 is not None
                and (
                    not isinstance(observed.log_sha256, str)
                    or len(observed.log_sha256) != 64
                    or any(char not in "0123456789abcdef" for char in observed.log_sha256)
                )
            )
        ):
            raise RunError("CANDIDATE_CHECK_OBSERVATION_MISMATCH")
        with _connection(self.admissions.database, readonly=False) as db:
            operation = self.admissions._load(db, run_id, operation_id)
            row = self._row(operation, check_run_id)
            if (
                row["execution_digest"] != observed.execution_digest
                or row.get("native_claim") is None
            ):
                raise RunError("CANDIDATE_CHECK_OBSERVATION_MISMATCH")
            if row.get("observation") is not None and row["observation"] != document:
                raise RunError("CANDIDATE_CHECK_OBSERVATION_CONFLICT")
            row["observation"] = document
            row["phase"] = "observed"
            cycle_for_check(operation, check_run_id)["checks"]["phase"] = "observed"
            self.admissions._save(db, operation)

    @contextmanager
    def _current_run(self, operation: dict[str, Any], principal: str) -> Iterator[dict[str, Any]]:
        planner = self.admissions.routing.planner
        with _connection(planner.database, readonly=False) as run_db:
            run_db.execute("PRAGMA query_only=ON")
            run = planner._get(run_db, operation["run_id"])
            _approved_task(run, operation, principal)
            projects = planner.projects
            with _connection(projects.database, readonly=False) as project_db:
                project_db.execute("PRAGMA query_only=ON")
                projects._require_owner(project_db, run["project_id"], principal)
                project = project_db.execute(
                    "SELECT snapshot FROM projects WHERE id=?", (run["project_id"],)
                ).fetchone()
                fixed = run["execution_policy_snapshot"]
                registered = project_db.execute(
                    "SELECT record FROM execution_policies "
                    "WHERE project_id=? AND id=? AND revision=?",
                    (run["project_id"], fixed["id"], fixed["revision"]),
                ).fetchone()
                if project is None or registered is None or json.loads(registered[0]) != fixed:
                    raise RunError("CANDIDATE_CHECKS_APPROVAL_CHANGED")
                if (
                    json.loads(project[0])["repository"]
                    != run["configuration_snapshot"]["repository"]
                ):
                    raise RunError("CANDIDATE_CHECKS_REPOSITORY_CHANGED")
                transition = parse_transition(operation)
                binding = (
                    transition
                    if transition is not None and transition["phase"] == "ready"
                    else operation.get("validation", {}).get("review_binding")
                )
                if binding is not None:
                    if self.subject_validator is None:
                        raise RunError("REVIEW_BINDING_PRODUCER_REQUIRED")
                    self.subject_validator(project_db, run, operation, binding, principal=principal)
                yield run

    def _claim(
        self, db: Any, run: dict[str, Any], operation: dict[str, Any], row: dict[str, Any]
    ) -> None:
        if self.launch_compiler is None:
            raise RunError("CANDIDATE_CHECK_LAUNCH_COMPILER_REQUIRED")
        now = self.admissions.routing.planner.clock()
        budget = claim_process(
            db, run, operation, attempt_id=row["attempt_id"], scope="check", now=now
        )
        deadline = min(
            now + row["check"]["timeout_seconds"],
            budget["started_at"] + budget["max_duration_seconds"],
        )
        row.update(
            claimed_at=now,
            deadline=deadline,
            effective_timeout_seconds=deadline - now,
            cleanup_allowance_seconds=30,
            timeout_seconds=deadline - now + 30,
        )
        execution = execution_document(row)
        launch = self.launch_compiler(execution)
        if not isinstance(launch, CheckLaunchSpec):
            raise RunError("CANDIDATE_CHECK_LAUNCH_INVALID")
        row.update(
            phase="claimed",
            execution_digest=digest(execution),
            launch={
                "process_spec": launch.process_spec.document(),
                "bootstrap_digest": launch.bootstrap_digest,
            },
            host_prepared_id=None,
            host_observation=None,
            native_claim=None,
            observation=None,
            evidence_request=None,
            evidence=None,
        )
        row["launch"]["manifest"] = CheckAttemptManifest(
            schema_version="karajan.check-attempt.v1",
            id=row["attempt_id"],
            fence=row["fence"],
            role="check",
            authorization_ref=row["authorization_ref"],
            budget_ref=row["budget_ref"],
            permissions=["execute_checks", "candidate_copy"],
            environment_id=row["environment"]["id"],
            environment_revision=row["environment"]["revision"],
            environment_source_sha256=row["environment"]["source_sha256"],
            execution_sha256=row["execution_digest"],
        ).model_dump()
        row["launch"]["activation"] = asdict(
            Activation(
                id=row["activation_key"],
                attempt_id=row["attempt_id"],
                fence=row["fence"],
                authorization_ref=row["authorization_ref"],
                budget_ref=row["budget_ref"],
                expires_at=deadline,
            )
        )
        operation["validation"]["checks"]["phase"] = "claimed"

    def _check_budget(
        self, db: Any, run: dict[str, Any], operation: dict[str, Any], row: dict[str, Any]
    ) -> None:
        now = self.admissions.routing.planner.clock()
        current_process(db, run, operation, attempt_id=row["attempt_id"], now=now)
        if now < row["claimed_at"] or now >= row["deadline"]:
            raise RunError("CANDIDATE_CHECK_DEADLINE_EXPIRED")
        if digest(execution_document(row)) != row["execution_digest"]:
            raise RunError("CANDIDATE_CHECK_EXECUTION_CHANGED")

    def _prepare_host(self, row: dict[str, Any]) -> None:
        if self.host is None:
            raise RunError("CANDIDATE_CHECK_HOST_REQUIRED")
        stored = row["launch"]["process_spec"]
        process = ProcessSpec(tuple(stored["argv"]), Path(stored["cwd"]), stored["timeout_seconds"])
        snapshot = self.host.prepare(
            CheckAttemptManifest.model_validate(row["launch"]["manifest"]),
            row["start_key"],
            process,
        )
        if snapshot.prepared_id != row["start_key"]:
            raise RunError("CANDIDATE_CHECK_HOST_IDENTITY_CHANGED")
        self.host.initialize_control_once(
            row["attempt_id"],
            prepared_id=row["start_key"],
            fence=row["fence"],
            authorization_ref=row["authorization_ref"],
        )
        row["host_prepared_id"] = snapshot.prepared_id
        row["phase"] = "host_prepared"

    def _prepare(
        self, run: dict[str, Any], operation: dict[str, Any], principal: str
    ) -> dict[str, Any]:
        if operation["cancel_requested"] or operation.get("execution", {}).get("cancel_requested"):
            raise RunError("CANDIDATE_CHECKS_CANCELLED")
        plan, _ = _approved_task(run, operation, principal)
        source = operation["workspace"]["source_binding"]
        if source["plan"] != plan or source["execution_policy"] != run["execution_policy_snapshot"]:
            raise RunError("CANDIDATE_CHECKS_APPROVAL_CHANGED")
        collection = operation.get("execution", {}).get("collection")
        if not isinstance(collection, dict) or not isinstance(collection.get("candidate"), dict):
            raise RunError("CANDIDATE_CHECKS_CAPTURE_REQUIRED")
        capture = collection["capture"]
        if digest(capture) != collection["capture_digest"]:
            raise RunError("TASK_CAPTURE_IDENTITY_CONFLICT")
        candidate = self.candidates.lookup_projection_capture(
            capture["freeze_request"],
            projection=capture["projection"],
            captured_files=capture["captured_files"],
        )
        if candidate is None or any(
            candidate[key] != value for key, value in collection["candidate"].items()
        ):
            raise RunError("CANDIDATE_CHECKS_CAPTURE_MISMATCH")
        policy = _validation_policy(source)
        retained_subject = None
        if operation.get("validation") is not None:
            resolved = current_subject(operation, self.candidates)
            retained_subject, candidate = resolved["subject"], resolved["candidate"]
            installed = operation["validation"].get("review_binding")
            if installed is not None:
                policy["review"]["approved_reviewers"] = [
                    row["reviewer"] for row in installed["binding"]["reviewer_sources"]
                ]
        if candidate["request"]["policy"] != policy:
            raise RunError("CANDIDATE_CHECKS_POLICY_MISMATCH")
        current = {
            "repository_identity": source["repository"]["identity_sha256"],
            "base_sha": source["repository"]["base_sha"],
            "input_sha256": operation["workspace"]["input_sha256"],
            "policy_sha256": digest(policy),
        }
        gate = self.candidates.gate(candidate["id"], current=current)
        if any(
            reason in gate["reasons"]
            for reason in (
                "CURRENT_CONTEXT_CHANGED",
                "CANDIDATE_SUPERSEDED",
                "ARTIFACT_UNAVAILABLE",
            )
        ):
            raise RunError("CANDIDATE_CHECKS_SUBJECT_INVALID")
        identity = {
            key: candidate[key]
            for key in (
                "id",
                "series_id",
                "revision",
                "repository_identity",
                "base_sha",
                "tree_sha",
                "content_sha256",
                "manifest_sha256",
                "input_sha256",
                "policy_sha256",
            )
        }
        identity["baseline_id"] = candidate["request"]["baseline_id"]
        subject = {
            "schema_version": "karajan.candidate-validation-subject.v1",
            "revision": 1,
            "source_capture_digest": collection["capture_digest"],
            "source_candidate": identity,
            "candidate": deepcopy(identity),
            "approval_digest": digest(source["approval"]),
            "plan_digest": plan["plan_digest"],
            "execution_policy_digest": source["execution_policy"]["digest"],
        }
        if retained_subject is not None:
            subject = retained_subject
        if self.runner is None or self.controller_source is None:
            raise RunError("CANDIDATE_CHECK_RUNNER_REQUIRED")
        controller = self.controller_source()
        validation = source["execution_policy"]["validation"]
        check_definitions = {row["id"]: row for row in validation["checks"]}
        environments = {(row["id"], row["revision"]): row for row in validation["environments"]}
        executions = []
        for approved in policy["checks"]:
            check = deepcopy(check_definitions[approved["id"]])
            ref = check["environment_ref"]
            environment = deepcopy(environments[(ref["id"], ref["revision"])])
            observed = self.runner.source(environment)
            if observed.get("environment_sha256") != environment["source_sha256"]:
                raise RunError("CANDIDATE_CHECK_ENVIRONMENT_CHANGED")
            token = str(uuid.uuid4())
            executions.append(
                {
                    "schema_version": "karajan.candidate-check-execution.v1",
                    "check_run_id": "check:" + token,
                    "attempt_id": "check-attempt:" + token,
                    "fence": 1,
                    "start_key": "check-start:" + token,
                    "activation_key": "check-activate:" + token,
                    "authorization_ref": plan["authorization_digest"],
                    "budget_ref": plan["plan"]["authorization"]["budget_ref"],
                    "evidence_key": "check-evidence:" + token,
                    "run_id": run["id"],
                    "operation_id": operation["id"],
                    "principal": principal,
                    "root_task_id": operation["assessment"]["route"]["snapshots"]["task"][
                        "root_task_id"
                    ],
                    "subject_digest": digest(subject),
                    "candidate": deepcopy(identity),
                    "check": check,
                    "environment": environment,
                    "source": {"controller": controller, "runner": observed},
                    "phase": "prepared",
                }
            )
        # Validate JSON before it becomes durable control material.
        result = {
            "schema_version": "karajan.candidate-validation.v1",
            "subject": subject,
            "checks": {"revision": subject["revision"], "phase": "prepared", "runs": executions},
            "review": "not_run",
            "local_gate_passed": False,
            "delivery_eligible": False,
        }
        return dict(json.loads(json.dumps(result, allow_nan=False)))
