"""Shared fixtures and helpers for the workflow bundle HTTP cases.

Every case goes through the real FastAPI application: real session bootstrap,
real CSRF, real SQLite, real files on disk. Restart cases construct a second
application over the same state directory, which is the only honest way to test
that a readback comes from the files rather than from a warm cache.

No case contacts a provider, resolves a credential, calls a model or invokes the
business adapter: the compile path validates the template and the capability, and
a deploy-only template is never executed to manufacture readiness.
"""

import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from karajan.projects import ProjectRegistry
from karajan.web import create_app

ORIGIN = "http://127.0.0.1:8765"
BUNDLE = "compare-options"
CONVERSATION_KEY = "conversation-create"

WORKFLOW_TEXT = """schema_version: workflow.v1
id: compare-options
revision: 1
delivery_kind: report
input_contract: text@1
inputs:
  - requirement.option_a
  - requirement.option_b
roles:
  researcher: role:source-researcher@2
  editor: role:technical-editor@1
steps:
  - id: option-a
    execution_kind: artifact_aggregate@1
    role: researcher
    output_contract: aggregated-report@1
    inputs:
      sources: requirement.option_a
  - id: option-b
    execution_kind: artifact_aggregate@1
    role: researcher
    output_contract: aggregated-report@1
    inputs:
      sources: requirement.option_b
  - id: comparison
    execution_kind: artifact_aggregate@1
    role: editor
    output_contract: aggregated-report@1
    depends_on:
      - option-a
      - option-b
    join: all_required
    inputs:
      sources:
        - option-a.output
        - option-b.output
completion:
  required_steps:
    - option-a
    - option-b
    - comparison
  artifact: comparison.output
"""

SECOND_WORKFLOW_TEXT = """schema_version: workflow.v1
id: repair-config-loaders
revision: 1
delivery_kind: report
input_contract: text@1
inputs:
  - requirement.defect_report
roles:
  coordinator: role:repair-coordinator@1
steps:
  - id: triage
    execution_kind: artifact_aggregate@1
    role: coordinator
    output_contract: aggregated-report@1
    inputs:
      sources: requirement.defect_report
completion:
  required_steps:
    - triage
  artifact: triage.output
"""

RESEARCHER_ROLE = """id: source-researcher
revision: 2
display_name: Source researcher
responsibilities:
  - read approved material
stop_conditions:
  - material is outside the approved scope
required_capabilities:
  - read_only_research
tool_constraints:
  paths:
    - docs/**
  network: denied
"""

EDITOR_ROLE = """id: technical-editor
revision: 1
display_name: Technical editor
responsibilities:
  - assemble the comparison
stop_conditions:
  - sources are incomplete
tool_constraints:
  paths:
    - reports/**
"""

COORDINATOR_ROLE = """id: repair-coordinator
revision: 1
display_name: Repair coordinator
responsibilities:
  - triage the confirmed defects
stop_conditions: []
tool_constraints:
  paths:
    - src/**
"""


def make_repository(root: Path, name: str = "fixture") -> Path:
    repository = root / "repositories" / name
    repository.mkdir(parents=True)
    (repository / "fixture.txt").write_text("fixture\n", encoding="utf-8")
    for arguments in (
        ["init", "--initial-branch=main", str(repository)],
        ["-C", str(repository), "add", "fixture.txt"],
        [
            "-C",
            str(repository),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
    ):
        subprocess.run(["git", *arguments], check=True, capture_output=True)
    return repository


def files(
    *, workflow: str = WORKFLOW_TEXT, extra: list[dict[str, str]] | None = None
) -> list[dict[str, str]]:
    """The bundle content for one request, including every declared role file."""
    declared: dict[str, dict[str, str]] = {
        "workflow.yaml": {"path": "workflow.yaml", "content": workflow},
    }
    for reference, path, content in (
        ("role:source-researcher@", "roles/researcher.yaml", RESEARCHER_ROLE),
        ("role:technical-editor@", "roles/editor.yaml", EDITOR_ROLE),
        ("role:repair-coordinator@", "roles/coordinator.yaml", COORDINATOR_ROLE),
    ):
        if reference in workflow:
            declared[path] = {"path": path, "content": content}
    for item in extra or []:
        # A caller-supplied file replaces the default content for that path
        # rather than being appended beside it, which would be a duplicate.
        declared[item["path"]] = item
    return list(declared.values())


def login(client: TestClient, token: str = "bootstrap") -> dict[str, str]:
    response = client.post(
        "/v1/session/bootstrap", json={"token": token}, headers={"Origin": ORIGIN}
    )
    assert response.status_code == 200, response.text
    return {"Origin": ORIGIN, "X-CSRF-Token": response.json()["csrf_token"]}


@pytest.fixture
def bundle_case(tmp_path: Path) -> Any:
    repository = make_repository(tmp_path)
    directory = tmp_path / "state"
    directory.mkdir()
    registry = ProjectRegistry(directory / "projects.sqlite", [repository.parent])
    project = registry.create(
        {
            "name": "Workflow bundles",
            "repository_path": str(repository),
            "base_ref": "main",
            "target_branch": "main",
            "allowed_target_branches": ["main"],
        },
        command_key="project",
        principal="owner",
    )
    app = create_app(
        directory, origin=ORIGIN, bootstrap_token="bootstrap", allowed_roots=[repository.parent]
    )
    with TestClient(app, base_url=ORIGIN) as client:
        headers = login(client)
        conversation = client.post(
            f"/v1/projects/{project['id']}/conversations",
            json={"title": "Design session"},
            headers={**headers, "Idempotency-Key": CONVERSATION_KEY},
        )
        assert conversation.status_code == 201, conversation.text
        yield {
            "tmp_path": tmp_path,
            "repository": repository,
            "directory": directory,
            "registry": registry,
            "project_id": project["id"],
            "conversation_id": conversation.json()["id"],
            "app": app,
            "client": client,
            "headers": headers,
        }


def url(project_id: str, suffix: str) -> str:
    return f"/v1/projects/{project_id}{suffix}"


def create_bundle(
    case: dict[str, Any],
    *,
    bundle_id: str = BUNDLE,
    workflow: str = WORKFLOW_TEXT,
    extra: list[dict[str, str]] | None = None,
    key: str = "bundle-create",
    delivery_kind: str = "report",
    conversation_id: str | None = None,
    headers: dict[str, str] | None = None,
) -> Any:
    return case["client"].post(
        url(
            case["project_id"],
            f"/conversations/{conversation_id or case['conversation_id']}/workflows/{bundle_id}",
        ),
        json={
            "files": files(workflow=workflow, extra=extra),
            "delivery_kind": delivery_kind,
        },
        headers={**(headers or case["headers"]), "Idempotency-Key": key},
    )


