"""Durable Go execution identity in the original admission operation.

This controller port records intent and observations, never process or provider
effects. A committed claim is consumed even if its response is lost. Its caller
must revalidate all business guards and the current Host runner before effects.
"""

import json
import math
import re
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from karajan.adapters.opencode.go_journal import GoCallJournal, GoJournalError
from karajan.candidates import CandidateStore
from karajan.candidates.models import Freeze
from karajan.execution import ProcessIdentity, ProcessSpec, RunnerHost, Snapshot
from karajan.routing.compiler import digest
from karajan.runs import RunError
from karajan.runs.planning import encoded, identifier

from .admission import ApprovedTaskAdmission
from .execution_budget import claim_process, current_process
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


@dataclass(frozen=True)
class GoLaunchSpec:
    """Fixed deployment compiler output, never a public request payload."""

    process_spec: ProcessSpec
    bootstrap_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.process_spec, ProcessSpec) or not _sha256(self.bootstrap_digest):
            raise RunError("TASK_LAUNCH_SPEC_INVALID")
        self.process_spec.document()


@dataclass(frozen=True, init=False)
class GoTaskCaptureReceipt:
    """Internal Collector DTO with frozen metadata and no captured file bytes."""

    _document: str = field(repr=False)

    def __init__(self, document: dict[str, Any]) -> None:
        try:
            copied = json.loads(encoded(document))
            required = {
                "schema_version",
                "intent_digest",
                "workspace_digest",
                "input_sha256",
                "projection",
                "captured_files",
                "report",
                "evidence_digest",
                "freeze_request",
            }
            if (
                not isinstance(copied, dict)
                or set(copied) != required
                or copied["schema_version"] != "karajan.go-task-capture.v1"
                or not all(
                    _sha256(copied[key])
                    for key in (
                        "intent_digest",
                        "workspace_digest",
                        "input_sha256",
                        "evidence_digest",
                    )
                )
            ):
                raise ValueError()
            evidence = {
                key: copied[key]
                for key in (
                    "intent_digest",
                    "workspace_digest",
                    "input_sha256",
                    "projection",
                    "captured_files",
                    "report",
                )
            }
            request = Freeze.model_validate(copied["freeze_request"])
            if (
                digest(evidence) != copied["evidence_digest"]
                or request.input_sha256 != copied["input_sha256"]
                or request.writer.stopped is not True
                or request.writer.observation_ref != "go-task-stop:" + copied["evidence_digest"]
                or any(
                    author.provenance_ref != "go-task-author:" + copied["evidence_digest"]
                    for author in request.authors
                )
                or not isinstance(copied["report"], dict)
                or copied["report"].get("schema_version") != "karajan.go-native-task-observation.v1"
            ):
                raise ValueError()
            for name, keys in (
                ("projection", {"path", "sha256", "writable"}),
                ("captured_files", {"path", "sha256", "size"}),
            ):
                values = copied[name]
                if not isinstance(values, list) or not values:
                    raise ValueError()
                for row in values:
                    if (
                        not isinstance(row, dict)
                        or set(row) != keys
                        or not isinstance(row["path"], str)
                        or not _sha256(row["sha256"])
                    ):
                        raise ValueError()
                    if name == "projection" and type(row["writable"]) is not bool:
                        raise ValueError()
                    if name == "captured_files" and (
                        type(row["size"]) is not int or row["size"] < 0
                    ):
                        raise ValueError()
                if len({row["path"] for row in values}) != len(values):
                    raise ValueError()
            if {row["path"] for row in copied["projection"]} != {
                row["path"] for row in copied["captured_files"]
            }:
                raise ValueError()
            object.__setattr__(self, "_document", encoded(copied))
        except (ValueError, TypeError, KeyError):
            raise RunError("TASK_CAPTURE_RECEIPT_INVALID") from None

    def as_dict(self) -> dict[str, Any]:
        return dict(json.loads(self._document))


def _sha256(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


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
        self,
        admissions: ApprovedTaskAdmission,
        *,
        source: GoExecutionSource,
        host: RunnerHost,
        launch_compiler: Callable[[dict[str, Any]], GoLaunchSpec] | None = None,
        journal: GoCallJournal | None = None,
        candidates: CandidateStore | None = None,
    ) -> None:
        if not isinstance(source, GoExecutionSource):
            raise RunError("TASK_EXECUTION_SOURCE_INVALID")
        self.admissions, self.source, self.host = admissions, source, host
        self.launch_compiler, self.journal, self.candidates = launch_compiler, journal, candidates

    @staticmethod
    def _check_owner(
        admissions: ApprovedTaskAdmission, run_id: str, operation_id: str, principal: str
    ) -> None:
        for value in (run_id, operation_id, principal):
            identifier(value)
        planner = admissions.routing.planner
        with _connection(planner.database, readonly=True) as db:
            planner._owner(planner._get(db, run_id), principal)

    def _owner(self, run_id: str, operation_id: str, principal: str) -> None:
        self._check_owner(self.admissions, run_id, operation_id, principal)

    @staticmethod
    def read_operation(
        admissions: ApprovedTaskAdmission, run_id: str, operation_id: str, *, principal: str
    ) -> dict[str, Any]:
        """Open original operation history without constructing or refreshing source."""
        GoExecutionIntents._check_owner(admissions, run_id, operation_id, principal)
        with _connection(admissions.database, readonly=True) as db:
            return admissions._load(db, run_id, operation_id)

    @classmethod
    def open_existing(
        cls,
        admissions: ApprovedTaskAdmission,
        *,
        run_id: str,
        operation_id: str,
        principal: str,
        host: RunnerHost,
        source_if_unprepared: Callable[[], GoExecutionSource],
        launch_compiler: Callable[[dict[str, Any]], GoLaunchSpec] | None = None,
        journal: GoCallJournal | None = None,
        candidates: CandidateStore | None = None,
    ) -> "GoExecutionIntents":
        """Reconstruct history without requiring current executable or credentials.

        A missing execution uses the controller's lazy source compiler. A present
        but invalid execution never falls back to a new source or new identity.
        """
        operation = cls.read_operation(admissions, run_id, operation_id, principal=principal)
        try:
            source = (
                GoExecutionSource(
                    **{
                        key: operation["execution"]["intent"][key]
                        for key in ("runner_source_sha256", "native_source_sha256")
                    }
                )
                if "execution" in operation
                else source_if_unprepared()
            )
            instance = cls(
                admissions,
                source=source,
                host=host,
                launch_compiler=launch_compiler,
                journal=journal,
                candidates=candidates,
            )
            if "execution" in operation:
                instance._execution(operation)
            return instance
        except (KeyError, TypeError):
            raise RunError("TASK_EXECUTION_BINDING_MISMATCH") from None

    def read(self, run_id: str, operation_id: str, *, principal: str) -> dict[str, Any]:
        """Detached persisted status, with no clock, refresh, writes or effects."""
        return self.read_operation(self.admissions, run_id, operation_id, principal=principal)

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
                    claim_process(
                        db,
                        run,
                        operation,
                        attempt_id=intent["attempt_id"],
                        scope="writer",
                        now=self.admissions.routing.planner.clock(),
                    )
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

    def _execution(
        self, operation: dict[str, Any], *, current_source: bool = True
    ) -> dict[str, Any]:
        execution = operation.get("execution")
        if not isinstance(execution, dict):
            raise RunError("TASK_EXECUTION_NOT_PREPARED")
        intent = execution["intent"]
        if (
            execution["schema_version"] != "karajan.go-task-execution-intent.v1"
            or digest(intent) != execution["intent_digest"]
            or (
                current_source
                and any(intent[key] != value for key, value in asdict(self.source).items())
            )
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
        self, run_id: str, operation_id: str, principal: str, *, current_source: bool = True
    ) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
        self._owner(run_id, operation_id, principal)
        with _connection(self.admissions.database, readonly=False) as db:
            operation = self.admissions._load(db, run_id, operation_id)
            execution = self._execution(operation, current_source=current_source)
            before = encoded(operation)
            yield operation, execution
            if encoded(operation) != before:
                self.admissions._save(db, operation)

    def freeze_launch(self, run_id: str, operation_id: str, *, principal: str) -> dict[str, Any]:
        """Compile one exact launch from owned state and a fixed deployment compiler."""
        from .go_task_binding import task_grant_binding, task_host_activation, task_host_manifest

        with self._edit(run_id, operation_id, principal) as (operation, execution):
            if operation["cancel_requested"]:
                raise RunError("TASK_EXECUTION_CANCEL_REQUESTED")
            if self.launch_compiler is None:
                raise RunError("TASK_LAUNCH_COMPILER_REQUIRED")
            spec = self.launch_compiler(deepcopy(operation))
            if type(spec) is not GoLaunchSpec:
                raise RunError("TASK_LAUNCH_SPEC_INVALID")
            launch = {
                "schema_version": "karajan.go-task-launch.v1",
                "intent_digest": execution["intent_digest"],
                "manifest": task_host_manifest(operation).model_dump(),
                "activation": asdict(task_host_activation(operation)),
                "grant_binding": task_grant_binding(operation),
                "process_spec": spec.process_spec.document(),
                "bootstrap_digest": spec.bootstrap_digest,
            }
            launch["digest"] = digest(launch)
            # ProcessSpec uses tuples; store and first response must have exactly
            # the same JSON representation as all subsequent historical reads.
            launch = json.loads(encoded(launch))
            prior = execution.get("launch")
            if prior is not None:
                if prior != launch:
                    raise RunError("TASK_LAUNCH_BINDING_CONFLICT")
            elif execution["phase"] != "activated":
                raise RunError("TASK_EXECUTION_ACTIVATION_REQUIRED")
            else:
                execution["launch"] = launch
            return operation

    @staticmethod
    def _launch(execution: dict[str, Any]) -> dict[str, Any] | None:
        launch = execution.get("launch")
        if launch is None:
            return None
        if (
            not isinstance(launch, dict)
            or launch.get("schema_version") != "karajan.go-task-launch.v1"
            or launch.get("intent_digest") != execution["intent_digest"]
            or digest({key: value for key, value in launch.items() if key != "digest"})
            != launch.get("digest")
        ):
            raise RunError("TASK_LAUNCH_BINDING_CONFLICT")
        return launch

    def cleanup_binding(
        self, run_id: str, operation_id: str, *, principal: str
    ) -> dict[str, Any] | None:
        """Read the original cleanup identity even after cancellation/source change."""
        operation = self.read(run_id, operation_id, principal=principal)
        execution = self._execution(operation, current_source=False)
        launch = self._launch(execution)
        if launch is None:
            return None
        return {
            "intent": execution["intent"],
            "intent_digest": execution["intent_digest"],
            "launch": launch,
        }

    def observe_execution(
        self,
        run_id: str,
        operation_id: str,
        *,
        principal: str,
        failure: Literal["advance", "runner", "cancel"] | None = None,
    ) -> dict[str, Any]:
        """Append owned status observations; no stop, release or caller report JSON."""
        if failure not in {None, "advance", "runner", "cancel"}:
            raise RunError("TASK_OBSERVATION_REASON_INVALID")
        with self._edit(run_id, operation_id, principal, current_source=False) as (op, execution):
            launch = self._launch(execution)
            observed: dict[str, Any] = {
                "schema_version": "karajan.go-task-execution-observation.v1",
                "intent_digest": execution["intent_digest"],
                "failure": failure,
                "host": {"state": "unknown"},
                "grant": {"state": "not_observed"},
                "native_stop": "unknown",
                "provider_remote_stop": "unknown",
            }
            try:
                snapshot = self.host.inspect(execution["intent"]["attempt_id"])
                self._host_identity(execution, snapshot)
                observed["host"] = {
                    "state": snapshot.state,
                    "launch_phase": snapshot.launch_phase,
                    "prepared_id": snapshot.prepared_id,
                    "attempt_id": snapshot.attempt_id,
                }
            except KeyError:
                observed["host"] = {"state": "not_prepared"}
            except (OSError, sqlite3.Error, ValueError):
                pass
            if launch is not None and self.journal is not None:
                try:
                    grant = self.journal.snapshot(execution["intent"]["grant_id"])
                    if grant["binding"] != launch["grant_binding"]:
                        observed["grant"] = {"state": "binding_mismatch"}
                    else:
                        observed["grant"] = {
                            "state": grant["state"],
                            "request_count": grant["request_count"],
                            "calls_digest": digest(grant["calls"]),
                            "unknown_calls": sum(
                                row["state"] == "send_unknown" for row in grant["calls"]
                            ),
                            "revoked_at": grant["revoked_at"],
                        }
                except GoJournalError as error:
                    observed["grant"] = {
                        "state": "not_created" if str(error) == "GRANT_NOT_FOUND" else "unknown"
                    }
                except (OSError, sqlite3.Error, ValueError):
                    observed["grant"] = {"state": "unknown"}
            observed["digest"] = digest(observed)
            history = execution.setdefault("observations", [])
            if not history or history[-1] != observed:
                history.append(observed)
            execution["observation"] = observed
            if (
                failure
                and not op["cancel_requested"]
                and execution["phase"] != "candidate_recorded"
            ):
                op["state"] = "execution_unknown"
            return op

    def record_cleanup(self, run_id: str, operation_id: str, *, principal: str) -> dict[str, Any]:
        return self.observe_execution(run_id, operation_id, principal=principal)

    @staticmethod
    def _capture_identity(
        operation: dict[str, Any], execution: dict[str, Any], document: dict[str, Any]
    ) -> None:
        intent, workspace = execution["intent"], operation["workspace"]
        request, report = document["freeze_request"], document["report"]
        profile = workspace["source_binding"]["selected_profile"]
        expected_projection = [
            {
                "path": row["path"],
                "sha256": row["artifact"]["sha256"],
                "writable": "write" in row["access"],
            }
            for row in workspace["files"]
        ]
        if (
            document["intent_digest"] != execution["intent_digest"]
            or document["workspace_digest"] != intent["workspace_digest"]
            or document["input_sha256"] != intent["input_sha256"]
            or document["projection"] != expected_projection
            or request["baseline_id"] != workspace["baseline"]["id"]
            or request["series_id"] != "go-task-candidate:" + operation["id"]
            or request["allowed_paths"] != workspace["write_paths"]
            or request["writer"]["attempt_id"] != intent["attempt_id"]
            or request["writer"]["fence"] != intent["fence"]
            or len(request["authors"]) != 1
            or any(
                request["authors"][0][key] != value
                for key, value in {
                    "attempt_id": intent["attempt_id"],
                    "fence": intent["fence"],
                    "profile_id": profile["id"],
                    "profile_revision": profile["revision"],
                    "context_id": intent["context_id"],
                }.items()
            )
            or report.get("attempt_id") != intent["attempt_id"]
            or report.get("fence") != intent["fence"]
            or report.get("grant_id") != intent["grant_id"]
            or report.get("native_source_sha256") != intent["native_source_sha256"]
            or report.get("runner_source_digest") != intent["runner_source_sha256"]
            or report.get("subject")
            != {
                "kind": "task_attempt",
                "project_id": intent["project_id"],
                "run_id": intent["run_id"],
                "task_id": intent["task_id"],
            }
        ):
            raise RunError("TASK_CAPTURE_BINDING_MISMATCH")
        launch = GoExecutionIntents._launch(execution)
        if launch is None or report.get("grant_binding") != launch["grant_binding"]:
            raise RunError("TASK_CAPTURE_BINDING_MISMATCH")

    def capture_recorded(
        self,
        run_id: str,
        operation_id: str,
        *,
        principal: str,
        runner: ProcessIdentity,
        capture: GoTaskCaptureReceipt,
    ) -> dict[str, Any]:
        """Commit the trusted Collector's exact metadata before Candidate commit."""
        _runner(runner)
        if type(capture) is not GoTaskCaptureReceipt:
            raise RunError("TASK_CAPTURE_RECEIPT_REQUIRED")
        document = capture.as_dict()
        with self._edit(run_id, operation_id, principal, current_source=False) as (op, execution):
            self._capture_identity(op, execution, document)
            if execution["effect_claim"] != {
                "intent_digest": execution["intent_digest"],
                "runner": asdict(runner),
            }:
                raise RunError("TASK_EXECUTION_CLAIM_NOT_CURRENT")
            previous = execution.get("collection")
            if previous is not None:
                if previous["capture"] != document or previous["capture_digest"] != digest(
                    document
                ):
                    raise RunError("TASK_CAPTURE_IDENTITY_CONFLICT")
                return op
            self._execution(op)
            if op["cancel_requested"]:
                raise RunError("TASK_EXECUTION_CANCEL_REQUESTED")
            if execution["phase"] != "effect_claimed":
                raise RunError("TASK_EXECUTION_CLAIM_NOT_CURRENT")
            execution["collection"] = {
                "schema_version": "karajan.go-task-collection.v1",
                "capture": document,
                "capture_digest": digest(document),
                "candidate": None,
            }
            return op

    @contextmanager
    def collection_guard(
        self,
        run_id: str,
        operation_id: str,
        *,
        principal: str,
        runner: ProcessIdentity,
        capture_digest: str,
    ) -> Iterator[dict[str, Any]]:
        with self._capture_claim_guard(
            run_id, operation_id, principal=principal, runner=runner
        ) as op:
            collection = op["execution"].get("collection")
            if (
                not isinstance(collection, dict)
                or collection["capture_digest"] != capture_digest
                or digest(collection["capture"]) != capture_digest
            ):
                raise RunError("TASK_CAPTURE_IDENTITY_CONFLICT")
            yield op

    def candidate_recorded(
        self,
        run_id: str,
        operation_id: str,
        *,
        principal: str,
        capture_digest: str,
        candidate_id: str,
    ) -> dict[str, Any]:
        """Link an exact owned Candidate even when its commit response arrived late."""
        identifier(candidate_id)
        if self.candidates is None:
            raise RunError("TASK_CANDIDATE_STORE_REQUIRED")
        with self._edit(run_id, operation_id, principal, current_source=False) as (op, execution):
            collection = execution.get("collection")
            if (
                not isinstance(collection, dict)
                or collection["capture_digest"] != capture_digest
                or digest(collection["capture"]) != capture_digest
            ):
                raise RunError("TASK_CAPTURE_IDENTITY_CONFLICT")
            capture = collection["capture"]
            candidate = self.candidates.lookup_projection_capture(
                capture["freeze_request"],
                projection=capture["projection"],
                captured_files=capture["captured_files"],
            )
            if candidate is None or candidate["id"] != candidate_id:
                raise RunError("TASK_CANDIDATE_BINDING_MISMATCH")
            reference = {
                key: candidate[key]
                for key in (
                    "id",
                    "series_id",
                    "revision",
                    "content_sha256",
                    "manifest_sha256",
                    "input_sha256",
                    "policy_sha256",
                )
            }
            if collection["candidate"] is not None and collection["candidate"] != reference:
                raise RunError("TASK_CANDIDATE_IDENTITY_CONFLICT")
            collection["candidate"] = reference
            execution["phase"] = "candidate_recorded"
            # Candidate existence does not complete validation/review or release
            # the Task for another admission. In particular cancel never regresses.
            return op

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
        self, run_id: str, operation_id: str, principal: str, *, new_effect: bool = False
    ) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
        self._owner(run_id, operation_id, principal)
        with _connection(self.admissions.database, readonly=False) as db:
            db.execute("PRAGMA query_only=ON")
            operation = self.admissions._load(db, run_id, operation_id)
            execution = self._execution(operation)
            if operation["cancel_requested"]:
                raise RunError("TASK_EXECUTION_CANCEL_REQUESTED")
            if new_effect:
                # The operation transaction serializes all Run budget claims.
                # Do not retain a Run lock: the caller next acquires its original
                # routing Run/Project guards and must validate the same approval.
                planner = self.admissions.routing.planner
                with _connection(planner.database, readonly=True) as run_db:
                    run = planner._get(run_db, run_id)
                    _approved_task(run, operation, principal)
                    checked_at = planner.clock()
                    deadline = current_process(
                        db,
                        run,
                        operation,
                        attempt_id=execution["intent"]["attempt_id"],
                        now=checked_at,
                    )
                    operation["execution_budget_gate"] = {
                        "checked_at": checked_at,
                        "deadline": deadline,
                    }
            yield operation, execution

    def assert_effect_deadline(self, operation: dict[str, Any]) -> None:
        """Internal final comparison of the guard's nonpersistent budget facts.

        This gate bounds controller effect admission. Runtime/transport setup
        after the callback remains subject to its own deadlines and stop facts.
        """
        gate = operation.get("execution_budget_gate")
        if not isinstance(gate, dict) or set(gate) != {"checked_at", "deadline"}:
            raise RunError("RUN_EXECUTION_CLAIM_REQUIRED")
        now = self.admissions.routing.planner.clock()
        if type(now) not in (int, float) or not math.isfinite(now) or now < gate["checked_at"]:
            raise RunError("RUN_EXECUTION_CLOCK_REGRESSED")
        if now >= gate["deadline"]:
            raise RunError("RUN_DURATION_LIMIT")

    @contextmanager
    def activation_guard(
        self, run_id: str, operation_id: str, *, principal: str
    ) -> Iterator[dict[str, Any]]:
        """Hold the prepared operation before the original Capacity activation."""
        with self._guard(run_id, operation_id, principal, new_effect=True) as (op, execution):
            if execution["phase"] != "prepared" or execution["capacity_activation"] is not None:
                raise RunError("TASK_EXECUTION_PREPARED_REQUIRED")
            yield op

    @contextmanager
    def launch_preparation_guard(
        self, run_id: str, operation_id: str, *, principal: str
    ) -> Iterator[dict[str, Any]]:
        """Serialize cancellation with Host.prepare and one-time control setup."""
        with self._guard(run_id, operation_id, principal, new_effect=True) as (op, execution):
            if (
                execution["phase"] != "activated"
                or self._launch(execution) is None
                or execution["effect_claim"] is not None
            ):
                raise RunError("TASK_EXECUTION_LAUNCH_PREPARATION_REQUIRED")
            yield op

    @contextmanager
    def startup_guard(
        self, run_id: str, operation_id: str, *, principal: str
    ) -> Iterator[dict[str, Any]]:
        """Hold original launch intent before Run/Project/Capacity/Host guards.

        This is not fresh capacity or Host authority and must not surround a wait
        for child registration. It does not permit replaying a native start.
        """
        with self._guard(run_id, operation_id, principal, new_effect=True) as (
            operation,
            execution,
        ):
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
        """Revalidate shared budget before a new native start or provider send."""
        _runner(runner)
        with self._guard(run_id, operation_id, principal, new_effect=True) as (
            operation,
            execution,
        ):
            self._current_claim(execution, runner)
            yield operation

    @staticmethod
    def _current_claim(execution: dict[str, Any], runner: ProcessIdentity) -> None:
        if execution["phase"] != "effect_claimed" or execution["effect_claim"] != {
            "intent_digest": execution["intent_digest"],
            "runner": asdict(runner),
        }:
            raise RunError("TASK_EXECUTION_CLAIM_NOT_CURRENT")

    @contextmanager
    def _capture_claim_guard(
        self, run_id: str, operation_id: str, *, principal: str, runner: ProcessIdentity
    ) -> Iterator[dict[str, Any]]:
        """Original writer identity for stopped capture, not new effect budget."""
        _runner(runner)
        with self._guard(run_id, operation_id, principal) as (operation, execution):
            self._current_claim(execution, runner)
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
