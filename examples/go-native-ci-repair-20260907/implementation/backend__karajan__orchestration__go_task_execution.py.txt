"""Fixed approved Task execution; request inputs are original identities only."""

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any

import httpx

from karajan.adapters.opencode.go_context import GoRequestAccounting
from karajan.adapters.opencode.go_journal import GoCallJournal, GoJournalError
from karajan.adapters.opencode.go_relay import GoRelayAuthorization
from karajan.candidates import CandidateStore
from karajan.contracts.probe import AttemptManifest
from karajan.execution import Activation, ProcessIdentity, ProcessSpec
from karajan.isolation.go_task import _STABLE_FAILURE_CODES, execute_go_task
from karajan.isolation.opencode_runtime import IsolatedOpenCode
from karajan.projects.credential_sources import CredentialSourceStore, ResolvedCredential
from karajan.runs import RunError

from .go_execution_intent import GoExecutionIntents
from .go_task_binding import execution_source, task_authentication, task_relay_context
from .go_task_input import build_task_input


@dataclass(frozen=True)
class GoTaskServices:
    """Controller construction only; history need not resolve execution material."""

    intents: GoExecutionIntents
    candidates: CandidateStore
    journal: GoCallJournal
    credentials: CredentialSourceStore | None
    runtime: Path
    accounting: GoRequestAccounting | None
    work_root: Path
    fresh_source: Callable[[], dict[str, Any]]
    fixed_runner_spec: Callable[[str, str, str, float], ProcessSpec]
    client_factory: Callable[[], httpx.Client] | None = None


class ApprovedGoTaskExecution:
    def __init__(self, services: GoTaskServices) -> None:
        self.services = services

    def get(self, run_id: str, operation_id: str, *, principal: str) -> dict[str, Any]:
        return self.services.intents.read(run_id, operation_id, principal=principal)

    def advance(self, run_id: str, operation_id: str, *, principal: str) -> dict[str, Any]:
        operation = self.get(run_id, operation_id, principal=principal)
        if operation["cancel_requested"] or operation.get("execution", {}).get("effect_claim"):
            return self.reconcile(run_id, operation_id, principal=principal)
        if self.services.accounting is None or self.services.credentials is None:
            raise RunError("TASK_EXECUTION_SERVICES_REQUIRED")
        _check_source(self.services, operation)
        intents = self.services.intents
        try:
            operation = intents.prepare_intent(
                run_id,
                operation_id,
                principal=principal,
                command_key="go-task-prepare:" + operation_id,
            )
            # Compile from CAS before accepting any activation or process intent.
            intent = operation["execution"]["intent"]
            build_task_input(
                operation["workspace"],
                self.services.candidates,
                native_source_sha256=intent["native_source_sha256"],
                runner_source_digest=intent["runner_source_sha256"],
            )
            operation = intents.activation_recorded(run_id, operation_id, principal=principal)
            if operation["execution"]["phase"] == "prepared":
                with intents.activation_guard(run_id, operation_id, principal=principal) as held:
                    with _route_guard(self.services, held, principal):
                        intents.admissions.routing.capacity.activate(
                            intent["admission_id"],
                            command_key=intent["activation_key"],
                        )
                operation = intents.activation_recorded(run_id, operation_id, principal=principal)
            if operation["execution"]["phase"] == "activation_rejected":
                return self.reconcile(run_id, operation_id, principal=principal)
            if operation["execution"]["phase"] == "activated":
                operation = intents.freeze_launch(run_id, operation_id, principal=principal)
                with intents.launch_preparation_guard(
                    run_id,
                    operation_id,
                    principal=principal,
                ) as held:
                    with _route_guard(self.services, held, principal):
                        with intents.admissions.routing.capacity.pre_effect_guard(
                            intent["admission_id"],
                            expected_request=held["request"],
                        ):
                            launch = held["execution"]["launch"]
                            spec = launch["process_spec"]
                            intents.host.prepare(
                                AttemptManifest.model_validate(launch["manifest"]),
                                intent["start_key"],
                                ProcessSpec(
                                    tuple(spec["argv"]), Path(spec["cwd"]), spec["timeout_seconds"]
                                ),
                            )
                            control = intents.host.initialize_control_once(
                                intent["attempt_id"],
                                prepared_id=intent["start_key"],
                                fence=intent["fence"],
                                authorization_ref=intent["authorization_ref"],
                            )
                            if not control["dispatch_enabled"]:
                                raise RunError("TASK_EXECUTION_CONTROL_REVOKED")
                intents.record_host_prepared(run_id, operation_id, principal=principal)
                operation = intents.mark_start_unknown(run_id, operation_id, principal=principal)
            if operation["execution"]["phase"] == "start_unknown":
                with intents.startup_guard(run_id, operation_id, principal=principal) as held:
                    with _route_guard(self.services, held, principal):
                        with intents.admissions.routing.capacity.pre_effect_guard(
                            intent["admission_id"],
                            expected_request=held["request"],
                        ):
                            _check_source(self.services, held)
                            intents.host.start(
                                intent["start_key"],
                                Activation(**held["execution"]["launch"]["activation"]),
                            )
                intents.host_started(run_id, operation_id, principal=principal)
            return self.get(run_id, operation_id, principal=principal)
        except Exception:
            _observe_failure(self.services, run_id, operation_id, principal, "advance")
            raise

    def reconcile(self, run_id: str, operation_id: str, *, principal: str) -> dict[str, Any]:
        operation = self.get(run_id, operation_id, principal=principal)
        if "execution" not in operation:
            return operation
        cleanup_failed = operation["cancel_requested"] and self._cleanup(
            run_id, operation_id, principal
        )
        if operation["execution"]["capacity_activation"] is None:
            # Recover only the original committed command, including its expiry.
            # This public store port never calls Capacity.activate.
            operation = self.services.intents.activation_recorded(
                run_id,
                operation_id,
                principal=principal,
            )
        if operation["execution"].get("collection") is not None:
            from .go_task_collector import ApprovedGoCollector

            ApprovedGoCollector(
                self.services.intents,
                self.services.candidates,
                self.services.journal,
                source_check=lambda: None,
            ).recover(run_id, operation_id, principal=principal)
        return self.services.intents.observe_execution(
            run_id,
            operation_id,
            principal=principal,
            failure="cancel" if cleanup_failed else None,
        )

    def cancel(self, run_id: str, operation_id: str, *, principal: str) -> dict[str, Any]:
        intents = self.services.intents
        operation = intents.cancel_intent(run_id, operation_id, principal=principal)
        if "execution" not in operation:
            return operation
        failed = self._cleanup(run_id, operation_id, principal)
        return intents.observe_execution(
            run_id,
            operation_id,
            principal=principal,
            failure="cancel" if failed else None,
        )

    def _cleanup(self, run_id: str, operation_id: str, principal: str) -> bool:
        # The cancellation transaction is over before either external cleanup.
        # Recovery resumes these same old identities, never new grants or starts.
        intents = self.services.intents
        owned = intents.cleanup_binding(run_id, operation_id, principal=principal)
        failed = False
        if owned is not None:
            try:
                _revoke_owned(self.services.journal, owned["intent"], owned["launch"])
            except Exception:
                failed = True
            try:
                intent = owned["intent"]
                snapshot = intents.host.inspect(intent["attempt_id"])
                if snapshot.prepared_id != intent["start_key"]:
                    raise RunError("TASK_EXECUTION_HOST_BINDING_MISMATCH")
                intents.host.cancel(
                    intent["attempt_id"],
                    intent["cancel_key"],
                    expected_binding={
                        "prepared_id": intent["start_key"],
                        "manifest": owned["launch"]["manifest"],
                        "process_spec": owned["launch"]["process_spec"],
                    },
                    timeout_seconds=0
                    if snapshot.state in {"prepared", "cancelled_unstarted"}
                    else 3,
                )
            except Exception:
                failed = True
        return failed


def _revoke_owned(journal: GoCallJournal, intent: dict[str, Any], launch: dict[str, Any]) -> None:
    try:
        current = journal.snapshot(intent["grant_id"])
    except GoJournalError as error:
        if str(error) == "GRANT_NOT_FOUND":
            return
        raise
    if current["binding"] != launch["grant_binding"]:
        raise RunError("TASK_EXECUTION_GRANT_BINDING_MISMATCH")
    journal.revoke_grant(intent["grant_id"])


def _check_source(services: GoTaskServices, operation: dict[str, Any]) -> None:
    """Filesystem re-observation only; safe inside already-held business DB guards."""
    try:
        fresh = services.fresh_source()
        actual = execution_source(fresh)
        intent = operation.get("execution", {}).get("intent")
        if actual != services.intents.source or (
            intent is not None
            and (
                intent["runner_source_sha256"] != actual.runner_source_sha256
                or intent["native_source_sha256"] != actual.native_source_sha256
            )
        ):
            raise ValueError()
        qualified = operation["workspace"]["source_binding"]["profile_source"]["qualification"][
            "observation"
        ]["binding"]["execution_start"]["source"]["runtime_source"]
        if fresh["native_task"]["qualified_mechanism_descriptor"] != qualified:
            raise ValueError()
    except Exception:
        raise RunError("TASK_EXECUTION_SOURCE_CHANGED") from None


@contextmanager
def _route_guard(
    services: GoTaskServices,
    operation: dict[str, Any],
    principal: str,
) -> Iterator[None]:
    _check_source(services, operation)
    assessment = operation["assessment"]
    with services.intents.admissions.routing.reserved_execution_guard(
        operation["run_id"],
        assessment["id"],
        principal=principal,
    ) as current:
        if (
            current["state"] != "selected"
            or current["original_assessment_digest"] != assessment["digest"]
            or current["route"]["selected_profile"] != assessment["route"]["selected_profile"]
            or current["planned_attempt_id"] != operation["planned_attempt_id"]
            or current["planned_context_id"] != operation["planned_context_id"]
        ):
            raise RunError("TASK_EXECUTION_ROUTE_NOT_CURRENT")
        _check_source(services, operation)
        yield


@contextmanager
def _effect_guard(
    services: GoTaskServices,
    run_id: str,
    operation_id: str,
    principal: str,
    runner: ProcessIdentity,
) -> Iterator[None]:
    intents = services.intents
    with intents.effect_claim_guard(
        run_id,
        operation_id,
        principal=principal,
        runner=runner,
    ) as operation:
        intent = operation["execution"]["intent"]
        with _route_guard(services, operation, principal):
            with intents.admissions.routing.capacity.pre_effect_guard(
                intent["admission_id"],
                expected_request=operation["request"],
            ):
                with intents.host.current_runner_guard(
                    intent["attempt_id"],
                    fence=intent["fence"],
                    authorization_ref=intent["authorization_ref"],
                ) as current:
                    if current != runner:
                        raise RunError("TASK_EXECUTION_RUNNER_CHANGED")
                    _check_source(services, operation)
                    yield


def _observe_failure(
    services: GoTaskServices,
    run_id: str,
    operation_id: str,
    principal: str,
    kind: str,
) -> None:
    # A disappearing ledger must not mask the original error or initialize a
    # replacement. A later historical reconciliation retains its unavailable fact.
    try:
        services.intents.observe_execution(
            run_id,
            operation_id,
            principal=principal,
            failure="advance" if kind == "advance" else "runner",
        )
    except Exception:
        pass


def consume_go_task(
    services: GoTaskServices,
    run_id: str,
    operation_id: str,
    *,
    principal: str,
) -> dict[str, Any]:
    """Called only by the fixed direct child; claim/grant replies are one-use.

    Startup registration is awaited without business locks. Credential material
    resolves outside Project locks, then all current facts are checked again at
    actual namespace start and each Journal/HTTP boundary. No public result input.
    """
    facade = ApprovedGoTaskExecution(services)
    operation = facade.get(run_id, operation_id, principal=principal)
    if operation["cancel_requested"] or operation.get("execution", {}).get("effect_claim"):
        return facade.reconcile(run_id, operation_id, principal=principal)
    if services.accounting is None or services.credentials is None:
        raise RunError("TASK_EXECUTION_SERVICES_REQUIRED")
    intents = services.intents
    owned: dict[str, Any] | None = None
    try:
        _check_source(services, operation)
        intent = operation["execution"]["intent"]
        runner = intents.host.wait_for_runner_registration(intent["attempt_id"], timeout_seconds=10)
        task = build_task_input(
            operation["workspace"],
            services.candidates,
            native_source_sha256=intent["native_source_sha256"],
            runner_source_digest=intent["runner_source_sha256"],
        )
        context = task_relay_context(operation, services.accounting)
        with intents.startup_guard(run_id, operation_id, principal=principal) as held:
            with _route_guard(services, held, principal):
                with intents.admissions.routing.capacity.pre_effect_guard(
                    intent["admission_id"],
                    expected_request=held["request"],
                ):
                    with intents.host.current_runner_guard(
                        intent["attempt_id"],
                        fence=intent["fence"],
                        authorization_ref=intent["authorization_ref"],
                    ) as current:
                        if current != runner:
                            raise RunError("TASK_EXECUTION_RUNNER_CHANGED")
        claimed = intents.effect_start_claim(
            run_id,
            operation_id,
            principal=principal,
            runner=runner,
        )
        if not claimed["claim_allowed"]:
            return facade.reconcile(run_id, operation_id, principal=principal)
        authentication = task_authentication(operation)
        credential = services.credentials.resolve_exact(
            authentication["project_id"],
            authentication["auth_ref"],
            authentication["generation"],
            principal=principal,
        )
        _credential_identity(credential, authentication)
        owned = {"intent": intent, "launch": operation["execution"]["launch"]}
        with _effect_guard(services, run_id, operation_id, principal, runner):
            grant = services.journal.create_grant(
                owned["launch"]["grant_binding"],
                grant_id=intent["grant_id"],
            )
        if grant["capability"] is None:
            raise RunError("TASK_EXECUTION_GRANT_CAPABILITY_UNAVAILABLE")
        authorization = GoRelayAuthorization(
            services.journal,
            intent["grant_id"],
            grant["binding"],
            grant["capability"],
        )
        start_lock, started = Lock(), False

        def start_native(native: IsolatedOpenCode) -> dict[str, Any]:
            nonlocal started
            with start_lock:
                if started:
                    raise RunError("TASK_NATIVE_START_ALREADY_CLAIMED")
                started = True
            with _effect_guard(services, run_id, operation_id, principal, runner):
                return native.start()

        def send_guard() -> AbstractContextManager[None]:
            return _effect_guard(services, run_id, operation_id, principal, runner)

        result = execute_go_task(
            services.runtime,
            services.work_root / ("operation-" + operation_id),
            task,
            credential,
            authorization,
            context,
            start_native=start_native,
            send_guard=send_guard,
            client_factory=services.client_factory,
        )
        _record_native_failure_diagnostic(
            intents, run_id, operation_id, principal=principal, runner=runner, result=result
        )
        from .go_task_collector import ApprovedGoCollector

        ApprovedGoCollector(
            intents,
            services.candidates,
            services.journal,
            source_check=lambda: _check_source(services, operation),
        ).collect(run_id, operation_id, principal=principal, runner=runner, result=result)
        return facade.get(run_id, operation_id, principal=principal)
    except Exception:
        if owned is not None:
            try:
                _revoke_owned(services.journal, owned["intent"], owned["launch"])
            except Exception:
                pass
        _observe_failure(services, run_id, operation_id, principal, "runner")
        raise


def _record_native_failure_diagnostic(
    intents: GoExecutionIntents,
    run_id: str,
    operation_id: str,
    *,
    principal: str,
    runner: ProcessIdentity,
    result: Any,
) -> None:
    """Carry only the fixed producer failure facts across Collector rejection."""
    if getattr(result, "capture", object()) is not None:
        return
    report = result.report
    candidates = [report.get("error_reason_code")]
    candidates.extend(report.get("reason_codes", []))
    reason_code = next((value for value in candidates if value in _STABLE_FAILURE_CODES), None)
    error_type = report.get("error_type", "Exception")
    native_stop = report.get("native_cleanup", {}).get("local_stop")
    relay_status = report.get("relay_cleanup", {}).get("status")
    if (
        reason_code is None
        or not isinstance(error_type, str)
        or native_stop not in {"confirmed", "not_started", "unknown"}
        or relay_status not in {"closed", "unknown"}
    ):
        return
    intents.record_failure_diagnostic(
        run_id,
        operation_id,
        principal=principal,
        runner=runner,
        reason_code=reason_code,
        error_type=error_type,
        native_stop=native_stop,
        relay_status=relay_status,
    )


def _credential_identity(credential: ResolvedCredential, source: dict[str, Any]) -> None:
    if (
        not isinstance(credential, ResolvedCredential)
        or credential.project_id != source["project_id"]
        or credential.auth_ref != source["auth_ref"]
        or credential.generation != source["generation"]
        or credential.source_id != source["source"]["id"]
    ):
        raise RunError("TASK_AUTHENTICATION_SOURCE_CHANGED")
