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

    def __init__(self, database: Path, *, authority_kind: str) -> None:
        if authority_kind not in {"fixture", "production"}:
            raise RunError("PLANNING_OUTPUT_AUTHORITY_INVALID")
        self.database = database.resolve()
        self.authority_kind = authority_kind
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as db:
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
        return self.read_output(execution_id, binding)

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

    def execute(self, execution_id: str, *, principal: str, command_key: str) -> dict[str, Any]:
        for value in (execution_id, principal, command_key):
            identifier(value)
        current = self.execution.get(execution_id, principal=principal)
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
            self.outputs.arm(current["binding"], self.producer.source(current["binding"]))
            current = self.execution.admit(
                execution_id, principal=principal, command_key="planning-admit:" + execution_id
            )
            if current["state"] == "awaiting_output":
                admission = current.get("admission")
                if not isinstance(admission, dict):
                    raise RunError("PLANNING_ADMISSION_EVIDENCE_INVALID")
                content = self.producer.produce(
                    model_input, binding=current["binding"], admission=admission
                )
                self.outputs.publish(current["binding"], content)
        return self.execution.submit(execution_id, principal=principal, command_key=command_key)


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
            socket_root = _relay_socket_root()
            socket = socket_root.path / "inference.sock"
            relay.start(unix_socket=socket)
            directory = self.work_root / ("planning-" + binding["execution_id"])
            native = IsolatedOpenCode(
                self.runtime, directory, socket, relay.capability, projection=[], no_tools=True
            )
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
            if native is not None:
                native.close()
            relay.close()
            if socket_root is not None:
                _cleanup_relay_socket_root(socket_root)

    @staticmethod
    def _native_output(native: IsolatedOpenCode, model_input: PlanningModelInput) -> bytes:
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
        deadline = time.monotonic() + 90
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
