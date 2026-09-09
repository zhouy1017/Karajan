"""Controlled planning-output transport for the owner workbench operation.

The HTTP boundary supplies only an existing execution ID.  This module freezes
that execution's trusted repository view, compiles its complete metered input,
and consumes a sealed output through ``PlanningExecution.submit``.  The Go
fixture producer is deliberately explicit and test-only: it exercises the same
local Relay and Journal boundaries, but it is never constructed by the default
application factory and therefore cannot claim production qualification.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any, Protocol, cast
from uuid import uuid4

import httpx

from karajan.adapters.opencode.go_context import GoRequestAccounting
from karajan.adapters.opencode.go_journal import GoCallJournal
from karajan.adapters.opencode.go_relay import GoPlanningRelayContext, GoRelay, GoRelayAuthorization
from karajan.isolation.go_task import _cleanup_relay_socket_root, _relay_socket_root
from karajan.isolation.opencode_runtime import IsolatedOpenCode
from karajan.runs import RunError
from karajan.runs.planning import digest, identifier
from karajan.storage import require_schema

from .planning_execution import PlanningExecution
from .planning_input import PlanningModelInput, compile_planning_input


def _sha256_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _artifact_bytes(model_input: PlanningModelInput) -> bytes:
    """Accept narrow legacy fixture construction while production is complete."""
    artifact = getattr(model_input, "artifact_bytes", None)
    if type(artifact) is bytes:
        return artifact
    request = getattr(model_input, "request_bytes", None)
    if type(request) is bytes:
        return request
    raise RunError("PLANNING_INPUT_INVALID")


def _native_log_evidence(directory: Path, cleanup: dict[str, Any]) -> dict[str, Any]:
    """Read exactly the stopped owned namespace log without unbounded buffering."""
    if cleanup.get("local_stop") != "confirmed":
        raise RunError("PLANNING_NATIVE_LOG_EVIDENCE_UNAVAILABLE")
    path = directory / "namespace.log"
    limit = 1_048_576
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise OSError
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                content = stream.read(limit + 1)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        if len(content) > limit or after.st_size > limit:
            raise RunError("PLANNING_NATIVE_LOG_LIMIT_EXCEEDED")
        if (
            (before.st_dev, before.st_ino, before.st_mode, before.st_nlink)
            != (after.st_dev, after.st_ino, after.st_mode, after.st_nlink)
            or after.st_size != len(content)
        ):
            raise OSError
    except RunError:
        raise
    except OSError:
        raise RunError("PLANNING_NATIVE_LOG_EVIDENCE_UNAVAILABLE") from None
    return {"bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}


class PlanningProducer(Protocol):
    """Private producer port; no browser value crosses this boundary."""

    authority_kind: str

    def source(self, binding: dict[str, Any]) -> dict[str, Any]: ...

    def produce(
        self,
        model_input: PlanningModelInput,
        *,
        binding: dict[str, Any],
        admission: dict[str, Any],
    ) -> bytes: ...


def observe_production_output_source(
    control_directory: Path, admissions: Any, binding: dict[str, Any]
) -> dict[str, Any]:
    """Read the live authority that makes a stored production output usable.

    This is bound while the OutputAuthority is built. Recovery may call
    ``PlanningExecution`` directly, so the arm-time source cannot become a
    historical authority merely because no ``PlanningTransport`` was reopened.
    The Commander reader performs the protected descriptor, runtime, tokenizer
    and sealed-current-credential observation; this helper creates no grant,
    reservation, or provider send.
    """
    from karajan.orchestration.go_commander_qualification import (
        read_commander_qualification_settings,
        validate_commander_qualification_settings,
    )
    from karajan.orchestration.planning_admission import (
        COMMANDER_QUALIFICATION_READER_VERSION,
        COMMANDER_QUALIFICATION_SCOPE,
    )

    settings, descriptor_sha256 = read_commander_qualification_settings(control_directory)
    validate_commander_qualification_settings(admissions.planner.projects, settings)
    current = admissions.current_output_source(binding)
    qualification = admissions.qualifications.read_commander(
        binding,
        scope=COMMANDER_QUALIFICATION_SCOPE,
        reader_version=COMMANDER_QUALIFICATION_READER_VERSION,
    )
    qualified = (
        qualification
        if isinstance(qualification, dict)
        and qualification.get("schema_version") == "karajan.commander-qualification.v1"
        and qualification.get("scope") == COMMANDER_QUALIFICATION_SCOPE
        and qualification.get("reader_version") == COMMANDER_QUALIFICATION_READER_VERSION
        and qualification.get("binding_sha256") == digest(binding)
        and qualification.get("provenance") == "official"
        and isinstance(qualification.get("source_generation_sha256"), str)
        and isinstance(qualification.get("record_sha256"), str)
        and isinstance(qualification.get("valid_until"), (int, float))
        and qualification["valid_until"] > admissions.planner.clock()
        else None
    )
    accounting = GoRequestAccounting(settings.tokenizer_directory)
    return {
        "schema_version": "karajan.production-go-planning-output-source.v2",
        "binding_sha256": digest(binding),
        "descriptor_sha256": descriptor_sha256,
        "runtime_sha256": _sha256_file(settings.runtime),
        "tokenizer_source": accounting.source(),
        # This hash includes the sealed credential/authentication observation
        # even when Commander is not currently qualified. Arm still succeeds
        # in that case so admission records its specific durable denial.
        "current_source_sha256": digest(current),
        "qualification_source_sha256": (
            qualified["source_generation_sha256"] if qualified is not None else None
        ),
        "qualification_record_sha256": (
            qualified["record_sha256"] if qualified is not None else None
        ),
    }


class PlanningOutputStore:
    """Durable OutputAuthority fed solely by a private planning producer."""

    def __init__(
        self, database: Path, *, authority_kind: str, existing_only: bool = False
    ) -> None:
        if authority_kind not in {"fixture", "production"}:
            raise RunError("PLANNING_OUTPUT_AUTHORITY_INVALID")
        self.database = database.absolute()
        self.authority_kind = authority_kind
        self._source_reader: Callable[[dict[str, Any]], dict[str, Any]] | None = None
        if existing_only and not self.database.is_file():
            raise RunError("PLANNING_OUTPUT_AUTHORITY_UNAVAILABLE")
        if not existing_only:
            self.database.parent.mkdir(parents=True, exist_ok=True)
        if existing_only:
            require_schema(
                self.database,
                {
                    "planning_output_sources": ["execution_id", "binding_sha256", "source_sha256"],
                    "planning_outputs": [
                        "execution_id",
                        "binding_sha256",
                        "source_sha256",
                        "content",
                        "content_sha256",
                    ],
                    "planning_output_claims": ["execution_id", "binding_sha256", "state"],
                    "planning_execute_commands": [
                        "principal",
                        "command_key",
                        "execution_id",
                        "binding_sha256",
                    ],
                },
            )
        with self._transaction() as db:
            if not existing_only:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS planning_output_sources ("
                    "execution_id TEXT PRIMARY KEY, binding_sha256 TEXT NOT NULL, "
                    "source_sha256 TEXT NOT NULL)"
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS planning_outputs ("
                    "execution_id TEXT PRIMARY KEY, binding_sha256 TEXT NOT NULL, "
                    "source_sha256 TEXT NOT NULL, content BLOB NOT NULL, "
                    "content_sha256 TEXT NOT NULL)"
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS planning_output_claims ("
                    "execution_id TEXT PRIMARY KEY, binding_sha256 TEXT NOT NULL, "
                    "state TEXT NOT NULL)"
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS planning_execute_commands ("
                    "principal TEXT NOT NULL, command_key TEXT NOT NULL, "
                    "execution_id TEXT NOT NULL, binding_sha256 TEXT NOT NULL, "
                    "PRIMARY KEY(principal, command_key))"
                )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.database, isolation_level=None)
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
    def _binding(binding: dict[str, Any]) -> tuple[str, str]:
        execution_id = binding.get("execution_id")
        if not isinstance(execution_id, str):
            raise RunError("PLANNING_OUTPUT_BINDING_INVALID")
        identifier(execution_id)
        return execution_id, digest(binding)

    def arm(self, binding: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
        execution_id, binding_sha256 = self._binding(binding)
        source_sha256 = digest(source)
        with self._transaction() as db:
            row = db.execute(
                "SELECT binding_sha256,source_sha256 FROM planning_output_sources "
                "WHERE execution_id=?",
                (execution_id,),
            ).fetchone()
            if row is None:
                db.execute(
                    "INSERT INTO planning_output_sources VALUES (?,?,?)",
                    (execution_id, binding_sha256, source_sha256),
                )
            elif tuple(row) != (binding_sha256, source_sha256):
                raise RunError("PLANNING_OUTPUT_SOURCE_CHANGED")
        return self.read_source(binding)

    def bind_source_reader(self, reader: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
        if self._source_reader is not None and self._source_reader is not reader:
            raise RunError("PLANNING_OUTPUT_SOURCE_READER_CHANGED")
        self._source_reader = reader

    def publish(self, binding: dict[str, Any], content: bytes) -> dict[str, Any]:
        execution_id, binding_sha256 = self._binding(binding)
        if type(content) is not bytes or len(content) > 1_000_000:
            raise RunError("PLANNING_OUTPUT_INVALID")
        with self._transaction() as db:
            source = db.execute(
                "SELECT binding_sha256,source_sha256 FROM planning_output_sources "
                "WHERE execution_id=?",
                (execution_id,),
            ).fetchone()
            if source is None or source[0] != binding_sha256:
                raise RunError("PLANNING_OUTPUT_SOURCE_UNAVAILABLE")
            content_sha256 = hashlib.sha256(content).hexdigest()
            row = db.execute(
                "SELECT binding_sha256,source_sha256,content_sha256 FROM planning_outputs "
                "WHERE execution_id=?",
                (execution_id,),
            ).fetchone()
            expected = (binding_sha256, source[1], content_sha256)
            if row is None:
                db.execute(
                    "INSERT INTO planning_outputs VALUES (?,?,?,?,?)",
                    (execution_id, binding_sha256, source[1], content, content_sha256),
                )
            elif tuple(row) != expected:
                raise RunError("PLANNING_OUTPUT_EVIDENCE_CHANGED")
            db.execute(
                "UPDATE planning_output_claims SET state='completed' "
                "WHERE execution_id=? AND binding_sha256=?",
                (execution_id, binding_sha256),
            )
        return self.read_output(execution_id, binding)

    def claim_dispatch(self, binding: dict[str, Any]) -> str:
        """Durably allow exactly one producer attempt for this execution.

        A caller that loses its process after this insert leaves ``pending``.
        That is deliberately recoverable only as an unknown, never as a new
        provider request with a new grant or credential generation.
        """
        execution_id, binding_sha256 = self._binding(binding)
        with self._transaction() as db:
            source = db.execute(
                "SELECT binding_sha256 FROM planning_output_sources WHERE execution_id=?",
                (execution_id,),
            ).fetchone()
            if source is None or source[0] != binding_sha256:
                raise RunError("PLANNING_OUTPUT_SOURCE_UNAVAILABLE")
            row = db.execute(
                "SELECT binding_sha256,state FROM planning_output_claims WHERE execution_id=?",
                (execution_id,),
            ).fetchone()
            if row is None:
                db.execute(
                    "INSERT INTO planning_output_claims VALUES (?,?,?)",
                    (execution_id, binding_sha256, "pending"),
                )
                return "claimed"
            if row[0] != binding_sha256 or row[1] not in {"pending", "completed"}:
                raise RunError("PLANNING_OUTPUT_EVIDENCE_CHANGED")
            return str(row[1])

    def claim_execute_command(
        self, binding: dict[str, Any], *, principal: str, command_key: str
    ) -> None:
        """Bind the browser command before any producer or Run-store effect.

        The output claim prevents a second send for one execution.  This
        separate receipt prevents one idempotency key from being used to start
        a different execution before that execution reaches the output claim.
        """
        identifier(principal)
        identifier(command_key)
        execution_id, binding_sha256 = self._binding(binding)
        with self._transaction() as db:
            row = db.execute(
                "SELECT execution_id,binding_sha256 FROM planning_execute_commands "
                "WHERE principal=? AND command_key=?",
                (principal, command_key),
            ).fetchone()
            expected = (execution_id, binding_sha256)
            if row is None:
                db.execute(
                    "INSERT INTO planning_execute_commands VALUES (?,?,?,?)",
                    (principal, command_key, *expected),
                )
            elif tuple(row) != expected:
                raise RunError("IDEMPOTENCY_CONFLICT")

    def read_source(self, binding: dict[str, Any]) -> dict[str, Any]:
        execution_id, binding_sha256 = self._binding(binding)
        with sqlite3.connect(self.database) as db:
            row = db.execute(
                "SELECT binding_sha256,source_sha256 FROM planning_output_sources "
                "WHERE execution_id=?",
                (execution_id,),
            ).fetchone()
        if row is None or row[0] != binding_sha256:
            raise RunError("PLANNING_OUTPUT_SOURCE_UNAVAILABLE")
        source = {
            "schema_version": "karajan.planning-output-source.v1",
            "binding_sha256": binding_sha256,
            "authority_kind": self.authority_kind,
            "source_sha256": row[1],
        }
        if self._source_reader is not None:
            try:
                current = self._source_reader(binding)
            except Exception as error:
                raise RunError("PLANNING_OUTPUT_SOURCE_UNAVAILABLE") from error
            if digest(current) != source["source_sha256"]:
                raise RunError("PLANNING_OUTPUT_SOURCE_CHANGED")
        return source

    def read_output(self, execution_id: str, binding: dict[str, Any]) -> dict[str, Any]:
        expected_execution_id, binding_sha256 = self._binding(binding)
        if execution_id != expected_execution_id:
            raise RunError("PLANNING_OUTPUT_BINDING_INVALID")
        with sqlite3.connect(self.database) as db:
            row = db.execute(
                "SELECT binding_sha256,source_sha256,content,content_sha256 FROM planning_outputs "
                "WHERE execution_id=?",
                (execution_id,),
            ).fetchone()
        if row is None or row[0] != binding_sha256 or hashlib.sha256(row[2]).hexdigest() != row[3]:
            raise RunError("PLANNING_OUTPUT_UNAVAILABLE")
        return {
            "schema_version": "karajan.planning-output-evidence.v1",
            "execution_id": execution_id,
            "binding_sha256": binding_sha256,
            "authority_kind": self.authority_kind,
            "source_sha256": row[1],
            "completed": True,
            "artifact_sha256": row[3],
            "artifact_size": len(row[2]),
            "content": row[2],
        }


class PlanningTransport:
    """One owner operation over an already-durable PlanningExecution identity."""

    def __init__(
        self,
        execution: PlanningExecution,
        accounting: GoRequestAccounting,
        producer: PlanningProducer,
        outputs: PlanningOutputStore,
    ) -> None:
        if producer.authority_kind != outputs.authority_kind:
            raise RunError("PLANNING_OUTPUT_AUTHORITY_INVALID")
        self.execution, self.accounting = execution, accounting
        self.producer, self.outputs = producer, outputs

    @classmethod
    def from_trusted_factory(cls, control_directory: Path) -> PlanningTransport:
        """Reopen the one fixed production transport; never provision it."""
        execution = PlanningExecution.from_trusted_factory(control_directory)
        producer = ProductionGoPlanningProducer.from_trusted_factory(
            control_directory, execution
        )
        outputs = execution.outputs
        if not isinstance(outputs, PlanningOutputStore):
            raise RunError("PLANNING_OUTPUT_AUTHORITY_UNAVAILABLE")
        return cls(execution, producer.accounting, producer, outputs)

    def execute(self, execution_id: str, *, principal: str, command_key: str) -> dict[str, Any]:
        for value in (execution_id, principal, command_key):
            identifier(value)
        current = self.execution.get(execution_id, principal=principal)
        model_input: PlanningModelInput | None = None
        if current["state"] == "admission_unknown":
            # Unknown admission is receipt-only recovery.  Do not compile,
            # register, activate, or dispatch until the original persisted
            # receipt itself proves an admitted transition.
            current = self.execution.reconcile(execution_id, principal=principal)
            if current["state"] == "admission_unknown":
                return current
            if current["state"] != "awaiting_output":
                return current
        if current["state"] == "awaiting_admission":
            # A non-request estimate has no trusted conversion.  This lookup
            # is deliberately read-only: command identity must be durable
            # before snapshot, estimate, or any other stateful operation.
            self._preflight_controller_estimate_unit(current, principal=principal)
        # Persist the user command's exact subject/resource binding before an
        # output source is armed, a dispatch is claimed, or a producer can send.
        self.outputs.claim_execute_command(
            current["binding"], principal=principal, command_key=command_key
        )
        if current["state"] == "awaiting_output":
            pending = self._dispatch_or_recover_output(
                current, principal=principal, model_input=model_input
            )
            if pending is not None:
                return pending
        if current["state"] == "awaiting_admission":
            if model_input is None:
                self.execution.freeze_repository_snapshot(
                    execution_id,
                    principal=principal,
                    command_key="planning-snapshot:" + execution_id,
                )
                model_input = compile_planning_input(
                    self.execution,
                    self.accounting,
                    execution_id=execution_id,
                    principal=principal,
                )
                if current["state"] == "awaiting_admission":
                    try:
                        self._register_controller_estimate(
                            current, model_input, principal=principal
                        )
                    except RunError as error:
                        if str(error) != "PLANNING_ESTIMATE_SOURCE_UNAVAILABLE":
                            raise
            # This records a read-only output identity.  ``admit`` verifies it
            # before it can transition to awaiting_output; it does not send.
            self.outputs.arm(current["binding"], self.producer.source(current["binding"]))
            current = self.execution.admit(
                execution_id, principal=principal, command_key="planning-admit:" + execution_id
            )
            if current["state"] == "awaiting_output":
                pending = self._dispatch_or_recover_output(
                    current, principal=principal, model_input=model_input
                )
                if pending is not None:
                    return pending
        return self.execution.submit(execution_id, principal=principal, command_key=command_key)

    def _dispatch_or_recover_output(
        self,
        current: dict[str, Any],
        *,
        principal: str,
        model_input: PlanningModelInput | None,
    ) -> dict[str, Any] | None:
        """Let the one claimant produce; pending dispatch remains unknown."""
        revalidate = getattr(self.producer, "revalidate_source", None)
        if callable(revalidate):
            self.outputs.arm(current["binding"], revalidate(current["binding"]))
        dispatch = self.outputs.claim_dispatch(current["binding"])
        if dispatch == "completed":
            return None
        if dispatch == "pending":
            return self.execution.get(current["id"], principal=principal)
        if model_input is None:
            model_input = compile_planning_input(
                self.execution,
                self.accounting,
                execution_id=current["id"],
                principal=principal,
            )
        admission = current.get("admission")
        if not isinstance(admission, dict):
            raise RunError("PLANNING_ADMISSION_EVIDENCE_INVALID")
        content = self.producer.produce(
            model_input, binding=current["binding"], admission=admission
        )
        self.outputs.publish(current["binding"], content)
        return None

    def _preflight_controller_estimate_unit(
        self, execution: dict[str, Any], *, principal: str
    ) -> None:
        """Reject only an unconvertible live unit before binding the command."""
        try:
            source = self._controller_estimate_source(execution, principal=principal)
        except RunError as error:
            # Preserve the existing source-unavailable path: it records the
            # command first, then lets admission supply its existing blocker.
            if str(error) == "PLANNING_ESTIMATE_SOURCE_UNAVAILABLE":
                return
            raise
        if source is None:
            return
        _, _, _, pools, _, selected, _ = source
        if any(selected[pool].get("unit") != "requests" for pool in pools):
            raise RunError("PLANNING_ESTIMATE_UNIT_UNSUPPORTED")

    def _controller_estimate_source(
        self, execution: dict[str, Any], *, principal: str
    ) -> tuple[
        Any,
        dict[str, Any],
        dict[str, Any],
        list[str],
        dict[str, Any],
        dict[str, dict[str, Any]],
        dict[str, Any],
    ] | None:
        """Read the fixed profile pools and current capacity windows without effects."""
        authority = self.execution.admissions
        if authority is None or getattr(authority, "authority_kind", None) != "production":
            return None
        admissions: Any = authority
        binding = execution["binding"]
        if not isinstance(binding, dict):
            raise RunError("PLANNING_ESTIMATE_SOURCE_UNAVAILABLE")
        run = self.execution.planner.get(binding["run_id"], principal=principal)
        profiles = run["configuration_snapshot"]["configuration"]["resources"]["profiles"]
        registration = next(
            (
                row
                for row in profiles
                if {"id": row["id"], "revision": row["revision"]} == binding["profile"]
            ),
            None,
        )
        profile = registration.get("profile") if isinstance(registration, dict) else None
        account_id = (
            profile.get("binding", {}).get("account_id") if isinstance(profile, dict) else None
        )
        pools = registration.get("quota_pool_refs") if isinstance(registration, dict) else None
        if not isinstance(account_id, str) or not isinstance(pools, list) or not pools:
            raise RunError("PLANNING_ESTIMATE_SOURCE_UNAVAILABLE")
        account = next(
            (
                row
                for row in admissions.capacity.resource_view()["accounts"]
                if row.get("id") == account_id
            ),
            None,
        )
        if not isinstance(account, dict) or type(account.get("policy_revision")) is not int:
            raise RunError("PLANNING_ESTIMATE_SOURCE_UNAVAILABLE")
        pool_ids = [pool for pool in pools if isinstance(pool, str)]
        if len(pool_ids) != len(pools):
            raise RunError("PLANNING_ESTIMATE_SOURCE_UNAVAILABLE")
        selected = {
            row_id: cast(dict[str, Any], row)
            for row in account.get("pools", [])
            if isinstance(row, dict) and isinstance(row_id := row.get("id"), str)
        }
        if any(pool not in selected for pool in pool_ids):
            raise RunError("PLANNING_ESTIMATE_SOURCE_UNAVAILABLE")
        windows: dict[str, Any] = {}
        for pool in pool_ids:
            window = selected[pool].get("window_id")
            if not isinstance(window, str):
                raise RunError("PLANNING_ESTIMATE_SOURCE_UNAVAILABLE")
            windows[pool] = window
        return admissions, binding, run, pool_ids, cast(dict[str, Any], account), selected, windows

    def _register_controller_estimate(
        self, execution: dict[str, Any], model_input: PlanningModelInput, *, principal: str
    ) -> None:
        """Derive the one finite estimate from frozen input and live capacity.

        This is controller-only bookkeeping.  The browser cannot nominate a
        pool, amount, duration, policy revision, or budget; a missing current
        capacity observation fails admission rather than widening the request.
        """
        del model_input
        source = self._controller_estimate_source(execution, principal=principal)
        if source is None:
            return
        admissions, binding, run, pools, account, selected, windows = source
        # A planning invocation has one countable request.  There is no trusted
        # conversion from its frozen input to a percentage or token quantity, so
        # refuse those pools rather than under-reserving an arbitrary "1".
        if any(selected[pool].get("unit") != "requests" for pool in pools):
            raise RunError("PLANNING_ESTIMATE_UNIT_UNSUPPORTED")
        ceiling = run["authorization_ceiling"]
        budget = next(
            (
                item
                for item in run["configuration_snapshot"]["configuration"]["resources"][
                    "budgets"
                ]
                if item["id"] == binding["budget_ref"]
            ),
            None,
        )
        if not isinstance(budget, dict):
            raise RunError("PLANNING_ESTIMATE_SOURCE_UNAVAILABLE")
        limit = min(
            300,
            ceiling["max_attempt_duration_seconds"],
            budget["max_duration_seconds"],
        )
        if type(limit) is not int or limit < 1:
            raise RunError("PLANNING_ESTIMATE_SOURCE_UNAVAILABLE")
        admissions.register_estimate(
            binding["run_id"],
            binding["budget_ref"],
            binding["profile"],
            demand={pool: "1" for pool in pools},
            expected_capacity={
                "policy_revision": account["policy_revision"],
                "pool_windows": windows,
                "lead_reserve_access": True,
            },
            duration_seconds=limit,
            max_requests=1,
            max_duration_seconds=limit,
            principal=principal,
            command_key="planning-estimate:" + binding["execution_id"],
        )


class FixtureGoPlanningProducer:
    """C/P-only producer using a real local Go Relay and durable Go Journal.

    ``upstream`` is injected by the test fixture.  It produces an SSE response;
    the browser cannot select it, provide an endpoint, or provide plan content.
    """

    authority_kind = "fixture"

    def __init__(
        self,
        journal: GoCallJournal,
        accounting: GoRequestAccounting,
        *,
        upstream: Callable[[httpx.Request], httpx.Response],
        runtime: Path | None = None,
        work_root: Path | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.journal, self.accounting, self.upstream, self.now = journal, accounting, upstream, now
        self.runtime, self.work_root = runtime, work_root
        if (runtime is None) != (work_root is None):
            raise RunError("PLANNING_NATIVE_CONFIGURATION_INVALID")

    def source(self, binding: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": "karajan.fixture-go-planning-output-source.v1",
            "execution_id": binding["execution_id"],
            "binding_sha256": digest(binding),
            "producer_sha256": hashlib.sha256(type(self).__name__.encode()).hexdigest(),
        }

    def produce(
        self,
        model_input: PlanningModelInput,
        *,
        binding: dict[str, Any],
        admission: dict[str, Any],
    ) -> bytes:
        policy = model_input.execution_policy
        context_policy = policy.get("context_policy")
        if not isinstance(context_policy, dict):
            raise RunError("PLANNING_INPUT_POLICY_UNSUPPORTED")
        reserved = context_policy.get("reserved_output_tokens")
        maximum = policy.get("max_context_tokens")
        if type(reserved) is not int or type(maximum) is not int:
            raise RunError("PLANNING_INPUT_POLICY_UNSUPPORTED")
        if self.accounting.source() != model_input.accounting_source:
            raise RunError("PLANNING_ACCOUNTING_SOURCE_CHANGED")
        source_sha256 = digest(model_input.accounting_source)
        grant_binding = {
            "schema_version": "karajan.go-planning-native-grant.v1",
            "attempt_id": binding["attempt_id"],
            "fence": binding["fence"],
            "profile_digest": binding["profile_sha256"],
            "runtime_digest": hashlib.sha256(b"fixture-go-planning-runtime.v1").hexdigest(),
            "channel": "opencode-go",
            "model": "glm-5.3-flash",
            "auth_generation": "fixture",
            "expires_at": self.now() + 60,
            "max_requests": 1,
            "subject": {
                "kind": "planning_execution",
                "project_id": binding["project_id"],
                "run_id": binding["run_id"],
                "intent_id": binding["intent_id"],
                "execution_id": binding["execution_id"],
            },
            "planning_binding_sha256": digest(binding),
            "admission_sha256": digest(admission),
            "input_sha256": model_input.artifact_sha256,
            "authentication_source_digest": hashlib.sha256(b"fixture").hexdigest(),
            "context": {
                "source_sha256": source_sha256,
                "approved_input_tokens": maximum,
                "reserved_output_tokens": reserved,
                "operating_context_tokens": maximum,
                "fixed_margin": 2048,
                "ratio_margin_basis_points": 1000,
            },
            "tool_policy": "none",
        }
        grant_id = "planning-" + binding["execution_id"]
        grant = self.journal.create_grant(grant_binding, grant_id=grant_id)
        context = GoPlanningRelayContext(
            accounting=self.accounting,
            **grant_binding["context"],
            planning_binding_sha256=grant_binding["planning_binding_sha256"],
            admission_sha256=grant_binding["admission_sha256"],
            input_sha256=grant_binding["input_sha256"],
        )
        relay = GoRelay(
            "fixture-provider-secret",
            "fixture-denied-canary",
            authorization=GoRelayAuthorization(
                self.journal, grant_id, grant_binding, grant["capability"]
            ),
            context=context,
            send_guard=lambda: _allowed(),
            client_factory=lambda: httpx.Client(
                transport=httpx.MockTransport(self.upstream), trust_env=False
            ),
        )
        socket_root = None
        native = None
        native_log_error: str | None = None
        if self.runtime is None:
            relay.start()
        else:
            runtime, work_root = self.runtime, self.work_root
            if work_root is None:
                raise RunError("PLANNING_NATIVE_CONFIGURATION_INVALID")
            socket_root = _relay_socket_root()
            socket = socket_root.path / "inference.sock"
            relay.start(unix_socket=socket)
            work_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory = work_root / ("planning-" + binding["execution_id"])
            projection_path = "planning-input.json"
            projection = {
                "path": projection_path,
                "sha256": hashlib.sha256(_artifact_bytes(model_input)).hexdigest(),
                "writable": False,
            }
            native = IsolatedOpenCode(
                runtime,
                directory,
                socket,
                relay.capability,
                output_tokens=reserved,
                projection=[projection],
                no_tools=True,
            )
            (native.workspace / projection_path).write_bytes(_artifact_bytes(model_input))
        try:
            if native is None:
                with httpx.Client(trust_env=False, timeout=10) as client:
                    response = client.post(
                        relay.url + "/chat/completions",
                        headers={
                            "Authorization": "Bearer " + relay.capability,
                            "x-opencode-session": "planning_" + uuid4().hex,
                        },
                        content=model_input.request_bytes,
                    )
                if response.status_code != 200:
                    raise RunError("PLANNING_TRANSPORT_REJECTED")
                return _sse_content(response.content)
            return self._native_output(native, model_input)
        finally:
            self.journal.revoke_grant(grant_id)
            if native is not None:
                try:
                    cleanup = native.close()
                    _native_log_evidence(native.directory, cleanup)
                except RunError as error:
                    native_log_error = error.code
                except Exception:
                    native_log_error = "PLANNING_NATIVE_LOG_EVIDENCE_UNAVAILABLE"
            relay.close()
            if socket_root is not None:
                _cleanup_relay_socket_root(socket_root)
            if native_log_error is not None:
                raise RunError(native_log_error)

    @staticmethod
    def _native_output(
        native: IsolatedOpenCode,
        model_input: PlanningModelInput,
        *,
        timeout_seconds: int = 90,
        completion_guard: Callable[[], AbstractContextManager[dict[str, Any]]] | None = None,
        start_native: bool = True,
    ) -> bytes:
        if type(timeout_seconds) is not int or timeout_seconds < 1:
            raise RunError("PLANNING_NATIVE_TIMEOUT_INVALID")
        if start_native:
            started = native.start()
            if started.get("state") != "running":
                raise RunError("PLANNING_NATIVE_START_FAILED")
        session = native.request("POST", "/session", {"title": "Planning", "agent": "probe"})
        native.request(
            "POST",
            f"/session/{session['id']}/prompt_async",
            {
                "agent": "probe",
                "model": {"providerID": "opencode-go", "modelID": "glm-5.3-flash"},
                "parts": [{"type": "text", "text": model_input.request["messages"][1]["content"]}],
            },
        )
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            messages = native.request("GET", f"/session/{session['id']}/message")
            complete = [
                message
                for message in messages
                if message.get("info", {}).get("role") == "assistant"
                and message.get("info", {}).get("time", {}).get("completed")
            ]
            if complete:
                with _allowed() if completion_guard is None else completion_guard():
                    if len(complete) != 1:
                        raise RunError("PLANNING_NATIVE_OUTPUT_AMBIGUOUS")
                    text = "".join(
                        part.get("text", "")
                        for part in complete[-1].get("parts", [])
                        if part.get("type") == "text" and isinstance(part.get("text"), str)
                    )
                    if complete[-1].get("info", {}).get("finish") == "stop" and text:
                        return text.encode("utf-8")
                    raise RunError("PLANNING_NATIVE_OUTPUT_INVALID")
            time.sleep(0.1)
        raise RunError("PLANNING_NATIVE_TIMEOUT")


class ProductionGoPlanningProducer:
    """The fixed Go no-tools producer assembled from protected controller state.

    It has no endpoint, model, prompt, path, or credential constructor input.
    The Commander qualification reader remains the separate admission authority;
    this producer independently resolves the current sealed credential only at
    the local Relay boundary.
    """

    authority_kind = "production"

    def __init__(
        self,
        execution: PlanningExecution,
        accounting: GoRequestAccounting,
        journal: GoCallJournal,
        runtime: Path,
        work_root: Path,
        credentials: Any,
        descriptor_sha256: str,
    ) -> None:
        self.execution = execution
        self.accounting = accounting
        self.journal = journal
        self.runtime = runtime
        self.work_root = work_root
        self.credentials = credentials
        self.descriptor_sha256 = descriptor_sha256

    @classmethod
    def from_trusted_factory(
        cls, control_directory: Path, execution: PlanningExecution
    ) -> ProductionGoPlanningProducer:
        from karajan.orchestration.go_commander_qualification import (
            read_commander_qualification_settings,
            validate_commander_qualification_settings,
        )
        from karajan.projects.credential_sources import CredentialSourceStore, LocalKeyFile

        settings, descriptor_sha256 = read_commander_qualification_settings(control_directory)
        if settings.journal_path is None or settings.work_root is None:
            raise RunError("PLANNING_PRODUCTION_AUTHORITY_UNAVAILABLE")
        validate_commander_qualification_settings(execution.planner.projects, settings)
        credentials = CredentialSourceStore(
            execution.planner.projects,
            sources={
                (source.project_id, source.auth_ref): LocalKeyFile(source.source_id, source.path)
                for source in settings.credential_sources
            },
            private_directory=settings.credential_private_directory,
            existing_only=True,
        )
        return cls(
            execution,
            GoRequestAccounting(settings.tokenizer_directory),
            GoCallJournal(settings.journal_path, existing_only=True),
            settings.runtime,
            settings.work_root,
            credentials,
            descriptor_sha256,
        )

    def source(self, binding: dict[str, Any]) -> dict[str, Any]:
        if self.execution.admissions is None:
            raise RunError("PLANNING_PRODUCTION_AUTHORITY_UNAVAILABLE")
        control_directory: Path | None = getattr(
            self.execution.admissions, "bootstrap_control_directory", None
        )
        if not isinstance(control_directory, Path):
            raise RunError("PLANNING_PRODUCTION_AUTHORITY_UNAVAILABLE")
        return observe_production_output_source(
            control_directory, self.execution.admissions, binding
        )

    def revalidate_source(self, binding: dict[str, Any]) -> dict[str, Any]:
        """Recheck current production authority before consuming old output."""
        # The OutputAuthority reader is deliberately read-only. Holding an
        # admission effect guard here would retain execution/Run/Capacity while
        # it re-enters the Project source reader, creating a nested DB lock.
        return self.source(binding)

    def _authentication(self, binding: dict[str, Any]) -> tuple[dict[str, Any], Any]:
        run = self.execution.planner.get(binding["run_id"], principal=binding["owner"])
        profiles = run["configuration_snapshot"]["configuration"]["resources"]["profiles"]
        profile = next(
            (
                row
                for row in profiles
                if {"id": row["id"], "revision": row["revision"]} == binding["profile"]
            ),
            None,
        )
        profile_document = profile.get("profile") if isinstance(profile, dict) else None
        auth_ref = profile_document.get("auth_ref") if isinstance(profile_document, dict) else None
        if not isinstance(auth_ref, str):
            raise RunError("PLANNING_AUTHENTICATION_SOURCE_UNAVAILABLE")
        authentication = self.credentials.current(
            binding["project_id"], auth_ref, principal=binding["owner"]
        )
        credential = self.credentials.resolve_exact(
            binding["project_id"],
            auth_ref,
            authentication["generation"],
            principal=binding["owner"],
        )
        return authentication, credential

    def produce(
        self,
        model_input: PlanningModelInput,
        *,
        binding: dict[str, Any],
        admission: dict[str, Any],
    ) -> bytes:
        if sys.platform != "linux" or self.execution.admissions is None:
            raise RunError("PLANNING_NATIVE_CONFIGURATION_INVALID")
        admissions: Any = self.execution.admissions
        policy = model_input.execution_policy
        context_policy = policy.get("context_policy")
        maximum = policy.get("max_context_tokens")
        reserved = (
            context_policy.get("reserved_output_tokens")
            if isinstance(context_policy, dict)
            else None
        )
        if type(maximum) is not int or type(reserved) is not int:
            raise RunError("PLANNING_INPUT_POLICY_UNSUPPORTED")
        if self.accounting.source() != model_input.accounting_source:
            raise RunError("PLANNING_ACCOUNTING_SOURCE_CHANGED")
        duration_seconds = admission.get("duration_seconds")
        if type(duration_seconds) is not int or duration_seconds < 1:
            raise RunError("PLANNING_ADMISSION_EVIDENCE_INVALID")
        authentication, credential = self._authentication(binding)
        runtime_digest = _sha256_file(self.runtime)
        grant_binding = {
            "schema_version": "karajan.go-planning-native-grant.v1",
            "attempt_id": binding["attempt_id"],
            "fence": binding["fence"],
            "profile_digest": binding["profile_sha256"],
            "runtime_digest": runtime_digest,
            "channel": "opencode-go",
            "model": "glm-5.3-flash",
            "auth_generation": authentication["generation"],
            "expires_at": self.execution.clock() + duration_seconds,
            "max_requests": 1,
            "subject": {
                key: binding[key]
                for key in ("project_id", "run_id", "intent_id", "execution_id")
            }
            | {"kind": "planning_execution"},
            "planning_binding_sha256": digest(binding),
            "admission_sha256": digest(admission),
            "input_sha256": model_input.artifact_sha256,
            "authentication_source_digest": digest(authentication),
            "context": {
                "source_sha256": digest(model_input.accounting_source),
                "approved_input_tokens": maximum,
                "reserved_output_tokens": reserved,
                "operating_context_tokens": maximum,
                "fixed_margin": 2048,
                "ratio_margin_basis_points": 1000,
            },
            "tool_policy": "none",
        }
        grant_id = "planning-native-" + binding["execution_id"]
        try:
            with admissions.effect_guard(
                binding["execution_id"],
                binding["owner"],
                "planning-native-grant:" + binding["execution_id"],
            ):
                grant = self.journal.create_grant(grant_binding, grant_id=grant_id)
            if not grant["capability"]:
                raise RunError("PLANNING_GRANT_RECOVERY_REQUIRED")
            context = GoPlanningRelayContext(
                accounting=self.accounting,
                **grant_binding["context"],
                planning_binding_sha256=grant_binding["planning_binding_sha256"],
                admission_sha256=grant_binding["admission_sha256"],
                input_sha256=grant_binding["input_sha256"],
            )
            relay = GoRelay(
                credential.reveal(),
                "planning-denied-" + uuid4().hex,
                authorization=GoRelayAuthorization(
                    self.journal, grant_id, grant_binding, grant["capability"]
                ),
                context=context,
                send_guard=lambda: admissions.effect_guard(
                    binding["execution_id"],
                    binding["owner"],
                    "planning-native-send:" + binding["execution_id"],
                ),
            )
            socket_root = _relay_socket_root()
            native = None
            unregister_native_stop: Callable[[], None] | None = None
            content: bytes | None = None
            relay_result: dict[str, Any] | None = None
            native_cleanup: dict[str, Any] | None = None
            native_log: dict[str, Any] | None = None
            native_log_error: str | None = None
            try:
                socket = socket_root.path / "inference.sock"
                relay.start(unix_socket=socket)
                self.work_root.mkdir(mode=0o700, parents=True, exist_ok=True)
                projection_path = "planning-input.json"
                native = IsolatedOpenCode(
                    self.runtime,
                    self.work_root / ("planning-" + binding["execution_id"]),
                    socket,
                    relay.capability,
                    output_tokens=reserved,
                    projection=[{
                        "path": projection_path,
                        "sha256": hashlib.sha256(_artifact_bytes(model_input)).hexdigest(),
                        "writable": False,
                    }],
                    no_tools=True,
                )
                (native.workspace / projection_path).write_bytes(_artifact_bytes(model_input))
                unregister_native_stop = self.execution.register_native_stopper(
                    binding["execution_id"], native.close
                )
                # Starting the isolated native process is an effect, but its
                # model wait must not retain the execution/Run/Capacity locks:
                # Relay re-enters the send guard on another thread.
                with admissions.effect_guard(
                    binding["execution_id"],
                    binding["owner"],
                    "planning-native-start:" + binding["execution_id"],
                ):
                    native.start()
                self.execution.register_native_stop_proof(
                    binding["execution_id"], digest(binding), native.stop_proof()
                )
                try:
                    content = FixtureGoPlanningProducer._native_output(
                        native,
                        model_input,
                        timeout_seconds=duration_seconds,
                        completion_guard=lambda: admissions.effect_guard(
                            binding["execution_id"],
                            binding["owner"],
                            "planning-native-complete:" + binding["execution_id"],
                        ),
                        start_native=False,
                    )
                except (OSError, ValueError):
                    current = self.execution.get(
                        binding["execution_id"], principal=binding["owner"]
                    )
                    if current["cancel_requested"]:
                        raise RunError("PLANNING_EXECUTION_CANCELLED") from None
                    raise RunError("PLANNING_NATIVE_RUNTIME_FAILED") from None
            finally:
                if unregister_native_stop is not None:
                    unregister_native_stop()
                if native is not None:
                    try:
                        native_cleanup = native.close()
                        native_log = _native_log_evidence(native.directory, native_cleanup)
                    except RunError as error:
                        native_log_error = error.code
                    except Exception:
                        native_cleanup = {"local_stop": "unknown"}
                        native_log_error = "PLANNING_NATIVE_LOG_EVIDENCE_UNAVAILABLE"
                try:
                    relay_result = relay.close()
                except Exception:
                    relay_result = {"status": "unknown"}
                if (
                    native_cleanup is not None
                    and native_cleanup.get("local_stop") == "confirmed"
                    and relay_result.get("status") == "closed"
                ):
                    try:
                        _cleanup_relay_socket_root(socket_root)
                    except Exception:
                        relay_result = {"status": "unknown"}
            if (
                content is None
                or native_cleanup is None
                or native_cleanup.get("local_stop") != "confirmed"
                or native_log is None
                or relay_result is None
                or relay_result.get("status") != "closed"
            ):
                raise RunError(native_log_error or "PLANNING_NATIVE_CLEANUP_UNKNOWN")
            self._assert_completed_call(grant_id)
            return content
        finally:
            self.journal.revoke_grant(grant_id)

    def _assert_completed_call(self, grant_id: str) -> None:
        snapshot = self.journal.snapshot(grant_id)
        calls = snapshot.get("calls")
        if snapshot.get("state") != "active" or not isinstance(calls, list) or len(calls) != 1:
            raise RunError("PLANNING_NATIVE_COMPLETION_UNKNOWN")
        outcome = calls[0].get("outcome") if isinstance(calls[0], dict) else None
        if not isinstance(outcome, dict) or (
            outcome.get("state") != "response_received"
            or outcome.get("protocol_passed") is not True
            or not isinstance(outcome.get("usage"), dict)
            or not isinstance(outcome.get("response_bytes"), int)
            or outcome["response_bytes"] < 1
            or outcome.get("reason_codes")
        ):
            raise RunError("PLANNING_NATIVE_COMPLETION_UNKNOWN")

@contextmanager
def _allowed() -> Iterator[None]:
    yield


def _sse_content(raw: bytes) -> bytes:
    """Extract only plain assistant content after Relay has validated SSE framing."""
    try:
        text = raw.decode("utf-8")
        pieces: list[str] = []
        for event in text.replace("\r\n", "\n").split("\n\n"):
            data = [
                line[5:].removeprefix(" ")
                for line in event.split("\n")
                if line.startswith("data:")
            ]
            if not data or "\n".join(data) == "[DONE]":
                continue
            value = json.loads("\n".join(data))
            for choice in value.get("choices", []):
                content = choice.get("delta", {}).get("content")
                if isinstance(content, str):
                    pieces.append(content)
        result = "".join(pieces).encode("utf-8")
    except (UnicodeError, TypeError, ValueError, AttributeError):
        raise RunError("PLANNING_TRANSPORT_OUTPUT_INVALID") from None
    if not result:
        raise RunError("PLANNING_TRANSPORT_OUTPUT_INVALID")
    return result
