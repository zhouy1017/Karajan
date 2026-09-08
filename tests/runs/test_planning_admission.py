"""C evidence for durable planning admission; no provider is contacted."""

import inspect
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from threading import Barrier, Event
from typing import Any

import karajan.capacity.store as capacity_store
import karajan.orchestration.planning_admission as planning_admission
import karajan.orchestration.planning_snapshot as planning_snapshot
import pytest
from karajan.capacity import CapacityStore
from karajan.orchestration.go_task_runtime import (
    GoTaskCredentialSource,
    GoTaskSettings,
    write_go_task_bootstrap,
)
from karajan.orchestration.planning_admission import (
    COMMANDER_QUALIFICATION_SCOPE,
    PersistentCommanderQualificationReader,
    PlanningAdmissionAuthority,
)
from karajan.orchestration.planning_bootstrap import PLANNING_ADMISSION_BOOTSTRAP
from karajan.orchestration.planning_execution import PlanningExecution
from karajan.orchestration.planning_snapshot import (
    PlanningRepositorySnapshotStore,
    provision_planning_repository_snapshots,
    snapshot_database,
)
from karajan.projects import ProjectRegistry
from karajan.projects.credential_sources import (
    CredentialSourceError,
    CredentialSourceStore,
    LocalKeyFile,
)
from karajan.projects.qualification import ProfileQualificationStore
from karajan.runs import RunError, RunPlanner
from karajan.runs.planning import digest
from test_planning import create_request
from test_routing_authorization import policy_request, request_v2, submit_request

pytest_plugins = ["test_planning"]


@pytest.fixture
def configured(project: tuple[Any, dict, Path]) -> dict:
    registry, value, _ = project
    return {**deepcopy(value), "registry": registry}


class FixtureCommander:
    def __init__(
        self,
        profile_facts: dict[str, Any],
        available: bool = True,
        valid_until: float = 2_000_000_000.0,
    ) -> None:
        self.available = available
        self.profile_facts = profile_facts
        self.valid_until = valid_until

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
            "valid_until": self.valid_until,
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


def planning_capacity(
    directory: Path,
    *,
    pools: tuple[str, ...] = ("service-fixture",),
    remaining: str = "10",
    lead_reserve: dict[str, str] | None = None,
    clock: Callable[[], float] = lambda: 1000.0,
    observation_max_age_seconds: int = 30,
    conservative_observation_max_age_seconds: int = 30,
    max_attempt_duration_seconds: int = 60,
) -> CapacityStore:
    """Real SQLite Capacity facts matching the frozen fixture configuration."""
    directory.mkdir()
    store = CapacityStore(directory / "capacity.sqlite", clock=clock)
    for pool in pools:
        window = "fixture-window" if pools == ("service-fixture",) else "fixture-window-" + pool
        store.register_pool(
            {
                "id": pool,
                "account_id": "fixture-account",
                "kind": "service",
                "unit": "percent",
                "window_kind": "fixed",
            },
            command_key="pool-" + pool,
        )
        store.observe(
            {
                "pool_id": pool,
                "window_id": window,
                "observed_at": 1000.0,
                "reset_at": 2000.0,
                "source": "fixture",
                "source_ref": "fixture-observer",
                "metric": "remaining",
                "amount": remaining,
                "limit": remaining,
                "covered_usage_ids": [],
            },
            command_key="observe-" + pool,
        )
    store.activate_policy(
        {
            "account_id": "fixture-account",
            "max_active_attempts": 4,
            "max_attempt_duration_seconds": max_attempt_duration_seconds,
            "observation_max_age_seconds": observation_max_age_seconds,
            "require_official_observation": False,
            "safety_margin": {},
            "lead_reserve": lead_reserve or {},
            "lead_reserved_slots": 0,
            "conservative_mode": {
                "enabled": True,
                "max_local_active_attempts": 4,
                "max_attempt_duration_seconds": 60,
                "observation_max_age_seconds": conservative_observation_max_age_seconds,
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
    observation_max_age_seconds: int = 30,
    conservative_observation_max_age_seconds: int = 30,
    max_attempt_duration_seconds: int = 60,
    register_estimate: bool = True,
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
    capacity = planning_capacity(
        tmp_path / "capacity",
        clock=clock or (lambda: 1000.0),
        observation_max_age_seconds=observation_max_age_seconds,
        conservative_observation_max_age_seconds=conservative_observation_max_age_seconds,
        max_attempt_duration_seconds=max_attempt_duration_seconds,
    )
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
                    run["configuration_snapshot"]["configuration"]["resources"]["profiles"][0][
                        "profile"
                    ]
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
    if register_estimate:
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
        first_result["route_sources"]["reserved"]["selected_profile"] == first["binding"]["profile"]
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


def test_two_runs_contend_for_commander_protected_full_capacity_vector(
    configured: dict, tmp_path: Path
) -> None:
    """Two original Runs race on one real complete-vector Capacity ledger."""
    pools = ("planning-short", "planning-weekly", "planning-allowance")
    full_configuration = json.loads(
        (Path(__file__).parents[2] / "examples/projects/offline-configuration.json").read_text()
    )
    full_configuration["resources"]["profiles"][0]["quota_pool_refs"] = list(pools)
    full_configuration["resources"]["quota_pools"] = [
        {
            "id": pool,
            "account_id": "fixture-account",
            "kind": "service",
            "unit": "percent",
            "limit": "5",
            "observation_state": "unknown",
        }
        for pool in pools
    ]
    preview = configured["registry"].preview_configuration(
        configured["id"], full_configuration, command_key="three-vector-preview", principal="owner"
    )
    configured = {
        **configured["registry"].apply_configuration(
            configured["id"],
            preview["preview_id"],
            expected_revision=configured["revision"],
            command_key="three-vector-apply",
            principal="owner",
        ),
        "registry": configured["registry"],
    }
    capacity = planning_capacity(
        tmp_path / "shared-capacity",
        pools=pools,
        remaining="5",
        lead_reserve={pool: "5" for pool in pools},
    )
    planner = RunPlanner(tmp_path / "two-runs.sqlite", configured["registry"])
    fixed = configured["registry"].register_execution_policy(
        configured["id"],
        policy_request(configured),
        command_key="two-runs-policy",
        principal="owner",
    )
    runs = [
        planner.create(
            request_v2(configured, fixed), command_key="two-runs-" + label, principal="owner"
        )
        for label in ("one", "two")
    ]
    execution_service = PlanningExecution(tmp_path / "two-runs-execution.sqlite", planner)
    intents = [
        planner.planning_intent(
            run["id"], term=1, command_key="two-intent-" + str(index), principal="lead"
        )
        for index, run in enumerate(runs, start=1)
    ]
    executions = [
        execution_service.begin(
            run["id"], intent["id"], principal="owner", command_key="two-begin-" + str(index)
        )
        for index, (run, intent) in enumerate(zip(runs, intents, strict=True), start=1)
    ]
    profile = executions[0]["binding"]["profile"]
    capacity.register_profile(
        {
            "id": profile["id"],
            "revision": profile["revision"],
            "account_id": "fixture-account",
            "pool_ids": list(pools),
        },
        command_key="two-runs-profile",
    )
    facts = {
        "profile": profile,
        "profile_digest": digest(
            runs[0]["configuration_snapshot"]["configuration"]["resources"]["profiles"][0][
                "profile"
            ]
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
    }
    authority = PlanningAdmissionAuthority(
        tmp_path / "two-runs-admission.sqlite",
        execution_service.database,
        planner,
        capacity,
        FixtureCommander(facts),
    )
    expected_capacity = {
        "policy_revision": 1,
        "pool_windows": {pool: "fixture-window-" + pool for pool in pools},
        "lead_reserve_access": True,
    }
    for index, (run, execution) in enumerate(zip(runs, executions, strict=True), start=1):
        authority.register_estimate(
            run["id"],
            execution["binding"]["budget_ref"],
            profile,
            demand={pool: "5" for pool in pools},
            expected_capacity=expected_capacity,
            duration_seconds=25,
            max_requests=5,
            max_duration_seconds=100,
            principal="owner",
            command_key="two-estimate-" + str(index),
        )
    worker = {
        "attempt_id": "protected-worker",
        "run_id": "worker-run",
        "profile_id": profile["id"],
        "profile_revision": profile["revision"],
        "role": "worker",
        "purpose": None,
        "authorization_ref": "worker-scope",
        "rulebook_revision": "fixture-rulebook",
        "duration_seconds": 25,
        "demand": {pool: "5" for pool in pools},
        "expected_capacity": {**expected_capacity, "lead_reserve_access": False},
    }
    assert capacity.admit(worker, command_key="protected-worker")["decision"] == "rejected"
    assert capacity.snapshot()["reservations"] == []

    barrier = Barrier(2)

    def contend(index: int) -> dict[str, Any]:
        barrier.wait(timeout=5)
        return authority.advance(executions[index]["id"], "owner", "two-advance-" + str(index))

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(contend, range(2)))
    assert sorted(result["phase"] for result in results) == ["admitted", "denied"], results
    reservations = capacity.snapshot()["reservations"]
    assert len(reservations) == 1
    assert reservations[0]["request"]["demand"] == {pool: "5" for pool in pools}
    admitted = next(result for result in results if result["phase"] == "admitted")
    assert admitted["budget_usage"]["attempts"] == 5


def test_missing_commander_fact_is_a_production_zero_reservation_denial(
    configured: dict, tmp_path: Path
) -> None:
    _, authority, run, execution = _case(tmp_path, configured)
    authority.authority_kind = "production"
    denied = authority.advance(execution["id"], "owner", "advance")

    assert denied["phase"] == "denied"
    assert denied["reason_codes"] == ["COMMANDER_QUALIFICATION_REQUIRED"]
    assert authority.capacity.snapshot()["reservations"] == []
    assert COMMANDER_QUALIFICATION_SCOPE == "commander_planning.v1"


def test_missing_estimate_is_a_public_idempotent_zero_reservation_denial(
    configured: dict, tmp_path: Path
) -> None:
    _, authority, _, execution = _case(tmp_path, configured, register_estimate=False)

    denied = authority.advance(execution["id"], "owner", "estimate-missing")
    replay = authority.advance(execution["id"], "owner", "estimate-missing")

    assert denied["phase"] == "denied"
    assert denied["reason_codes"] == ["PLANNING_ESTIMATE_MISSING"]
    assert replay == denied
    assert authority.capacity.snapshot()["reservations"] == []


def test_estimate_cannot_widen_selected_rule_reserve_access(
    configured: dict, tmp_path: Path
) -> None:
    _, authority, run, execution = _case(tmp_path, configured)
    binding = execution["binding"]
    authority.register_estimate(
        run["id"],
        binding["budget_ref"],
        binding["profile"],
        demand={"service-fixture": "5"},
        expected_capacity={
            "policy_revision": 1,
            "pool_windows": {"service-fixture": "fixture-window"},
            # The selected lead rule grants this access. A provisioner cannot
            # weaken or widen it by substituting an estimate expectation.
            "lead_reserve_access": False,
        },
        duration_seconds=25,
        max_requests=5,
        max_duration_seconds=100,
        principal="owner",
        command_key="mismatched-reserve-estimate",
    )
    denied = authority.advance(execution["id"], "owner", "mismatched-reserve-advance")
    assert denied["reason_codes"] == ["PLANNING_LEAD_RESERVE_ACCESS_MISMATCH"]
    assert authority.capacity.snapshot()["reservations"] == []


def test_cancelled_execution_cannot_create_planning_capacity_effect(
    configured: dict, tmp_path: Path
) -> None:
    service, authority, _, execution = _case(tmp_path, configured)
    service.cancel(execution["id"], principal="owner", command_key="cancel-before-admit")
    with pytest.raises(RunError, match="^PLANNING_EXECUTION_CANCELLED$"):
        authority.advance(execution["id"], "owner", "cancelled-advance")
    assert authority.capacity.snapshot()["reservations"] == []


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


def test_capacity_boundary_rechecks_commander_expiry_before_reservation(
    configured: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [1000.0]
    _, authority, _, execution = _case(tmp_path, configured, clock=lambda: now[0])
    commander = authority.qualifications
    assert isinstance(commander, FixtureCommander)
    commander.valid_until = 1001.0
    original = authority.capacity.admit

    def expire_while_capacity_is_held(
        request: dict[str, Any],
        *,
        command_key: str,
        before_reserve: Callable[[], None] | None = None,
        after_capacity_facts: Callable[[Any], None] | None = None,
        before_reservation_write: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        def expired_callback() -> None:
            now[0] = 1001.0
            assert before_reserve is not None
            before_reserve()

        return original(
            request,
            command_key=command_key,
            before_reserve=expired_callback,
            after_capacity_facts=after_capacity_facts,
            before_reservation_write=before_reservation_write,
        )

    monkeypatch.setattr(authority.capacity, "admit", expire_while_capacity_is_held)
    denied = authority.advance(execution["id"], "owner", "advance")
    assert denied["reason_codes"] == ["COMMANDER_QUALIFICATION_EXPIRED"]
    assert authority.capacity.snapshot()["reservations"] == []


def test_capacity_boundary_rechecks_nested_commander_profile_facts_expiry(
    configured: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [1000.0]
    _, authority, _, execution = _case(tmp_path, configured, clock=lambda: now[0])
    commander = authority.qualifications
    assert isinstance(commander, FixtureCommander)
    commander.valid_until = 1100.0
    commander.profile_facts["valid_until"] = 1001.0
    original = authority.capacity.admit

    def expire_while_capacity_is_held(
        request: dict[str, Any],
        *,
        command_key: str,
        before_reserve: Callable[[], None] | None = None,
        after_capacity_facts: Callable[[Any], None] | None = None,
        before_reservation_write: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        def expired_callback() -> None:
            now[0] = 1002.0
            assert before_reserve is not None
            before_reserve()

        return original(
            request,
            command_key=command_key,
            before_reserve=expired_callback,
            after_capacity_facts=after_capacity_facts,
            before_reservation_write=before_reservation_write,
        )

    monkeypatch.setattr(authority.capacity, "admit", expire_while_capacity_is_held)
    denied = authority.advance(execution["id"], "owner", "advance")
    assert denied["reason_codes"] == ["COMMANDER_PROFILE_FACTS_EXPIRED"]
    assert authority.capacity.snapshot()["reservations"] == []


def test_capacity_boundary_retains_unknown_estimate_conservative_age(
    configured: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [1000.0]
    _, authority, _, execution = _case(
        tmp_path,
        configured,
        clock=lambda: now[0],
        conservative_observation_max_age_seconds=5,
    )
    original = authority.capacity.admit

    def cross_conservative_age(
        request: dict[str, Any],
        *,
        command_key: str,
        before_reserve: Callable[[], None] | None = None,
        after_capacity_facts: Callable[[Any], None] | None = None,
        before_reservation_write: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        def waited_before_facts() -> None:
            now[0] = 1006.0
            assert before_reserve is not None
            before_reserve()

        def facts_after_wait(boundary: Any) -> None:
            assert after_capacity_facts is not None
            after_capacity_facts(boundary)

        return original(
            request,
            command_key=command_key,
            before_reserve=waited_before_facts,
            after_capacity_facts=facts_after_wait,
            before_reservation_write=before_reservation_write,
        )

    monkeypatch.setattr(authority.capacity, "admit", cross_conservative_age)
    denied = authority.advance(execution["id"], "owner", "conservative-age")
    assert denied["reason_codes"] == ["PLANNING_BOUNDARY_ROUTE_REJECTED"]
    assert authority.capacity.snapshot()["reservations"] == []


def test_final_quota_fence_rejects_conservative_age_crossed_during_pure_route(
    configured: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [1004.0]
    _, authority, _, execution = _case(
        tmp_path,
        configured,
        clock=lambda: now[0],
        conservative_observation_max_age_seconds=5,
    )
    original = authority._revalidate_boundary_route

    def finish_after_conservative_age(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        # Capacity facts and the full shared evaluator both ran at age four.
        # Only the O(1) temporal tail sees the pure computation cross age five.
        now[0] = 1006.0
        return result

    monkeypatch.setattr(authority, "_revalidate_boundary_route", finish_after_conservative_age)
    denied = authority.advance(execution["id"], "owner", "pure-route-age")
    assert denied["reason_codes"] == ["PLANNING_BOUNDARY_ROUTE_REJECTED"]
    assert authority.capacity.snapshot()["reservations"] == []


def test_final_route_observes_conservative_age_after_sealed_estimate_digest(
    configured: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real final estimate digest finishes before the quota evaluation samples time."""
    now = [1004.0]
    _, authority, _, execution = _case(
        tmp_path,
        configured,
        clock=lambda: now[0],
        conservative_observation_max_age_seconds=5,
    )
    original_capture = authority._capture_final_boundary
    original_digest = planning_admission.digest
    inside_capture = [False]

    def capture(*args: Any, **kwargs: Any) -> Any:
        inside_capture[0] = True
        try:
            return original_capture(*args, **kwargs)
        finally:
            inside_capture[0] = False

    def digest_after_estimate_hash(value: object) -> str:
        result = original_digest(value)
        if (
            inside_capture[0]
            and isinstance(value, dict)
            and value.get("schema_version") == "karajan.planning-estimate.v1"
            and "digest" not in value
        ):
            now[0] = 1006.0
        return result

    monkeypatch.setattr(authority, "_capture_final_boundary", capture)
    monkeypatch.setattr(planning_admission, "digest", digest_after_estimate_hash)
    denied = authority.advance(execution["id"], "owner", "estimate-digest-age")
    assert denied["reason_codes"] == ["PLANNING_BOUNDARY_ROUTE_REJECTED"]
    assert authority.capacity.snapshot()["reservations"] == []


def test_final_route_observes_conservative_age_after_qualification_comparison(
    configured: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The complete Commander dictionary comparison also precedes quota time."""
    now = [1004.0]
    _, authority, _, execution = _case(
        tmp_path,
        configured,
        clock=lambda: now[0],
        conservative_observation_max_age_seconds=5,
    )
    original_capture = authority._capture_final_boundary
    original_binding = authority._assert_qualification_binding
    inside_capture = [False]

    def capture(*args: Any, **kwargs: Any) -> Any:
        inside_capture[0] = True
        try:
            return original_capture(*args, **kwargs)
        finally:
            inside_capture[0] = False

    def compare_then_cross(*args: Any, **kwargs: Any) -> dict[str, Any]:
        value = original_binding(*args, **kwargs)
        if inside_capture[0]:
            now[0] = 1006.0
        return value

    monkeypatch.setattr(authority, "_capture_final_boundary", capture)
    monkeypatch.setattr(authority, "_assert_qualification_binding", compare_then_cross)
    denied = authority.advance(execution["id"], "owner", "qualification-compare-age")
    assert denied["reason_codes"] == ["PLANNING_BOUNDARY_ROUTE_REJECTED"]
    assert authority.capacity.snapshot()["reservations"] == []


def test_final_reservation_hook_rechecks_original_budget_after_fact_capture(
    configured: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pure route-facts scan cannot outlive the original first-claim clock."""
    now = [1000.0]
    _, authority, _, execution = _case(
        tmp_path,
        configured,
        clock=lambda: now[0],
        observation_max_age_seconds=1000,
        conservative_observation_max_age_seconds=1000,
        max_attempt_duration_seconds=500,
    )
    original = authority.capacity.admit

    def cross_budget_after_facts(
        request: dict[str, Any],
        *,
        command_key: str,
        before_reserve: Callable[[], None] | None = None,
        after_capacity_facts: Callable[[Any], None] | None = None,
        before_reservation_write: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        def captured(boundary: Any) -> None:
            assert after_capacity_facts is not None
            after_capacity_facts(boundary)
            # Simulate the pure facts/hash/route work finishing after the
            # original Run's first-claim deadline, while Capacity remains valid.
            now[0] = 1301.0

        return original(
            request,
            command_key=command_key,
            before_reserve=before_reserve,
            after_capacity_facts=captured,
            before_reservation_write=before_reservation_write,
        )

    monkeypatch.setattr(authority.capacity, "admit", cross_budget_after_facts)
    denied = authority.advance(execution["id"], "owner", "late-facts-budget")
    assert denied["reason_codes"] == ["PLANNING_BUDGET_EXPIRED"]
    assert authority.capacity.snapshot()["reservations"] == []


def _advance_after_reservation_encoding_crosses(
    authority: PlanningAdmissionAuthority,
    execution: dict[str, Any],
    now: list[float],
    target: float,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    """Advance the shared clock only after Capacity encoded its reservation."""
    original = capacity_store.encoded
    encoded_reservation = False

    def encode_then_cross(value: Any) -> str:
        nonlocal encoded_reservation
        result = original(value)
        if isinstance(value, dict) and value.get("state") == "reserved":
            encoded_reservation = True
            now[0] = target
        return result

    monkeypatch.setattr(capacity_store, "encoded", encode_then_cross)
    result = authority.advance(execution["id"], "owner", "encoded-final-boundary")
    assert encoded_reservation
    return result


def test_final_reservation_closure_rechecks_conservative_age_after_encoding(
    configured: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deferred controller tail rejects age crossed by Capacity encoding."""
    now = [1004.0]
    _, authority, _, execution = _case(
        tmp_path,
        configured,
        clock=lambda: now[0],
        conservative_observation_max_age_seconds=5,
    )

    denied = _advance_after_reservation_encoding_crosses(
        authority, execution, now, 1006.0, monkeypatch
    )

    assert denied["reason_codes"] == ["PLANNING_BOUNDARY_ROUTE_REJECTED"]
    assert authority.capacity.snapshot()["reservations"] == []


def test_final_reservation_closure_rechecks_commander_expiry_after_encoding(
    configured: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deferred controller tail retains the original Commander deadline."""
    now = [1000.0]
    _, authority, _, execution = _case(tmp_path, configured, clock=lambda: now[0])
    commander = authority.qualifications
    assert isinstance(commander, FixtureCommander)
    commander.valid_until = 1001.0

    denied = _advance_after_reservation_encoding_crosses(
        authority, execution, now, 1001.0, monkeypatch
    )

    assert denied["reason_codes"] == ["COMMANDER_QUALIFICATION_EXPIRED"]
    assert authority.capacity.snapshot()["reservations"] == []


def test_final_reservation_closure_rechecks_original_budget_after_encoding(
    configured: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deferred controller tail cannot outlive the first planning claim."""
    now = [1000.0]
    _, authority, _, execution = _case(
        tmp_path,
        configured,
        clock=lambda: now[0],
        observation_max_age_seconds=1000,
        conservative_observation_max_age_seconds=1000,
        max_attempt_duration_seconds=500,
    )

    denied = _advance_after_reservation_encoding_crosses(
        authority, execution, now, 1301.0, monkeypatch
    )

    assert denied["reason_codes"] == ["PLANNING_BUDGET_EXPIRED"]
    assert authority.capacity.snapshot()["reservations"] == []


def test_final_effect_closure_rechecks_commander_expiry_after_capacity_preparation(
    configured: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The effect consumer supplies a deferred O(1) closure to Capacity."""
    now = [1000.0]
    _, authority, _, execution = _case(tmp_path, configured, clock=lambda: now[0])
    commander = authority.qualifications
    assert isinstance(commander, FixtureCommander)
    commander.valid_until = 1001.0
    assert authority.advance(execution["id"], "owner", "admit")["phase"] == "admitted"
    original = authority.capacity.pre_effect_guard
    callbacks: list[str] = []

    @contextmanager
    def after_capacity_preparation(
        admission_id: str,
        *,
        expected_request: dict[str, Any],
        before_effect: Callable[[], None] | None = None,
        after_capacity_facts: Callable[[Any], None] | None = None,
        before_effect_yield: Callable[[], Callable[[], None] | None] | None = None,
    ) -> Any:
        def prepare_final() -> Callable[[], None] | None:
            assert before_effect_yield is not None
            deferred = before_effect_yield()
            assert callable(deferred)

            def final() -> None:
                callbacks.append("final")
                now[0] = 1001.0
                deferred()

            return final

        with original(
            admission_id,
            expected_request=expected_request,
            before_effect=before_effect,
            after_capacity_facts=after_capacity_facts,
            before_effect_yield=prepare_final,
        ) as capacity:
            yield capacity

    monkeypatch.setattr(authority.capacity, "pre_effect_guard", after_capacity_preparation)
    entered = False
    with pytest.raises(RunError, match="^COMMANDER_QUALIFICATION_EXPIRED$"):
        with authority.effect_guard(execution["id"], "owner", "encoded-effect-boundary"):
            entered = True
    assert callbacks == ["final"]
    assert not entered


def test_final_effect_hook_rechecks_nested_fact_expiry_after_fact_capture(
    configured: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No effect body is entered when held Commander facts expire after capture."""
    now = [1000.0]
    _, authority, _, execution = _case(tmp_path, configured, clock=lambda: now[0])
    commander = authority.qualifications
    assert isinstance(commander, FixtureCommander)
    commander.valid_until = 1001.0
    commander.profile_facts["valid_until"] = 1100.0
    assert authority.advance(execution["id"], "owner", "admit")["phase"] == "admitted"
    original = authority.capacity.pre_effect_guard

    @contextmanager
    def expire_after_facts(
        admission_id: str,
        *,
        expected_request: dict[str, Any],
        before_effect: Callable[[], None] | None = None,
        after_capacity_facts: Callable[[Any], None] | None = None,
        before_effect_yield: Callable[[], None] | None = None,
    ) -> Any:
        def captured(boundary: Any) -> None:
            assert after_capacity_facts is not None
            after_capacity_facts(boundary)
            now[0] = 1002.0

        with original(
            admission_id,
            expected_request=expected_request,
            before_effect=before_effect,
            after_capacity_facts=captured,
            before_effect_yield=before_effect_yield,
        ) as capacity:
            yield capacity

    monkeypatch.setattr(authority.capacity, "pre_effect_guard", expire_after_facts)
    entered = False
    with pytest.raises(RunError, match="^COMMANDER_QUALIFICATION_EXPIRED$"):
        with authority.effect_guard(execution["id"], "owner", "late-facts-effect"):
            entered = True
    assert not entered


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


def test_lost_admit_reply_allows_only_receipt_proven_unknown_enrichment(
    configured: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read-only admit receipt can add its activation ID without a new effect."""
    _, authority, _, execution = _case(tmp_path, configured)
    original_admit = authority.capacity.admit

    def lose_admit_reply(*args: Any, **kwargs: Any) -> dict[str, Any]:
        original_admit(*args, **kwargs)
        raise RuntimeError("admit reply lost")

    monkeypatch.setattr(authority.capacity, "admit", lose_admit_reply)
    with pytest.raises(RuntimeError, match="admit reply lost"):
        authority.advance(execution["id"], "owner", "admit-lost")
    controller = PlanningExecution(
        authority.execution_database,
        authority.planner,
        admissions=authority,
        capacity=authority.capacity,
        allow_fixture_authorities=True,
    )
    assert controller.reconcile(execution["id"], principal="owner")["state"] == "admission_unknown"
    assert authority.advance(execution["id"], "owner", "admit-recover")["phase"] == (
        "capacity_activate_unknown"
    )
    # This is the receipt-proven unknown -> unknown enrichment: it retains the
    # original request/key, adds only the exact admission ID, and remains
    # resumable while activation is still unknown. Recovery does not activate
    # or otherwise replay an effect under the lost-admit uncertainty.
    resumed = controller.reconcile(execution["id"], principal="owner")
    assert resumed["state"] == "admission_unknown"
    assert resumed["reason_codes"] == ["PLANNING_ADMISSION_UNKNOWN"]
    assert "PLANNING_ADMISSION_EVIDENCE_CHANGED" not in resumed["reason_codes"]
    assert (
        authority.capacity.command_receipt(
            "activate",
            {"admission_id": authority.capacity.snapshot()["reservations"][0]["id"]},
            command_key="planning-activate:" + execution["id"],
        )
        is None
    )


def _protected_factory_control(tmp_path: Path, authority: PlanningAdmissionAuthority) -> Path:
    """Copy complete existing stores into a Linux-private controller deployment."""
    state = tmp_path / "protected-state"
    state.mkdir(mode=0o700)
    for source, name in (
        (authority.planner.database, "runs.sqlite"),
        (authority.execution_database, "planning-execution.sqlite"),
        (authority.database, "planning-admission.sqlite"),
        (authority.capacity.path, "capacity.sqlite"),
        (authority.planner.projects.database, "projects.sqlite"),
    ):
        destination = state / name
        shutil.copy2(source, destination)
        destination.chmod(0o600)
    control = tmp_path / "protected-control"
    control.mkdir(mode=0o700)
    descriptor = {
        "schema_version": "karajan.planning-admission-bootstrap.v1",
        "state_directory": str(state),
        "planning_execution_database": str(state / "planning-execution.sqlite"),
        "planning_admission_database": str(state / "planning-admission.sqlite"),
        "capacity_database": str(state / "capacity.sqlite"),
        "projects_database": str(state / "projects.sqlite"),
        "allowed_roots": [str(tmp_path)],
    }
    path = control / PLANNING_ADMISSION_BOOTSTRAP
    path.write_text(json.dumps(descriptor), encoding="utf-8")
    path.chmod(0o600)
    return control


def test_persistent_factory_missing_descriptor_rejects_without_creating_stores(
    tmp_path: Path,
) -> None:
    control = tmp_path / "empty-control"
    control.mkdir()
    with pytest.raises(RunError, match="^PLANNING_ADMISSION_BOOTSTRAP_INVALID$"):
        PlanningExecution.from_trusted_factory(control)
    assert list(control.iterdir()) == []
    assert not list(tmp_path.glob("*.sqlite"))


@pytest.mark.skipif(sys.platform == "win32", reason="private deployment modes require Linux")
def test_persistent_factory_keeps_historical_execution_readable_without_snapshot_ledger(
    configured: dict, tmp_path: Path
) -> None:
    _, authority, _, execution = _case(tmp_path, configured)
    control = _protected_factory_control(tmp_path, authority)
    state = tmp_path / "protected-state"
    ledger = snapshot_database(control)
    artifacts = state / "planning-repository-snapshot-blobs"
    service = PlanningExecution.from_trusted_factory(control)
    assert service.snapshots is None
    assert service.get(execution["id"], principal="owner")["id"] == execution["id"]
    for operation in (
        lambda: service.freeze_repository_snapshot(
            execution["id"], principal="owner", command_key="freeze"
        ),
        lambda: service.read_repository_snapshot(execution["id"], principal="owner"),
    ):
        with pytest.raises(RunError, match="^PLANNING_REPOSITORY_SNAPSHOT_UNAVAILABLE$"):
            operation()
    assert not ledger.exists()
    assert not artifacts.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="private deployment modes require Linux")
def test_persistent_factory_recovers_committed_submission_without_snapshot_ledger(
    configured: dict, tmp_path: Path
) -> None:
    service, authority, run, execution = _case(tmp_path, configured)
    persisted_run = service.planner.get(run["id"], principal="owner")
    intent = next(
        item for item in persisted_run["planning_intents"] if item["id"] == execution["intent_id"]
    )
    request = {"run_id": run["id"], **submit_request(persisted_run, intent)}
    command_key = "planning-execution-submit:" + execution["id"]
    receipt = service.planner._submit_planning_execution_plan(
        run["id"],
        execution["intent_id"],
        request,
        execution_id=execution["id"],
        binding_sha256=execution["binding_sha256"],
        principal="owner",
        command_key=command_key,
    )
    with service._transaction() as db:
        pending = service._load(db, execution["id"])
        pending.update(
            {
                "state": "submit_claimed",
                "submission_request": request,
                "submission_command_key": command_key,
                "submission_principal": "lead",
                "submission_started": True,
            }
        )
        service._save(db, pending)
    control = _protected_factory_control(tmp_path, authority)
    state = tmp_path / "protected-state"
    ledger = snapshot_database(control)
    artifacts = state / "planning-repository-snapshot-blobs"
    before_capacity = authority.capacity.snapshot()
    before_plans = service.planner.get(run["id"], principal="owner")["plans"]

    reopened = PlanningExecution.from_trusted_factory(control)
    assert reopened.snapshots is None
    recovered = reopened.submit(execution["id"], principal="owner", command_key="recover")

    assert recovered["state"] == "submitted"
    assert recovered["submission"] == receipt
    assert reopened.get(execution["id"], principal="owner")["submission"] == receipt
    assert reopened.capacity is not None
    assert reopened.capacity.snapshot() == before_capacity
    assert reopened.planner.get(run["id"], principal="owner")["plans"] == before_plans == [receipt]
    assert not ledger.exists()
    assert not artifacts.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="private deployment modes require Linux")
def test_persistent_factory_opens_a_provisioned_snapshot_ledger(
    configured: dict, tmp_path: Path
) -> None:
    _, authority, _, _ = _case(tmp_path, configured)
    control = _protected_factory_control(tmp_path, authority)
    path = provision_planning_repository_snapshots(control)
    assert path.exists() and path.stat().st_mode & 0o077 == 0
    PlanningRepositorySnapshotStore(path, existing_only=True)
    service = PlanningExecution.from_trusted_factory(control)
    assert service.snapshots is not None


@pytest.mark.skipif(sys.platform == "win32", reason="private deployment modes require Linux")
def test_factory_freezes_registered_base_bytes_and_reopens(
    configured: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = configured["registry"]
    project = registry.get(configured["id"])
    root = Path(project["repository"]["root"])
    (root / "src").mkdir(exist_ok=True)
    (root / "tests").mkdir(exist_ok=True)
    source = root / "src" / "planning-input.txt"
    source.write_bytes(b"registered base bytes\n")
    test_source = root / "tests" / "planning-input-test.txt"
    test_source.write_bytes(b"registered test base bytes\n")
    subprocess.run(
        ["git", "-C", str(root), "add", "src/planning-input.txt", "tests/planning-input-test.txt"],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=x",
            "-c",
            "user.email=x@y.z",
            "commit",
            "-m",
            "snapshot",
        ],
        check=True,
    )
    registry.update(
        project["id"],
        {
            "name": project["name"],
            "base_ref": project["repository"]["base_ref"],
            "target_branch": project["target_branch"],
            "allowed_target_branches": project["allowed_target_branches"],
        },
        expected_revision=project["revision"],
        command_key="update-base",
        principal="owner",
    )
    configured.update(registry.get(project["id"]))
    configured["registry"] = registry
    _, authority, run, execution = _case(tmp_path, configured)
    # Provision the applicable Qualification ledger before the protected
    # factory copies its owned SQLite state into the fixture boundary.
    ProfileQualificationStore(authority.planner.projects)
    control = _protected_factory_control(tmp_path, authority)
    provision_planning_repository_snapshots(control)
    service = PlanningExecution.from_trusted_factory(control)
    assert service.capacity is not None
    before = service.capacity.snapshot()
    # These are the real ledgers rebuilt by the protected planning factory.
    # The factory has no Journal, Host, native runtime, model adapter, or
    # output transport path; manufacturing an empty Journal would not observe
    # a receiver.  The applicable controller effect boundaries here are the
    # real Project qualification records, Run plan records, and Capacity
    # reservations.  Physical native/model/provider absence is recorded as
    # not_run in the implementation evidence, not promoted to a ledger claim.
    assert service.outputs is None
    _qualification = ProfileQualificationStore(service.planner.projects)
    process_call_sites: list[tuple[str, str]] = []
    network_calls: list[object] = []
    original_popen = subprocess.Popen
    original_connect = socket.socket.connect

    def observe_process(*args: Any, **kwargs: Any) -> subprocess.Popen[Any]:
        # ``subprocess.run`` is only a convenience wrapper.  Observe the
        # shared child-creation boundary, so an executor using Popen directly
        # cannot bypass this fixture.  Attribute fixed Git reads by their
        # Python call provenance instead of guessing an OS-specific argv form.
        source_frame = next(
            (
                frame
                for frame in inspect.stack()
                if frame.frame.f_globals.get("__name__") == planning_snapshot.__name__
            ),
            None,
        )
        process_call_sites.append(
            (
                planning_snapshot.__name__ if source_frame is not None else "unexpected",
                source_frame.function if source_frame is not None else "unexpected",
            )
        )
        return original_popen(*args, **kwargs)

    def observe_network(sock: socket.socket, address: object) -> object:
        network_calls.append(address)
        return original_connect(sock, address)

    # These are actual OS receiving boundaries for this process.  The factory
    # has no configured Host, Journal, native runtime, model adapter, or output
    # receiver to observe directly; any unexpected child creation is instead
    # caught at Popen, and every network connect is recorded here.
    monkeypatch.setattr(subprocess, "Popen", observe_process)
    monkeypatch.setattr(socket.socket, "connect", observe_network)

    def effect_counts() -> tuple[int, int, int]:
        with sqlite3.connect(service.planner.projects.database) as db:
            qualified = db.execute(
                "SELECT count(*) FROM profile_qualification_records"
            ).fetchone()[0]
        current = service.planner.get(run["id"], principal="owner")
        reservations = service.capacity.snapshot()["reservations"]
        return qualified, len(current["plans"]), len(reservations)

    effects_before = effect_counts()
    frozen = service.freeze_repository_snapshot(
        execution["id"], principal="owner", command_key="freeze"
    )
    assert frozen["repository_identity_sha256"] == project["repository"]["identity_sha256"]
    assert frozen["base_sha"] == configured["repository"]["base_sha"]
    assert frozen["requirement_sha256"] == execution["binding"]["requirement_sha256"]
    assert (
        frozen["authorization_ceiling_sha256"]
        == execution["binding"]["authorization_ceiling_sha256"]
    )
    assert frozen["read_paths_sha256"] == digest(run["authorization_ceiling"]["read_paths"])
    assert [item["mode"] for item in frozen["files"]] == ["100644", "100644"]
    assert service.read_repository_snapshot(execution["id"], principal="owner")["content"] == {
        "src/planning-input.txt": b"registered base bytes\n",
        "tests/planning-input-test.txt": b"registered test base bytes\n",
    }
    source.write_bytes(b"worktree changed\n")
    reopened = PlanningExecution.from_trusted_factory(control)
    assert (
        reopened.freeze_repository_snapshot(
            execution["id"], principal="owner", command_key="freeze"
        )
        == frozen
    )
    assert (
        reopened.read_repository_snapshot(execution["id"], principal="owner")["content"][
            "src/planning-input.txt"
        ]
        == b"registered base bytes\n"
    )
    # Keep using this already-open trusted factory while replacing each private
    # spelling with a compatible repository-controlled copy.  The retained
    # reader must reject before SQLite or blob materialization can follow the
    # alias; restoring the original inode/directory proves the valid replay
    # remains available without repairing or rewriting either copy.
    ledger = snapshot_database(control)
    artifacts = ledger.parent / "planning-repository-snapshot-blobs"
    external_ledger = tmp_path / "repository-controlled-retained-ledger.sqlite"
    shutil.copy2(ledger, external_ledger)
    external_ledger_before = external_ledger.read_bytes()
    held_ledger = ledger.parent / ".retained-ledger"
    ledger.rename(held_ledger)
    ledger.symlink_to(external_ledger)
    for operation in (
        lambda: reopened.read_repository_snapshot(execution["id"], principal="owner"),
        lambda: reopened.freeze_repository_snapshot(
            execution["id"], principal="owner", command_key="freeze"
        ),
    ):
        with pytest.raises(RunError, match="^PLANNING_REPOSITORY_SNAPSHOT_UNAVAILABLE$"):
            operation()
    assert external_ledger.read_bytes() == external_ledger_before
    ledger.unlink()
    held_ledger.rename(ledger)

    external_artifacts = tmp_path / "repository-controlled-retained-blobs"
    shutil.copytree(artifacts, external_artifacts)
    external_blobs_before = {
        item.name: item.read_bytes() for item in external_artifacts.iterdir()
    }
    held_artifacts = artifacts.parent / ".retained-blobs"
    artifacts.rename(held_artifacts)
    artifacts.symlink_to(external_artifacts, target_is_directory=True)
    for operation in (
        lambda: reopened.read_repository_snapshot(execution["id"], principal="owner"),
        lambda: reopened.freeze_repository_snapshot(
            execution["id"], principal="owner", command_key="freeze"
        ),
    ):
        with pytest.raises(RunError, match="^PLANNING_REPOSITORY_SNAPSHOT_UNAVAILABLE$"):
            operation()
    assert {
        item.name: item.read_bytes() for item in external_artifacts.iterdir()
    } == external_blobs_before
    artifacts.unlink()
    held_artifacts.rename(artifacts)
    assert isinstance(reopened.snapshots, PlanningRepositorySnapshotStore)
    external_root = tmp_path / "repository-controlled-retained-private-root"
    shutil.copytree(ledger.parent, external_root)
    external_root_ledger = external_root / ledger.name
    external_root_before = external_root_ledger.read_bytes()
    held_root = tmp_path / "retained-private-root"
    ledger.parent.rename(held_root)
    ledger.parent.symlink_to(external_root, target_is_directory=True)
    for operation in (
        lambda: reopened.snapshots.read(execution["binding"]),
        reopened.snapshots._connect,
    ):
        with pytest.raises(RunError, match="^PLANNING_REPOSITORY_SNAPSHOT_UNAVAILABLE$"):
            operation()
    assert external_root_ledger.read_bytes() == external_root_before
    ledger.parent.unlink()
    held_root.rename(ledger.parent)
    assert reopened.read_repository_snapshot(execution["id"], principal="owner")["content"] == {
        "src/planning-input.txt": b"registered base bytes\n",
        "tests/planning-input-test.txt": b"registered test base bytes\n",
    }
    # A saved freeze-command replay verifies immutable bytes outside the
    # Execution/Run writers.  Cancellation therefore completes while this
    # deliberately slow historical reader is paused, and the original bytes
    # remain available after cancellation.
    assert isinstance(reopened.snapshots, PlanningRepositorySnapshotStore)
    entered = Event()
    release = Event()
    original_read = reopened.snapshots.read

    def slow_read(binding: dict[str, Any]) -> dict[str, Any]:
        entered.set()
        assert release.wait(timeout=5)
        return original_read(binding)

    monkeypatch.setattr(reopened.snapshots, "read", slow_read)
    with ThreadPoolExecutor(max_workers=2) as workers:
        replay = workers.submit(
            reopened.freeze_repository_snapshot,
            execution["id"],
            principal="owner",
            command_key="freeze",
        )
        assert entered.wait(timeout=5)
        cancelled = workers.submit(
            service.cancel, execution["id"], principal="owner", command_key="cancel"
        )
        assert cancelled.result(timeout=2)["cancel_requested"]
        release.set()
        assert replay.result(timeout=5) == frozen
    assert original_read(execution["binding"])["content"] == {
        "src/planning-input.txt": b"registered base bytes\n",
        "tests/planning-input-test.txt": b"registered test base bytes\n",
    }
    assert service.capacity.snapshot() == before
    assert effect_counts() == effects_before
    assert process_call_sites == [
        (planning_snapshot.__name__, "_git")
    ] * (1 + 2 * len(frozen["files"]))
    assert network_calls == []

    ledger = snapshot_database(control)
    artifact = Path(
        ledger.parent / "planning-repository-snapshot-blobs" / frozen["files"][0]["sha256"]
    )
    artifact.write_bytes(b"tampered")
    # A saved public command receipt is not authority to skip immutable-store
    # verification: replay must fail just like an explicit reader call.
    with pytest.raises(RunError, match="^PLANNING_REPOSITORY_SNAPSHOT_CHANGED$"):
        reopened.freeze_repository_snapshot(
            execution["id"], principal="owner", command_key="freeze"
        )
    with pytest.raises(RunError, match="^PLANNING_REPOSITORY_SNAPSHOT_CHANGED$"):
        reopened.read_repository_snapshot(execution["id"], principal="owner")
    with sqlite3.connect(ledger) as db:
        assert artifact.read_bytes() == b"tampered"
        db.execute("UPDATE snapshots SET data=?", ("{}",))
    with pytest.raises(RunError, match="^PLANNING_REPOSITORY_SNAPSHOT_CHANGED$"):
        reopened.freeze_repository_snapshot(
            execution["id"], principal="owner", command_key="freeze"
        )
    with pytest.raises(RunError, match="^PLANNING_REPOSITORY_SNAPSHOT_CHANGED$"):
        reopened.read_repository_snapshot(execution["id"], principal="owner")
    assert service.capacity.snapshot() == before
    assert effect_counts() == effects_before

    ledger.unlink()
    with pytest.raises(RunError, match="^PLANNING_REPOSITORY_SNAPSHOT_UNAVAILABLE$"):
        reopened.freeze_repository_snapshot(
            execution["id"], principal="owner", command_key="freeze"
        )
    # A ledger removed after publication prevents snapshot recovery, but it is
    # not grounds to deny the old controller factory all existing-only reads.
    # The reopened factory exposes no snapshot port and each snapshot operation
    # still fails closed without provisioning a replacement ledger.
    historical = PlanningExecution.from_trusted_factory(control)
    assert historical.snapshots is None
    with pytest.raises(RunError, match="^PLANNING_REPOSITORY_SNAPSHOT_UNAVAILABLE$"):
        historical.freeze_repository_snapshot(
            execution["id"], principal="owner", command_key="freeze"
        )
    with pytest.raises(RunError, match="^PLANNING_REPOSITORY_SNAPSHOT_UNAVAILABLE$"):
        historical.read_repository_snapshot(execution["id"], principal="owner")
    assert not ledger.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="private deployment modes require Linux")
def test_persistent_reader_observes_material_sealed_current_generation(
    configured: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reader uses a real existing CredentialSourceStore, never descriptor text."""
    _, authority, run, execution = _case(tmp_path, configured)
    key = tmp_path / "synthetic-current.key"
    key.write_text("synthetic-current-key\n", encoding="utf-8")
    key.chmod(0o600)
    private = tmp_path / "synthetic-credential-private"
    profile_record = run["configuration_snapshot"]["configuration"]["resources"]["profiles"][0]
    auth_ref = profile_record["profile"]["auth_ref"]
    credentials = CredentialSourceStore(
        authority.planner.projects,
        sources={(run["project_id"], auth_ref): LocalKeyFile("synthetic-current", key)},
        private_directory=private,
        clock=lambda: 1000.0,
    )
    registered = credentials.register(
        run["project_id"], auth_ref, principal="owner", command_key="synthetic-current-register"
    )
    control = tmp_path / "go-source-control"
    control.mkdir(mode=0o700)
    settings = GoTaskSettings(
        control,
        tmp_path,
        tmp_path / "candidates",
        tmp_path / "host",
        tmp_path / "journal.sqlite",
        tmp_path / "qualification",
        tmp_path / "task-work",
        tmp_path / "python",
        tmp_path / "runtime",
        Path(os.environ["KARAJAN_GO_TOKENIZER_DIRECTORY"]).resolve(),
        private,
        tuple(authority.planner.projects.allowed_roots),
        (GoTaskCredentialSource(run["project_id"], auth_ref, "synthetic-current", key),),
    )
    write_go_task_bootstrap(settings)
    monkeypatch.setattr(
        "karajan.orchestration.go_task_runtime.deployment_source",
        lambda _settings, _accounting: {"runtime": "synthetic-observed"},
    )
    persistent_projects = ProjectRegistry(
        authority.planner.projects.database,
        authority.planner.projects.allowed_roots,
        existing_only=True,
    )
    persistent_planner = RunPlanner(
        authority.planner.database, persistent_projects, existing_only=True
    )
    reader = PersistentCommanderQualificationReader(
        persistent_planner,
        ProfileQualificationStore(persistent_projects, commander_reader_only=True),
        control_directory=control,
    )
    registration = profile_record
    with persistent_projects._transaction() as db:
        observed = reader._current_source(
            db, run["project_id"], {"registration": registration}, "owner"
        )
    assert observed["credential_generation"] == registered["generation"]
    assert observed["credential_source"] == registered["source"]
    key.write_text("synthetic-current-key-changed\n", encoding="utf-8")
    with persistent_projects._transaction() as db:
        with pytest.raises(CredentialSourceError, match="^CREDENTIAL_MATERIAL_CHANGED$"):
            reader._current_source(db, run["project_id"], {"registration": registration}, "owner")


@pytest.mark.skipif(sys.platform == "win32", reason="private deployment modes require Linux")
def test_effect_guard_reobserves_material_sealed_commander_source_before_body(
    configured: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A changed sealed key rejects before the effect context yields its body."""
    _, authority, run, execution = _case(tmp_path, configured)
    key = tmp_path / "synthetic-boundary.key"
    key.write_text("synthetic-boundary-key\n", encoding="utf-8")
    key.chmod(0o600)
    private = tmp_path / "synthetic-boundary-private"
    profile_record = run["configuration_snapshot"]["configuration"]["resources"]["profiles"][0]
    auth_ref = profile_record["profile"]["auth_ref"]
    CredentialSourceStore(
        authority.planner.projects,
        sources={(run["project_id"], auth_ref): LocalKeyFile("synthetic-boundary", key)},
        private_directory=private,
        clock=lambda: 1000.0,
    ).register(
        run["project_id"], auth_ref, principal="owner", command_key="synthetic-boundary-register"
    )
    # Initialize the reader ledger before reopening every production dependency
    # in existing-only mode.  The record itself is an explicit test producer;
    # production cannot create it from a caller supplied pass.
    ProfileQualificationStore(authority.planner.projects)
    control = tmp_path / "go-boundary-control"
    control.mkdir(mode=0o700)
    settings = GoTaskSettings(
        control,
        tmp_path,
        tmp_path / "candidates",
        tmp_path / "host",
        tmp_path / "journal.sqlite",
        tmp_path / "qualification",
        tmp_path / "task-work",
        tmp_path / "python",
        tmp_path / "runtime",
        Path(os.environ["KARAJAN_GO_TOKENIZER_DIRECTORY"]).resolve(),
        private,
        tuple(authority.planner.projects.allowed_roots),
        (GoTaskCredentialSource(run["project_id"], auth_ref, "synthetic-boundary", key),),
    )
    write_go_task_bootstrap(settings)
    monkeypatch.setattr(
        "karajan.orchestration.go_task_runtime.deployment_source",
        lambda _settings, _accounting: {"runtime": "synthetic-boundary-observed"},
    )
    projects = ProjectRegistry(
        authority.planner.projects.database,
        authority.planner.projects.allowed_roots,
        existing_only=True,
    )
    planner = RunPlanner(authority.planner.database, projects, existing_only=True)
    qualifications = ProfileQualificationStore(projects, commander_reader_only=True)
    reader = PersistentCommanderQualificationReader(
        planner, qualifications, control_directory=control
    )
    qualifications.commander_source = reader._current_source
    with projects._transaction() as db:
        source = reader._current_source(
            db, run["project_id"], {"registration": profile_record}, "owner"
        )
    with qualifications._owned(run["project_id"], "owner") as db:
        bound = qualifications._binding(
            db,
            run["project_id"],
            {"id": profile_record["id"], "revision": profile_record["revision"]},
        )
        start = {
            "qualification_scope": COMMANDER_QUALIFICATION_SCOPE,
            "profile_binding": bound,
            "source": source,
            "execution_start": {"synthetic": "capacity-boundary"},
        }
        facts = authority.qualifications.read_commander(
            execution["binding"],
            scope=COMMANDER_QUALIFICATION_SCOPE,
            reader_version="karajan.commander-qualification-reader.v1",
        )
        assert facts is not None
        record = {
            "id": "synthetic-capacity-boundary",
            "binding": start,
            "qualification_scope": COMMANDER_QUALIFICATION_SCOPE,
            "status": "passed",
            "provenance": "official",
            "observed_at": 1000.0,
            "valid_until": facts["valid_until"],
            "commander_facts": {
                "profile_facts": facts["profile_facts"],
                "capability_evidence": facts["capability_evidence"],
                "source_generation_sha256": digest(source),
            },
        }
        db.execute(
            "INSERT INTO profile_qualification_starts VALUES (?,?,?,?,?,?)",
            (
                record["id"],
                run["project_id"],
                "owner",
                "synthetic-boundary",
                record["id"],
                json.dumps(start),
            ),
        )
        db.execute(
            "INSERT INTO profile_qualification_start_seals VALUES (?,?)",
            (record["id"], digest(start)),
        )
        db.execute(
            "INSERT INTO profile_qualification_records VALUES (?,?,?)",
            (record["id"], json.dumps(record), digest(record)),
        )
    authority.qualifications = reader
    assert (
        authority.advance(execution["id"], "owner", "synthetic-boundary-advance")["phase"]
        == "admitted"
    )
    original = authority.capacity.pre_effect_guard

    @contextmanager
    def mutate_key_while_capacity_is_held(
        admission_id: str,
        *,
        expected_request: dict[str, Any],
        before_effect: Callable[[], None] | None = None,
        after_capacity_facts: Callable[[Any], None] | None = None,
        before_effect_yield: Callable[[], None] | None = None,
    ) -> Any:
        # This wrapper is reached after #111 has retained the Run/Project
        # guards and before the real Capacity callback invokes its source
        # recheck. The context body must remain unreachable.
        key.write_text("synthetic-boundary-key-changed\n", encoding="utf-8")
        with original(
            admission_id,
            expected_request=expected_request,
            before_effect=before_effect,
            after_capacity_facts=after_capacity_facts,
            before_effect_yield=before_effect_yield,
        ) as capacity:
            yield capacity

    monkeypatch.setattr(authority.capacity, "pre_effect_guard", mutate_key_while_capacity_is_held)
    entered = False
    with pytest.raises(RunError, match="^COMMANDER_QUALIFICATION_CHANGED$"):
        with authority.effect_guard(execution["id"], "owner", "synthetic-boundary-effect"):
            entered = True
    assert not entered


@pytest.mark.skipif(sys.platform == "win32", reason="private deployment modes require Linux")
def test_persistent_factory_rebuilds_production_reader_and_reserves_nothing_without_commander(
    configured: dict, tmp_path: Path
) -> None:
    _, authority, _, execution = _case(tmp_path, configured)
    control = _protected_factory_control(tmp_path, authority)
    provision_planning_repository_snapshots(control)
    service = PlanningExecution.from_trusted_factory(control)
    assert service.admissions is not None
    production = service.admissions.advance(execution["id"], "owner", "factory-admit")
    assert production["phase"] == "denied"
    assert production["reason_codes"] == ["COMMANDER_QUALIFICATION_REQUIRED"]
    assert service.capacity is not None
    assert service.capacity.snapshot()["reservations"] == []


@pytest.mark.skipif(sys.platform == "win32", reason="private deployment modes require Linux")
def test_persistent_factory_requires_complete_existing_store_set(
    configured: dict, tmp_path: Path
) -> None:
    _, authority, _, _ = _case(tmp_path, configured)
    control = _protected_factory_control(tmp_path, authority)
    missing = tmp_path / "protected-state" / "planning-execution.sqlite"
    missing.unlink()
    with pytest.raises(RunError, match="^PLANNING_ADMISSION_BOOTSTRAP_INVALID$"):
        PlanningExecution.from_trusted_factory(control)
    assert not missing.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="private deployment modes require Linux")
def test_persistent_factory_rejects_run_database_alias(configured: dict, tmp_path: Path) -> None:
    _, authority, _, _ = _case(tmp_path, configured)
    control = _protected_factory_control(tmp_path, authority)
    state = tmp_path / "protected-state"
    target = tmp_path / "repository-controlled-runs.sqlite"
    shutil.copy2(state / "runs.sqlite", target)
    (state / "runs.sqlite").unlink()
    (state / "runs.sqlite").symlink_to(target)
    with pytest.raises(RunError, match="^PLANNING_ADMISSION_BOOTSTRAP_INVALID$"):
        PlanningExecution.from_trusted_factory(control)


@pytest.mark.skipif(sys.platform == "win32", reason="private deployment modes require Linux")
@pytest.mark.parametrize("alias", ["symlink", "hardlink"])
def test_persistent_factory_rejects_snapshot_ledger_alias_without_writes(
    configured: dict, tmp_path: Path, alias: str
) -> None:
    _, authority, _, _ = _case(tmp_path, configured)
    control = _protected_factory_control(tmp_path, authority)
    ledger = provision_planning_repository_snapshots(control)
    state = ledger.parent
    external = tmp_path / "repository-controlled-snapshot.sqlite"
    shutil.copy2(ledger, external)
    if alias == "symlink":
        ledger.unlink()
        ledger.symlink_to(external)
    else:
        # Keep the protected spelling as a two-link inode: it is just as
        # unsuitable as a symlink even though SQLite can open it.
        ledger.unlink()
        os.link(external, ledger)
    before = external.read_bytes()
    with pytest.raises(RunError, match="^PLANNING_REPOSITORY_SNAPSHOT_UNAVAILABLE$"):
        PlanningExecution.from_trusted_factory(control)
    assert external.read_bytes() == before
    assert not list((state / "planning-repository-snapshot-blobs").iterdir())


@pytest.mark.skipif(sys.platform == "win32", reason="private deployment modes require Linux")
def test_fixture_admission_cannot_be_relabelled_after_production_reopen(
    configured: dict, tmp_path: Path
) -> None:
    _, authority, _, execution = _case(tmp_path, configured)
    admitted = authority.advance(execution["id"], "owner", "fixture-admit")
    assert admitted["phase"] == "admitted"
    assert authority.read_admission(execution["binding"])["authority_kind"] == "fixture"
    control = _protected_factory_control(tmp_path, authority)
    provision_planning_repository_snapshots(control)
    production = PlanningExecution.from_trusted_factory(control)
    assert production.admissions is not None
    with pytest.raises(RunError, match="PLANNING_ADMISSION_PROVENANCE_FORBIDDEN"):
        production.admissions.advance(execution["id"], "owner", "production-admit")
