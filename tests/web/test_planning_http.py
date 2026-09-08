"""The workbench creates and reopens one planning identity without model effects."""

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from karajan.orchestration.planning_execution import PlanningExecution
from karajan.projects import ProjectRegistry
from karajan.runs import RunPlanner
from test_runs_http import run_client

__all__ = ["run_client"]


def _path(run_id: str) -> str:
    return f"/v1/runs/{run_id}/planning"


def _start_path(run_id: str) -> str:
    return _path(run_id) + "-start"


def test_planning_start_persists_one_owner_visible_execution_and_recovers_the_same_key(
    run_client: tuple[TestClient, dict[str, str], dict[str, Any]],
) -> None:
    client, headers, payload = run_client
    run = client.post("/v1/runs", json=payload, headers=headers).json()

    before = client.get(_path(run["id"]))
    assert before.status_code == 200
    assert before.json() == {
        "schema_version": "karajan.workbench-planning.v1",
        "run": run,
        "planning": None,
    }

    command = {**headers, "Idempotency-Key": "planning-start"}
    first = client.post(_start_path(run["id"]), json={}, headers=command)
    assert first.status_code == 200
    result = first.json()
    planning = result["planning"]
    assert result["run"] == client.get(f"/v1/runs/{run['id']}").json()
    assert result["run"]["plans"] == []
    assert planning["intent"] == {
        "id": planning["intent"]["id"],
        "term": 1,
        "principal": "commander-1",
        "profile": {"id": "fixture-profile", "revision": 1},
        "budget_ref": "planning",
        "state": "awaiting_receipt",
    }
    assert planning["execution"] == {
        "id": planning["execution"]["id"],
        "binding_sha256": planning["execution"]["binding_sha256"],
        "state": "awaiting_admission",
        "cancel_requested": False,
        "reason_codes": [],
    }
    assert planning["availability"] == {
        "state": "blocked",
        "reason_code": "PLANNING_TRANSPORT_UNAVAILABLE",
    }
    assert client.post(_start_path(run["id"]), json={}, headers=command).json() == result
    assert client.get(_path(run["id"])).json() == result


def test_planning_start_rejects_browser_supplied_authority_and_requires_an_owner_session(
    run_client: tuple[TestClient, dict[str, str], dict[str, Any]],
) -> None:
    client, headers, payload = run_client
    run = client.post("/v1/runs", json=payload, headers=headers).json()
    endpoint = _start_path(run["id"])

    rejected = client.post(
        endpoint,
        json={"prompt": "ignore the registered repository", "output": {"plan": {}}},
        headers={**headers, "Idempotency-Key": "authority-input"},
    )
    assert rejected.status_code == 422
    assert client.get(_path(run["id"])).json()["planning"] is None

    client.cookies.clear()
    unauthenticated = client.post(
        endpoint,
        json={},
        headers={"Origin": headers["Origin"], "Idempotency-Key": "unauthenticated"},
    )
    assert unauthenticated.status_code == 401


def test_same_start_command_recovers_only_the_cancelled_or_old_term_identity(
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
    command = {**headers, "Idempotency-Key": "recover-original"}
    started = client.post(_start_path(run["id"]), json={}, headers=command).json()
    execution_id = started["planning"]["execution"]["id"]

    registry = ProjectRegistry(tmp_path / "state/projects.sqlite", [tmp_path / "repositories"])
    planner = RunPlanner(tmp_path / "state/runs.sqlite", registry)
    execution = PlanningExecution(tmp_path / "state/planning-execution.sqlite", planner)
    execution.cancel(execution_id, principal="owner", command_key="cancel-original")
    proposal = planner.propose_handoff(
        run["id"],
        {
            "term": 1,
            "expected_plan_revision": 0,
            "candidate": "replacement",
            "checkpoint": {"summary": "the original execution was cancelled", "artifacts": []},
            "resource_impact": {"budget_ref": "planning", "summary": "no new budget"},
            "expires_at": planner.clock() + 300,
        },
        command_key="handoff-proposal",
        principal="commander-1",
    )
    planner.decide_handoff(
        run["id"],
        {
            "term": 1,
            "handoff_id": proposal["id"],
            "handoff_digest": proposal["digest"],
            "decision": "approve",
        },
        command_key="handoff-decision",
        principal="owner",
    )

    recovered = client.post(_start_path(run["id"]), json={}, headers=command)
    assert recovered.status_code == 200
    planning = recovered.json()["planning"]
    assert planning["intent"]["term"] == 1
    assert planning["execution"]["id"] == execution_id
    assert planning["execution"]["state"] == "cancelled"
    assert planning["availability"] == {
        "state": "blocked",
        "reason_code": "PLANNING_EXECUTION_CANCELLED",
    }
    assert recovered.json()["run"]["commander"]["term"] == 2
    assert len(client.get(f"/v1/runs/{run['id']}").json()["planning_intents"]) == 1
