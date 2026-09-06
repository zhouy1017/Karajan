"""Independent Standards boundary checks against this worktree's public modules."""

from copy import deepcopy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from karajan.projects import ProjectError, ProjectRegistry
from karajan.runs import RunError, RunPlanner
from karajan.web import create_app
from test_planning import project
from test_routing_authorization import policy_request, request_v2

__all__ = ["project"]


def test_http_v2_large_reference_returns_json_rejection_instead_of_server_error(
    tmp_path: Path, project: tuple
) -> None:
    registry, configured, _ = project
    fixed = registry.register_execution_policy(
        configured["id"], policy_request(configured), command_key="policy", principal="owner"
    )
    creation = request_v2(configured, fixed)
    creation["execution_policy"]["revision"] = 10**40
    origin = "http://127.0.0.1:8765"
    app = create_app(
        tmp_path, origin=origin, bootstrap_token="fixture-bootstrap", allowed_roots=[tmp_path]
    )
    with TestClient(app, base_url=origin, raise_server_exceptions=False) as client:
        login = client.post(
            "/v1/session/bootstrap", json={"token": "fixture-bootstrap"}, headers={"Origin": origin}
        )
        assert login.status_code == 200
        response = client.post(
            "/v1/runs",
            json=creation,
            headers={
                "Origin": origin,
                "X-CSRF-Token": login.json()["csrf_token"],
                "Idempotency-Key": "large-revision",
            },
        )
        assert response.status_code == 422, (response.status_code, response.text)
        assert "reason_code" in response.json()


@pytest.mark.parametrize("entry", ["registry", "run"])
def test_out_of_range_policy_reference_is_a_stable_domain_rejection(
    tmp_path: Path, project: tuple, entry: str
) -> None:
    registry, configured, _ = project
    fixed = registry.register_execution_policy(
        configured["id"], policy_request(configured), command_key="policy", principal="owner"
    )
    if entry == "registry":
        with pytest.raises(ProjectError):
            registry.get_execution_policy(configured["id"], fixed["id"], 10**40, principal="owner")
    else:
        creation = request_v2(configured, fixed)
        creation["execution_policy"]["revision"] = 10**40
        planner = RunPlanner(tmp_path / "runs.sqlite", registry)
        with pytest.raises(RunError):
            planner.create(creation, command_key="too-large", principal="owner")
        assert planner.list(principal="owner") == []


def test_policy_identifier_rejects_surrogate_before_sql_binding(project: tuple) -> None:
    registry, configured, _ = project
    request = policy_request(configured)
    request["id"] = "\ud800"
    with pytest.raises(ProjectError):
        registry.register_execution_policy(
            configured["id"], request, command_key="invalid-policy-id", principal="owner"
        )


def test_returned_policy_and_run_objects_cannot_mutate_stored_bindings(
    tmp_path: Path, project: tuple
) -> None:
    registry, configured, _ = project
    policy_input = policy_request(configured)
    fixed = registry.register_execution_policy(
        configured["id"], policy_input, command_key="policy", principal="owner"
    )
    canonical = deepcopy(fixed)
    planner = RunPlanner(tmp_path / "runs.sqlite", registry)
    request = request_v2(configured, fixed)
    created = planner.create(request, command_key="run", principal="owner")
    original_run = deepcopy(created)
    fixed["constraints"]["tools"].append("forged")
    policy_input["risk_policy"]["mapping"]["critical"] = "T1"
    request["authorization"]["max_attempt_duration_seconds"] = 999
    created["execution_policy_snapshot"]["tool_policy"]["tool_permissions"].clear()
    reread = ProjectRegistry(registry.database, [tmp_path])
    assert (
        reread.get_execution_policy(
            configured["id"], canonical["id"], canonical["revision"], principal="owner"
        )
        == canonical
    )
    assert RunPlanner(planner.database, reread).get(original_run["id"]) == original_run
