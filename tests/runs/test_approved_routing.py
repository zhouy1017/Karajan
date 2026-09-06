"""Trusted routing consumes persisted approval, without caller-supplied snapshots."""

from copy import deepcopy
from pathlib import Path

import pytest
from karajan.capacity import CapacityStore
from karajan.orchestration.routing import ApprovedRunRouting
from karajan.projects.qualification import ProfileQualificationStore
from karajan.routing import compile_rulebook
from karajan.runs import RunError
from test_routing_authorization import (
    admitted_v2,
    approve_request,
    project,
    submit_request,
)

__all__ = ["project"]


@pytest.mark.parametrize("group", ["commander_qualified", "adviser_qualified"])
def test_v2_accepts_legal_custom_names_for_commander_groups(
    tmp_path: Path, project: tuple, group: str
) -> None:
    registry, configured, repository = project
    configuration = deepcopy(registry.get_configuration(configured["id"])["configuration"])
    rulebook = configuration["rulebook"]
    rulebook["revision"] = 2
    replacement = "my-" + group
    rulebook["profile_groups"][replacement] = rulebook["profile_groups"].pop(group)
    for rule in rulebook["rules"]:
        for field in ("eligible_groups", "quality_escalation_groups"):
            rule[field] = [replacement if item == group else item for item in rule.get(field, [])]
    assert compile_rulebook(rulebook)["issues"] == []
    preview = registry.preview_configuration(
        configured["id"], configuration, command_key="custom", principal="owner"
    )
    assert preview["status"] == "offline_valid"
    configured = registry.apply_configuration(
        configured["id"],
        preview["preview_id"],
        expected_revision=configured["revision"],
        command_key="apply-custom",
        principal="owner",
    )
    planner, run, intent = admitted_v2(tmp_path, (registry, configured, repository))
    plan = planner.submit_plan(
        run["id"], submit_request(run, intent), command_key="custom-plan", principal="lead"
    )
    assert plan["plan_revision"] == 1


def test_approved_requirements_reach_router_and_unknown_qualification_is_preserved(
    tmp_path: Path, project: tuple
) -> None:
    planner, run, intent = admitted_v2(tmp_path, project)
    plan = planner.submit_plan(
        run["id"], submit_request(run, intent), command_key="plan", principal="lead"
    )
    planner.approve_plan(run["id"], approve_request(plan), command_key="approve", principal="owner")
    service = ApprovedRunRouting(
        planner,
        ProfileQualificationStore(planner.projects),
        CapacityStore(tmp_path / "capacity.sqlite"),
    )
    receipt = service.assess(run["id"], "implement", principal="owner", command_key="assess")
    assert receipt["state"] == "blocked"
    assert receipt["activation_allowed"] is False
    assert receipt["route"]["rule_id"] == "bounded-worker"
    task = receipt["route"]["snapshots"]["task"]
    assert task["plan_revision"] == 1
    assert task["context_tokens"] == 4096
    assert task["reserved_output_tokens"] == 1024
    assert task["duration_seconds"] == 20
    assert task["authorization_digest"] == plan["authorization_digest"]
    assert receipt["sources"]["profiles"][0]["reason_codes"] == ["RUNTIME_TOOLS_NOT_QUALIFIED"]
    assert receipt["route"]["snapshots"]["policy"]["profile_facts"] == []
    assert receipt["route"]["snapshots"]["capacity"]["budget_remaining"] == {}
    assert service.get(run["id"], receipt["id"], principal="owner") == receipt
    assert (
        service.assess(run["id"], "implement", principal="owner", command_key="assess") == receipt
    )


def test_another_rules_grants_never_authorize_this_rule(tmp_path: Path, project: tuple) -> None:
    planner, run, intent = admitted_v2(tmp_path, project)
    request = submit_request(run, intent)
    request["plan"]["authorization"]["stage_permissions"]["bounded-worker"] = {
        "normal": False,
        "quality_indices": [],
    }
    plan = planner.submit_plan(run["id"], request, command_key="plan", principal="lead")
    planner.approve_plan(run["id"], approve_request(plan), command_key="approve", principal="owner")
    service = ApprovedRunRouting(
        planner,
        ProfileQualificationStore(planner.projects),
        CapacityStore(tmp_path / "capacity.sqlite"),
    )
    receipt = service.assess(run["id"], "implement", principal="owner", command_key="assessment")
    assert receipt["reason_codes"] == ["STAGE_NOT_AUTHORIZED"]
    auth = receipt["route"]["snapshots"]["task"]["authorization"]
    assert auth["allowed_stages"] == []
    assert auth["approved_groups"] == {}
    assert auth["approved_quality_stage_indices"] == []


def test_pending_proposal_does_not_replace_the_active_approved_plan(
    tmp_path: Path, project: tuple
) -> None:
    planner, run, intent = admitted_v2(tmp_path, project)
    request = submit_request(run, intent)
    first = planner.submit_plan(run["id"], request, command_key="first", principal="lead")
    planner.approve_plan(
        run["id"], approve_request(first), command_key="approve", principal="owner"
    )
    request["expected_plan_revision"] = 1
    request["plan"]["authorization"]["stage_permissions"]["bounded-worker"]["quality_indices"] = []
    planner.submit_plan(run["id"], request, command_key="pending", principal="lead")
    service = ApprovedRunRouting(
        planner,
        ProfileQualificationStore(planner.projects),
        CapacityStore(tmp_path / "capacity.sqlite"),
    )
    receipt = service.assess(run["id"], "implement", principal="owner", command_key="assessment")
    task = receipt["route"]["snapshots"]["task"]
    assert task["plan_revision"] == 1
    assert task["authorization"]["approved_quality_stage_indices"] == [0]
    assert receipt["sources"]["routing_digest"] == first["routing_digest"]


def test_unapproved_owner_and_dependency_boundaries_do_not_manufacture_author_evidence(
    tmp_path: Path, project: tuple
) -> None:
    planner, run, intent = admitted_v2(tmp_path, project)
    service = ApprovedRunRouting(
        planner,
        ProfileQualificationStore(planner.projects),
        CapacityStore(tmp_path / "capacity.sqlite"),
    )
    assert service.assess(run["id"], "implement", principal="owner", command_key="before")[
        "reason_codes"
    ] == ["APPROVED_PLAN_REQUIRED"]
    with pytest.raises(RunError):
        service.assess(run["id"], "implement", principal="lead", command_key="rogue")
    plan = planner.submit_plan(
        run["id"], submit_request(run, intent), command_key="plan", principal="lead"
    )
    planner.approve_plan(run["id"], approve_request(plan), command_key="approve", principal="owner")
    receipt = service.assess(run["id"], "review", principal="owner", command_key="review")
    assert receipt["reason_codes"] == ["EXECUTION_LINEAGE_REQUIRED"]
    assert receipt["route"] is None
    with pytest.raises(RunError, match="IDEMPOTENCY_CONFLICT"):
        service.assess(run["id"], "implement", principal="owner", command_key="review")
