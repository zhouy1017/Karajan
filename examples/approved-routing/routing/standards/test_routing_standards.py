"""Standards boundary checks using real approved Run HTTP and persistent sources."""

import json
import time
from copy import deepcopy

import pytest
from karajan.capacity import CapacityStore
from karajan.orchestration.routing import ApprovedRunRouting
from karajan.projects import ProjectRegistry
from karajan.projects.demand import AttemptEstimateStore
from karajan.projects.qualification import ProfileQualificationStore
from karajan.routing.compiler import digest
from karajan.runs import RunError, RunPlanner
from test_routing_authorization import policy_request, request_v2
from test_v2_approval_workbench import approval, run_client, v2_plan

__all__ = ["run_client", "v2_plan"]


def approve(fixture):
    client, headers, planner, run, submission, plan = fixture
    result = client.post(
        f"/v1/runs/{run['id']}/plan-approval",
        json=approval(plan),
        headers={**headers, "Idempotency-Key": "approve"},
    )
    assert result.status_code == 200
    return client, headers, planner, run, submission, plan


def test_actual_http_record_is_durable_truthful_and_side_effect_free(v2_plan, tmp_path):
    client, headers, planner, run, _, plan = approve(v2_plan)
    capacity = CapacityStore(tmp_path / "state/capacity.sqlite")
    before = capacity.snapshot()
    url = f"/v1/runs/{run['id']}/tasks/feature/routing-assessments"
    result = client.post(url, json={}, headers={**headers, "Idempotency-Key": "assess"})
    assert result.status_code == 201
    receipt = result.json()
    assert receipt["digest"] == digest({k: v for k, v in receipt.items() if k != "digest"})
    assert receipt["state"] == "blocked"
    assert receipt["activation_allowed"] is False and receipt["dispatch_enabled"] is False
    assert receipt["route"]["activation_allowed"] is False
    assert receipt["sources"]["approval"]["plan_digest"] == plan["plan_digest"]
    assert receipt["route"]["snapshots"]["task"]["reserved_output_tokens"] == 1024
    assert receipt["route"]["snapshots"]["policy"]["profile_facts"] == []
    assert all(
        p["capability_evidence"] == []
        for p in receipt["route"]["snapshots"]["policy"]["resources"]["profiles"]
    )
    assert receipt["route"]["snapshots"]["capacity"]["budget_remaining"] == {}
    assert capacity.snapshot() == before
    reopened = ApprovedRunRouting(planner, ProfileQualificationStore(planner.projects), capacity)
    assert reopened.get(run["id"], receipt["id"], principal="owner") == receipt
    assert (
        client.post(url, json={}, headers={**headers, "Idempotency-Key": "assess"}).json()
        == receipt
    )
    assert len({receipt["id"], receipt["planned_attempt_id"], receipt["planned_context_id"]}) == 3


@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        1,
        "claim",
        {"profile_ref": {"id": "chosen", "revision": 1}},
        {"sources": {}},
        {"expected_capacity": {}},
        {"planned_attempt_id": "fixed"},
        {"principal": "owner"},
        {"capacity": {}},
        {"authorization": {}},
        {"stage": "quality"},
    ],
)
def test_http_cannot_accept_a_client_supplied_routing_material(v2_plan, body):
    client, headers, _, run, _, _ = v2_plan
    url = f"/v1/runs/{run['id']}/tasks/feature/routing-assessments"
    response = client.post(
        url, content=json.dumps(body), headers={**headers, "Content-Type": "application/json"}
    )
    assert response.status_code == 422


def test_old_receipt_is_historical_and_new_key_reads_current_profile(v2_plan):
    client, headers, planner, run, _, _ = approve(v2_plan)
    url = f"/v1/runs/{run['id']}/tasks/feature/routing-assessments"
    before = client.post(url, json={}, headers={**headers, "Idempotency-Key": "assess"}).json()
    projects = planner.projects
    project = projects.get(run["project_id"])
    configuration = projects.get_configuration(run["project_id"])["configuration"]
    configuration["resources"]["profiles"][0]["enabled"] = False
    preview = projects.preview_configuration(
        run["project_id"], configuration, command_key="disable-preview", principal="owner"
    )
    projects.apply_configuration(
        run["project_id"],
        preview["preview_id"],
        expected_revision=project["revision"],
        command_key="disable",
        principal="owner",
    )
    assert (
        client.post(url, json={}, headers={**headers, "Idempotency-Key": "assess"}).json() == before
    )
    after = client.post(url, json={}, headers={**headers, "Idempotency-Key": "new-assess"}).json()
    assert after["sources"]["catalog_digest"] != before["sources"]["catalog_digest"]
    assert "CURRENT_PROFILE_RESTRICTED" in after["sources"]["profiles"][0]["reason_codes"]
    assert after["planned_attempt_id"] != before["planned_attempt_id"]
    assert after["activation_allowed"] is False


def test_conflicting_command_and_wrong_run_cannot_read_other_assessment(v2_plan):
    client, headers, _, run, _, _ = approve(v2_plan)
    url = f"/v1/runs/{run['id']}/tasks/feature/routing-assessments"
    receipt = client.post(url, json={}, headers={**headers, "Idempotency-Key": "assess"}).json()
    conflict = client.post(
        f"/v1/runs/{run['id']}/tasks/not-feature/routing-assessments",
        json={},
        headers={**headers, "Idempotency-Key": "assess"},
    )
    assert conflict.status_code == 409
    assert (
        client.get(f"/v1/runs/not-this-run/routing-assessments/{receipt['id']}").status_code == 404
    )
    client.cookies.clear()
    assert (
        client.get(f"/v1/runs/{run['id']}/routing-assessments/{receipt['id']}").status_code == 401
    )


def test_wrong_controller_source_database_is_rejected(v2_plan, tmp_path):
    _, _, planner, _, _, _ = v2_plan
    other = ProjectRegistry(tmp_path / "other-project.sqlite", [tmp_path])
    capacity = CapacityStore(tmp_path / "other-capacity.sqlite")
    with pytest.raises(RunError, match="ROUTING_PROJECT_SOURCE_MISMATCH"):
        ApprovedRunRouting(planner, ProfileQualificationStore(other), capacity)
    other_planner = RunPlanner(tmp_path / "other-runs.sqlite", planner.projects)
    estimates = AttemptEstimateStore(other_planner)
    with pytest.raises(RunError, match="ROUTING_ESTIMATE_SOURCE_MISMATCH"):
        ApprovedRunRouting(
            planner, ProfileQualificationStore(planner.projects), capacity, estimates=estimates
        )


def test_numeric_official_label_does_not_upgrade_assurance_or_create_cash(v2_plan, tmp_path):
    client, headers, planner, run, _, _ = approve(v2_plan)
    capacity = CapacityStore(tmp_path / "state/capacity.sqlite")
    config = run["configuration_snapshot"]["configuration"]["resources"]
    for pool in config["quota_pools"]:
        capacity.register_pool(
            {**{k: pool[k] for k in ("id", "account_id", "kind", "unit")}, "window_kind": "fixed"},
            command_key="pool-" + pool["id"],
        )
        now = time.time()
        capacity.observe(
            {
                "pool_id": pool["id"],
                "window_id": "w1",
                "observed_at": now,
                "reset_at": now + 100,
                "source": "official",
                "source_ref": "test-labelled-only",
                "metric": "remaining",
                "amount": "12",
                "limit": "100",
                "covered_usage_ids": [],
            },
            command_key="obs-" + pool["id"],
        )
    for row in config["profiles"]:
        capacity.register_profile(
            {
                "id": row["id"],
                "revision": row["revision"],
                "account_id": row["profile"]["binding"]["account_id"],
                "pool_ids": row["quota_pool_refs"],
            },
            command_key="profile-" + row["id"],
        )
    for account in config["accounts"]:
        capacity.activate_policy(
            {
                "account_id": account["id"],
                "max_active_attempts": 4,
                "max_attempt_duration_seconds": 60,
                "observation_max_age_seconds": 60,
                "require_official_observation": True,
                "safety_margin": {},
                "lead_reserve": {},
                "lead_reserved_slots": 1,
                "conservative_mode": None,
            },
            expected_revision=0,
            command_key="policy-" + account["id"],
        )
    before = capacity.snapshot()
    result = client.post(
        f"/v1/runs/{run['id']}/tasks/feature/routing-assessments",
        json={},
        headers={**headers, "Idempotency-Key": "numeric"},
    ).json()
    snapshot = result["route"]["snapshots"]["capacity"]
    assert snapshot["estimates"] == []
    assert all(
        p["confidence"] == "unknown" and p["reported_remaining"] == "12.000000"
        for p in snapshot["pools"]
    )
    assert all(a["cash_remaining"] == {} for a in snapshot["accounts"])
    assert snapshot["budget_remaining"] == {} and snapshot["fx"] is None
    assert capacity.snapshot() == before


@pytest.mark.parametrize(
    "membership,allowed",
    [("worker", False), ("quality_only", False), ("advice_only", False), ("normal_lead", True)],
)
def test_real_run_creation_does_not_turn_other_groups_into_lead_membership(
    v2_plan, tmp_path, membership, allowed
):
    _, _, old_planner, run, _, _ = v2_plan
    registry = old_planner.projects
    configured = registry.get(run["project_id"])
    configuration = registry.get_configuration(run["project_id"])["configuration"]
    extra = deepcopy(configuration["resources"]["profiles"][0])
    extra["id"] = extra["profile"]["id"] = "extra-profile"
    for entry in extra["capability_evidence"]:
        entry["profile_digest"] = digest(extra["profile"])
    configuration["resources"]["profiles"].append(extra)
    ref = {"id": "extra-profile", "revision": 1}
    configuration["approved_profile_refs"].append(ref)
    rulebook = configuration["rulebook"]
    rulebook["revision"] += 1
    if membership == "worker":
        rulebook["profile_groups"]["fast_qualified"].append(ref)
    elif membership == "advice_only":
        rulebook["profile_groups"]["adviser_qualified"].append(ref)
    elif membership == "quality_only":
        rulebook["profile_groups"]["quality-only-planner"] = [ref]
        lead = next(
            r
            for r in rulebook["rules"]
            if r["when"]["role"] == "commander" and r["when"]["purpose"] == "lead"
        )
        lead["quality_escalation_groups"] = ["quality-only-planner"]
    else:
        rulebook["profile_groups"]["commander_qualified"].append(ref)
    preview = registry.preview_configuration(
        run["project_id"], configuration, command_key="membership-preview", principal="owner"
    )
    configured = registry.apply_configuration(
        run["project_id"],
        preview["preview_id"],
        expected_revision=configured["revision"],
        command_key="membership-apply",
        principal="owner",
    )
    assert configured["configuration"]["status"] == "offline_valid"
    policy = registry.register_execution_policy(
        run["project_id"],
        policy_request(configured),
        command_key="membership-policy",
        principal="owner",
    )
    planner = RunPlanner(tmp_path / "membership-runs.sqlite", registry)
    creation = request_v2(configured, policy)
    creation["participants"] = [{"principal": "lead", "profile": ref, "purpose": "lead"}]
    if allowed:
        created = planner.create(creation, command_key="membership-run", principal="owner")
        assert created["state"] == "planning"
        assert created["dispatch_enabled"] is False
        assert created["planning_intents"] == [] and created["approvals"] == []
        intent = planner.planning_intent(
            created["id"], term=1, command_key="membership-intent", principal="lead"
        )
        assert intent["state"] == "awaiting_receipt" and intent["dispatch_enabled"] is False
    else:
        with pytest.raises(RunError, match="PLANNER_PROFILE_NOT_APPROVED"):
            planner.create(creation, command_key="membership-run", principal="owner")
        assert planner.list(principal="owner") == []
