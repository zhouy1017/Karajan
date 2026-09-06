"""Public persisted v2 seed and authenticated HTTP, no provider or Git writes."""

import copy
import json
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from karajan.projects import ProjectRegistry
from karajan.runs import RunPlanner
from karajan.web import create_app
from seed import WORKTREE, advance, creation_input, seed, verify_imports

ORIGIN = "http://127.0.0.1:8972"
REPOSITORY = Path(os.environ["KARAJAN_SPEC_REPOSITORY"])


@pytest.fixture
def case(tmp_path):
    verify_imports()
    state = tmp_path / "state"
    manifest = seed(state, REPOSITORY)
    app = create_app(
        state, origin=ORIGIN, bootstrap_token="synthetic-ui-spec", allowed_roots=[REPOSITORY]
    )
    with TestClient(app, base_url=ORIGIN) as client:
        login = client.post(
            "/v1/session/bootstrap", json={"token": "synthetic-ui-spec"}, headers={"Origin": ORIGIN}
        )
        assert login.status_code == 200
        headers = {
            "Origin": ORIGIN,
            "X-CSRF-Token": login.json()["csrf_token"],
            "Idempotency-Key": "spec-approval",
        }
        yield client, headers, state, manifest


def read(case):
    client, _, _, manifest = case
    response = client.get("/v1/runs/" + manifest["runs"]["v2"]["run_id"])
    assert response.status_code == 200
    return response.json()


def post(case, payload, **headers):
    client, defaults, _, manifest = case
    route = "/v1/runs/" + manifest["runs"]["v2"]["run_id"] + "/plan-approval"
    return client.post(route, json=payload, headers={**defaults, **headers})


def test_authentic_http_exposes_whole_frozen_v2_and_exact_idempotent_approval(case):
    client, _, state, manifest = case
    row = manifest["runs"]["v2"]
    current = read(case)
    assert current["plans"] == [row["plan"]]
    assert current["execution_policy_snapshot"] == manifest["policy"]
    assert current["authorization_ceiling"] == row["creation"]["authorization"]
    assert current["dispatch_enabled"] is False
    assert current["live_qualification"] == "not_run"
    assert current["plans"][0]["plan"]["authorization"]["currency_limits"] == {
        "USD": "0",
        "CNY": "0",
    }
    evidence = WORKTREE / ".cache/v2-ui-spec/published-observed"
    evidence.mkdir(exist_ok=True)
    project = client.get("/v1/projects/" + manifest["project_id"]).json()
    legacy = client.get("/v1/runs/" + manifest["runs"]["v1"]["run_id"]).json()
    (evidence / "valid-view.json").write_text(
        json.dumps(
            {
                "scope": "synthetic-public-HTTP-fixture",
                "project": project,
                "run": current,
                "v1_run": legacy,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    accepted = post(case, row["approval"])
    assert accepted.status_code == 200
    assert post(case, row["approval"]).json() == accepted.json()
    approved = read(case)
    assert approved["approvals"] == [accepted.json()]
    assert approved["active_plan_revision"] == 1
    assert approved["dispatch_enabled"] is False
    reopened = RunPlanner(
        state / "runs.sqlite", ProjectRegistry(state / "projects.sqlite", [REPOSITORY])
    )
    assert reopened.get(row["run_id"], principal="owner") == approved


@pytest.mark.parametrize(
    "field", ["plan_digest", "authorization_digest", "configuration_digest", "routing_digest"]
)
def test_changed_confirmation_digest_cannot_approve_and_preserves_run(case, field):
    before = read(case)
    payload = copy.deepcopy(case[3]["runs"]["v2"]["approval"])
    payload[field] = "0" * 64
    response = post(case, payload)
    assert response.status_code == 409
    assert response.json()["reason_code"] == "APPROVAL_BINDING_MISMATCH"
    assert read(case) == before


@pytest.mark.parametrize("kind", ["missing-routing", "legacy-v1-body"])
def test_incomplete_or_legacy_confirmation_cannot_approve_v2(case, kind):
    before = read(case)
    payload = copy.deepcopy(case[3]["runs"]["v2"]["approval"])
    del payload["routing_digest"]
    if kind == "legacy-v1-body":
        del payload["schema_version"]
    response = post(case, payload)
    assert response.status_code == (409 if kind == "legacy-v1-body" else 422)
    assert read(case) == before


def test_approval_captured_before_new_plan_is_rejected_then_exact_new_plan_can_be_confirmed(case):
    _, _, state, manifest = case
    replacement = advance(state)
    before = read(case)
    response = post(case, manifest["runs"]["v2"]["approval"])
    assert response.status_code == 409
    assert response.json()["reason_code"] == "PLAN_REVISION_STALE"
    assert read(case) == before
    assert replacement["plan"]["plan_revision"] == 2
    accepted = post(case, replacement["approval"], **{"Idempotency-Key": "new-reviewed-plan"})
    assert accepted.status_code == 200
    assert read(case)["active_plan_revision"] == 2
    assert read(case)["dispatch_enabled"] is False


def test_approval_captured_before_explicit_commander_handoff_is_rejected(case):
    _, _, state, manifest = case
    row = manifest["runs"]["v2"]
    registry = ProjectRegistry(state / "projects.sqlite", [REPOSITORY])
    planner = RunPlanner(state / "runs.sqlite", registry)
    handoff = planner.propose_handoff(
        row["run_id"],
        {
            "term": 1,
            "expected_plan_revision": 1,
            "candidate": "synthetic-next",
            "checkpoint": {"summary": "仅演示审批失效", "artifacts": []},
            "resource_impact": {"budget_ref": "planning", "summary": "无额外现金授权"},
            "expires_at": time.time() + 300,
        },
        command_key="synthetic-handoff",
        principal="synthetic-lead",
    )
    planner.decide_handoff(
        row["run_id"],
        {
            "term": 1,
            "handoff_id": handoff["id"],
            "handoff_digest": handoff["digest"],
            "decision": "approve",
        },
        command_key="owner-handoff",
        principal="owner",
    )
    before = read(case)
    response = post(case, row["approval"])
    assert response.status_code == 409
    assert response.json()["reason_code"] == "COMMANDER_TERM_STALE"
    assert read(case) == before
    assert before["commander"]["term"] == 2


def test_new_current_configuration_does_not_replace_run_frozen_scope(case):
    _, _, state, manifest = case
    before = read(case)
    registry = ProjectRegistry(state / "projects.sqlite", [REPOSITORY])
    config = registry.get_configuration(manifest["project_id"])["configuration"]
    config["resources"]["budgets"][1]["max_duration_seconds"] = 301
    preview = registry.preview_configuration(
        manifest["project_id"], config, command_key="new-config-preview", principal="owner"
    )
    changed = registry.apply_configuration(
        manifest["project_id"],
        preview["preview_id"],
        expected_revision=2,
        command_key="new-config-apply",
        principal="owner",
    )
    assert changed["configuration"]["digest"] != before["configuration_snapshot"]["digest"]
    assert read(case) == before
    assert post(case, manifest["runs"]["v2"]["approval"]).status_code == 200


def test_v2_approval_requires_real_session_csrf_boundary(case):
    before = read(case)
    response = post(case, case[3]["runs"]["v2"]["approval"], **{"X-CSRF-Token": "wrong"})
    assert response.status_code == 403
    assert response.json()["reason_code"] == "CSRF_REJECTED"
    assert read(case) == before


def test_default_create_http_remains_v1_without_fabricated_v2_authority(case):
    client, headers, state, manifest = case
    registry = ProjectRegistry(state / "projects.sqlite", [REPOSITORY])
    request = creation_input(registry.get(manifest["project_id"]))
    assert "schema_version" not in request and "execution_policy" not in request
    response = client.post(
        "/v1/runs", json=request, headers={**headers, "Idempotency-Key": "default-new-run"}
    )
    assert response.status_code == 201
    assert response.json()["schema_version"] == "karajan.run-planning.v1"
    assert "execution_policy_snapshot" not in response.json()
    assert response.json()["dispatch_enabled"] is False
