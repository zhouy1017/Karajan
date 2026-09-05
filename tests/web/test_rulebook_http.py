"""Authenticated publication commands persist real versions without activation."""

import copy
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from karajan.capacity import CapacityStore
from karajan.projects import ProjectRegistry
from karajan.web import create_app

ORIGIN = "http://127.0.0.1:8765"


@pytest.fixture
def publication_case(tmp_path: Path) -> dict[str, Any]:
    repository = tmp_path / "repositories" / "fixture"
    repository.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(repository)], check=True, capture_output=True
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=",
            "commit",
            "--allow-empty",
            "-m",
            "fixture",
        ],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    directory = tmp_path / "state"
    directory.mkdir()
    registry = ProjectRegistry(directory / "projects.sqlite", [repository.parent])
    project = registry.create(
        {
            "name": "Rulebook HTTP",
            "repository_path": str(repository),
            "base_ref": "main",
            "target_branch": "main",
            "allowed_target_branches": ["main"],
        },
        command_key="project",
        principal="owner",
    )
    configuration = json.loads(Path("examples/projects/offline-configuration.json").read_bytes())
    preview = registry.preview_configuration(
        project["id"],
        configuration,
        command_key="configure-preview",
        principal="owner",
    )
    project = registry.apply_configuration(
        project["id"],
        preview["preview_id"],
        expected_revision=1,
        command_key="configure-apply",
        principal="owner",
    )
    app = create_app(
        directory, origin=ORIGIN, bootstrap_token="bootstrap", allowed_roots=[repository.parent]
    )
    return {
        "directory": directory,
        "repository": repository,
        "project": project,
        "registry": registry,
        "configuration": configuration,
        "app": app,
    }


def login(client: TestClient, token: str = "bootstrap") -> dict[str, str]:
    response = client.post(
        "/v1/session/bootstrap", json={"token": token}, headers={"Origin": ORIGIN}
    )
    assert response.status_code == 200
    return {
        "Origin": ORIGIN,
        "X-CSRF-Token": response.json()["csrf_token"],
        "If-Match": '"2"',
        "Idempotency-Key": "rulebook-preview",
    }


def test_preview_publish_retry_and_history_survive_restart_without_reservations(
    publication_case: dict[str, Any],
) -> None:
    case = publication_case
    base = f"/v1/projects/{case['project']['id']}/rulebook"
    capacity = CapacityStore(case["directory"] / "capacity.sqlite")
    before = capacity.snapshot()
    document = copy.deepcopy(case["configuration"]["rulebook"])
    document["revision"] += 1
    document["description"] = "Confirmed future rule version"
    with TestClient(case["app"], base_url=ORIGIN) as client:
        assert client.get(base + "/versions").status_code == 401
        headers = login(client)
        response = client.post(base + "/preview", json=document, headers=headers)
        assert response.status_code == 200
        preview = response.json()
        assert preview["can_publish"] is True
        assert case["registry"].get(case["project"]["id"])["revision"] == 2
        publish_headers = {**headers, "Idempotency-Key": "rulebook-publish"}
        body = {"preview_id": preview["preview_id"]}
        published = client.post(base + "/publish", json=body, headers=publish_headers)
        assert published.status_code == 200
        assert published.headers["ETag"] == '"3"'
        assert published.json()["activation_allowed"] is False
        assert published.json()["state"] == "waiting_qualification"
        assert (
            client.post(base + "/publish", json=body, headers=publish_headers).json()
            == published.json()
        )
        assert len(client.get(base + "/versions").json()["items"]) == 2
        assert client.get(base + "/publications").json()["items"] == [published.json()]
        assert (
            client.post(
                base + "/publish",
                json=body,
                headers={**publish_headers, "Idempotency-Key": "new-stale-command"},
            ).status_code
            == 409
        )
    reopened = create_app(
        case["directory"],
        origin=ORIGIN,
        bootstrap_token="second-bootstrap",
        allowed_roots=[case["repository"].parent],
    )
    with TestClient(reopened, base_url=ORIGIN) as client:
        login(client, "second-bootstrap")
        assert client.get(base + "/publications").json()["items"] == [published.json()]
    assert capacity.snapshot() == before


@pytest.mark.parametrize("phase", ["preview", "publish"])
@pytest.mark.parametrize("invalid", ["csrf", "origin", "revision", "key"])
def test_rulebook_commands_require_session_proof_revision_and_command_identity(
    publication_case: dict[str, Any],
    phase: str,
    invalid: str,
) -> None:
    case = publication_case
    base = f"/v1/projects/{case['project']['id']}/rulebook"
    document = copy.deepcopy(case["configuration"]["rulebook"])
    document["revision"] += 1
    with TestClient(case["app"], base_url=ORIGIN) as client:
        headers = login(client)
        body = document
        if phase == "publish":
            preview = client.post(base + "/preview", json=document, headers=headers).json()
            body = {"preview_id": preview["preview_id"]}
        bad_headers = {**headers, "Idempotency-Key": "rejected-command"}
        bad_headers.pop(
            {
                "csrf": "X-CSRF-Token",
                "origin": "Origin",
                "revision": "If-Match",
                "key": "Idempotency-Key",
            }[invalid]
        )
        response = client.post(base + "/" + phase, json=body, headers=bad_headers)
        assert (
            response.status_code
            == {"csrf": 403, "origin": 403, "revision": 428, "key": 400}[invalid]
        )
        assert client.get(base + "/publications").json()["items"] == []
    assert case["registry"].get(case["project"]["id"])["revision"] == 2


def test_secret_and_invalid_unicode_previews_never_create_a_publishable_document(
    publication_case: dict[str, Any],
) -> None:
    case = publication_case
    base = f"/v1/projects/{case['project']['id']}/rulebook"
    with TestClient(case["app"], base_url=ORIGIN) as client:
        headers = login(client)
        for index, change in enumerate(
            ({"api_key": "FAKE-RULEBOOK-SECRET-CANARY"}, {"description": "\ud800"})
        ):
            document = {**case["configuration"]["rulebook"], "revision": 2, **change}
            response = client.post(
                base + "/preview",
                content=json.dumps(document).encode(),
                headers={
                    **headers,
                    "Content-Type": "application/json",
                    "Idempotency-Key": f"bad-preview-{index}",
                },
            )
            if index == 0:
                assert response.status_code == 200, response.text
                assert response.json()["can_save_draft"] is False
                assert response.json()["can_publish"] is False
            else:
                assert response.status_code == 422, response.text
                assert response.json()["reason_code"] == "INPUT_INVALID"
            assert "FAKE-RULEBOOK-SECRET-CANARY" not in response.text
        assert client.get(base + "/publications").json()["items"] == []
