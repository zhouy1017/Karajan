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
import sqlite3
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

import httpx

from karajan.adapters.opencode.go_context import GoRequestAccounting
from karajan.adapters.opencode.go_journal import GoCallJournal
from karajan.adapters.opencode.go_relay import GoPlanningRelayContext, GoRelay, GoRelayAuthorization
from karajan.isolation.go_task import _cleanup_relay_socket_root, _relay_socket_root
from karajan.isolation.opencode_runtime import IsolatedOpenCode
from karajan.runs import RunError
from karajan.runs.planning import digest, identifier

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


class PlanningOutputStore:
    """Durable OutputAuthority fed solely by a private planning producer."""

    def __init__(
        self, database: Path, *, authority_kind: str, existing_only: bool = False
    ) -> None:
        if authority_kind not in {"fixture", "production"}:
            raise RunError("PLANNING_OUTPUT_AUTHORITY_INVALID")
        self.database = database.resolve()
        self.authority_kind = authority_kind
        if existing_only and not self.database.is_file():
            raise RunError("PLANNING_OUTPUT_AUTHORITY_UNAVAILABLE")
        if not existing_only:
            self.database.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as db:
            if existing_only:
                tables = {
                    row[0]
                    for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
                if not {
                    "planning_output_sources",
                    "planning_outputs",
                    "planning_output_claims",
                } <= tables:
                    raise RunError("PLANNING_OUTPUT_AUTHORITY_UNAVAILABLE")
            else:
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
        return {
            "schema_version": "karajan.planning-output-source.v1",
            "binding_sha256": binding_sha256,
            "authority_kind": self.authority_kind,
            "source_sha256": row[1],
        }

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
        if current["state"] == "awaiting_output":
            # Another process either owns the one dispatch attempt or has
            # already published its immutable output.  Only the latter may be
            # submitted; a pending attempt remains visible without a resend.
            if self.outputs.claim_dispatch(current["binding"]) != "completed":
                return current
        if current["state"] == "awaiting_admission":
            self.execution.freeze_repository_snapshot(
                execution_id, principal=principal, command_key="planning-snapshot:" + execution_id
            )
            model_input = compile_planning_input(
                self.execution,
                self.accounting,
                execution_id=execution_id,
                principal=principal,
            )
            self._register_controller_estimate(current, model_input, principal=principal)
            self.outputs.arm(current["binding"], self.producer.source(current["binding"]))
            current = self.execution.admit(
                execution_id, principal=principal, command_key="planning-admit:" + execution_id
            )
            if current["state"] == "awaiting_output":
                admission = current.get("admission")
                if not isinstance(admission, dict):
                    raise RunError("PLANNING_ADMISSION_EVIDENCE_INVALID")
                dispatch = self.outputs.claim_dispatch(current["binding"])
                if dispatch == "claimed":
                    content = self.producer.produce(
                        model_input, binding=current["binding"], admission=admission
                    )
                    self.outputs.publish(current["binding"], content)
                elif dispatch == "pending":
                    return self.execution.get(execution_id, principal=principal)
        return self.execution.submit(execution_id, principal=principal, command_key=command_key)

    def _register_controller_estimate(
        self, execution: dict[str, Any], model_input: PlanningModelInput, *, principal: str
    ) -> None:
        """Derive the one finite estimate from frozen input and live capacity.

        This is controller-only bookkeeping.  The browser cannot nominate a
        pool, amount, duration, policy revision, or budget; a missing current
        capacity observation fails admission rather than widening the request.
        """
        authority = self.execution.admissions
        if authority is None or getattr(authority, "authority_kind", None) != "production":
            return
        admissions: Any = authority
        binding = execution["binding"]
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
        selected = {row.get("id"): row for row in account.get("pools", []) if isinstance(row, dict)}
        if any(not isinstance(pool, str) or pool not in selected for pool in pools):
            raise RunError("PLANNING_ESTIMATE_SOURCE_UNAVAILABLE")
        windows = {pool: selected[pool].get("window_id") for pool in pools}
        if any(not isinstance(window, str) for window in windows.values()):
            raise RunError("PLANNING_ESTIMATE_SOURCE_UNAVAILABLE")
        ceiling = run["authorization_ceiling"]
        budget = next(
            item
            for item in run["configuration_snapshot"]["configuration"]["budgets"]
            if item["id"] == binding["budget_ref"]
        )
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
                native.close()
            relay.close()
            if socket_root is not None:
                _cleanup_relay_socket_root(socket_root)

    @staticmethod
    def _native_output(
        native: IsolatedOpenCode, model_input: PlanningModelInput, *, timeout_seconds: int = 90
    ) -> bytes:
        if type(timeout_seconds) is not int or timeout_seconds < 1:
            raise RunError("PLANNING_NATIVE_TIMEOUT_INVALID")
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
        return {
            "schema_version": "karajan.production-go-planning-output-source.v1",
            "binding_sha256": digest(binding),
            "descriptor_sha256": self.descriptor_sha256,
            "runtime_sha256": _sha256_file(self.runtime),
            "tokenizer_source": self.accounting.source(),
        }

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
        estimate = admission.get("estimate")
        duration_seconds = (
            estimate.get("duration_seconds") if isinstance(estimate, dict) else None
        )
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
            with self.execution.admissions.effect_guard(
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
                send_guard=lambda: self.execution.admissions.effect_guard(
                    binding["execution_id"],
                    binding["owner"],
                    "planning-native-send:" + binding["execution_id"],
                ),
            )
            socket_root = _relay_socket_root()
            native = None
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
                with self.execution.admissions.effect_guard(
                    binding["execution_id"],
                    binding["owner"],
                    "planning-native-start:" + binding["execution_id"],
                ):
                    return FixtureGoPlanningProducer._native_output(
                        native, model_input, timeout_seconds=duration_seconds
                    )
            finally:
                if native is not None:
                    native.close()
                relay.close()
                _cleanup_relay_socket_root(socket_root)
        finally:
            self.journal.revoke_grant(grant_id)

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
