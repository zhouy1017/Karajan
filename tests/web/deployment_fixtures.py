"""Shared helpers for the workflow deployment HTTP cases.

Every case goes through the real FastAPI application, real SQLite and real files
on disk. Restart cases build a **second application over the same state
directory**, and the deepest ones run a genuinely separate interpreter, which is
the only honest way to show that a load is performed by the process that reports
it rather than inherited from a warm cache.

No case contacts a provider, resolves a credential, calls a model or drives the
business adapter through a deployment: ``deploy_only`` loads and activates a
definition, and executing it is #178's authority.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from karajan.web import create_app
from workflow_fixtures import (  # noqa: F401  (re-exported for the cases)
    BUNDLE,
    COORDINATOR_ROLE,
    EDITOR_ROLE,
    ORIGIN,
    RESEARCHER_ROLE,
    SECOND_WORKFLOW_TEXT,
    WORKFLOW_TEXT,
    bundle_case,
    create_bundle,
    files,
    login,
    make_repository,
    url,
)

SLOT = "default"


def deployment_url(project_id: str, suffix: str) -> str:
    return url(project_id, suffix)


def preview_id(case: dict[str, Any], *, bundle_id: str = BUNDLE, revision: int = 1) -> str:
    """The current preview identity of one revision, read through the real API."""
    response = case["client"].get(
        url(case["project_id"], f"/workflows/{bundle_id}/revisions/{revision}/preview")
    )
    assert response.status_code == 200, response.text
    return str(response.json()["preview_id"])


def deploy(
    case: dict[str, Any],
    *,
    bundle_id: str = BUNDLE,
    revision: int = 1,
    key: str = "deploy-1",
    slot: str = SLOT,
    expected_active_revision: int = 0,
    preview: str | None = None,
    action: str = "deploy_only",
    conversation_id: str | None = None,
    headers: dict[str, str] | None = None,
    extra: dict[str, Any] | None = None,
) -> Any:
    """One deploy_only confirmation through the authenticated HTTP boundary."""
    if preview is None:
        preview = preview_id(case, bundle_id=bundle_id, revision=revision)
    payload: dict[str, Any] = {
        "action": action,
        "slot": slot,
        "expected_active_revision": expected_active_revision,
        "preview_id": preview,
    }
    payload.update(extra or {})
    return case["client"].post(
        deployment_url(
            case["project_id"],
            f"/conversations/{conversation_id or case['conversation_id']}"
            f"/workflows/{bundle_id}/revisions/{revision}/deployments",
        ),
        json=payload,
        headers={**(headers or case["headers"]), "Idempotency-Key": key},
    )


def status(case: dict[str, Any], *, slot: str = SLOT, client: Any = None) -> dict[str, Any]:
    response = (client or case["client"]).get(
        deployment_url(case["project_id"], f"/workflow-deployments/{slot}")
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


def history(case: dict[str, Any], *, slot: str = SLOT, client: Any = None) -> dict[str, Any]:
    response = (client or case["client"]).get(
        deployment_url(case["project_id"], f"/workflow-deployments/{slot}/history")
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


def deployment_path(
    case: dict[str, Any], deployment_id: str, *, slot: str = SLOT
) -> Path:
    return (
        case["directory"]
        / "workflow-deployments"
        / case["project_id"]
        / slot
        / deployment_id
        / "pending"
    )


def revision_path(case: dict[str, Any], *, bundle_id: str = BUNDLE, revision: int = 1) -> Path:
    return (
        case["directory"]
        / "workflow-bundles"
        / case["project_id"]
        / bundle_id
        / f"revision-{revision}"
    )


def state_store(case: dict[str, Any]) -> Path:
    return case["directory"] / "workflow-deployments"


def restart(case: dict[str, Any], token: str = "second") -> tuple[Any, TestClient]:
    """A second application over the same real state directory."""
    app = create_app(
        case["directory"],
        origin=ORIGIN,
        bootstrap_token=token,
        allowed_roots=[case["repository"].parent],
    )
    return app, TestClient(app, base_url=ORIGIN)


@pytest.fixture
def deployed_case(bundle_case: dict[str, Any]) -> dict[str, Any]:
    """One project with a published bundle and one activated deployment."""
    assert create_bundle(bundle_case).status_code == 201
    response = deploy(bundle_case)
    assert response.status_code == 201, response.text
    bundle_case["deployment"] = response.json()
    return bundle_case


def active_deployment_id(case: dict[str, Any], *, slot: str = SLOT) -> str:
    return str(status(case, slot=slot)["active_deployment_id"])


def sqlite_records(case: dict[str, Any], query: str, parameters: tuple[Any, ...] = ()) -> list[Any]:
    """Read the durable ledger directly, to prove a fact is really persisted."""
    import sqlite3

    connection = sqlite3.connect(case["directory"] / "projects.sqlite")
    try:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(query, parameters)]
    finally:
        connection.close()


def json_rows(case: dict[str, Any], query: str, parameters: tuple[Any, ...] = ()) -> list[Any]:
    return [json.loads(row["record"]) for row in sqlite_records(case, query, parameters)]


__all__ = [
    "BUNDLE",
    "COORDINATOR_ROLE",
    "EDITOR_ROLE",
    "ORIGIN",
    "RESEARCHER_ROLE",
    "SECOND_WORKFLOW_TEXT",
    "SLOT",
    "WORKFLOW_TEXT",
    "active_deployment_id",
    "bundle_case",
    "create_bundle",
    "deploy",
    "deployed_case",
    "deployment_path",
    "deployment_url",
    "files",
    "history",
    "json_rows",
    "login",
    "make_repository",
    "preview_id",
    "restart",
    "revision_path",
    "sqlite_records",
    "state_store",
    "status",
    "url",
]
