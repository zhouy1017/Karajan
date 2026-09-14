"""Review regressions for the gateway catalog at its HTTP boundary.

Each test here corresponds to a defect found in independent review of PR #179.
They assert the observable contract, not the implementation: content digests,
addressable identities, credential-name rejection, non-finite parameters and
malformed upstream authorities.
"""

import json
import sqlite3
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
from karajan.gateway.catalog import content_digest
from karajan.projects import ProjectRegistry
from karajan.web import create_app

CONNECTION = "local-gateway"
BINDING = "vendor-binding"
REVIEW_SENTINEL = "FAKE_REVIEW_CREDENTIAL_179"


@pytest.fixture
def case(tmp_path: Path) -> Any:
    repository = make_repository(tmp_path)
    directory = tmp_path / "state"
    directory.mkdir()
    registry = ProjectRegistry(directory / "projects.sqlite", [repository.parent])
    project = registry.create(
        {
            "name": "Gateway review",
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
            directory,
            origin=ORIGIN,
            bootstrap_token="bootstrap",
            allowed_roots=[repository.parent],
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
    client: TestClient, headers: dict[str, str], project_id: str, server: Any, **overrides: Any
) -> Any:
    return client.post(
        url(project_id, "/gateway-connections"),
        json=connection_payload(server, **overrides),
        headers={**headers, "Idempotency-Key": "connection-create"},
    )


def create_binding(
    client: TestClient, headers: dict[str, str], project_id: str, **overrides: Any
) -> Any:
    return client.post(
        url(project_id, "/gateway-bindings"),
        json=binding_payload(**overrides),
        headers={**headers, "Idempotency-Key": "binding-create"},
    )


def test_public_objects_carry_a_stable_canonical_content_digest(case: dict[str, Any]) -> None:
    """Finding 1: connection and binding expose their own verified digest."""
    project_id = case["project_id"]
    with TestClient(case["app"], base_url=ORIGIN) as client:
        headers = login(client)
        created = create_connection(client, headers, project_id, case["server"])
        assert created.status_code == 201, created.text
        connection = created.json()
        assert isinstance(connection["digest"], str) and len(connection["digest"]) == 64
        # The digest covers the object's own content, excluding the digest field.
        assert content_digest(connection) == connection["digest"]

        binding_response = create_binding(client, headers, project_id)
        assert binding_response.status_code == 201, binding_response.text
        binding = binding_response.json()
        assert content_digest(binding) == binding["digest"]
        # The referenced connection digest is the connection's own digest.
        assert binding["connection"]["digest"] == connection["digest"]

        # GET, list and an idempotent replay all report the same digest.
        fetched = client.get(
            url(project_id, f"/gateway-connections/{CONNECTION}/revisions/1")
        ).json()
        assert fetched["digest"] == connection["digest"]
        listed = client.get(url(project_id, "/gateway-connections")).json()["items"]
        assert [row["digest"] for row in listed] == [connection["digest"]]
        listed_bindings = client.get(url(project_id, "/gateway-bindings")).json()["items"]
        assert [row["digest"] for row in listed_bindings] == [binding["digest"]]

        replayed = client.post(
            url(project_id, "/gateway-connections"),
            json=connection_payload(case["server"]),
            headers={**headers, "Idempotency-Key": "connection-create"},
        )
        assert replayed.json() == connection

        # A new revision gets a distinct digest; the old reference is unchanged.
        revised_body = connection_payload(case["server"])
        revised_body["request_transformation"]["revision"] = 2
        revised = client.put(
            url(project_id, f"/gateway-connections/{CONNECTION}"),
            json=revised_body,
            headers={**headers, "Idempotency-Key": "connection-revise", "If-Match": '"1"'},
        )
        assert revised.status_code == 201, revised.text
        assert revised.json()["digest"] != connection["digest"]
        assert content_digest(revised.json()) == revised.json()["digest"]
        assert (
            client.get(url(project_id, f"/gateway-connections/{CONNECTION}/revisions/1")).json()
            == connection
        )

        # A replay of the revision command returns the same new digest.
        assert (
            client.put(
                url(project_id, f"/gateway-connections/{CONNECTION}"),
                json=revised_body,
                headers={**headers, "Idempotency-Key": "connection-revise", "If-Match": '"1"'},
            ).json()
            == revised.json()
        )

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


def test_tampered_stored_record_is_refused_on_read_and_list(case: dict[str, Any]) -> None:
    """Finding 1: integrity is validated on list as well as on single read."""
    project_id = case["project_id"]
    with TestClient(case["app"], base_url=ORIGIN) as client:
        headers = login(client)
        assert create_connection(client, headers, project_id, case["server"]).status_code == 201
    database = case["directory"] / "projects.sqlite"
    with sqlite3.connect(database) as db:
        row = db.execute("SELECT record FROM gateway_connections").fetchone()
        record = json.loads(row[0])
        record["base_url"] = "http://127.0.0.1:1"
        db.execute("UPDATE gateway_connections SET record=?", (json.dumps(record),))
    # A fresh application instance, because the original bootstrap token is
    # single-use and this test needs a second authenticated session.
    reopened = create_app(
        case["directory"],
        origin=ORIGIN,
        bootstrap_token="tamper-probe",
        allowed_roots=[case["repository"].parent],
    )
    with TestClient(reopened, base_url=ORIGIN) as client:
        login(client, "tamper-probe")
        listed = client.get(url(project_id, "/gateway-connections"))
        assert listed.status_code == 422
        assert listed.json()["reason_code"] == "GATEWAY_RECORD_CHANGED"
        single = client.get(url(project_id, f"/gateway-connections/{CONNECTION}/revisions/1"))
        assert single.json()["reason_code"] == "GATEWAY_RECORD_CHANGED"


@pytest.mark.parametrize("identity", ["group/gateway", "group%2Fgateway", "a b", "..", "."])
def test_unaddressable_connection_identities_are_rejected_before_persistence(
    case: dict[str, Any], identity: str
) -> None:
    """Finding 2: an accepted id must be addressable by exactly one URL."""
    project_id = case["project_id"]
    with TestClient(case["app"], base_url=ORIGIN) as client:
        headers = login(client)
        response = create_connection(
            client, headers, project_id, case["server"], connection_id=identity
        )
        assert response.status_code == 422, response.text
        assert response.json()["reason_code"] == "INPUT_INVALID"
        assert client.get(url(project_id, "/gateway-connections")).json()["items"] == []
    # The rejected identity left no durable record: no table grew a row.
    with sqlite3.connect(case["directory"] / "projects.sqlite") as db:
        assert db.execute("SELECT COUNT(*) FROM gateway_connections").fetchone()[0] == 0
        assert db.execute(
            "SELECT COUNT(*) FROM commands WHERE key LIKE 'connection-create%'"
        ).fetchone()[0] == 0


def test_addressable_connection_and_binding_identities_round_trip(case: dict[str, Any]) -> None:
    """Finding 2: a dotted/dashed id is creatable, readable and revisable."""
    project_id = case["project_id"]
    with TestClient(case["app"], base_url=ORIGIN) as client:
        headers = login(client)
        created = create_connection(
            client, headers, project_id, case["server"], connection_id="team.gateway_1"
        )
        assert created.status_code == 201, created.text
        assert (
            client.get(url(project_id, "/gateway-connections/team.gateway_1/revisions/1")).json()
            == created.json()
        )
        revised_body = connection_payload(case["server"], connection_id="team.gateway_1")
        revised = client.put(
            url(project_id, "/gateway-connections/team.gateway_1"),
            json=revised_body,
            headers={**headers, "Idempotency-Key": "revise-dotted", "If-Match": '"1"'},
        )
        assert revised.status_code == 201, revised.text

        binding = client.post(
            url(project_id, "/gateway-bindings"),
            json=binding_payload(
                binding_id="vendor.binding-1", connection_id="team.gateway_1"
            ),
            headers={**headers, "Idempotency-Key": "binding-dotted"},
        )
        assert binding.status_code == 201, binding.text
        assert (
            client.get(url(project_id, "/gateway-bindings/vendor.binding-1/revisions/1")).json()
            == binding.json()
        )


def test_model_alias_and_secret_ref_keep_their_wider_grammar(case: dict[str, Any]) -> None:
    """Finding 2: URL-segment limits must not leak onto non-addressable fields."""
    project_id = case["project_id"]
    with TestClient(case["app"], base_url=ORIGIN) as client:
        headers = login(client)
        created = create_connection(
            client,
            headers,
            project_id,
            case["server"],
            # A controller namespace reference, not a URL segment.
            secret_ref="secret:team/gateway",
        )
        assert created.status_code == 201, created.text
        assert created.json()["secret_ref"] == "secret:team/gateway"
        # A provider alias legitimately contains a slash and is never a segment.
        binding = create_binding(client, headers, project_id, model_alias="vendor/model-a")
        assert binding.status_code == 201, binding.text
        assert binding.json()["model_alias"] == "vendor/model-a"


@pytest.mark.parametrize(
    "name",
    [
        "token_auth",
        "auth_token",
        "tokenauth",
        "accesstoken",
        "accessToken",
        "access-token",
        "idtoken",
        "refresh_token",
        "session_token",
        "api_key",
        "x-api-key",
        "Authorization",
        "bearer",
        "jwt",
        "signature",
        "client_secret",
        "credential",
        "cookies",
        "headers",
        "password",
    ],
)
def test_credential_shaped_parameter_names_are_refused_over_http(
    case: dict[str, Any], name: str
) -> None:
    """Finding 6: reordered and compact credential names are refused too."""
    project_id = case["project_id"]
    with TestClient(case["app"], base_url=ORIGIN) as client:
        headers = login(client)
        assert create_connection(client, headers, project_id, case["server"]).status_code == 201
        response = create_binding(
            client,
            headers,
            project_id,
            declared={
                "provider_id": "vendor",
                "account_id": "vendor-account",
                "billing_path": "subscription_only",
                "required_parameters": {name: REVIEW_SENTINEL},
            },
        )
        assert response.status_code == 422, (name, response.text)
        assert response.json()["reason_code"] == "INPUT_INVALID"
        assert REVIEW_SENTINEL not in response.text
        assert client.get(url(project_id, "/gateway-bindings")).json()["items"] == []
    # No binding row was written, and no command receipt was added for the
    # rejected name: the ledger still holds only the project and connection.
    with sqlite3.connect(case["directory"] / "projects.sqlite") as db:
        assert db.execute("SELECT COUNT(*) FROM gateway_bindings").fetchone()[0] == 0
        assert db.execute(
            "SELECT COUNT(*) FROM commands WHERE key='binding-create'"
        ).fetchone()[0] == 0


def test_legitimate_model_parameters_still_persist_and_survive_restart(
    case: dict[str, Any],
) -> None:
    """Finding 6: the tightened rule must not reject ordinary parameters."""
    project_id = case["project_id"]
    parameters = {
        "temperature": 0.2,
        "top_p": 1.0,
        "max_tokens": 2048,
        "max_output_tokens": 4096,
        "seed": 7,
        "stop": "\n",
        "frequency_penalty": 0,
        "presence_penalty": 0,
        "api_version": "2024-10",
        "response_format": "json_object",
        "stream": False,
    }
    with TestClient(case["app"], base_url=ORIGIN) as client:
        headers = login(client)
        assert create_connection(client, headers, project_id, case["server"]).status_code == 201
        response = create_binding(
            client,
            headers,
            project_id,
            declared={
                "provider_id": "vendor",
                "account_id": "vendor-account",
                "billing_path": "api_cash",
                "required_parameters": parameters,
            },
        )
        assert response.status_code == 201, response.text
        assert response.json()["declared"]["required_parameters"] == parameters
    reopened = create_app(
        case["directory"],
        origin=ORIGIN,
        bootstrap_token="second",
        allowed_roots=[case["repository"].parent],
    )
    with TestClient(reopened, base_url=ORIGIN) as client:
        login(client, "second")
        restored = client.get(url(project_id, f"/gateway-bindings/{BINDING}/revisions/1")).json()
        assert restored["declared"]["required_parameters"] == parameters


def test_non_finite_parameters_are_rejected_without_a_server_error(
    case: dict[str, Any],
) -> None:
    """Finding 5: a JSON number such as 1e9999 must not reach the digest step.

    ``1e9999`` is valid JSON that parses to ``float('inf')``. Backgrounding the
    value through ``json.dumps`` would emit ``Infinity``, which is not JSON, so
    the literal is substituted into the serialized body at a marker.
    """
    project_id = case["project_id"]
    with TestClient(case["app"], base_url=ORIGIN) as client:
        headers = login(client)
        assert create_connection(client, headers, project_id, case["server"]).status_code == 201
        marker = "NONFINITE_MARKER"
        body = binding_payload(
            declared={
                "provider_id": "vendor",
                "account_id": "vendor-account",
                "billing_path": "subscription_only",
                "required_parameters": {"temperature": marker},
            }
        )
        raw = json.dumps(body).replace(f'"{marker}"', "1e9999")
        assert "1e9999" in raw
        response = client.post(
            url(project_id, "/gateway-bindings"),
            content=raw.encode(),
            headers={
                **headers,
                "Content-Type": "application/json",
                "Idempotency-Key": "binding-nonfinite",
            },
        )
        assert response.status_code == 422, response.text
        assert response.json()["reason_code"] == "INPUT_INVALID"
        assert client.get(url(project_id, "/gateway-bindings")).json()["items"] == []
        assert client.get(url(project_id, "/gateway-bindings")).status_code == 200


def test_malformed_upstream_authority_is_refused_at_registration(case: dict[str, Any]) -> None:
    """Finding 7: a host with whitespace never becomes a stored authority."""
    project_id = case["project_id"]
    with TestClient(case["app"], base_url=ORIGIN) as client:
        headers = login(client)
        response = create_connection(
            client,
            headers,
            project_id,
            case["server"],
            base_url="https://bad host",
            remote_data_destination="vendor-eu",
        )
        assert response.status_code == 422, response.text
        assert response.json()["reason_code"] == "GATEWAY_BASE_URL_INVALID"
        assert client.get(url(project_id, "/gateway-connections")).json()["items"] == []


def test_unconnectable_stored_authority_yields_a_structured_observation(
    case: dict[str, Any],
) -> None:
    """Finding 7: a constructor-stage failure is an observation, not a 500.

    The registration path refuses such an authority outright; this asserts the
    deeper guarantee that even a record which somehow reached storage cannot
    turn a probe into an unhandled server error.
    """
    project_id = case["project_id"]
    with TestClient(case["app"], base_url=ORIGIN) as client:
        headers = login(client)
        assert create_connection(client, headers, project_id, case["server"]).status_code == 201
    with sqlite3.connect(case["directory"] / "projects.sqlite") as db:
        row = db.execute("SELECT record FROM gateway_connections").fetchone()
        record = json.loads(row[0])
        # A tampered authority that the HTTP client constructor rejects.
        record["base_url"] = "https://bad host"
        record["digest"] = content_digest(record)
        db.execute(
            "UPDATE gateway_connections SET record=?, digest=?",
            (json.dumps(record, sort_keys=True, separators=(",", ":")), record["digest"]),
        )
    reopened = create_app(
        case["directory"],
        origin=ORIGIN,
        bootstrap_token="authority-probe",
        allowed_roots=[case["repository"].parent],
    )
    with TestClient(reopened, base_url=ORIGIN) as client:
        headers = login(client, "authority-probe")
        probe = client.post(
            url(project_id, f"/gateway-connections/{CONNECTION}/revisions/1/catalog-probes"),
            headers={**headers, "Idempotency-Key": "probe-bad-authority"},
        )
        assert probe.status_code == 201, probe.text
        catalog = probe.json()["catalog"]
        assert catalog["status"] == "invalid_response"
        assert catalog["reason_codes"] == ["GATEWAY_ORIGIN_NOT_CONNECTABLE"]
        assert catalog["execution_eligible"] is False
