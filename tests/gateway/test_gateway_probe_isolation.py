"""Probe isolation and command-key reservation under real concurrency.

These regressions use an actual file-backed SQLite database and a controlled
loopback HTTP upstream that can be held open, so the assertions cover real lock
and race behavior rather than a simulation.
"""

import json
import sqlite3
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from gateway_fixtures import (
    ORIGIN,
    RecordingHandler,
    binding_payload,
    connection_payload,
    login,
    make_repository,
    upstream,
)
from karajan.gateway import GatewayCatalogStore
from karajan.gateway.catalog import PENDING_SCHEMA_VERSION
from karajan.projects import ProjectRegistry
from karajan.web import create_app

CONNECTION = "local-gateway"


class GatedHandler(RecordingHandler):
    """Hold the first request open until the test releases it."""

    def do_GET(self) -> None:
        self.server.received.append(self.path)  # type: ignore[attr-defined]
        self.server.started.set()  # type: ignore[attr-defined]
        # Wait for the test to release, then answer normally.
        self.server.release.wait(timeout=30)  # type: ignore[attr-defined]
        body = json.dumps(self.catalog).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def gated_server() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), GatedHandler)
    server.daemon_threads = True
    server.received = []  # type: ignore[attr-defined]
    server.started = threading.Event()  # type: ignore[attr-defined]
    server.release = threading.Event()  # type: ignore[attr-defined]
    return server


@pytest.fixture
def case(tmp_path: Path) -> Any:
    repository = make_repository(tmp_path)
    directory = tmp_path / "state"
    directory.mkdir()
    registry = ProjectRegistry(directory / "projects.sqlite", [repository.parent])

    def add(name: str) -> str:
        return str(
            registry.create(
                {
                    "name": name,
                    "repository_path": str(repository),
                    "base_ref": "main",
                    "target_branch": "main",
                    "allowed_target_branches": ["main"],
                },
                command_key=f"project-{name}",
                principal="owner",
            )["id"]
        )

    with upstream(RecordingHandler) as quiet:
        quiet.received = []  # type: ignore[attr-defined]
        yield {
            "tmp_path": tmp_path,
            "repository": repository,
            "directory": directory,
            "registry": registry,
            "project_a": add("a"),
            "project_b": add("b"),
            "quiet": quiet,
            "app": create_app(
                directory,
                origin=ORIGIN,
                bootstrap_token="bootstrap",
                allowed_roots=[repository.parent],
            ),
        }


def url(project_id: str, suffix: str) -> str:
    return f"/v1/projects/{project_id}{suffix}"


def connection_client(
    case: dict[str, Any],
    server: Any,
    project_id: str,
    *,
    secret_ref: str | None = None,
    key: str | None = None,
) -> Any:
    """Register one connection in a project using a short-lived session.

    The command key is namespaced per project because the ledger is global to
    the principal: two projects reusing one key would legitimately conflict.
    """
    token = f"setup-{project_id}"
    app = create_app(
        case["directory"],
        origin=ORIGIN,
        bootstrap_token=token,
        allowed_roots=[case["repository"].parent],
    )
    with TestClient(app, base_url=ORIGIN) as client:
        headers = login(client, token)
        response = client.post(
            url(project_id, "/gateway-connections"),
            json=connection_payload(server, secret_ref=secret_ref),
            headers={**headers, "Idempotency-Key": key or f"connection-{project_id}"},
        )
        assert response.status_code == 201, response.text
        return response.json()


def test_a_paused_probe_does_not_block_an_unrelated_project(case: dict[str, Any]) -> None:
    """Finding 3: no project write transaction is held across network I/O."""
    project_a, project_b = case["project_a"], case["project_b"]
    gated = gated_server()
    thread = threading.Thread(target=gated.serve_forever, kwargs={"poll_interval": 0.02})
    thread.daemon = True
    thread.start()
    try:
        connection_client(case, gated, project_a)
        connection_client(case, case["quiet"], project_b)
        store = GatewayCatalogStore(case["registry"])

        blocked = threading.Event()
        outcome: dict[str, Any] = {}

        def paused_probe() -> None:
            blocked.wait(timeout=10)
            outcome["probe"] = store.probe_catalog(
                project_a, CONNECTION, 1, principal="owner", command_key="probe-a"
            )

        worker = threading.Thread(target=paused_probe)
        worker.start()
        blocked.set()
        # The probe is now inside the upstream read, holding no project lock.
        assert gated.started.wait(timeout=15), "probe never reached the upstream"

        # Project B must complete both a read and a write while A is paused.
        done = threading.Event()

        def other_project() -> None:
            outcome["b_binding"] = store.create_binding(
                project_b,
                binding_payload(connection_revision=1),
                principal="owner",
                command_key="binding-b",
            )[0]
            outcome["b_list"] = store.list_connections(project_b, principal="owner")
            done.set()

        other = threading.Thread(target=other_project)
        other.start()
        assert done.wait(timeout=15), "project B was blocked by project A's probe"
        assert outcome["b_binding"]["revision"] == 1
        assert [row["id"] for row in outcome["b_list"]] == [CONNECTION]

        gated.release.set()
        worker.join(timeout=20)
        other.join(timeout=20)
        assert outcome["probe"]["catalog"]["status"] == "ok"
        assert outcome["probe"]["state"] == "completed"
    finally:
        gated.release.set()
        gated.shutdown()
        gated.server_close()
        thread.join(timeout=5)


def test_same_key_race_never_sends_a_duplicate_probe(case: dict[str, Any]) -> None:
    """Finding 3: a key reserved by an in-flight probe is not stolen."""
    project = case["project_a"]
    gated = gated_server()
    thread = threading.Thread(target=gated.serve_forever, kwargs={"poll_interval": 0.02})
    thread.daemon = True
    thread.start()
    try:
        connection_client(case, gated, project)
        store = GatewayCatalogStore(case["registry"])
        started = threading.Event()
        results: dict[str, Any] = {}

        def first() -> None:
            started.wait(timeout=10)
            results["first"] = store.probe_catalog(
                project, CONNECTION, 1, principal="owner", command_key="shared-key"
            )

        worker = threading.Thread(target=first)
        worker.start()
        started.set()
        assert gated.started.wait(timeout=15)

        # While the probe is in flight, the same key is already reserved.
        second = store.probe_catalog(
            project, CONNECTION, 1, principal="owner", command_key="shared-key"
        )
        assert second["state"] == "pending"
        assert second["observation_available"] is False
        assert "observation_id" not in second
        assert second["catalog"]["status"] == "probe_in_progress"
        # The request count is unknown, not asserted to be zero.
        assert second["catalog"]["requests_sent"] is None
        assert second["retry_with_new_command_key"] is True

        # A different command reusing the same key must conflict, not overwrite.
        with pytest.raises(Exception) as conflict:
            store.create_binding(
                project,
                binding_payload(connection_revision=1),
                principal="owner",
                command_key="shared-key",
            )
        assert getattr(conflict.value, "code", str(conflict.value)) == "IDEMPOTENCY_CONFLICT"
        assert case["registry"].get(project) is not None

        gated.release.set()
        worker.join(timeout=20)
        assert results["first"]["state"] == "completed"
        assert results["first"]["catalog"]["status"] == "ok"
        # Exactly one upstream request was made for this key.
        assert len(gated.received) == 1  # type: ignore[attr-defined]
        # A replay returns the settled observation, still no second request.
        replay = store.probe_catalog(
            project, CONNECTION, 1, principal="owner", command_key="shared-key"
        )
        assert replay == results["first"]
        assert len(gated.received) == 1  # type: ignore[attr-defined]
    finally:
        gated.release.set()
        gated.shutdown()
        gated.server_close()
        thread.join(timeout=5)


class AbortingResolver:
    """Stand in for the worker process ending between claim and observation.

    It is reached through the real public path, so the claim and its ``commands``
    reservation are written by production code. Raising ``SystemExit`` mirrors an
    abrupt termination: it is not caught by the probe's network handlers, so no
    observation is ever stored and the claim is left ``in_progress``.
    """

    def __init__(self) -> None:
        self.calls = 0

    def resolve(self, project_id: str, secret_ref: str) -> str:
        self.calls += 1
        raise SystemExit("worker terminated")


def test_interrupted_claim_is_reconciled_without_resending(case: dict[str, Any]) -> None:
    """Recovery: interruption between claim and observation, then same-key retry.

    The claim is opened through the real public path (which also writes the
    ``commands`` reservation), the process is abandoned before the observation
    is stored, the store is reconstructed over the same file-backed database,
    and the clock advances past the TTL. The retry under the same key must
    produce exactly one durable unknown-outcome receipt and must not resend.
    """
    project = case["project_a"]
    server = gated_server()
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
    thread.daemon = True
    thread.start()
    try:
        connection_client(case, server, project, secret_ref="secret:interrupted")
        database = case["registry"].database
        resolver = AbortingResolver()

        # Interrupt the probe after its claim and reservation are committed.
        interrupting = GatewayCatalogStore(
            ProjectRegistry(database, [case["repository"].parent]),
            resolver=resolver,
            clock=lambda: 1_000.0,
        )
        with pytest.raises(SystemExit):
            interrupting.probe_catalog(
                project, CONNECTION, 1, principal="owner", command_key="interrupted-key"
            )
        assert resolver.calls == 1

        # Reconstruct the store over the same file-backed database.
        recovered_store = GatewayCatalogStore(
            ProjectRegistry(database, [case["repository"].parent]),
            clock=lambda: 1_010.0,
            claim_ttl_seconds=5.0,
        )

        # The claim and its command reservation are durable; no observation is.
        claims = recovered_store.list_probe_claims(project, principal="owner")
        assert [row["state"] for row in claims] == ["in_progress"]
        assert recovered_store.list_catalog_observations(project, principal="owner") == []
        with sqlite3.connect(database) as db:
            reserved = db.execute(
                "SELECT result FROM commands WHERE principal='owner' AND key='interrupted-key'"
            ).fetchone()
        assert reserved is not None
        assert json.loads(reserved[0])["schema_version"] == PENDING_SCHEMA_VERSION

        # Just before the TTL the retry is explicitly pending, not settled.
        pending_store = GatewayCatalogStore(
            ProjectRegistry(database, [case["repository"].parent]),
            clock=lambda: 1_004.0,
            claim_ttl_seconds=5.0,
        )
        pending = pending_store.probe_catalog(
            project, CONNECTION, 1, principal="owner", command_key="interrupted-key"
        )
        assert pending["state"] == "pending"
        assert pending["observation_available"] is False
        assert "observation_id" not in pending
        assert pending["catalog"]["status"] == "probe_in_progress"
        # Unknown, not zero: a request may already have been attempted.
        assert pending["catalog"]["requests_sent"] is None
        assert pending["retry_with_new_command_key"] is True
        assert resolver.calls == 1, "pending must not re-enter the resolver"
        assert server.received == []  # type: ignore[attr-defined]

        # Past the TTL the same key reconciles to one durable unknown outcome.
        result = recovered_store.probe_catalog(
            project, CONNECTION, 1, principal="owner", command_key="interrupted-key"
        )
        assert result["state"] == "unresolved"
        assert result["catalog"]["status"] == "outcome_unknown"
        assert result["catalog"]["reason_codes"] == ["PROBE_INTERRUPTED_OUTCOME_UNKNOWN"]
        assert result["catalog"]["requests_sent"] is None
        assert resolver.calls == 1, "reconciliation must not resend the probe"
        assert server.received == []  # type: ignore[attr-defined]

        # The claim is abandoned and the outcome recorded exactly once. An
        # abandoned claim is terminal, so `expired` (which means "in progress
        # and past its TTL") is False; the settled completion time proves the
        # TTL path ran rather than a fresh probe.
        claims = recovered_store.list_probe_claims(project, principal="owner")
        assert [row["state"] for row in claims] == ["abandoned"]
        assert claims[0]["completed_at"] is not None
        assert claims[0]["observation_id"] == result["observation_id"]
        observations = recovered_store.list_catalog_observations(project, principal="owner")
        assert [row["state"] for row in observations] == ["unresolved"]
        assert [row["observation_id"] for row in observations] == [result["observation_id"]]

        # A further same-key call is an identical replay, with no new work.
        replay = recovered_store.probe_catalog(
            project, CONNECTION, 1, principal="owner", command_key="interrupted-key"
        )
        assert replay == result
        assert resolver.calls == 1
        assert len(recovered_store.list_catalog_observations(project, principal="owner")) == 1

        # A fresh key is the supported way to deliberately probe again, and it
        # is the only thing that reaches the upstream. This store deliberately
        # has no resolver, so the reserved connection's secret_ref is reported
        # as an explicit gap rather than reaching the network: the point is that
        # a new key is processed at all, not that it succeeds.
        fresh = GatewayCatalogStore(
            ProjectRegistry(database, [case["repository"].parent]),
            clock=lambda: 1_020.0,
            claim_ttl_seconds=5.0,
        )
        retried = fresh.probe_catalog(
            project, CONNECTION, 1, principal="owner", command_key="a-new-key"
        )
        assert retried["state"] == "completed"
        assert retried["catalog"]["status"] == "credential_unavailable"
        assert retried["catalog"]["reason_codes"] == ["GATEWAY_SECRET_REF_UNCONFIGURED"]
        assert server.received == []  # type: ignore[attr-defined]
    finally:
        server.release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
