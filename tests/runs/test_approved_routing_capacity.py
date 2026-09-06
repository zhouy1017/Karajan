"""Real approved Run/estimate/quota stores; qualification alone is a labeled double."""

from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path

from karajan.capacity import CapacityStore
from karajan.orchestration.routing import ApprovedRunRouting
from karajan.projects.demand import AttemptEstimateStore
from karajan.projects.qualification import ProfileQualificationStore
from karajan.routing.compiler import digest
from test_routing_authorization import admitted_v2, approve_request, project, submit_request

__all__ = ["project"]


class SyntheticQualifiedSource(ProfileQualificationStore):
    """Test-only source; never installed in app wiring or real admission."""

    destination = "local-fixture"

    @contextmanager
    def routing_facts_guard(self, project_id, frozen_registrations, **kwargs):
        with super().routing_facts_guard(project_id, frozen_registrations, **kwargs) as view:
            for row, registration in zip(view["profiles"], frozen_registrations, strict=True):
                profile = registration["profile"]
                row["reason_codes"] = []
                row["qualification"] = {
                    "capability_evidence": deepcopy(registration["capability_evidence"]),
                    "qualification_scope": "test_double",
                    "dispatch_eligible": False,
                    "facts": {
                        "profile": row["profile"],
                        "profile_digest": digest(profile),
                        "runtime_version": profile["binding"]["runtime_version"],
                        "roles": ["worker"],
                        "tools": ["fixture-tools"],
                        "context_tokens": 8192,
                        "data_destination": self.destination,
                        "budget_enforcement": "unknown",
                        "provenance": "fixture",
                        "evidence_ref": "test-only-source",
                        "observed_at": 1000.0,
                        "valid_until": 2000.0,
                    },
                }
            yield view


def capacity_for_plan(tmp_path: Path, configuration: dict) -> CapacityStore:
    capacity = CapacityStore(tmp_path / "capacity.sqlite", clock=lambda: 1000.0)
    for pool in configuration["resources"]["quota_pools"]:
        capacity.register_pool(
            {**{k: pool[k] for k in ("id", "account_id", "kind", "unit")}, "window_kind": "fixed"},
            command_key="pool-" + pool["id"],
        )
        capacity.observe(
            {
                "pool_id": pool["id"],
                "window_id": "window-one",
                "observed_at": 1000.0,
                "reset_at": 2000.0,
                "source": "fixture",
                "source_ref": "test-only-observer",
                "metric": "remaining",
                "amount": "80",
                "limit": "100",
                "covered_usage_ids": [],
            },
            command_key="observe-" + pool["id"],
        )
    for row in configuration["resources"]["profiles"]:
        capacity.register_profile(
            {
                "id": row["id"],
                "revision": row["revision"],
                "account_id": row["profile"]["binding"]["account_id"],
                "pool_ids": row["quota_pool_refs"],
            },
            command_key="profile-" + row["id"],
        )
    for account in configuration["resources"]["accounts"]:
        capacity.activate_policy(
            {
                "account_id": account["id"],
                "max_active_attempts": 4,
                "max_attempt_duration_seconds": 60,
                "observation_max_age_seconds": 60,
                "require_official_observation": False,
                "safety_margin": {},
                "lead_reserve": {},
                "lead_reserved_slots": 1,
                "conservative_mode": {
                    "enabled": True,
                    "max_local_active_attempts": 4,
                    "max_attempt_duration_seconds": 60,
                    "observation_max_age_seconds": 60,
                    "cooldown_seconds": 10,
                },
            },
            expected_revision=0,
            command_key="policy-" + account["id"],
        )
    return capacity


def test_real_approved_run_estimate_capacity_and_revocation_are_consumed(
    tmp_path: Path, project: tuple
) -> None:
    planner, run, intent = admitted_v2(tmp_path, project)
    plan = planner.submit_plan(
        run["id"], submit_request(run, intent), command_key="plan", principal="lead"
    )
    planner.approve_plan(run["id"], approve_request(plan), command_key="approve", principal="owner")
    configuration = run["configuration_snapshot"]["configuration"]
    capacity = capacity_for_plan(tmp_path, configuration)
    estimates = AttemptEstimateStore(planner, clock=lambda: 1000.0)
    ref = {"id": "fixture-profile", "revision": 1}
    demand = [
        {"pool_id": p["id"], "unit": p["unit"], "window_kind": "fixed", "amount": "3"}
        for p in configuration["resources"]["quota_pools"]
    ]
    record = estimates.register(
        run["id"],
        "implement",
        ref,
        {
            "id": "explicit-prediction",
            "revision": 1,
            "source_kind": "owner_conservative_estimate",
            "validity_seconds": 60,
            "measurement_semantics": "window_independent_attempt",
            "demand": demand,
            "completion_seconds": None,
            "basis": "Synthetic test prediction: three units for this exact task.",
        },
        principal="owner",
        command_key="prediction",
    )
    service = ApprovedRunRouting(
        planner, SyntheticQualifiedSource(planner.projects), capacity, estimates=estimates
    )
    result = service.assess(run["id"], "implement", principal="owner", command_key="route")
    assert result["state"] == "selected", result["route"]["candidates"]
    assert result["route"]["selected_profile"] == ref
    assert result["sources"]["estimates"][0]["source_binding"]["digest"] == record["digest"]
    snapshot = result["route"]["snapshots"]["capacity"]
    assert snapshot["estimates"][0]["confidence"] == "unknown"
    assert all(p["confidence"] == "unknown" for p in snapshot["pools"])
    assert snapshot["accounts"][0]["cash_remaining"] == {}
    assert snapshot["estimates"][0]["completion_seconds"] is None
    assert result["admission_expectations"][0]["expected_capacity"] == {
        "policy_revision": 1,
        "pool_windows": {p["pool_id"]: "window-one" for p in demand},
        "lead_reserve_access": False,
    }
    assert capacity.snapshot()["reservations"] == []
    assert result["activation_allowed"] is False
    estimates.revoke(
        run["project_id"], "explicit-prediction", 1, principal="owner", reason="test-revoked"
    )
    later = service.assess(run["id"], "implement", principal="owner", command_key="after-revoke")
    assert later["state"] == "blocked"
    assert "RESOURCE_ESTIMATE_MISSING" in later["route"]["candidates"][0]["reason_codes"]
    assert later["sources"]["estimates"][0]["reason_codes"] == ["RESOURCE_ESTIMATE_REVOKED"]
    assert service.get(run["id"], result["id"], principal="owner") == result


def test_channel_destination_must_equal_its_fixed_mapping_even_when_both_destinations_are_allowed(
    tmp_path: Path, project: tuple
) -> None:
    # A destination allowed for one channel cannot replace this channel's fixed destination.
    from karajan.runs import RunPlanner
    from test_planning import ScriptedAdmissionReader
    from test_routing_authorization import policy_request, request_v2

    registry, configured, _ = project
    request = policy_request(configured)
    request["constraints"]["data_destinations"].append("second-approved-destination")
    fixed = registry.register_execution_policy(
        configured["id"], request, command_key="policy", principal="owner"
    )
    authority = ScriptedAdmissionReader()
    planner = RunPlanner(tmp_path / "runs.sqlite", registry, admissions=authority)
    creation = request_v2(configured, fixed)
    creation["authorization"]["data_destinations"].append("second-approved-destination")
    run = planner.create(creation, command_key="run", principal="owner")
    intent = planner.planning_intent(run["id"], term=1, command_key="intent", principal="lead")
    planner.attach_planning_receipt(
        run["id"],
        intent["id"],
        receipt_ref=authority.grant(intent),
        command_key="receipt",
        principal="owner",
    )
    plan = planner.submit_plan(
        run["id"], submit_request(run, intent), command_key="plan", principal="lead"
    )
    planner.approve_plan(run["id"], approve_request(plan), command_key="approve", principal="owner")
    source = SyntheticQualifiedSource(registry)
    source.destination = "second-approved-destination"
    service = ApprovedRunRouting(
        planner, source, CapacityStore(tmp_path / "capacity.sqlite", clock=lambda: 1000.0)
    )
    result = service.assess(run["id"], "implement", principal="owner", command_key="assessment")
    assert (
        "PROFILE_DESTINATION_BINDING_MISMATCH" in result["sources"]["profiles"][0]["reason_codes"]
    )
    assert result["route"]["snapshots"]["policy"]["resources"]["profiles"][0]["enabled"] is False
