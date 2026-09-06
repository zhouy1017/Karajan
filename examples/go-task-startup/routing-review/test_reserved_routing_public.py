"""Public stored-Run routing checks; real stores, explicitly synthetic qualification only."""

from contextlib import contextmanager
from copy import deepcopy

import pytest
from karajan.orchestration.routing import ApprovedRunRouting
from karajan.runs import RunError
from test_approved_routing_capacity import SyntheticQualifiedSource
from test_routing_authorization import approve_request
from test_task_admission import prepared, project

__all__ = ["prepared", "project"]


def assess(prepared):
    _, routing, run = prepared
    original = routing.assess(run["id"], "implement", principal="owner", command_key="spec-assess")
    assert original["state"] == "selected", original
    return routing, run, original


def test_guard_is_only_current_authority_evidence_and_does_not_require_or_create_a_reservation(
    prepared,
):
    routing, run, original = assess(prepared)
    before = routing.capacity.snapshot()
    with routing.reserved_execution_guard(run["id"], original["id"], principal="owner") as result:
        assert result["state"] == "selected"
        assert result["route"]["selected_profile"] == original["route"]["selected_profile"]
        assert result["planned_attempt_id"] == original["planned_attempt_id"]
        assert result["planned_context_id"] == original["planned_context_id"]
        assert result["route"]["quota_revalidation_required"] is True
        assert result["activation_allowed"] is False
        assert result["dispatch_enabled"] is False
    assert routing.capacity.snapshot() == before
    assert before["reservations"] == []


def test_modifying_returned_assessment_cannot_replace_persisted_identity_or_material(prepared):
    routing, run, original = assess(prepared)
    saved = deepcopy(original)
    original["planned_attempt_id"] = "forged-attempt"
    original["planned_context_id"] = "forged-context"
    original["route"]["selected_profile"]["id"] = "forged-profile"
    original["sources"]["approval"]["plan_digest"] = "f" * 64
    reopened = ApprovedRunRouting(
        routing.planner, routing.qualifications, routing.capacity, estimates=routing.estimates
    )
    with reopened.reserved_execution_guard(run["id"], saved["id"], principal="owner") as result:
        assert result["state"] == "selected"
        assert result["planned_attempt_id"] == saved["planned_attempt_id"]
        assert result["planned_context_id"] == saved["planned_context_id"]
        assert result["route"]["selected_profile"] == saved["route"]["selected_profile"]
        assert result["sources"]["approval"] == saved["sources"]["approval"]
    assert reopened.get(run["id"], saved["id"], principal="owner") == saved


@pytest.mark.parametrize("change", ["policy", "balance"])
def test_capacity_constraints_are_left_for_capacity_guard_without_reranking_authority(
    prepared, change
):
    admissions, routing, run = prepared
    queued = admissions.enqueue(run["id"], "implement", principal="owner", command_key="spec-queue")
    operation = admissions.advance(run["id"], queued["id"], principal="owner")
    assert operation["state"] == "reserved"
    original = operation["assessment"]
    if change == "policy":
        policy = routing.capacity.snapshot()["policies"][0]["policy"]
        policy.update(max_active_attempts=1, lead_reserved_slots=1)
        routing.capacity.activate_policy(policy, expected_revision=1, command_key="no-worker-slots")
    else:
        old = routing.capacity.snapshot()["observations"][0]["observation"]
        routing.capacity.clock = lambda: 1001.0
        changed = routing.capacity.observe(
            {**old, "observed_at": 1001.0, "amount": "0", "source_ref": "spec:zero-balance"},
            command_key="zero",
        )
        assert changed["applied"] is True
    before = routing.capacity.snapshot()
    with routing.reserved_execution_guard(run["id"], original["id"], principal="owner") as result:
        assert result["state"] == "selected", result
        assert result["route"]["selected_profile"] == original["route"]["selected_profile"]
        assert result["route"]["quota_revalidation_required"] is True
        assert result["activation_allowed"] is False
    assert routing.capacity.snapshot() == before
    # Actual capacity activation remains the distinct rejecting boundary.
    assert (
        routing.capacity.activate(
            operation["capacity_receipt"]["admission_id"], command_key="actual-capacity-check"
        )["decision"]
        == "rejected"
    )


def test_current_profile_restriction_blocks_original_assessment(prepared):
    routing, run, original = assess(prepared)
    projects = routing.planner.projects
    current = projects.get(run["project_id"])
    configuration = projects.get_configuration(run["project_id"])["configuration"]
    configuration["resources"]["profiles"][0]["enabled"] = False
    preview = projects.preview_configuration(
        run["project_id"], configuration, command_key="spec-disable-preview", principal="owner"
    )
    projects.apply_configuration(
        run["project_id"],
        preview["preview_id"],
        expected_revision=current["revision"],
        command_key="spec-disable",
        principal="owner",
    )
    with routing.reserved_execution_guard(run["id"], original["id"], principal="owner") as result:
        assert result["state"] == "blocked", result
        assert result["route"]["selected_profile"] is None
        assert "CURRENT_PROFILE_RESTRICTED" in result["sources"]["profiles"][0]["reason_codes"]


class ChangingTestQualification(SyntheticQualifiedSource):
    """Labeled source-double revision, not a runtime or credential qualification."""

    revision = 1

    @contextmanager
    def routing_facts_guard(self, *args, **kwargs):
        with super().routing_facts_guard(*args, **kwargs) as view:
            for row in view["profiles"]:
                row["qualification"]["facts"]["evidence_ref"] = "spec-test-source:" + str(
                    self.revision
                )
            yield view


def test_new_qualification_record_cannot_silently_replace_original_selected_source(prepared):
    _, routing, _ = prepared
    source = ChangingTestQualification(routing.planner.projects)
    routing.qualifications = source
    routing, run, original = assess(prepared)
    source.revision = 2
    with routing.reserved_execution_guard(run["id"], original["id"], principal="owner") as result:
        assert result["state"] == "blocked", result
        assert result["reason_codes"] == ["RESERVED_EXECUTION_INPUT_CHANGED"]
        assert result["route"]["selected_profile"] is None


def submit_changed_scope(routing, run):
    current = routing.planner.get(run["id"], principal="owner")
    plan = deepcopy(current["plans"][-1]["plan"])
    plan["tasks"][0]["revision"] += 1
    plan["tasks"][0]["paths"] = ["src/replacement.py"]
    return routing.planner.submit_plan(
        run["id"],
        {
            "schema_version": "karajan.submit-plan.v2",
            "term": current["commander"]["term"],
            "intent_id": current["planning_intents"][0]["id"],
            "expected_plan_revision": current["latest_plan_revision"],
            "plan": plan,
        },
        command_key="spec-changed-plan",
        principal="lead",
    )


def test_pending_changed_task_does_not_override_the_active_approved_scope(prepared):
    routing, run, original = assess(prepared)
    submit_changed_scope(routing, run)
    with routing.reserved_execution_guard(run["id"], original["id"], principal="owner") as result:
        assert result["state"] == "selected", result
        assert result["sources"]["approval"] == original["sources"]["approval"]
        assert result["route"]["snapshots"]["task"] == original["route"]["snapshots"]["task"]


def test_approving_changed_task_blocks_old_assessment_even_if_model_identity_is_unchanged(prepared):
    routing, run, original = assess(prepared)
    plan = submit_changed_scope(routing, run)
    routing.planner.approve_plan(
        run["id"], approve_request(plan), command_key="spec-approve-change", principal="owner"
    )
    with routing.reserved_execution_guard(run["id"], original["id"], principal="owner") as result:
        assert result["state"] == "blocked", result
        assert result["route"]["selected_profile"] is None
    assert routing.get(run["id"], original["id"], principal="owner") == original


def test_original_estimate_expiration_is_not_hidden_by_retained_assessment(prepared):
    routing, run, original = assess(prepared)
    routing.estimates.clock = lambda: 1061.0
    routing.capacity.clock = lambda: 1061.0
    with routing.reserved_execution_guard(run["id"], original["id"], principal="owner") as result:
        assert result["state"] == "blocked", result
        assert result["route"]["selected_profile"] is None
        assert result["sources"]["estimates"][0]["reason_codes"] == ["RESOURCE_ESTIMATE_EXPIRED"]


def test_stored_blocked_assessment_is_not_promoted_to_a_fresh_selection(prepared):
    _, routing, run = prepared
    original = routing.assess(run["id"], "review", principal="owner", command_key="spec-review")
    assert original["state"] == "blocked"
    with pytest.raises(RunError, match="RESERVED_ROUTE_REQUIRED"):
        with routing.reserved_execution_guard(run["id"], original["id"], principal="owner"):
            pytest.fail("Blocked historical assessment entered as a reserved selection")
