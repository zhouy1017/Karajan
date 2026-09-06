"""The Web workbench approves persisted v2 scopes through the authenticated public API."""

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from karajan.projects import ProjectRegistry
from karajan.runs import RunPlanner
from test_runs_http import run_client

__all__ = ["run_client"]


@pytest.fixture
def v2_plan(
    tmp_path: Path, run_client: tuple[TestClient, dict[str, str], dict[str, Any]]
) -> tuple[TestClient, dict[str, str], RunPlanner, dict, dict, dict]:
    client, headers, request = run_client
    registry = ProjectRegistry(tmp_path / "state/projects.sqlite", [tmp_path / "repositories"])
    policy = registry.register_execution_policy(
        request["project_id"],
        {
            "schema_version": "karajan.execution-policy.v1",
            "id": "web-owner-policy",
            "revision": 1,
            "configuration_digest": request["configuration_digest"],
            "constraints": {
                "profile_refs": request["authorization"]["profile_refs"],
                "channel_ids": ["fixture-channel"],
                "tools": ["fixture-tools"],
                "data_destinations": ["local-fixture"],
                "required_capabilities": [],
                "min_isolation": "tool_sandboxed",
            },
            "channel_destinations": {"fixture-channel": "local-fixture"},
            "tool_policy": {
                "id": "web-tools",
                "revision": 1,
                "tool_permissions": {"fixture-tools": ["fixture-tools"]},
            },
            "risk_policy": {
                "id": "web-risk",
                "revision": 1,
                "mapping": {"standard": "T1", "critical": "T3"},
                "path_floors": [],
            },
            "context_policy": {
                "id": "web-context",
                "revision": 1,
                "input_accounting": "explicit_approved_upper_bound",
                "reserved_output_tokens": 1024,
            },
            "max_context_tokens": 8192,
        },
        command_key="web-policy",
        principal="owner",
    )
    request.update(
        schema_version="karajan.create-run.v2",
        execution_policy={key: policy[key] for key in ("id", "revision", "digest")},
    )
    request["authorization"].update(
        channel_ids=["fixture-channel"],
        tools=["fixture-tools"],
        data_destinations=["local-fixture"],
        required_capabilities=["controlled_tools"],
        min_isolation="tool_sandboxed",
        currency_limits={"USD": "0", "CNY": "0"},
        max_attempt_duration_seconds=25,
        max_quality_repair_rounds=2,
        stage_permissions={"bounded-worker": {"normal": True, "quality_indices": [0]}},
    )
    response = client.post("/v1/runs", json=request, headers=headers)
    assert response.status_code == 201
    run = response.json()
    receipts: dict[str, dict[str, Any]] = {}
    planner = RunPlanner(tmp_path / "state/runs.sqlite", registry, admissions=receipts.__getitem__)
    intent = planner.planning_intent(
        run["id"], term=1, command_key="web-intent", principal="commander-1"
    )
    receipts["web-fixture-receipt"] = {
        "receipt_ref": "web-fixture-receipt",
        "authority_revision": "web-fixture-authority",
        "run_id": run["id"],
        "intent_id": intent["id"],
        "term": 1,
        "principal": "commander-1",
        "profile": request["participants"][0]["profile"],
        "budget_ref": intent["budget_ref"],
        "state": "admitted",
        "provenance": "fixture",
    }
    planner.attach_planning_receipt(
        run["id"],
        intent["id"],
        receipt_ref="web-fixture-receipt",
        command_key="web-receipt",
        principal="owner",
    )
    submitted = {
        "schema_version": "karajan.submit-plan.v2",
        "term": 1,
        "intent_id": intent["id"],
        "expected_plan_revision": 0,
        "plan": {
            "summary": "Fixed Web authorization fixture",
            "authorization": request["authorization"],
            "tasks": [
                {
                    "id": "feature",
                    "revision": 1,
                    "role": "worker",
                    "purpose": None,
                    "readiness": "ready",
                    "complexity": "T2",
                    "risk": "standard",
                    "paths": ["src/feature.py"],
                    "depends_on": [],
                    "required": True,
                    "acceptance": ["The greeting is displayed"],
                    "domains": ["code"],
                    "required_capabilities": ["bounded_code_edit"],
                    "tools": ["fixture-tools"],
                    "context_tokens": 4096,
                    "duration_seconds": 20,
                }
            ],
        },
    }
    plan = planner.submit_plan(
        run["id"], submitted, command_key="web-plan", principal="commander-1"
    )
    return client, headers, planner, run, submitted, plan


def approval(plan: dict) -> dict:
    return {
        "schema_version": "karajan.approve-plan.v2",
        **{
            key: plan[key]
            for key in (
                "term",
                "plan_revision",
                "plan_digest",
                "configuration_digest",
                "authorization_digest",
                "routing_digest",
            )
        },
    }


def test_http_exposes_the_frozen_v2_scope_and_replays_the_exact_owner_approval(
    v2_plan: tuple,
) -> None:
    client, headers, planner, run, _, plan = v2_plan
    url = f"/v1/runs/{run['id']}"
    displayed = client.get(url).json()
    assert displayed == planner.get(run["id"], principal="owner")
    assert displayed["schema_version"] == "karajan.run-planning.v2"
    assert displayed["execution_policy_snapshot"]["registered_by"] == "owner"
    assert displayed["execution_policy_snapshot"]["project_id"] == run["project_id"]
    assert displayed["plans"][-1]["routing_binding"]["stage_grants"]["bounded-worker"] == {
        "normal": {"standard_qualified": [{"id": "fixture-profile", "revision": 1}]},
        "quality": [
            {
                "index": 0,
                "group": "critical_qualified",
                "profiles": [{"id": "fixture-profile", "revision": 1}],
            }
        ],
    }
    assert (
        displayed["plans"][-1]["routing_binding"]["task_requirements"]["feature"]["context_tokens"]
        == 4096
    )
    assert displayed["execution_policy_snapshot"]["channel_destinations"] == {
        "fixture-channel": "local-fixture"
    }
    assert displayed["plans"][-1]["plan"]["authorization"]["currency_limits"] == {
        "USD": "0",
        "CNY": "0",
    }
    command = {**headers, "Idempotency-Key": "v2-web-approve"}
    first = client.post(url + "/plan-approval", json=approval(plan), headers=command)
    assert first.status_code == 200
    assert first.json()["routing_digest"] == plan["routing_digest"]
    assert first.json()["dispatch_enabled"] is False
    assert (
        client.post(url + "/plan-approval", json=approval(plan), headers=command).json()
        == first.json()
    )
    current = client.get(url).json()
    assert current["active_plan_revision"] == 1
    assert current["approvals"] == [first.json()]
    assert current["dispatch_enabled"] is False


@pytest.mark.parametrize(
    ("kind", "status"),
    [("legacy", 409), ("missing-routing", 422), ("unknown-version", 422), ("wrong-routing", 409)],
)
def test_http_rejects_incomplete_or_wrong_version_approval_without_changing_the_run(
    v2_plan: tuple, kind: str, status: int
) -> None:
    client, headers, _, run, _, plan = v2_plan
    body = approval(plan)
    if kind == "legacy":
        del body["schema_version"]
        del body["routing_digest"]
    elif kind == "missing-routing":
        del body["routing_digest"]
    elif kind == "unknown-version":
        body["schema_version"] = "karajan.approve-plan.v3"
    else:
        body["routing_digest"] = "0" * 64
    url = f"/v1/runs/{run['id']}"
    before = client.get(url).json()
    response = client.post(
        url + "/plan-approval", json=body, headers={**headers, "Idempotency-Key": "invalid-" + kind}
    )
    assert response.status_code == status
    assert client.get(url).json() == before


def test_http_changed_scope_rejects_the_previously_displayed_confirmation(v2_plan: tuple) -> None:
    client, headers, planner, run, submitted, plan = v2_plan
    revised = deepcopy(submitted)
    revised["expected_plan_revision"] = 1
    revised["plan"]["authorization"]["currency_limits"] = {"USD": "0"}
    replacement = planner.submit_plan(
        run["id"], revised, command_key="web-replacement", principal="commander-1"
    )
    url = f"/v1/runs/{run['id']}"
    stale = client.post(
        url + "/plan-approval", json=approval(plan), headers={**headers, "Idempotency-Key": "stale"}
    )
    assert stale.status_code == 409
    assert stale.json()["reason_code"] == "PLAN_REVISION_STALE"
    current = client.get(url).json()
    assert current["active_plan_revision"] is None
    assert current["plans"][-1] == replacement
    assert current["plans"][-1]["authorization_digest"] != plan["authorization_digest"]
    accepted = client.post(
        url + "/plan-approval",
        json=approval(replacement),
        headers={**headers, "Idempotency-Key": "revised"},
    )
    assert accepted.status_code == 200
    assert accepted.json()["plan_revision"] == 2
