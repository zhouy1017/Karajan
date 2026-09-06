import json
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from karajan.projects import ProjectRegistry
from karajan.runs import RunPlanner
from karajan.web import create_app


@pytest.fixture
def run_client(tmp_path: Path) -> Iterator[tuple[TestClient, dict[str, str], dict[str, Any]]]:
    repository = tmp_path / "repositories" / "sample"
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
    app = create_app(
        tmp_path / "state",
        origin="http://127.0.0.1:8765",
        bootstrap_token="bootstrap",
        allowed_roots=[repository.parent],
    )
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        assert client.get("/v1/runs").status_code == 401
        login = client.post(
            "/v1/session/bootstrap",
            json={"token": "bootstrap"},
            headers={"Origin": "http://127.0.0.1:8765"},
        )
        headers = {
            "Origin": "http://127.0.0.1:8765",
            "X-CSRF-Token": login.json()["csrf_token"],
            "Idempotency-Key": "register",
        }
        project = client.post(
            "/v1/projects",
            json={
                "name": "Sample",
                "repository_path": str(repository),
                "base_ref": "main",
                "target_branch": "main",
                "allowed_target_branches": ["main"],
            },
            headers=headers,
        ).json()
        configuration = json.loads(
            Path("examples/projects/offline-configuration.json").read_text(encoding="utf-8")
        )
        preview = client.post(
            f"/v1/projects/{project['id']}/configuration/preview",
            json=configuration,
            headers={**headers, "Idempotency-Key": "preview"},
        ).json()
        project = client.post(
            f"/v1/projects/{project['id']}/configuration/apply",
            json={"preview_id": preview["preview_id"]},
            headers={**headers, "Idempotency-Key": "apply", "If-Match": '"1"'},
        ).json()
        payload = {
            "project_id": project["id"],
            "project_revision": project["revision"],
            "configuration_digest": project["configuration"]["digest"],
            "requirement": {"goal": "Add a greeting", "acceptance": ["The greeting is displayed"]},
            "participants": [
                {
                    "principal": "commander-1",
                    "profile": {"id": "fixture-profile", "revision": 1},
                    "purpose": "lead",
                }
            ],
            "authorization": {
                "profile_refs": [{"id": "fixture-profile", "revision": 1}],
                "read_paths": ["src", "tests"],
                "write_paths": ["src", "tests"],
                "budget_ref": "run",
                "checks": ["unit-tests", "independent_review"],
                "delivery": "pull_request",
                "target_branch": "main",
            },
        }
        yield client, {**headers, "Idempotency-Key": "create-run"}, payload


def test_http_creates_and_recovers_a_requirement_without_authorizing_execution(
    run_client: tuple[TestClient, dict[str, str], dict[str, Any]],
) -> None:
    client, headers, payload = run_client
    first = client.post("/v1/runs", json=payload, headers=headers)
    assert first.status_code == 201
    run = first.json()
    assert run["owner"] == "owner"
    assert run["dispatch_enabled"] is False
    assert run["plans"] == []
    assert client.post("/v1/runs", json=payload, headers=headers).json() == run
    listed = client.get("/v1/runs", params={"project_id": payload["project_id"]})
    assert [item["id"] for item in listed.json()["items"]] == [run["id"]]
    assert client.get(f"/v1/runs/{run['id']}").json() == run
    assert client.post(f"/v1/runs/{run['id']}/plans", json={}, headers=headers).status_code == 404
    assert (
        client.post(f"/v1/runs/{run['id']}/planning-receipts", json={}, headers=headers).status_code
        == 404
    )
    assert (
        client.post(f"/v1/runs/{run['id']}/handoffs", json={}, headers=headers).status_code == 404
    )


def test_owner_approves_only_the_exact_trusted_plan_and_retries_the_same_command(
    tmp_path: Path, run_client: tuple[TestClient, dict[str, str], dict[str, Any]]
) -> None:
    client, headers, payload = run_client
    run = client.post("/v1/runs", json=payload, headers=headers).json()
    receipts: dict[str, dict[str, Any]] = {}
    planner = RunPlanner(
        tmp_path / "state" / "runs.sqlite",
        ProjectRegistry(tmp_path / "state" / "projects.sqlite", [tmp_path / "repositories"]),
        admissions=receipts.__getitem__,
    )
    intent = planner.planning_intent(
        run["id"], term=1, command_key="intent", principal="commander-1"
    )
    receipts["fixture-receipt"] = {
        "receipt_ref": "fixture-receipt",
        "authority_revision": "fixture-authority",
        "run_id": run["id"],
        "intent_id": intent["id"],
        "term": 1,
        "principal": "commander-1",
        "profile": payload["participants"][0]["profile"],
        "budget_ref": intent["budget_ref"],
        "state": "admitted",
        "provenance": "fixture",
    }
    planner.attach_planning_receipt(
        run["id"],
        intent["id"],
        receipt_ref="fixture-receipt",
        command_key="receipt",
        principal="owner",
    )
    proposed = planner.submit_plan(
        run["id"],
        {
            "term": 1,
            "intent_id": intent["id"],
            "expected_plan_revision": 0,
            "plan": {
                "summary": "Fixture proposal",
                "authorization": payload["authorization"],
                "tasks": [
                    {
                        "id": "greeting",
                        "revision": 1,
                        "role": "worker",
                        "readiness": "ready",
                        "complexity": "T1",
                        "risk": "standard",
                        "paths": ["src/greeting.py"],
                        "depends_on": [],
                        "acceptance": ["The greeting is displayed"],
                        "required": True,
                    }
                ],
            },
        },
        command_key="submit",
        principal="commander-1",
    )
    approval = {
        key: proposed[key]
        for key in (
            "term",
            "plan_revision",
            "plan_digest",
            "authorization_digest",
            "configuration_digest",
        )
    }
    path = f"/v1/runs/{run['id']}/plan-approval"
    wrong = client.post(
        path,
        json={**approval, "plan_digest": "0" * 64},
        headers={**headers, "Idempotency-Key": "wrong-approval"},
    )
    assert wrong.status_code == 409
    assert client.get(f"/v1/runs/{run['id']}").json()["active_plan_revision"] is None
    accepted = client.post(path, json=approval, headers={**headers, "Idempotency-Key": "approve"})
    assert accepted.status_code == 200
    assert (
        client.post(path, json=approval, headers={**headers, "Idempotency-Key": "approve"}).json()
        == accepted.json()
    )
    approved = client.get(f"/v1/runs/{run['id']}").json()
    assert approved["active_plan_revision"] == 1
    assert approved["dispatch_enabled"] is False


def test_commander_handoff_waits_for_an_owner_decision_and_rejects_stale_confirmation(
    tmp_path: Path, run_client: tuple[TestClient, dict[str, str], dict[str, Any]]
) -> None:
    client, headers, payload = run_client
    payload["participants"].append(
        {
            "principal": "replacement",
            "profile": {"id": "fixture-profile", "revision": 1},
            "purpose": "candidate",
        }
    )
    run = client.post("/v1/runs", json=payload, headers=headers).json()
    planner = RunPlanner(
        tmp_path / "state" / "runs.sqlite",
        ProjectRegistry(tmp_path / "state" / "projects.sqlite", [tmp_path / "repositories"]),
    )
    proposal = {
        "term": 1,
        "expected_plan_revision": 0,
        "candidate": "replacement",
        "checkpoint": {"summary": "The requirement remains unchanged", "artifacts": []},
        "resource_impact": {"budget_ref": "planning", "summary": "No extra cash is permitted"},
        "expires_at": time.time() + 300,
    }
    pending = planner.propose_handoff(
        run["id"], proposal, command_key="propose-1", principal="commander-1"
    )
    path = f"/v1/runs/{run['id']}/handoff-decision"
    assert client.get(f"/v1/runs/{run['id']}").json()["commander"]["term"] == 1
    decision = {
        "term": 1,
        "handoff_id": pending["id"],
        "handoff_digest": pending["digest"],
        "decision": "reject",
    }
    rejected = client.post(path, json=decision, headers={**headers, "Idempotency-Key": "reject"})
    assert rejected.status_code == 200
    assert client.get(f"/v1/runs/{run['id']}").json()["commander"]["term"] == 1
    pending = planner.propose_handoff(
        run["id"], proposal, command_key="propose-2", principal="commander-1"
    )
    accepted = client.post(
        path,
        json={
            "term": 1,
            "handoff_id": pending["id"],
            "handoff_digest": pending["digest"],
            "decision": "approve",
        },
        headers={**headers, "Idempotency-Key": "handoff-approve"},
    )
    assert accepted.status_code == 200
    current = client.get(f"/v1/runs/{run['id']}").json()
    assert current["commander"]["principal"] == "replacement"
    assert current["commander"]["term"] == 2
    assert current["dispatch_enabled"] is False
    assert (
        client.post(
            path,
            json={**decision, "decision": "approve"},
            headers={**headers, "Idempotency-Key": "stale-handoff"},
        ).status_code
        == 409
    )
