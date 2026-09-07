"""C evidence for durable planning admission; no provider is contacted."""

import json
import time
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from karajan.capacity import CapacityStore
from karajan.orchestration.planning_admission import (
    COMMANDER_QUALIFICATION_SCOPE,
    PLANNING_ADMISSION_BOOTSTRAP,
    PlanningAdmissionAuthority,
)
from karajan.orchestration.planning_execution import PlanningExecution
from karajan.runs import RunError, RunPlanner
from karajan.runs.planning import digest
from test_planning import create_request
from test_routing_authorization import policy_request, request_v2

pytest_plugins = ["test_planning"]


@pytest.fixture
def configured(project: tuple[Any, dict, Path]) -> dict:
    registry, value, _ = project
    return {**deepcopy(value), "registry": registry}


class FixtureCommander:
    def __init__(self, profile_facts: dict[str, Any], available: bool = True) -> None:
        self.available = available
        self.profile_facts = profile_facts

    def read_commander(
        self, binding: dict[str, Any], *, scope: str, reader_version: str
    ) -> dict[str, Any] | None:
        if not self.available:
            return None
        return {
            "schema_version": "karajan.commander-qualification.v1",
            "scope": scope,
            "reader_version": reader_version,
            "binding_sha256": digest(binding),
            "record_sha256": "a" * 64,
            "source_generation_sha256": "b" * 64,
            "valid_until": 2_000_000_000.0,
            "capabilities": ["design_reasoning", "structured_plan_output"],
            "provenance": "fixture",
            "profile_facts": self.profile_facts,
            "capability_evidence": [
                {
                    "capability": capability,
                    "status": "passed",
                    "profile_digest": self.profile_facts["profile_digest"],
                    "runtime_version": self.profile_facts["runtime_version"],
                    "evidence_ref": "fixture:commander:" + capability,
                    "provenance": "fixture",
                }
                for capability in (
                    "design_reasoning",
                    "structured_plan_output",
                    "controlled_tools",
                )
            ],
        }


def planning_capacity(directory: Path) -> CapacityStore:
    """Real SQLite Capacity facts matching the frozen fixture configuration."""
    directory.mkdir()
    store = CapacityStore(directory / "capacity.sqlite", clock=lambda: 1000.0)
    store.register_pool(
        {
            "id": "service-fixture",
            "account_id": "fixture-account",
            "kind": "service",
            "unit": "percent",
            "window_kind": "fixed",
        },
        command_key="pool",
    )
    store.observe(
        {
            "pool_id": "service-fixture",
            "window_id": "fixture-window",
            "observed_at": 1000.0,
            "reset_at": 2000.0,
            "source": "fixture",
            "source_ref": "fixture-observer",
            "metric": "remaining",
            "amount": "10",
            "limit": "10",
            "covered_usage_ids": [],
        },
        command_key="observe",
    )
    store.activate_policy(
        {
            "account_id": "fixture-account",
            "max_active_attempts": 4,
            "max_attempt_duration_seconds": 60,
            "observation_max_age_seconds": 30,
            "require_official_observation": False,
            "safety_margin": {},
            "lead_reserve": {},
            "lead_reserved_slots": 0,
            "conservative_mode": {
                "enabled": True,
                "max_local_active_attempts": 4,
                "max_attempt_duration_seconds": 60,
                "observation_max_age_seconds": 30,
                "cooldown_seconds": 10,
            },
        },
        expected_revision=0,
        command_key="policy",
    )
    return store


def _case(
    tmp_path: Path,
    configured: dict,
    *,
    available: bool = True,
    clock: Callable[[], float] | None = None,
) -> tuple[PlanningExecution, PlanningAdmissionAuthority, dict, Any]:
    planner = RunPlanner(tmp_path / "runs.sqlite", configured["registry"], clock=clock or time.time)
    fixed = configured["registry"].register_execution_policy(
        configured["id"], policy_request(configured), command_key="policy", principal="owner"
    )
    run = planner.create(request_v2(configured, fixed), command_key="run", principal="owner")
    assert run["plans"] == []
    intent = planner.planning_intent(run["id"], term=1, command_key="intent-1", principal="lead")
    execution = PlanningExecution(tmp_path / "planning.sqlite", planner).begin(
        run["id"], intent["id"], principal="owner", command_key="begin-1"
    )
    capacity = planning_capacity(tmp_path / "capacity")
    binding = execution["binding"]
    capacity.register_profile(
        {
            "id": binding["profile"]["id"],
            "revision": binding["profile"]["revision"],
            "account_id": "fixture-account",
            "pool_ids": ["service-fixture"],
        },
        command_key="profile",
    )
    authority = PlanningAdmissionAuthority(
        tmp_path / "planning-admissions.sqlite",
        tmp_path / "planning.sqlite",
        planner,
        capacity,
        FixtureCommander(
            {
                "profile": binding["profile"],
                "profile_digest": digest(
                    run["configuration_snapshot"]["configuration"]["resources"]["profiles"][0]["profile"]
                ),
                "runtime_version": "1",
                "roles": ["commander"],
                "tools": ["fixture-tools"],
                "context_tokens": 8192,
                "data_destination": "local-fixture",
                "budget_enforcement": "bounded_calls",
                "provenance": "fixture",
                "evidence_ref": "fixture:commander",
                "observed_at": 0.0,
                "valid_until": 2_000_000_000.0,
            },
            available,
        ),
        authority_kind="fixture",
    )
    authority.register_estimate(
        run["id"],
        binding["budget_ref"],
        binding["profile"],
        demand={"service-fixture": "5"},
        expected_capacity={
            "policy_revision": 1,
            "pool_windows": {
                "service-fixture": "fixture-window",
            },
            "lead_reserve_access": True,
        },
        duration_seconds=25,
        max_requests=5,
        max_duration_seconds=100,
        principal="owner",
        command_key="estimate",
    )
    return PlanningExecution(tmp_path / "planning.sqlite", planner), authority, run, execution


def test_two_intents_share_the_original_frozen_planning_budget(
    configured: dict, tmp_path: Path
) -> None:
    service, authority, run, first = _case(tmp_path, configured)
    first_result = authority.advance(first["id"], "owner", "advance-1")
    assert first_result["phase"] == "admitted", first_result["reason_codes"]
    assert first_result["route_sources"]["route"]["rule_id"] == "lead-planning"
    assert (
        first_result["route_sources"]["reserved"]["selected_profile"]
        == first["binding"]["profile"]
    )
    task = first_result["route_sources"]["route"]["snapshots"]["task"]
    assert (task["duration_seconds"], task["context_tokens"], task["reserved_output_tokens"]) == (
        25,
        7168,
        1024,
    )
    assert run["plans"] == []

    intent = service.planner.planning_intent(
        run["id"], term=1, command_key="intent-2", principal="lead"
    )
    second = service.begin(run["id"], intent["id"], principal="owner", command_key="begin-2")
    denied = authority.advance(second["id"], "owner", "advance-2")

    assert denied["phase"] == "denied"
    assert denied["reason_codes"] == ["PLANNING_BUDGET_EXHAUSTED"]
    assert len(authority.capacity.snapshot()["reservations"]) == 1


def test_missing_commander_fact_is_a_production_zero_reservation_denial(
    configured: dict, tmp_path: Path
) -> None:
    _, authority, _, execution = _case(tmp_path, configured)
    authority.authority_kind = "production"
    denied = authority.advance(execution["id"], "owner", "advance")

    assert denied["phase"] == "denied"
    assert denied["reason_codes"] == ["COMMANDER_QUALIFICATION_REQUIRED"]
    assert authority.capacity.snapshot()["reservations"] == []
    assert COMMANDER_QUALIFICATION_SCOPE == "commander_planning.v1"


@pytest.mark.parametrize("field,value", [("valid_until", 0.0), ("binding_sha256", "0" * 64)])
def test_stale_or_misbinding_commander_fact_cannot_reserve_capacity(
    configured: dict, tmp_path: Path, field: str, value: object
) -> None:
    _, authority, _, execution = _case(tmp_path, configured)
    original = authority.qualifications.read_commander

    def invalid(
        binding: dict[str, Any], *, scope: str, reader_version: str
    ) -> dict[str, Any] | None:
        result = original(binding, scope=scope, reader_version=reader_version)
        assert result is not None
        result[field] = value
        return result

    authority.qualifications.read_commander = invalid  # type: ignore[method-assign]
    denied = authority.advance(execution["id"], "owner", "advance")
    assert denied["reason_codes"] == ["COMMANDER_QUALIFICATION_REQUIRED"]
    assert authority.capacity.snapshot()["reservations"] == []


def test_effect_guard_reuses_only_the_original_binding(configured: dict, tmp_path: Path) -> None:
    _, authority, _, execution = _case(tmp_path, configured)
    authority.advance(execution["id"], "owner", "advance")

    with authority.effect_guard(execution["id"], "owner", "start") as guard:
        assert guard["attempt_id"] == execution["binding"]["attempt_id"]
        assert guard["capacity"]["request"]["run_id"] == execution["run_id"]
        assert guard["source_generation_sha256"] == "b" * 64


def test_effect_guard_rechecks_the_original_run_budget_deadline(
    configured: dict, tmp_path: Path
) -> None:
    now = [1000.0]
    _, authority, _, execution = _case(tmp_path, configured, clock=lambda: now[0])
    assert authority.advance(execution["id"], "owner", "advance")["phase"] == "admitted"
    now[0] = 1301.0
    with pytest.raises(RunError, match="PLANNING_BUDGET_EXPIRED"):
        with authority.effect_guard(execution["id"], "owner", "start"):
            pass


def test_lost_activation_reply_reopens_the_original_capacity_command(
    configured: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, authority, run, execution = _case(tmp_path, configured)
    original = authority.capacity.activate

    def lose_reply(*args: Any, **kwargs: Any) -> dict[str, Any]:
        original(*args, **kwargs)
        raise RuntimeError("activation reply lost")

    monkeypatch.setattr(authority.capacity, "activate", lose_reply)
    with pytest.raises(RuntimeError, match="reply lost"):
        authority.advance(execution["id"], "owner", "advance")

    # The external reply was lost after Capacity committed. The caller key was
    # bound before that mutation, so it cannot be recycled for another Run
    # execution while the original receipt remains unresolved.
    intent = service.planner.planning_intent(
        run["id"], term=1, command_key="intent-2", principal="lead"
    )
    second = service.begin(run["id"], intent["id"], principal="owner", command_key="begin-2")
    with pytest.raises(RunError, match="IDEMPOTENCY_CONFLICT"):
        authority.advance(second["id"], "owner", "advance")

    reopened = PlanningAdmissionAuthority(
        authority.database,
        authority.execution_database,
        authority.planner,
        authority.capacity,
        authority.qualifications,
        authority_kind="fixture",
    )
    recovered = reopened.advance(execution["id"], "owner", "recover")
    assert recovered["phase"] == "admitted"
    assert len(authority.capacity.snapshot()["reservations"]) == 1


def test_rejected_command_key_cannot_be_reused_for_another_execution(
    configured: dict, tmp_path: Path
) -> None:
    service, authority, run, first = _case(tmp_path, configured, available=False)
    assert authority.advance(first["id"], "owner", "same")["phase"] == "denied"
    authority.qualifications.available = True
    intent = service.planner.planning_intent(
        run["id"], term=1, command_key="intent-2", principal="lead"
    )
    second = service.begin(run["id"], intent["id"], principal="owner", command_key="begin-2")
    with pytest.raises(RunError, match="IDEMPOTENCY_CONFLICT"):
        authority.advance(second["id"], "owner", "same")
    assert authority.capacity.snapshot()["reservations"] == []


def test_cancelled_execution_cannot_enter_effect_guard(configured: dict, tmp_path: Path) -> None:
    service, authority, _, execution = _case(tmp_path, configured)
    authority.advance(execution["id"], "owner", "advance")
    service.cancel(execution["id"], principal="owner", command_key="cancel")
    with pytest.raises(RunError, match="PLANNING_EXECUTION_CANCELLED"):
        with authority.effect_guard(execution["id"], "owner", "start"):
            pass


def test_legacy_run_without_frozen_policy_is_a_zero_reservation_denial(
    configured: dict, tmp_path: Path
) -> None:
    planner = RunPlanner(tmp_path / "runs.sqlite", configured["registry"])
    run = planner.create(create_request(configured), command_key="run", principal="owner")
    intent = planner.planning_intent(run["id"], term=1, command_key="intent", principal="lead")
    execution = PlanningExecution(tmp_path / "planning.sqlite", planner).begin(
        run["id"], intent["id"], principal="owner", command_key="begin"
    )
    capacity = planning_capacity(tmp_path / "capacity")
    authority = PlanningAdmissionAuthority(
        tmp_path / "admission.sqlite",
        tmp_path / "planning.sqlite",
        planner,
        capacity,
        FixtureCommander({}, available=False),
        authority_kind="production",
    )
    denied = authority.advance(execution["id"], "owner", "advance")
    assert denied["reason_codes"] == ["PLANNING_POLICY_REQUIRED"]
    assert capacity.snapshot()["reservations"] == []


def test_capacity_unknown_is_read_only_and_controller_stays_resumable(
    configured: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, authority, _, execution = _case(tmp_path, configured)
    binding = execution["binding"]
    record = authority._prepare(execution["id"], binding, "owner")
    with authority._transaction() as db:
        record["capacity_request"] = {
            "attempt_id": binding["attempt_id"],
            "run_id": binding["run_id"],
            "profile_id": binding["profile"]["id"],
            "profile_revision": binding["profile"]["revision"],
            "role": "commander",
            "purpose": "lead",
            "authorization_ref": record["budget_identity"],
            "rulebook_revision": binding["rulebook_sha256"],
            "duration_seconds": 25,
            "demand": {"service-fixture": "5"},
            "expected_capacity": record["estimate"]["expected_capacity"],
        }
        record["phase"] = "capacity_admit_unknown"
        authority._save(db, record)

    def must_not_mutate(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("unknown recovery must only read the original receipt")

    monkeypatch.setattr(authority.capacity, "admit", must_not_mutate)
    recovered = authority.advance(execution["id"], "owner", "recover")
    assert recovered["phase"] == "capacity_admit_unknown"
    controller = PlanningExecution(
        authority.execution_database,
        authority.planner,
        admissions=authority,
        capacity=authority.capacity,
        allow_fixture_authorities=True,
    )
    resumed = controller.reconcile(execution["id"], principal="owner")
    assert resumed["state"] == "admission_unknown"
    assert resumed["reason_codes"] == ["PLANNING_ADMISSION_UNKNOWN"]


def test_controller_accepts_exact_unknown_receipt_completion(
    configured: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, authority, _, execution = _case(tmp_path, configured)
    original = authority.capacity.activate

    def lose_reply(*args: Any, **kwargs: Any) -> dict[str, Any]:
        original(*args, **kwargs)
        raise RuntimeError("activation reply lost")

    monkeypatch.setattr(authority.capacity, "activate", lose_reply)
    with pytest.raises(RuntimeError, match="reply lost"):
        authority.advance(execution["id"], "owner", "advance")
    controller = PlanningExecution(
        authority.execution_database,
        authority.planner,
        admissions=authority,
        capacity=authority.capacity,
        allow_fixture_authorities=True,
    )
    assert controller.reconcile(execution["id"], principal="owner")["state"] == "admission_unknown"
    reopened = PlanningAdmissionAuthority(
        authority.database,
        authority.execution_database,
        authority.planner,
        authority.capacity,
        authority.qualifications,
        authority_kind="fixture",
    )
    assert reopened.advance(execution["id"], "owner", "recover")["phase"] == "admitted"
    # The next blocker is the intentionally absent #112 output reader. It is
    # not an evidence-changed blocker, proving the exact unknown -> completed
    # receipt transition was accepted monotonically.
    completed = controller.reconcile(execution["id"], principal="owner")
    assert completed["reason_codes"] == ["PLANNING_OUTPUT_AUTHORITY_UNAVAILABLE"]


def test_persistent_factory_rebuilds_production_reader_and_reserves_nothing_without_commander(
    configured: dict, tmp_path: Path
) -> None:
    _, authority, _, execution = _case(tmp_path, configured)
    control = tmp_path / "control"
    control.mkdir()
    (control / PLANNING_ADMISSION_BOOTSTRAP).write_text(
        json.dumps(
            {
                "schema_version": "karajan.planning-admission-bootstrap.v1",
                "state_directory": str(tmp_path),
                "planning_execution_database": str(authority.execution_database),
                "planning_admission_database": str(authority.database),
                "capacity_database": str(authority.capacity.path),
                "projects_database": str(authority.planner.projects.database),
                "allowed_roots": [str(tmp_path)],
            }
        ),
        encoding="utf-8",
    )
    service = PlanningExecution.from_trusted_factory(control)
    assert service.admissions is not None
    production = service.admissions.advance(execution["id"], "owner", "factory-admit")
    assert production["phase"] == "denied"
    assert production["reason_codes"] == ["COMMANDER_QUALIFICATION_REQUIRED"]
    assert authority.capacity.snapshot()["reservations"] == []


def test_fixture_admission_cannot_be_relabelled_after_production_reopen(
    configured: dict, tmp_path: Path
) -> None:
    _, authority, _, execution = _case(tmp_path, configured)
    admitted = authority.advance(execution["id"], "owner", "fixture-admit")
    assert admitted["phase"] == "admitted"
    assert authority.read_admission(execution["binding"])["authority_kind"] == "fixture"
    control = tmp_path / "control"
    control.mkdir()
    (control / PLANNING_ADMISSION_BOOTSTRAP).write_text(
        json.dumps(
            {
                "schema_version": "karajan.planning-admission-bootstrap.v1",
                "state_directory": str(tmp_path),
                "planning_execution_database": str(authority.execution_database),
                "planning_admission_database": str(authority.database),
                "capacity_database": str(authority.capacity.path),
                "projects_database": str(authority.planner.projects.database),
                "allowed_roots": [str(tmp_path)],
            }
        ),
        encoding="utf-8",
    )
    production = PlanningExecution.from_trusted_factory(control)
    assert production.admissions is not None
    with pytest.raises(RunError, match="PLANNING_ADMISSION_PROVENANCE_FORBIDDEN"):
        production.admissions.advance(execution["id"], "owner", "production-admit")
