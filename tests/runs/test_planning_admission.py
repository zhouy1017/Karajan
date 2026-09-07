"""C evidence for durable planning admission; no provider is contacted."""

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from karajan.orchestration.planning_admission import (
    COMMANDER_QUALIFICATION_SCOPE,
    PlanningAdmissionAuthority,
)
from karajan.orchestration.planning_execution import PlanningExecution
from karajan.runs import RunError, RunPlanner
from karajan.runs.planning import digest
from test_planning import create_request
from test_planning_execution import capacity_store

pytest_plugins = ["test_planning"]


@pytest.fixture
def configured(project: tuple[Any, dict, Path]) -> dict:
    registry, value, _ = project
    return {**deepcopy(value), "registry": registry}


class FixtureCommander:
    def __init__(self, available: bool = True) -> None:
        self.available = available

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
        }


def _case(
    tmp_path: Path, configured: dict, *, available: bool = True
) -> tuple[PlanningExecution, PlanningAdmissionAuthority, dict, Any]:
    planner = RunPlanner(tmp_path / "runs.sqlite", configured["registry"])
    run = planner.create(create_request(configured), command_key="run", principal="owner")
    intent = planner.planning_intent(run["id"], term=1, command_key="intent-1", principal="lead")
    execution = PlanningExecution(tmp_path / "planning.sqlite", planner).begin(
        run["id"], intent["id"], principal="owner", command_key="begin-1"
    )
    capacity = capacity_store(tmp_path / "capacity")
    binding = execution["binding"]
    capacity.register_profile(
        {
            "id": binding["profile"]["id"],
            "revision": binding["profile"]["revision"],
            "account_id": "shared-account",
            "pool_ids": ["short", "weekly", "allowance"],
        },
        command_key="profile",
    )
    authority = PlanningAdmissionAuthority(
        tmp_path / "planning-admissions.sqlite",
        tmp_path / "planning.sqlite",
        planner,
        capacity,
        FixtureCommander(available),
        authority_kind="fixture",
    )
    authority.register_estimate(
        run["id"],
        binding["budget_ref"],
        binding["profile"],
        demand={"short": "5", "weekly": "5", "allowance": "5"},
        expected_capacity={
            "policy_revision": 1,
            "pool_windows": {
                "short": "fixture-window",
                "weekly": "fixture-window",
                "allowance": "fixture-window",
            },
            "lead_reserve_access": True,
        },
        duration_seconds=30,
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


def test_effect_guard_reuses_only_the_original_binding(configured: dict, tmp_path: Path) -> None:
    _, authority, _, execution = _case(tmp_path, configured)
    authority.advance(execution["id"], "owner", "advance")

    with authority.effect_guard(execution["id"], "owner", "start") as guard:
        assert guard["attempt_id"] == execution["binding"]["attempt_id"]
        assert guard["capacity"]["request"]["run_id"] == execution["run_id"]
        assert guard["source_generation_sha256"] == "b" * 64


def test_lost_activation_reply_reopens_the_original_capacity_command(
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
