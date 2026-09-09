import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from karajan.web import create_app

ORIGIN = "http://127.0.0.1:8765"


@pytest.fixture
def client_and_projects(
    tmp_path: Path,
) -> Iterator[tuple[TestClient, dict[str, str], dict[str, Any], dict[str, Any]]]:
    root = tmp_path / "repositories"
    projects = []
    for name in ("one", "two"):
        repository = root / name
        repository.mkdir(parents=True)
        subprocess.run(
            ["git", "init", "--initial-branch=main", str(repository)],
            check=True,
            capture_output=True,
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
        projects.append(repository)
    app = create_app(
        tmp_path / "state", origin=ORIGIN, bootstrap_token="bootstrap", allowed_roots=[root]
    )
    with TestClient(app, base_url=ORIGIN) as client:
        login = client.post(
            "/v1/session/bootstrap", json={"token": "bootstrap"}, headers={"Origin": ORIGIN}
        )
        headers = {
            "Origin": ORIGIN,
            "X-CSRF-Token": login.json()["csrf_token"],
            "Idempotency-Key": "project-one",
        }
        result = []
        for index, repository in enumerate(projects):
            result.append(
                client.post(
                    "/v1/projects",
                    json={
                        "name": f"Project {index}",
                        "repository_path": str(repository),
                        "base_ref": "main",
                        "target_branch": "main",
                        "allowed_target_branches": ["main"],
                    },
                    headers={**headers, "Idempotency-Key": f"project-{index}"},
                ).json()
            )
        yield client, headers, result[0], result[1]


def test_conversation_persists_draft_messages_and_nonexecution_task_drafts(
    client_and_projects: tuple[TestClient, dict[str, str], dict[str, Any], dict[str, Any]],
) -> None:
    client, headers, project, _ = client_and_projects
    assert client.get(f"/v1/projects/{project['id']}/qualifications").json() == {"items": []}
    path = f"/v1/projects/{project['id']}/conversations"
    created = client.post(
        path, json={"title": "Commander"}, headers={**headers, "Idempotency-Key": "conversation"}
    )
    assert created.status_code == 201
    conversation = created.json()
    assert (
        client.post(
            path,
            json={"title": "Commander"},
            headers={**headers, "Idempotency-Key": "conversation"},
        ).json()
        == conversation
    )
    message = client.post(
        f"/v1/conversations/{conversation['id']}/messages",
        json={"client_message_id": "hello", "content": "Add a button"},
        headers={**headers, "Idempotency-Key": "message"},
    )
    assert message.status_code == 201
    task = client.post(
        f"/v1/conversations/{conversation['id']}/task-drafts",
        json={"requirement": "Add a button"},
        headers={**headers, "Idempotency-Key": "task"},
    )
    assert task.status_code == 201 and task.json()["state"] == "draft"
    draft = client.put(
        f"/v1/conversations/{conversation['id']}/draft",
        json={"content": "unfinished", "selected_task_id": task.json()["id"]},
        headers={**headers, "Idempotency-Key": "draft", "If-Match": '"1"'},
    )
    assert draft.status_code == 200
    hub = client.get(f"/v1/conversations/{conversation['id']}/hub").json()
    assert (
        hub["runs"] == []
        and hub["draft"]["content"] == "unfinished"
        and hub["messages"][0]["content"] == "Add a button"
    )


def test_revisions_csrf_cross_project_and_sse_recovery(
    client_and_projects: tuple[TestClient, dict[str, str], dict[str, Any], dict[str, Any]],
) -> None:
    client, headers, project_one, project_two = client_and_projects
    first = client.post(
        f"/v1/projects/{project_one['id']}/conversations",
        json={"title": "one"},
        headers={**headers, "Idempotency-Key": "one"},
    ).json()
    second = client.post(
        f"/v1/projects/{project_two['id']}/conversations",
        json={"title": "two"},
        headers={**headers, "Idempotency-Key": "two"},
    ).json()
    assert client.post(
        "/v1/runs",
        json={"conversation_id": second["id"]},
        headers={**headers, "Idempotency-Key": "wrong"},
    ).status_code in {409, 422}
    assert client.get(
        "/v1/runs",
        params={"project_id": project_one["id"], "conversation_id": second["id"]},
    ).status_code == 409
    assert (
        client.put(
            f"/v1/conversations/{first['id']}/draft",
            json={"content": "x"},
            headers={**headers, "Idempotency-Key": "missing-match"},
        ).status_code
        == 428
    )
    saved = client.put(
        f"/v1/conversations/{first['id']}/draft",
        json={"content": "x"},
        headers={**headers, "Idempotency-Key": "saved", "If-Match": '"1"'},
    )
    assert saved.status_code == 200
    stale = client.put(
        f"/v1/conversations/{first['id']}/draft",
        json={"content": "y"},
        headers={**headers, "Idempotency-Key": "stale", "If-Match": '"1"'},
    )
    assert stale.status_code == 409 and stale.json()["current_revision"] == 2
    events = client.get(f"/v1/conversations/{first['id']}/events", params={"after_seq": 0})
    assert (
        events.headers["content-type"].startswith("text/event-stream")
        and "event: draft_saved" in events.text
    )
    gap = client.get(f"/v1/conversations/{first['id']}/events", params={"after_seq": 999})
    assert "event: event_gap" in gap.text and "snapshot_required" in gap.text


def test_rejected_draft_receipt_is_durable_and_replays_its_revision(
    client_and_projects: tuple[TestClient, dict[str, str], dict[str, Any], dict[str, Any]],
) -> None:
    client, headers, project, _ = client_and_projects
    conversation = client.post(
        f"/v1/projects/{project['id']}/conversations",
        json={"title": "receipt"},
        headers={**headers, "Idempotency-Key": "conversation"},
    ).json()
    path = f"/v1/conversations/{conversation['id']}/draft"
    assert client.put(
        path,
        json={"content": "accepted"},
        headers={**headers, "Idempotency-Key": "accepted", "If-Match": '"1"'},
    ).status_code == 200
    rejected_headers = {**headers, "Idempotency-Key": "rejected", "If-Match": '"1"'}
    first = client.put(path, json={"content": "first"}, headers=rejected_headers)
    assert first.status_code == 409 and first.json()["current_revision"] == 2
    assert (
        client.get(f"/v1/conversations/{conversation['id']}/hub").json()["draft"]["content"]
        == "accepted"
    )
    replay = client.put(path, json={"content": "first"}, headers=rejected_headers)
    assert replay.status_code == 409 and replay.json()["current_revision"] == 2
    reused = client.put(path, json={"content": "changed"}, headers=rejected_headers)
    assert reused.status_code == 409 and reused.json()["reason_code"] == "IDEMPOTENCY_KEY_REUSED"


def test_sse_route_uses_the_single_batch_watermark(
    client_and_projects: tuple[TestClient, dict[str, str], dict[str, Any], dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from karajan.conversations import ConversationStore

    client, headers, project, _ = client_and_projects
    conversation = client.post(
        f"/v1/projects/{project['id']}/conversations",
        json={"title": "events"},
        headers={**headers, "Idempotency-Key": "events"},
    ).json()
    monkeypatch.setattr(
        ConversationStore,
        "snapshot",
        lambda *args: (_ for _ in ()).throw(AssertionError("separate snapshot read")),
    )
    events = client.get(f"/v1/conversations/{conversation['id']}/events")
    assert events.status_code == 200
    assert events.headers["X-Snapshot-Watermark"] == "1"


def test_conversation_create_replays_before_mutable_selection_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from karajan.conversations import ConversationStore
    from karajan.projects import ProjectRegistry
    from karajan.runs import RunPlanner

    registry = ProjectRegistry(tmp_path / "projects.sqlite", [tmp_path])
    # The test only needs a stable project identity; selection is deliberately
    # supplied by the registry-facing options seam.
    monkeypatch.setattr(registry, "get", lambda project_id: {"id": project_id})
    store = ConversationStore(registry, RunPlanner(tmp_path / "runs.sqlite", registry))
    allowed = [{"profile_ref": "profile", "source_ref": "source"}]
    monkeypatch.setattr(store, "commander_options", lambda project_id: allowed)
    request = {
        "title": "selected",
        "commander_profile_ref": "profile",
        "commander_source_ref": "source",
    }
    first = store.create("project", request, principal="owner", key="selection")
    allowed.clear()
    assert store.create("project", request, principal="owner", key="selection") == first
