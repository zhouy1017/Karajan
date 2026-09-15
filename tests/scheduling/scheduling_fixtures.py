"""Shared fixtures for the role-directed scheduling cases.

Every case goes through the real FastAPI application: real session bootstrap,
real CSRF, real SQLite, real files on disk and the real compiled workflow
compiler. Nothing here inserts a grant, a decision, a claim or a capacity row
directly: the whole chain is entered through the authenticated management
commands and the separately issued protocol credentials, which is the only way
an authority boundary can be shown to hold.

No case calls a model, starts a process or executes a business step. The
registered ``artifact_aggregate`` adapter is present as a capability, and the
cases assert that a deployment and a claim never invoke it.
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
SLOT = "default"
CONVERSATION_KEY = "conversation-create"
PROJECT_KEY = "project"
DEPLOY_KEY = "deploy-1"
RUN_KEY = "run-1"

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

REQUIRED_INPUT_A = "requirement.option_a"
REQUIRED_INPUT_B = "requirement.option_b"
REQUIRED_OUTCOME = "comparison-report"
INPUTS = {"requirement.option_a": "Option A summary", "requirement.option_b": "Option B summary"}


def make_repository(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    repository = root / "repository"
    repository.mkdir()
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


def bundle_files() -> list[dict[str, str]]:
    return [
        {"path": "workflow.yaml", "content": WORKFLOW_TEXT},
        {"path": "roles/researcher.yaml", "content": RESEARCHER_ROLE},
        {"path": "roles/editor.yaml", "content": EDITOR_ROLE},
    ]


def login(client: TestClient, token: str = "bootstrap") -> dict[str, str]:
    response = client.post(
        "/v1/session/bootstrap", json={"token": token}, headers={"Origin": ORIGIN}
    )
    assert response.status_code == 200, response.text
    return {"Origin": ORIGIN, "X-CSRF-Token": response.json()["csrf_token"]}


def url(project_id: str, suffix: str) -> str:
    return f"/v1/projects/{project_id}{suffix}"


def key(value: str) -> dict[str, str]:
    return {"Idempotency-Key": value}


# ------------------------------------------------------------------ grant bodies


def grant_body(
    *,
    grant_id: str = "configuration-repair-scope",
    role_instance: str = "role-instance-1",
    actions: tuple[str, ...] = (
        "expand_graph",
        "set_priority",
        "seal",
        "bind_role",
        "set_dependencies",
        "request_dispatch",
    ),
    inputs: tuple[str, ...] = (REQUIRED_INPUT_A, REQUIRED_INPUT_B),
    scope: tuple[str, ...] = ("src/config/**", "reports/**"),
    write_scope: tuple[str, ...] = ("src/config/**", "reports/**"),
    outcomes: tuple[str, ...] = (REQUIRED_OUTCOME, "config-defect-triage"),
    roles: dict[str, str] | None = None,
    model_refs: tuple[str, ...] = (),
    kinds: tuple[str, ...] = ("artifact_aggregate@1",),
    tools: tuple[str, ...] = ("read", "write"),
    write_zones: tuple[str, ...] = ("src/config/**",),
    destinations: tuple[str, ...] = ("local",),
    artifact: str = "report",
    pool: str = "pool-a",
    budget: str = "budget-user-approved",
    cost: int = 1,
    max_active: int | None = None,
    delegation: dict[str, Any] | None = None,
    expires_at: float | None = None,
) -> dict[str, Any]:
    """One complete grant command. Every dimension is explicit."""
    body: dict[str, Any] = {
        "grant_id": grant_id,
        "subject_ref": f"subject:{role_instance}",
        "role_instance": role_instance,
        "allowed_actions": list(actions),
        "inputs": list(inputs),
        "scope": list(scope),
        "write_scope": list(write_scope),
        "required_outcomes": list(outcomes),
        "role_refs": roles or {"researcher": "role:source-researcher@2"},
        "model_refs": list(model_refs),
        "execution_kind_refs": list(kinds),
        "tool_refs": list(tools),
        "data_destinations": list(destinations),
        "artifact": artifact,
        "resource_policy": {
            "budget_ref": budget,
            "pool_id": pool,
            "cost_per_claim": cost,
            "max_active_claims": max_active,
        },
    }
    if delegation is not None:
        body["delegation"] = delegation
    if expires_at is not None:
        body["expires_at"] = expires_at
    return body


def decision_body(
    *,
    decision_id: str = "decision-1",
    grant_id: str = "configuration-repair-scope",
    term: int = 0,
    expected_graph_revision: int = 0,
    graph_digest: str | None = None,
    inputs_digest: str,
    actions: list[dict[str, Any]],
    trigger: str = "approved_input",
    reason: str = "one shared defect",
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "decision_id": decision_id,
        "grant_id": grant_id,
        "term": term,
        "expected_graph_revision": expected_graph_revision,
        "inputs_digest": inputs_digest,
        "trigger": trigger,
        "reason": reason,
        "actions": actions,
    }
    if graph_digest is not None:
        body["graph_digest"] = graph_digest
    return body


def task_spec(
    *,
    item_key: str,
    role: str = "researcher",
    expansion_id: str = "defects",
    sources: Any = REQUIRED_INPUT_A,
    zone: str = "src/config/a",
    outcomes: tuple[str, ...] = ("config-defect-triage",),
    priority: int = 0,
    depends_on: tuple[str, ...] = (),
    joins: tuple[str, ...] = (),
    required: bool = True,
) -> dict[str, Any]:
    return {
        "item_key": item_key,
        "role_alias": role,
        "execution_kind_ref": "artifact_aggregate@1",
        "output_contract_ref": "aggregated-report@1",
        "inputs": {"sources": sources},
        "depends_on": list(depends_on),
        "joins": list(joins),
        "required": required,
        "priority": priority,
        "write_zone": zone,
        "path_scope": ["src/config/**"],
        "required_outcomes": list(outcomes),
        "expansion_id": expansion_id,
    }


@pytest.fixture
def case(tmp_path: Path) -> Any:
    """One project with a deployed definition and a Run created from it."""
    repository = make_repository(tmp_path)
    directory = tmp_path / "state"
    directory.mkdir()
    registry = ProjectRegistry(directory / "projects.sqlite", [repository.parent])
    project = registry.create(
        {
            "name": "Role scheduling",
            "repository_path": str(repository),
            "base_ref": "main",
            "target_branch": "main",
            "allowed_target_branches": ["main"],
        },
        command_key=PROJECT_KEY,
        principal="owner",
    )
    app = create_app(
        directory, origin=ORIGIN, bootstrap_token="bootstrap", allowed_roots=[repository.parent]
    )
    with TestClient(app, base_url=ORIGIN) as client:
        headers = login(client)
        base = url(project["id"], "")
        conversation = client.post(
            f"{base}/conversations",
            json={"title": "Design session"},
            headers={**headers, **key(CONVERSATION_KEY)},
        )
        assert conversation.status_code == 201, conversation.text
        conversation_id = conversation.json()["id"]
        created = client.post(
            f"{base}/conversations/{conversation_id}/workflows/{BUNDLE}",
            json={"files": bundle_files(), "delivery_kind": "report"},
            headers={**headers, **key("bundle-create")},
        )
        assert created.status_code == 201, created.text
        preview = client.get(f"{base}/workflows/{BUNDLE}/revisions/1/preview")
        assert preview.status_code == 200, preview.text
        deployed = client.post(
            f"{base}/conversations/{conversation_id}/workflows/{BUNDLE}/revisions/1/deployments",
            json={
                "action": "deploy_only",
                "slot": SLOT,
                "expected_active_revision": 0,
                "preview_id": preview.json()["preview_id"],
            },
            headers={**headers, **key(DEPLOY_KEY)},
        )
        assert deployed.status_code == 201, deployed.text
        yield {
            "tmp_path": tmp_path,
            "repository": repository,
            "directory": directory,
            "registry": registry,
            "project_id": project["id"],
            "conversation_id": conversation_id,
            "deployment": deployed.json(),
            "app": app,
            "client": client,
            "headers": headers,
        }


@pytest.fixture
def run_case(case: dict[str, Any]) -> dict[str, Any]:
    """A Run created through the real authenticated management entry."""
    base = url(case["project_id"], "")
    response = case["client"].post(
        f"{base}/workflow-runs",
        json={
            "conversation_id": case["conversation_id"],
            "slot": SLOT,
            "expected_active_revision": 1,
            "deployment_id": case["deployment"]["deployment"]["deployment_id"],
            "inputs": INPUTS,
            "requirement": {"goal": "compare the two options", "acceptance": ["both read"]},
            "required_outcomes": [REQUIRED_OUTCOME],
            "title": "Compare options",
        },
        headers={**case["headers"], **key(RUN_KEY)},
    )
    assert response.status_code == 201, response.text
    case["run"] = response.json()
    case["run_id"] = case["run"]["run_id"]
    case["base"] = base
    return case


@pytest.fixture
def granted_case(run_case: dict[str, Any]) -> dict[str, Any]:
    """A Run with one top-level grant, one expansion set and two credentials."""
    base = run_case["base"]
    issued = run_case["client"].post(
        f"{base}/workflow-runs/{run_case['run_id']}/grants",
        json=grant_body(),
        headers={**run_case["headers"], **key("grant-1")},
    )
    assert issued.status_code == 201, issued.text
    run_case["grant"] = issued.json()

    expansion = run_case["client"].post(
        f"{base}/workflow-runs/{run_case['run_id']}/expansions",
        json={
            "grant_id": "configuration-repair-scope",
            "expansion_id": "defects",
            "goal": "the confirmed configuration defects",
            "required_outcomes": ["config-defect-triage"],
        },
        headers={**run_case["headers"], **key("expansion-1")},
    )
    assert expansion.status_code == 201, expansion.text
    run_case["expansion"] = expansion.json()

    role_credential = issue(run_case, kind="role_protocol", grant_id="configuration-repair-scope",
                            role_instance="role-instance-1", key_value="cred-role-1")
    consumer_credential = issue(run_case, kind="execution_consumer", key_value="cred-consumer-1")
    run_case["role_token"] = role_credential["token"]
    run_case["consumer_token"] = consumer_credential["token"]
    run_case["role_credential"] = role_credential["credential"]
    run_case["consumer_credential"] = consumer_credential["credential"]
    return run_case


def issue(
    case: dict[str, Any],
    *,
    kind: str,
    key_value: str,
    grant_id: str | None = None,
    role_instance: str | None = None,
    expires_in: float | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"kind": kind, "run_id": case["run_id"]}
    if grant_id is not None:
        body["grant_id"] = grant_id
    if role_instance is not None:
        body["role_instance"] = role_instance
    if expires_in is not None:
        body["expires_in"] = expires_in
    response = case["client"].post(
        f"{case['base']}/workflow-runs/{case['run_id']}/credentials",
        json=body,
        headers={**case["headers"], **key(key_value)},
    )
    assert response.status_code in {200, 201}, response.text
    return response.json()


def protocol_headers(token: str) -> dict[str, str]:
    """The one header a protocol request carries. No session, no CSRF."""
    return {"Authorization": f"Bearer {token}"}


def decide(
    case: dict[str, Any],
    actions: list[dict[str, Any]],
    *,
    key_value: str = "decision-1",
    decision_id: str = "decision-1",
    token: str | None = None,
    grant_id: str = "configuration-repair-scope",
    term: int = 0,
    expected_graph_revision: int = 0,
    graph_digest: str | None = None,
    inputs_digest: str | None = None,
    extra: dict[str, Any] | None = None,
) -> Any:
    body = decision_body(
        decision_id=decision_id,
        grant_id=grant_id,
        term=term,
        expected_graph_revision=expected_graph_revision,
        graph_digest=graph_digest,
        inputs_digest=inputs_digest or case["run"]["inputs_digest"],
        actions=actions,
    )
    body.update(extra or {})
    return case["client"].post(
        f"/v1/scheduling/protocol/projects/{case['project_id']}/runs/{case['run_id']}"
        f"/grants/{grant_id}/decisions",
        json=body,
        headers={**protocol_headers(token or case["role_token"]), **key(key_value)},
    )


def expand(
    case: dict[str, Any],
    specs: list[dict[str, Any]],
    *,
    key_value: str = "decision-1",
    decision_id: str = "decision-1",
    token: str | None = None,
    grant_id: str = "configuration-repair-scope",
    term: int = 0,
    expected_graph_revision: int = 0,
    graph_digest: str | None = None,
) -> Any:
    return decide(
        case,
        [{"action": "expand_graph", "expansion_id": "defects", "tasks": specs}],
        key_value=key_value,
        decision_id=decision_id,
        token=token,
        grant_id=grant_id,
        term=term,
        expected_graph_revision=expected_graph_revision,
        graph_digest=graph_digest,
    )


def graph(case: dict[str, Any]) -> dict[str, Any]:
    response = case["client"].get(
        f"{case['base']}/workflow-runs/{case['run_id']}/graph", headers=case["headers"]
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


def tasks(case: dict[str, Any], **parameters: Any) -> dict[str, Any]:
    response = case["client"].get(
        f"{case['base']}/workflow-runs/{case['run_id']}/tasks",
        params={key: value for key, value in parameters.items() if value is not None},
        headers=case["headers"],
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


def pages(
    case: dict[str, Any], *, size: int = 50, state: str | None = None
) -> list[dict[str, Any]]:
    """Read the *whole* task set across pages, asserting nothing is dropped."""
    seen: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        page = tasks(case, limit=size, cursor=cursor, **({"state": state} if state else {}))
        seen.extend(page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            assert page["count"] == len(seen), (page["count"], len(seen))
            return seen


def queue(case: dict[str, Any], *, token: str | None = None, **parameters: Any) -> dict[str, Any]:
    response = case["client"].get(
        f"/v1/scheduling/protocol/projects/{case['project_id']}/runs/{case['run_id']}/queue",
        params={key: value for key, value in parameters.items() if value is not None},
        headers=protocol_headers(token or case["consumer_token"]),
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


def claim(
    case: dict[str, Any],
    task_id: str,
    *,
    key_value: str,
    token: str | None = None,
) -> Any:
    return case["client"].post(
        f"/v1/scheduling/protocol/projects/{case['project_id']}/runs/{case['run_id']}/claims",
        json={"claim_key": key_value, "task_id": task_id},
        headers={**protocol_headers(token or case["consumer_token"]), **key(key_value)},
    )


def report(
    case: dict[str, Any],
    task_id: str,
    payload: dict[str, Any],
    *,
    key_value: str = "report-1",
    token: str | None = None,
) -> Any:
    return case["client"].post(
        f"/v1/scheduling/protocol/projects/{case['project_id']}/runs/{case['run_id']}"
        f"/tasks/{task_id}/reports",
        json=payload,
        headers={
            **protocol_headers(token or case["consumer_token"]),
            **key(key_value),
        },
    )


def resource_policy(case: dict[str, Any], **overrides: Any) -> Any:
    body = {
        "pool_id": "pool-a",
        "max_concurrent_claims": 4,
        "safety_margin": "0",
        "require_observation": True,
    }
    body.update(overrides)
    return case["client"].post(
        "/v1/scheduling/resource-policies", json=body, headers={**case["headers"], **key("policy")}
    )


def admissible(case: dict[str, Any], *, capacity: int = 4, amount: str = "8") -> None:
    """Establish the trusted capacity a claim is admitted against.

    A pool with no declared policy and no trusted observation has *unknown*
    capacity, which is not the same thing as unlimited: the engine refuses to
    hand out work it cannot account for. Every claim case therefore states the
    policy and the reading it expects to be admitted under, exactly as an
    operator would.
    """
    policy = resource_policy(case, max_concurrent_claims=capacity)
    assert policy.status_code in {200, 201}, policy.text
    reading = observe(case, amount=amount)
    assert reading.status_code in {200, 201}, reading.text


def observe(
    case: dict[str, Any], *, amount: str = "8", metric: str = "remaining", **overrides: Any
) -> Any:
    body = {
        "pool_id": "pool-a",
        "window_id": "window-1",
        "metric": metric,
        "amount": amount,
        "source": "local_ledger",
        "source_ref": "ledger-1",
    }
    body.update(overrides)
    return case["client"].post(
        "/v1/scheduling/resource-observations",
        json=body,
        headers={**case["headers"], **key("observation")},
    )
