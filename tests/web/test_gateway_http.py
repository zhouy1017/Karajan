"""Authenticated catalog commands persist real versions and probe without inference.

Every request in this module goes to a loopback HTTP server started by the test.
No provider, credential or model is contacted, and no generation request is ever
issued; the upstream records exactly what it received.
"""

import json
import socket
import sqlite3
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from gateway_fixtures import (
    ORIGIN,
    SENTINEL,
    RecordingHandler,
    base_url,
    binding_payload,
    connection_payload,
    login,
    make_repository,
    recording_server,
    upstream,
)
from karajan.gateway.probe import LocalFileSecretResolver
from karajan.projects import ProjectRegistry
from karajan.web import create_app

CONNECTION = "local-gateway"
BINDING = "vendor-binding"


@pytest.fixture
def case(tmp_path: Path) -> Any:
    """A registered project, real web application and one controlled upstream."""
    repository = make_repository(tmp_path)
    directory = tmp_path / "state"
    directory.mkdir()
    registry = ProjectRegistry(directory / "projects.sqlite", [repository.parent])
    project = registry.create(
        {
            "name": "Gateway catalog",
            "repository_path": str(repository),
            "base_ref": "main",
            "target_branch": "main",
            "allowed_target_branches": ["main"],
        },
        command_key="project",
        principal="owner",
    )
    with upstream(RecordingHandler) as server:
        server.received = []  # type: ignore[attr-defined]
        app = create_app(
            directory, origin=ORIGIN, bootstrap_token="bootstrap", allowed_roots=[repository.parent]
        )
        yield {
            "tmp_path": tmp_path,
            "repository": repository,
            "directory": directory,
            "registry": registry,
            "project_id": project["id"],
            "server": server,
            "app": app,
        }


def url(project_id: str, suffix: str) -> str:
    return f"/v1/projects/{project_id}{suffix}"


def create_connection(
    client: TestClient,
    headers: dict[str, str],
    project_id: str,
    server: ThreadingHTTPServer,
    *,
    key: str = "connection-create",
    **overrides: Any,
) -> tuple[int, Any]:
    response = client.post(
        url(project_id, "/gateway-connections"),
        json=connection_payload(server, **overrides),
        headers={**headers, "Idempotency-Key": key},
    )
    return response.status_code, response


def create_binding(
    client: TestClient,
    headers: dict[str, str],
    project_id: str,
    *,
    key: str = "binding-create",
    **overrides: Any,
) -> tuple[int, Any]:
    response = client.post(
        url(project_id, "/gateway-bindings"),
        json=binding_payload(**overrides),
        headers={**headers, "Idempotency-Key": key},
    )
    return response.status_code, response


def test_connection_and_binding_revisions_survive_restart_and_keep_old_references(
    case: dict[str, Any],
) -> None:
    """AC1: immutable revisions, project ownership and durable readback."""
    project_id = case["project_id"]
    with TestClient(case["app"], base_url=ORIGIN) as client:
        headers = login(client)
        status, response = create_connection(client, headers, project_id, case["server"])
        assert status == 201, response.text
        connection = response.json()
        assert response.headers["ETag"] == '"1"'
        assert connection["revision"] == 1
        assert connection["base_url"] == base_url(case["server"])
        assert connection["secret_ref"] is None
        assert connection["execution_eligible"] is False

        status, response = create_binding(client, headers, project_id)
        assert status == 201, response.text
        binding = response.json()
        assert binding["revision"] == 1
        assert binding["connection"]["revision"] == 1
        assert binding["execution_eligible"] is False
        assert binding["dispatch_eligible"] is False
        assert binding["verified"] is False
        assert binding["alias_is_source_identity"] is False
        assert binding["provider_identity_observed"] == "unknown"
        assert binding["declared"]["billing_path"] == "subscription_only"

        # A revision publishes a new immutable record and retains the old one.
        revision_two = connection_payload(case["server"], remote_data_destination=None)
        revision_two["request_transformation"]["revision"] = 2
        response = client.put(
            url(project_id, f"/gateway-connections/{CONNECTION}"),
            json=revision_two,
            headers={**headers, "Idempotency-Key": "connection-revise", "If-Match": '"1"'},
        )
        assert response.status_code == 201, response.text
        assert response.json()["revision"] == 2
        assert response.json()["request_transformation"]["revision"] == 2

        first = client.get(url(project_id, f"/gateway-connections/{CONNECTION}/revisions/1"))
        assert first.status_code == 200
        assert first.json() == connection
        second = client.get(url(project_id, f"/gateway-connections/{CONNECTION}/revisions/2"))
        assert second.json()["revision"] == 2
        assert len(client.get(url(project_id, "/gateway-connections")).json()["items"]) == 2
        # The binding still points at the revision it was created against.
        stored_binding = client.get(
            url(project_id, f"/gateway-bindings/{BINDING}/revisions/1")
        )
        assert stored_binding.json() == binding

    reopened = create_app(
        case["directory"],
        origin=ORIGIN,
        bootstrap_token="second",
        allowed_roots=[case["repository"].parent],
    )
    with TestClient(reopened, base_url=ORIGIN) as client:
        login(client, "second")
        assert (
            client.get(url(project_id, f"/gateway-connections/{CONNECTION}/revisions/1")).json()
            == connection
        )
        assert (
            client.get(url(project_id, f"/gateway-bindings/{BINDING}/revisions/1")).json()
            == binding
        )


def test_write_commands_require_identity_cas_and_ownership(case: dict[str, Any]) -> None:
    """AC2: subject-bound idempotency, CAS, and project scoping."""
    project_id = case["project_id"]
    # A second registered project in the same database, registered before the
    # session is opened: the bootstrap token is single-use per application.
    other = case["registry"].create(
        {
            "name": "Other",
            "repository_path": str(make_repository(case["tmp_path"], "other")),
            "base_ref": "main",
            "target_branch": "main",
            "allowed_target_branches": ["main"],
        },
        command_key="other-project",
        principal="owner",
    )
    with TestClient(case["app"], base_url=ORIGIN) as client:
        headers = login(client)
        status, response = create_connection(client, headers, project_id, case["server"])
        assert status == 201
        original = response.json()

        # Same key and same payload returns the original result, not a new one.
        replayed = client.post(
            url(project_id, "/gateway-connections"),
            json=connection_payload(case["server"]),
            headers={**headers, "Idempotency-Key": "connection-create"},
        )
        assert replayed.status_code == 200
        assert replayed.json() == original
        assert len(client.get(url(project_id, "/gateway-connections")).json()["items"]) == 1

        # Same key with a different payload is a conflict, not a silent overwrite.
        conflict = client.post(
            url(project_id, "/gateway-connections"),
            json=connection_payload(case["server"], connection_id="other-gateway"),
            headers={**headers, "Idempotency-Key": "connection-create"},
        )
        assert conflict.status_code == 409
        assert conflict.json()["reason_code"] == "IDEMPOTENCY_CONFLICT"

        # A duplicate identity under a fresh key is refused rather than replaced.
        duplicate = client.post(
            url(project_id, "/gateway-connections"),
            json=connection_payload(case["server"]),
            headers={**headers, "Idempotency-Key": "connection-create-again"},
        )
        assert duplicate.status_code == 422
        assert duplicate.json()["reason_code"] == "GATEWAY_CONNECTION_EXISTS"

        # A stale expected revision reports the durable head.
        stale = client.put(
            url(project_id, f"/gateway-connections/{CONNECTION}"),
            json=connection_payload(case["server"]),
            headers={**headers, "Idempotency-Key": "stale-revise", "If-Match": '"7"'},
        )
        assert stale.status_code == 409
        assert stale.json() == {"reason_code": "GATEWAY_REVISION_CONFLICT", "current_revision": 1}

        # A missing preconditions header, and a missing key, keep existing semantics.
        assert (
            client.put(
                url(project_id, f"/gateway-connections/{CONNECTION}"),
                json=connection_payload(case["server"]),
                headers={**headers, "Idempotency-Key": "no-if-match"},
            ).status_code
            == 428
        )
        assert (
            client.put(
                url(project_id, f"/gateway-connections/{CONNECTION}"),
                json=connection_payload(case["server"]),
                headers={**headers, "If-Match": '"1"'},
            ).status_code
            == 400
        )

        # An unknown project on a write is not found, and nothing is created.
        unknown_write = client.post(
            url("00000000-0000-0000-0000-000000000000", "/gateway-connections"),
            json=connection_payload(case["server"]),
            headers={**headers, "Idempotency-Key": "unknown-project"},
        )
        assert unknown_write.status_code == 404
        assert unknown_write.json()["reason_code"] == "GATEWAY_PROJECT_NOT_FOUND"

        # The second project owns no gateway records, and a binding that names
        # the first project's connection is not resolvable from this one.
        assert client.get(url(other["id"], "/gateway-connections")).json()["items"] == []
        assert client.get(url(other["id"], "/gateway-bindings")).json()["items"] == []
        status, response = create_binding(client, headers, other["id"])
        assert status == 404
        assert response.json()["reason_code"] == "GATEWAY_CONNECTION_NOT_FOUND"


def test_unauthenticated_and_wrong_origin_writes_keep_existing_rejection_semantics(
    case: dict[str, Any],
) -> None:
    """AC2: no new authentication path, and no write without a session proof."""
    project_id = case["project_id"]
    with TestClient(case["app"], base_url=ORIGIN) as client:
        anonymous = client.post(
            url(project_id, "/gateway-connections"),
            json=connection_payload(case["server"]),
            headers={"Origin": ORIGIN, "Idempotency-Key": "anonymous"},
        )
        assert anonymous.status_code == 401
        assert anonymous.json()["reason_code"] == "AUTHENTICATION_REQUIRED"

        headers = login(client)
        assert (
            client.post(
                url(project_id, "/gateway-connections"),
                json=connection_payload(case["server"]),
                headers={**headers, "Origin": "https://attacker.test", "Idempotency-Key": "origin"},
            ).status_code
            == 403
        )
        without_csrf = {key: value for key, value in headers.items() if key != "X-CSRF-Token"}
        assert (
            client.post(
                url(project_id, "/gateway-connections"),
                json=connection_payload(case["server"]),
                headers={**without_csrf, "Idempotency-Key": "csrf"},
            ).status_code
            == 403
        )
        assert client.get(url(project_id, "/gateway-connections")).json()["items"] == []

    # A client without the session cookie is rejected on both reads and writes.
    with TestClient(case["app"], base_url=ORIGIN) as anonymous:
        assert (
            anonymous.get(
                url(project_id, "/gateway-connections"), headers={"Origin": ORIGIN}
            ).status_code
            == 401
        )
        assert anonymous.get(url(project_id, "/gateway-bindings")).status_code == 401
        assert anonymous.get(url(project_id, "/gateway-catalog-observations")).status_code == 401


def test_credentials_are_never_accepted_persisted_or_echoed(
    case: dict[str, Any], tmp_path: Path
) -> None:
    """AC3: a trusted resolver supplies the secret; records and errors never carry it."""
    project_id = case["project_id"]
    secret_file = tmp_path / "gateway.key"
    secret_file.write_text(SENTINEL + "-real-material\n", encoding="utf-8")
    resolver = LocalFileSecretResolver(
        sources={(project_id, "secret:local-gateway"): secret_file}
    )
    app = create_app(
        case["directory"],
        origin=ORIGIN,
        bootstrap_token="with-resolver",
        allowed_roots=[case["repository"].parent],
        gateway_secret_resolver=resolver,
    )
    with TestClient(app, base_url=ORIGIN) as client:
        headers = login(client, "with-resolver")

        # A plaintext credential field is refused outright, and not echoed.
        for parameter, value in (
            ("api_key", SENTINEL),
            ("headers", {"Authorization": "Bearer " + SENTINEL}),
        ):
            rejected = client.post(
                url(project_id, "/gateway-bindings"),
                json=binding_payload(
                    declared={
                        "provider_id": "vendor",
                        "account_id": "vendor-account",
                        "billing_path": "subscription_only",
                        "required_parameters": {parameter: value},
                    }
                ),
                headers={**headers, "Idempotency-Key": f"credential-{parameter}"},
            )
            assert rejected.status_code == 422, rejected.text
            assert rejected.json()["reason_code"] == "INPUT_INVALID"
            assert SENTINEL not in rejected.text

        status, response = create_connection(
            client,
            headers,
            project_id,
            case["server"],
            secret_ref="secret:local-gateway",
        )
        assert status == 201, response.text
        connection = response.json()
        assert connection["secret_ref"] == "secret:local-gateway"
        assert connection["credential_material"] == "not_accepted"
        assert SENTINEL not in response.text

        status, response = create_binding(client, headers, project_id)
        assert status == 201, response.text
        assert SENTINEL not in response.text

        probed = client.post(
            url(project_id, f"/gateway-connections/{CONNECTION}/revisions/1/catalog-probes"),
            headers={**headers, "Idempotency-Key": "probe-with-secret"},
        )
        assert probed.status_code == 201, probed.text
        assert SENTINEL not in probed.text
        # The upstream did receive the resolved credential, so resolution is real.
        assert request_headers(case["server"], "/v1/models")["Authorization"] == (
            "Bearer " + SENTINEL + "-real-material"
        )

    # No durable row may contain the credential material.
    raw = (case["directory"] / "projects.sqlite").read_bytes()
    assert SENTINEL.encode() not in raw
    for table, column in (
        ("gateway_connections", "record"),
        ("gateway_bindings", "record"),
        ("gateway_catalog_observations", "record"),
        ("commands", "result"),
    ):
        for stored in stored_text(case["directory"] / "projects.sqlite", table, column):
            assert SENTINEL not in stored


def test_unconfigured_secret_ref_is_an_explicit_gap_without_any_request(
    case: dict[str, Any],
) -> None:
    """AC4: a missing resolver configuration never becomes an upstream call."""
    project_id = case["project_id"]
    with TestClient(case["app"], base_url=ORIGIN) as client:
        headers = login(client)
        status, response = create_connection(
            client, headers, project_id, case["server"], secret_ref="secret:unconfigured"
        )
        assert status == 201, response.text
        probed = client.post(
            url(project_id, f"/gateway-connections/{CONNECTION}/revisions/1/catalog-probes"),
            headers={**headers, "Idempotency-Key": "probe-missing-secret"},
        )
        assert probed.status_code == 201, probed.text
        catalog = probed.json()["catalog"]
        assert catalog["status"] == "credential_unavailable"
        assert catalog["reason_codes"] == ["GATEWAY_SECRET_REF_UNCONFIGURED"]
        assert catalog["credential_supplied"] is False
        assert catalog["execution_eligible"] is False
        assert case["server"].received == []  # type: ignore[attr-defined]


def test_catalog_probe_reads_only_the_registered_discovery_surface(
    case: dict[str, Any],
) -> None:
    """AC4: one bounded read of the registered path; no inference, no fallback."""
    project_id = case["project_id"]
    with TestClient(case["app"], base_url=ORIGIN) as client:
        headers = login(client)
        status, response = create_connection(client, headers, project_id, case["server"])
        assert status == 201, response.text
        # The read surface is derived from the protocol family, not caller input.
        assert response.json()["discovery_path"] == "/v1/models"

        probed = client.post(
            url(project_id, f"/gateway-connections/{CONNECTION}/revisions/1/catalog-probes"),
            headers={**headers, "Idempotency-Key": "probe"},
        )
        assert probed.status_code == 201, probed.text
        observation = probed.json()
        catalog = observation["catalog"]
        assert catalog["status"] == "ok"
        assert catalog["model_ids"] == ["vendor-model-a"]
        assert catalog["model_count"] == 1
        assert catalog["requests_sent"] == 1
        assert catalog["inference_requests_sent"] == 0
        assert catalog["redirect_followed"] is False
        # A visible model is catalog data; it is never execution eligibility.
        assert catalog["catalog_visible"] is True
        assert catalog["execution_eligible"] is False
        assert catalog["dispatch_eligible"] is False
        assert catalog["live_qualified"] is False
        assert observation["declared_binding_effects"] == "none"

        received = case["server"].received  # type: ignore[attr-defined]
        assert [entry["path"] for entry in received] == ["/v1/models"]
        assert all(entry.get("method", "GET") == "GET" for entry in received)

        replayed = client.post(
            url(project_id, f"/gateway-connections/{CONNECTION}/revisions/1/catalog-probes"),
            headers={**headers, "Idempotency-Key": "probe"},
        )
        assert replayed.json() == observation
        assert len(case["server"].received) == 1  # type: ignore[attr-defined]
        assert (
            client.get(url(project_id, "/gateway-catalog-observations")).json()["items"]
            == [observation]
        )


@pytest.mark.parametrize(
    ("handler_status", "handler_redirect", "expected_status", "expected_codes", "calls"),
    [
        (401, None, "unauthorized", ["CATALOG_UNAUTHORIZED"], 1),
        (403, None, "unauthorized", ["CATALOG_UNAUTHORIZED"], 1),
        (500, None, "upstream_error", ["CATALOG_UPSTREAM_ERROR"], 1),
        (
            302,
            "http://127.0.0.1:1/v1/models",
            "redirect_rejected",
            ["CATALOG_REDIRECT_REJECTED"],
            1,
        ),
    ],
)
def test_probe_failures_are_structured_and_never_follow_a_redirect(
    case: dict[str, Any],
    handler_status: int,
    handler_redirect: str | None,
    expected_status: str,
    expected_codes: list[str],
    calls: int,
) -> None:
    """AC4: authentication failure, upstream error and redirection stay explicit."""

    class Handler(RecordingHandler):
        status = handler_status
        redirect_to = handler_redirect

    project_id = case["project_id"]
    with upstream(Handler) as server:
        server.received = []  # type: ignore[attr-defined]
        with TestClient(case["app"], base_url=ORIGIN) as client:
            headers = login(client)
            status, response = create_connection(client, headers, project_id, server)
            assert status == 201, response.text
            probed = client.post(
                url(project_id, f"/gateway-connections/{CONNECTION}/revisions/1/catalog-probes"),
                headers={**headers, "Idempotency-Key": "probe-failure"},
            )
            assert probed.status_code == 201, probed.text
            catalog = probed.json()["catalog"]
            assert catalog["status"] == expected_status
            assert catalog["reason_codes"] == expected_codes
            assert catalog["model_ids"] == []
            assert catalog["catalog_visible"] is False
            assert catalog["execution_eligible"] is False
            assert len(server.received) == calls  # type: ignore[attr-defined]


def test_network_failure_and_unreachable_upstream_are_reported_not_raised(
    case: dict[str, Any],
) -> None:
    """AC4: a closed port is a structured `network_error` on the registered origin."""
    project_id = case["project_id"]
    with socket.socket() as available:
        available.bind(("127.0.0.1", 0))
        dead_port = available.getsockname()[1]
    with TestClient(case["app"], base_url=ORIGIN) as client:
        headers = login(client)
        status, response = create_connection(
            client, headers, project_id, case["server"], base_url=f"http://127.0.0.1:{dead_port}"
        )
        assert status == 201, response.text
        probed = client.post(
            url(project_id, f"/gateway-connections/{CONNECTION}/revisions/1/catalog-probes"),
            headers={**headers, "Idempotency-Key": "probe-network"},
        )
        assert probed.status_code == 201, probed.text
        catalog = probed.json()["catalog"]
        assert catalog["status"] == "network_error"
        assert catalog["reason_codes"] == ["CATALOG_NETWORK_ERROR"]
        assert catalog["requested_origin"] == f"http://127.0.0.1:{dead_port}"
        assert catalog["requested_path"] == "/v1/models"


def test_a_credential_echoed_by_the_upstream_never_reaches_the_client(
    case: dict[str, Any], tmp_path: Path
) -> None:
    """AC3/AC4: a hostile catalog body cannot turn the probe into a leak."""
    project_id = case["project_id"]
    secret_file = tmp_path / "gateway.key"
    secret_file.write_text(SENTINEL + "-echoed\n", encoding="utf-8")
    resolver = LocalFileSecretResolver(sources={(project_id, "secret:echoing"): secret_file})

    class Handler(RecordingHandler):
        catalog = {"object": "list", "data": [{"id": SENTINEL + "-echoed"}]}

    app = create_app(
        case["directory"],
        origin=ORIGIN,
        bootstrap_token="echo",
        allowed_roots=[case["repository"].parent],
        gateway_secret_resolver=resolver,
    )
    with upstream(Handler) as server:
        server.received = []  # type: ignore[attr-defined]
        with TestClient(app, base_url=ORIGIN) as client:
            headers = login(client, "echo")
            status, response = create_connection(
                client, headers, project_id, server, secret_ref="secret:echoing"
            )
            assert status == 201, response.text
            probed = client.post(
                url(project_id, f"/gateway-connections/{CONNECTION}/revisions/1/catalog-probes"),
                headers={**headers, "Idempotency-Key": "probe-echo"},
            )
            assert probed.status_code == 201, probed.text
            assert SENTINEL not in probed.text
            catalog = probed.json()["catalog"]
            assert catalog["status"] == "invalid_response"
            assert catalog["reason_codes"] == ["CATALOG_CREDENTIAL_ECHO_REJECTED"]
            assert catalog["secret_echo_detected"] is True
            assert catalog["model_ids"] == ["[redacted]"]
            assert catalog["execution_eligible"] is False


def test_probe_uses_a_real_ipv6_loopback_authority(case: dict[str, Any]) -> None:
    """AC4: the stored IPv6 authority is connectable, not a malformed host string."""
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as probe:
            probe.bind(("::1", 0))
    except OSError:
        pytest.skip("No local IPv6 loopback on this host")
    project_id = case["project_id"]

    class Handler(RecordingHandler):
        catalog = {"object": "list", "data": [{"id": "vendor-model-a"}]}

    with upstream(Handler, host="::1") as server:
        server.received = []  # type: ignore[attr-defined]
        with TestClient(case["app"], base_url=ORIGIN) as client:
            headers = login(client)
            status, response = create_connection(
                client, headers, project_id, server, base_url=base_url(server)
            )
            assert status == 201, response.text
            assert response.json()["base_url"] == base_url(server)
            probed = client.post(
                url(project_id, f"/gateway-connections/{CONNECTION}/revisions/1/catalog-probes"),
                headers={**headers, "Idempotency-Key": "probe-ipv6"},
            )
            assert probed.status_code == 201, probed.text
            catalog = probed.json()["catalog"]
            assert catalog["status"] == "ok", catalog
            assert catalog["model_ids"] == ["vendor-model-a"]
            assert [entry["path"] for entry in server.received] == ["/v1/models"]  # type: ignore[attr-defined]


def test_binding_declares_identity_that_stays_separate_from_verification(
    case: dict[str, Any],
) -> None:
    """AC3: strict binding pins one gateway revision and one declared identity."""
    project_id = case["project_id"]
    with TestClient(case["app"], base_url=ORIGIN) as client:
        headers = login(client)
        status, response = create_connection(client, headers, project_id, case["server"])
        assert status == 201, response.text
        connection = response.json()

        status, response = create_binding(client, headers, project_id)
        assert status == 201, response.text
        binding = response.json()
        assert binding["connection"]["id"] == connection["id"]
        assert binding["connection"]["revision"] == connection["revision"]
        assert binding["connection"]["digest"]
        assert binding["model_alias"] == "vendor-model-a"
        assert binding["declared"]["provider_id"] == "vendor"
        assert binding["declared"]["account_id"] == "vendor-account"
        assert binding["declared"]["billing_path"] == "subscription_only"
        assert binding["request_transformation"] == {
            "policy_id": "no-transform",
            "revision": 1,
            "payload_override": False,
            "prompt_rewrite": False,
            "managed_tools_injected": False,
        }
        # Declared now, verified never: the two are stored separately.
        assert binding["declared_identity"] is True
        assert binding["verified"] is False
        assert binding["verification_evidence"] is None

        # An unresolvable connection revision cannot be bound. A distinct
        # binding identity is used so this exercises the revision lookup rather
        # than the duplicate-identity guard.
        status, response = create_binding(
            client,
            headers,
            project_id,
            key="missing-revision",
            binding_id="stale-binding",
            connection_revision=9,
        )
        assert status == 404, response.text
        assert response.json()["reason_code"] == "GATEWAY_REVISION_NOT_FOUND"
        # A binding never mutates the connection it referenced.
        assert (
            client.get(url(project_id, f"/gateway-connections/{CONNECTION}/revisions/1")).json()
            == connection
        )


def stored_text(database: Path, table: str, column: str) -> list[str]:
    with sqlite3.connect(database) as db:
        return [row[0] for row in db.execute(f"SELECT {column} FROM {table}")]


def request_headers(server: ThreadingHTTPServer, path: str) -> dict[str, str]:
    for entry in server.received:  # type: ignore[attr-defined]
        if entry["path"] == path:
            return dict(entry["headers"])
    raise AssertionError(f"No request recorded for {path}")


def test_recording_handler_requires_no_thread_leak() -> None:
    """Guard the helper itself: threads must not accumulate across tests."""
    before = threading.active_count()
    with recording_server() as server:
        server.received = []  # type: ignore[attr-defined]
        assert server.server_address[1] > 0
    assert threading.active_count() == before


def test_client_reaches_the_control_plane_without_any_gateway_record(
    case: dict[str, Any],
) -> None:
    """An unrelated endpoint is unaffected by this feature's registration."""
    with TestClient(case["app"], base_url=ORIGIN) as client:
        assert client.get("/health").json() == {"status": "ok"}
        headers = login(client)
        response = client.get("/v1/resources", headers=headers)
        assert response.status_code == 200
        assert "accounts" in response.json()


def test_gateway_records_never_inflate_dispatch_eligibility(case: dict[str, Any]) -> None:
    """AC4: registration and a visible catalog leave the project ineligible."""
    project_id = case["project_id"]
    with TestClient(case["app"], base_url=ORIGIN) as client:
        headers = login(client)
        assert create_connection(client, headers, project_id, case["server"])[0] == 201
        assert create_binding(client, headers, project_id)[0] == 201
        catalog = client.post(
            url(project_id, f"/gateway-connections/{CONNECTION}/revisions/1/catalog-probes"),
            headers={**headers, "Idempotency-Key": "probe-eligibility"},
        ).json()["catalog"]
        assert catalog["catalog_visible"] is True
        project = case["registry"].get(project_id)
        assert project["configuration"]["dispatch_eligible"] is False
        assert project["live_qualified"] is False
        assert (
            client.get(f"/v1/projects/{project_id}/planning-preparation-readiness").json()[
                "activation_allowed"
            ]
            is False
        )


def test_unregistered_discovery_paths_cannot_be_requested(case: dict[str, Any]) -> None:
    """AC4: management, generation and off-host paths are refused before any call."""
    project_id = case["project_id"]
    with TestClient(case["app"], base_url=ORIGIN) as client:
        headers = login(client)
        for path in (
            "/v0/management/api-keys",
            "/admin/reload",
            "//other.example/v1/models",
            "/v1/chat/completions",
            "https://attacker.test/v1/models",
            "v1/models",
        ):
            status, response = create_connection(
                client,
                headers,
                project_id,
                case["server"],
                key=f"path-{abs(hash(path))}",
                protocol={
                    "family": "openai_compatible",
                    "protocol_version": "v1",
                    "catalog_path": path,
                },
            )
            assert status == 422, (path, response.text)
            assert response.json()["reason_code"] == "INPUT_INVALID"
        # Only the derived discovery surface is ever stored or requested.
        status, response = create_connection(
            client, headers, project_id, case["server"], key="path-good"
        )
        assert status == 201, response.text
        assert response.json()["discovery_path"] == "/v1/models"
        assert case["server"].received == []  # type: ignore[attr-defined]


def test_json_body_of_probe_is_the_persisted_observation(
    case: dict[str, Any],
) -> None:
    """AC1/AC5: the durable observation equals the returned evidence."""
    project_id = case["project_id"]
    with TestClient(case["app"], base_url=ORIGIN) as client:
        headers = login(client)
        assert create_connection(client, headers, project_id, case["server"])[0] == 201
        probed = client.post(
            url(project_id, f"/gateway-connections/{CONNECTION}/revisions/1/catalog-probes"),
            headers={**headers, "Idempotency-Key": "probe-persist"},
        ).json()
    stored = stored_text(
        case["directory"] / "projects.sqlite", "gateway_catalog_observations", "record"
    )
    assert [json.loads(row) for row in stored] == [probed]
