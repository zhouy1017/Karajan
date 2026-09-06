"""Independent public assessment checks; all state remains in temporary fixtures."""

import copy
import importlib.util
from pathlib import Path

import pytest
from karajan.runs import RunError

WORKTREE = next(
    parent for parent in Path(__file__).resolve().parents if (parent / "pyproject.toml").is_file()
)
spec = importlib.util.spec_from_file_location(
    "approved_routing_spec_fixture", WORKTREE / "examples/approved-routing/routing/spec/fixture.py"
)
assert spec is not None and spec.loader is not None
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)
seeded, assess, estimate, observe, approval = (
    fixtures.seeded,
    fixtures.assess,
    fixtures.estimate,
    fixtures.observe,
    fixtures.approval,
)
REF = fixtures.REF


def test_only_selected_rule_grants_apply_even_when_another_rule_approves_same_group(tmp_path):
    case = seeded(tmp_path, normal=False, custom_rule=True)
    result = assess(case)
    task = result["route"]["snapshots"]["task"]
    assert result["route"]["rule_id"] == "bounded-worker"
    assert task["authorization"]["allowed_stages"] == ["quality"]
    assert "fast_qualified" not in task["authorization"]["approved_groups"]
    assert result["reason_codes"] == ["STAGE_NOT_AUTHORIZED"]
    assert result["state"] == "blocked" and result["dispatch_enabled"] is False
    assert case["capacity"].snapshot()["reservations"] == []


def test_valid_renamed_commander_group_can_create_approved_run_without_legacy_group_name(tmp_path):
    case = seeded(tmp_path, renamed_commander=True)
    result = assess(case)
    assert result["route"]["rule_id"] == "bounded-worker"
    assert "owner_lead_pool" in result["route"]["snapshots"]["policy"]["rulebook"]["profile_groups"]


def test_real_cross_run_usage_and_reservation_are_separate_and_assessment_has_no_admission(
    tmp_path,
):
    case = seeded(tmp_path)
    record = estimate(case)
    capacity = case["capacity"]
    other = capacity.admit(
        {
            "attempt_id": "other-attempt",
            "run_id": "other-run",
            "profile_id": REF["id"],
            "profile_revision": 1,
            "role": "worker",
            "purpose": None,
            "authorization_ref": "synthetic-other",
            "rulebook_revision": "other-rules",
            "duration_seconds": 30,
            "demand": {"service-fixture": "10"},
        },
        command_key="other-admit",
    )
    capacity.activate(other["admission_id"], command_key="other-start")
    capacity.record_usage(
        {
            "id": "other-call",
            "admission_id": other["admission_id"],
            "amounts": {"service-fixture": "3"},
            "window_ids": {"service-fixture": "fixed-current"},
            "evidence_ref": "synthetic-usage",
            "attribution_ref": "synthetic-window-attribution",
        },
        command_key="other-consume",
    )
    before = capacity.snapshot()
    result = assess(case)
    current = result["route"]["snapshots"]["capacity"]
    pool = current["pools"][0]
    assert (pool["reported_remaining"], pool["local_uncovered"], pool["future_reserved"]) == (
        "40.000000",
        "3.000000",
        "7.000000",
    )
    assert current["accounts"][0]["active_attempts"] == 1
    assert current["budget_remaining"] == {} and current["accounts"][0]["cash_remaining"] == {}
    assert current["estimates"][0]["confidence"] == "unknown"
    assert current["estimates"][0]["completion_seconds"] is None
    assert current["estimates"][0]["demand"] == [
        {
            "pool_id": "service-fixture",
            "unit": "percent",
            "window_id": "fixed-current",
            "amount": "7.25",
        }
    ]
    assert result["sources"]["estimates"][0]["source_binding"]["digest"] == record["digest"]
    assert result["admission_expectations"][0]["expected_capacity"] == {
        "policy_revision": 1,
        "pool_windows": {"service-fixture": "fixed-current"},
        "lead_reserve_access": False,
    }
    assert result["sources"]["profiles"][0]["reason_codes"] == ["RUNTIME_TOOLS_NOT_QUALIFIED"]
    assert result["state"] == "blocked" and result["activation_allowed"] is False
    assert capacity.snapshot() == before
    assert case["planner"].get(case["run"]["id"], principal="owner")["dispatch_enabled"] is False


def test_old_receipt_replay_is_historical_and_new_command_reads_latest_unknown_observation(
    tmp_path,
):
    case = seeded(tmp_path)
    first = assess(case)
    case["now"][0] += 1
    observe(case, metric="unknown", amount=None)
    assert assess(case) == first
    latest = assess(case, "new-assessment")
    assert latest["id"] != first["id"]
    assert latest["planned_attempt_id"] != first["planned_attempt_id"]
    assert latest["sources"]["capacity_facts_sha256"] != first["sources"]["capacity_facts_sha256"]
    assert first["route"]["snapshots"]["capacity"]["pools"][0]["reported_remaining"] == "40.000000"
    assert latest["route"]["snapshots"]["capacity"]["pools"][0]["reported_remaining"] is None
    assert case["service"].get(case["run"]["id"], first["id"], principal="owner") == first
    assert case["capacity"].snapshot()["reservations"] == []


def test_pending_new_plan_does_not_replace_active_approved_requirements(tmp_path):
    case = seeded(tmp_path)
    pending = copy.deepcopy(case["proposal"])
    pending["expected_plan_revision"] = 1
    pending["plan"]["tasks"][0].update(revision=2, context_tokens=2048, duration_seconds=19)
    case["planner"].submit_plan(
        case["run"]["id"], pending, principal="lead", command_key="pending-plan"
    )
    result = assess(case)
    task = result["route"]["snapshots"]["task"]
    assert task["plan_revision"] == 1
    assert task["context_tokens"] == 3072 and task["reserved_output_tokens"] == 1024
    assert task["duration_seconds"] == 21
    assert task["authorization_digest"] == case["plan"]["authorization_digest"]


def test_unapproved_task_and_unimplemented_lineage_never_get_assessed_as_workers(tmp_path):
    case = seeded(tmp_path, approve=False)
    assert assess(case)["reason_codes"] == ["APPROVED_PLAN_REQUIRED"]
    case["planner"].approve_plan(
        case["run"]["id"], approval(case["plan"]), principal="owner", command_key="approve-now"
    )
    assert assess(case, "missing", "missing")["reason_codes"] == ["TASK_SCOPE_NOT_APPROVED"]
    assert assess(case, "review", "review")["reason_codes"] == ["EXECUTION_LINEAGE_REQUIRED"]
    with pytest.raises(RunError, match="^IDEMPOTENCY_CONFLICT$"):
        assess(case, "missing", "implement")
    with pytest.raises(RunError):
        case["service"].assess(
            case["run"]["id"], "implement", principal="intruder", command_key="no-owner"
        )


def test_current_identity_restriction_preserves_frozen_plan_but_rejects_profile(tmp_path):
    case = seeded(tmp_path)
    estimate(case)
    first = assess(case)
    changed = copy.deepcopy(case["config"])
    changed["resources"]["accounts"][0]["provider_id"] = "changed-provider"
    preview = case["registry"].preview_configuration(
        case["project"], changed, command_key="changed-preview", principal="owner"
    )
    case["registry"].apply_configuration(
        case["project"],
        preview["preview_id"],
        expected_revision=case["registry"].get(case["project"])["revision"],
        command_key="changed-apply",
        principal="owner",
    )
    result = assess(case, "after-change")
    assert "CURRENT_PROFILE_RESTRICTED" in result["sources"]["profiles"][0]["reason_codes"]
    assert result["route"]["snapshots"]["policy"]["resources"]["profiles"][0]["enabled"] is False
    assert result["route"]["snapshots"]["capacity"]["estimates"] == []
    assert result["sources"]["estimates"][0]["reason_codes"] == ["PROFILE_IDENTITY_MISMATCH"]
    assert assess(case) == first


@pytest.mark.parametrize("condition", ["missing", "revoked", "expired"])
def test_estimate_absence_revocation_and_expiry_never_fill_a_default_vector(tmp_path, condition):
    case = seeded(tmp_path)
    if condition != "missing":
        record = estimate(case)
        if condition == "revoked":
            case["estimates"].revoke(
                case["project"],
                record["id"],
                record["revision"],
                principal="owner",
                reason="spec-stop",
            )
        else:
            case["now"][0] += 90
    result = assess(case)
    assert result["route"]["snapshots"]["capacity"]["estimates"] == []
    assert result["sources"]["estimates"][0]["reason_codes"] == [
        {
            "missing": "RESOURCE_ESTIMATE_MISSING",
            "revoked": "RESOURCE_ESTIMATE_REVOKED",
            "expired": "RESOURCE_ESTIMATE_EXPIRED",
        }[condition]
    ]
    assert result["admission_expectations"] == []
    assert case["capacity"].snapshot()["reservations"] == []
