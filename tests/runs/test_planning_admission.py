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

import httpx
import karajan.capacity.store as capacity_store
import karajan.orchestration.planning_admission as planning_admission
import karajan.orchestration.planning_snapshot as planning_snapshot
import pytest
from karajan.adapters.opencode.go_context import GoRequestAccounting
from karajan.adapters.opencode.go_journal import GoCallJournal
from karajan.adapters.opencode.go_relay import GoRelay
from karajan.capacity import CapacityError, CapacityStore
from karajan.orchestration.go_commander_qualification import (
    CommanderCredentialSource,
    CommanderQualificationSettings,
    write_commander_qualification_settings,
)
from karajan.orchestration.planning_admission import (
    COMMANDER_QUALIFICATION_SCOPE,
    PersistentCommanderQualificationReader,
    PlanningAdmissionAuthority,
)
from karajan.orchestration.planning_bootstrap import PLANNING_ADMISSION_BOOTSTRAP
from karajan.orchestration.planning_execution import PlanningExecution
from karajan.orchestration.planning_input import PlanningModelInput
from karajan.orchestration.planning_snapshot import (
    PlanningRepositorySnapshotStore,
    provision_planning_repository_snapshots,
    snapshot_database,
)
from karajan.orchestration.planning_transport import (
    PlanningOutputStore,
    PlanningTransport,
    ProductionGoPlanningProducer,
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


def _prepared_runtime() -> Path:
    configured = os.environ.get("KARAJAN_OPENCODE_LINUX_BINARY") or os.environ.get(
        "KARAJAN_GO_RUNTIME"
    )
    runtime = Path(configured) if configured else (
        Path(__file__).resolve().parents[2]
        / "runtimes/opencode/node_modules/opencode-linux-x64/bin/opencode"
    )
    if not runtime.is_file():
        if os.environ.get("KARAJAN_REQUIRE_OPENCODE_ISOLATION") == "1":
            pytest.fail("Prepared fixed Linux OpenCode artifact is required")
        pytest.skip("Prepared Linux OpenCode artifact is not available")
    return runtime


def _prepared_tokenizer() -> Path:
    configured = os.environ.get("KARAJAN_GO_TOKENIZER_DIRECTORY")
    tokenizer = Path(configured) if configured else Path(".cache/go-context-artifacts")
    tokenizer = tokenizer.resolve()
    if not tokenizer.is_dir():
        if os.environ.get("KARAJAN_REQUIRE_GO_TOKENIZER") == "1":
            pytest.fail("Prepared Go tokenizer artifacts are required")
        pytest.skip("Prepared Go tokenizer artifacts are not available")
    return tokenizer


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
    observation_at: float = 1000.0,
    observation_max_age_seconds: int = 30,
    conservative_observation_max_age_seconds: int = 30,
    max_attempt_duration_seconds: int = 60,
    unit: str = "percent",
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
                "unit": unit,
                "window_kind": "fixed",
            },
            command_key="pool-" + pool,
        )
        store.observe(
            {
                "pool_id": pool,
                "window_id": window,
                "observed_at": observation_at,
                "reset_at": observation_at + 1000.0,
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


def _native_v2_policy(configured: dict) -> dict:
    policy = policy_request(configured)
    policy.update(
        schema_version="karajan.execution-policy.v2",
        max_context_tokens=16384,
        context_policy={
            **policy["context_policy"],
            "measurement": {
                "method": "reference_tokenizer_estimate",
                "source_sha256": "a" * 64,
                "fixed_margin": 2048,
                "ratio_margin_basis_points": 1000,
            },
        },
        validation={
            "id": "native-production-transport-validation",
            "revision": 1,
            "checks": [
                {
                    "id": "tests",
                    "revision": 1,
                    "argv": ["python", "-m", "pytest"],
                    "environment_ref": {"id": "offline", "revision": 1},
                    "timeout_seconds": 60,
                }
            ],
            "environments": [
                {
                    "id": "offline",
                    "revision": 1,
                    "runtime_kind": "isolated-command",
                    "platform": "linux_x64",
                    "source_sha256": "b" * 64,
                    "filesystem": "candidate_copy",
                    "network": "none",
                    "env": {},
                    "max_log_bytes": 65536,
                }
            ],
            "review": {
                "id": "independent_review",
                "revision": 1,
                "environment_ref": {"id": "offline", "revision": 1},
                "context_policy": "candidate_and_acceptance_only",
                "independence_policy": "existing_candidate_independence_v1",
            },
        },
    )
    return policy


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
    paths: list[str] | None = None,
    execution_policy: dict | None = None,
    observation_at: float = 1000.0,
    capacity_unit: str = "percent",
) -> tuple[PlanningExecution, PlanningAdmissionAuthority, dict, Any]:
    planner = RunPlanner(tmp_path / "runs.sqlite", configured["registry"], clock=clock or time.time)
    fixed = configured["registry"].register_execution_policy(
        configured["id"],
        policy_request(configured) if execution_policy is None else execution_policy,
        command_key="policy",
        principal="owner",
    )
    request = request_v2(configured, fixed)
    if paths is not None:
        request["authorization"].update(read_paths=paths, write_paths=paths)
    run = planner.create(request, command_key="run", principal="owner")
    assert run["plans"] == []
    intent = planner.planning_intent(run["id"], term=1, command_key="intent-1", principal="lead")
    execution = PlanningExecution(tmp_path / "planning.sqlite", planner).begin(
        run["id"], intent["id"], principal="owner", command_key="begin-1"
    )
    capacity = planning_capacity(
        tmp_path / "capacity",
        clock=clock or (lambda: 1000.0),
        observation_at=observation_at,
        observation_max_age_seconds=observation_max_age_seconds,
        conservative_observation_max_age_seconds=conservative_observation_max_age_seconds,
        max_attempt_duration_seconds=max_attempt_duration_seconds,
        unit=capacity_unit,
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
                "context_tokens": fixed["max_context_tokens"],
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


class _LocalPlanningCredential:
    def reveal(self) -> str:
        return "local-planning-fixture-secret"


class _LocalPlanningCredentials:
    def current(self, project_id: str, auth_ref: str, *, principal: str) -> dict[str, Any]:
        del project_id, auth_ref, principal
        return {"generation": "local-fixture-generation", "source": {"id": "fixture"}}

    def resolve_exact(
        self, project_id: str, auth_ref: str, generation: str, *, principal: str
    ) -> _LocalPlanningCredential:
        del project_id, auth_ref, generation, principal
        return _LocalPlanningCredential()


def _native_model_input(accounting: GoRequestAccounting) -> PlanningModelInput:
    request = {
        "model": "glm-5.3-flash",
        "stream": True,
        "max_tokens": 1024,
        "messages": [
            {"role": "system", "content": "Return only JSON."},
            {"role": "user", "content": "Return one plan."},
        ],
        "reasoning_effort": "max",
        "clear_thinking": False,
    }
    payload = json.dumps(request, separators=(",", ":")).encode()
    return PlanningModelInput.model_construct(
        request=request,
        request_bytes=payload,
        artifact_bytes=payload,
        artifact_sha256="a" * 64,
        accounting_source=accounting.source(),
        execution_policy={
            "max_context_tokens": 7168,
            "context_policy": {"reserved_output_tokens": 1024},
        },
    )


def _local_response() -> httpx.Response:
    body = {
        "model": "glm-5.3-flash",
        "choices": [
            {
                "index": 0,
                "delta": {"content": '{"summary":"native"}', "tool_calls": None},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 20, "completion_tokens": 2, "total_tokens": 22},
    }
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=("data: " + json.dumps(body) + "\n\ndata: [DONE]\n\n").encode(),
    )


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


@pytest.mark.skipif(sys.platform != "linux", reason="native planning requires Linux namespaces")
def test_production_native_send_releases_guard_for_cancelled_wait(
    configured: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reopened production transport stops its original owned native process.

    The test-only persistent qualification and credential are installed before
    either transport is opened.  The worker and both controllers then use only
    the same trusted factory state; no live SQLite store is copied on reopen.
    """
    control, _, run, execution = _persistent_production_transport_case(tmp_path, configured)
    entered, release, stopped = Event(), Event(), Event()
    requests: list[dict[str, Any]] = []

    from karajan.isolation.opencode_runtime import IsolatedOpenCode

    original_close = IsolatedOpenCode.close

    def observed_close(native: IsolatedOpenCode) -> dict[str, Any]:
        result = original_close(native)
        stopped.set()
        return result

    monkeypatch.setattr(IsolatedOpenCode, "close", observed_close)

    class DelayedResponse(httpx.SyncByteStream):
        def __iter__(self) -> Any:
            entered.set()
            assert release.wait(timeout=10)
            yield _local_response().content

    def upstream(request: httpx.Request) -> httpx.Response:
        # MockTransport calls this synchronously while client.stream opens.
        # Delay body iteration instead, which is the real response wait after
        # the relay has released its one send guard.
        requests.append(json.loads(request.content))
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=DelayedResponse()
        )

    class LocalRelay(GoRelay):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["client_factory"] = lambda: httpx.Client(
                transport=httpx.MockTransport(upstream), trust_env=False
            )
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("karajan.orchestration.planning_transport.GoRelay", LocalRelay)
    transport = PlanningTransport.from_trusted_factory(control)

    def execute() -> dict[str, Any]:
        # This release belongs to the worker from its first instruction.  An
        # early factory/transport failure therefore cannot be hidden by the
        # executor context manager waiting on DelayedResponse.
        try:
            return transport.execute(execution["id"], principal="owner", command_key="execute")
        finally:
            release.set()

    with ThreadPoolExecutor(max_workers=1) as workers:
        pending = workers.submit(execute)
        try:
            if not entered.wait(timeout=20):
                assert pending.done(), "relay never crossed its production send guard"
                pending.result()
            reopened = PlanningTransport.from_trusted_factory(control)
            assert reopened.execution is not transport.execution
            assert reopened.execution._native_stoppers == {}
            cancelled_at = time.monotonic()
            cancelled = reopened.execution.cancel(
                execution["id"], principal="owner", command_key="cancel-wait"
            )
            assert time.monotonic() - cancelled_at < 2
            assert cancelled["state"] == "cancelled"
            assert stopped.wait(timeout=2)
            assert cancelled["native_cleanup"]["local_stop"] == "confirmed"
            assert cancelled["provider_remote_stop"] == "unknown"
            with pytest.raises(RunError) as failed:
                pending.result(timeout=30)
            assert failed.value.code in {
                "PLANNING_EXECUTION_CANCELLED",
                "PLANNING_EFFECT_NOT_ADMITTED",
                "PLANNING_NATIVE_TIMEOUT",
            }
        finally:
            release.set()
    assert len(requests) == 1
    assert reopened.execution.planner.get(run["id"], principal="owner")["plans"] == []


@pytest.mark.skipif(sys.platform != "linux", reason="native planning requires Linux namespaces")
def test_production_transport_consumes_registered_budget_and_publishes_one_plan(
    configured: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One C/P transport operation carries trusted admission through native publication.

    The Commander qualification and credential below are explicit, private test
    fixtures.  The transport, controller estimate, admission receipt, producer,
    Relay, Journal, output authority, and Run submit path remain production code.
    """
    control, service, run, execution = _persistent_production_transport_case(
        tmp_path, configured
    )
    plan = submit_request(
        service.planner.get(run["id"], principal="owner"),
        service.planner.get(run["id"], principal="owner")["planning_intents"][0],
    )["plan"]
    for task in plan["tasks"]:
        task["paths"] = ["original.txt"]

    class LocalRelay(GoRelay):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            body = {
                "model": "glm-5.3-flash",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "content": json.dumps(plan, separators=(",", ":")),
                            "tool_calls": None,
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 20, "completion_tokens": 2, "total_tokens": 22},
            }
            kwargs["client_factory"] = lambda: httpx.Client(
                transport=httpx.MockTransport(
                    lambda _request: httpx.Response(
                        200,
                        headers={"content-type": "text/event-stream"},
                        content=("data: " + json.dumps(body) + "\n\ndata: [DONE]\n\n").encode(),
                    )
                ),
                trust_env=False,
            )
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("karajan.orchestration.planning_transport.GoRelay", LocalRelay)
    transport = PlanningTransport.from_trusted_factory(control)
    result = transport.execute(execution["id"], principal="owner", command_key="execute")

    assert result["state"] == "submitted", result["reason_codes"]
    admitted = transport.execution.admissions.read_admission(execution["binding"])
    assert admitted["state"] == "admitted"
    assert admitted["duration_seconds"] == 25
    persisted = transport.execution.planner.get(run["id"], principal="owner")
    assert persisted["plans"] and persisted["plans"][0]["plan"] == plan


@pytest.mark.skipif(sys.platform != "linux", reason="private deployment modes require Linux")
def test_production_submission_rechecks_public_credential_revoke_before_plan_insert(
    configured: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A public revoke that wins after source read leaves no Plan behind."""
    control, _, run, execution = _persistent_production_transport_case(tmp_path, configured)
    transport = PlanningTransport.from_trusted_factory(control)
    persisted = transport.execution.planner.get(run["id"], principal="owner")
    intent = persisted["planning_intents"][0]
    plan = submit_request(persisted, intent)["plan"]
    for task in plan["tasks"]:
        task["paths"] = ["original.txt"]
    monkeypatch.setattr(
        ProductionGoPlanningProducer,
        "produce",
        lambda *args, **kwargs: json.dumps(plan, separators=(",", ":")).encode(),
    )

    assert isinstance(transport.producer, ProductionGoPlanningProducer)
    credentials = transport.producer.credentials
    profile = persisted["configuration_snapshot"]["configuration"]["resources"]["profiles"][0]
    auth_ref = profile["profile"]["auth_ref"]
    generation = credentials.current(run["project_id"], auth_ref, principal="owner")["generation"]
    source_read, release = Event(), Event()
    original_read_source = transport.outputs.read_source

    def delayed_final_source(binding: dict[str, Any]) -> dict[str, Any]:
        source = original_read_source(binding)
        if (
            transport.execution.get(execution["id"], principal="owner")["state"]
            == "submit_claimed"
        ):
            source_read.set()
            assert release.wait(30), "submission source read did not resume"
        return source

    monkeypatch.setattr(transport.outputs, "read_source", delayed_final_source)

    def execute() -> dict[str, Any]:
        try:
            return transport.execute(execution["id"], principal="owner", command_key="execute")
        finally:
            release.set()

    with ThreadPoolExecutor(max_workers=1) as workers:
        pending = workers.submit(execute)
        assert source_read.wait(30), "transport did not reach its final source read"
        revoked = credentials.revoke(
            run["project_id"],
            auth_ref,
            generation,
            principal="owner",
            command_key="revoke-before-plan",
        )
        assert revoked["revoked"] is True
        release.set()
        result = pending.result(timeout=30)

    assert result["state"] == "blocked"
    assert result["reason_codes"] == ["PLANNING_OUTPUT_SOURCE_CHANGED"]
    assert transport.execution.planner.get(run["id"], principal="owner")["plans"] == []


@pytest.mark.skipif(sys.platform != "linux", reason="private deployment modes require Linux")
def test_production_submission_holds_public_credential_authority_through_plan_commit(
    configured: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A revoke arriving after the source guard waits for the retained Plan fact."""
    control, _, run, execution = _persistent_production_transport_case(tmp_path, configured)
    transport = PlanningTransport.from_trusted_factory(control)
    persisted = transport.execution.planner.get(run["id"], principal="owner")
    intent = persisted["planning_intents"][0]
    plan = submit_request(persisted, intent)["plan"]
    for task in plan["tasks"]:
        task["paths"] = ["original.txt"]
    monkeypatch.setattr(
        ProductionGoPlanningProducer,
        "produce",
        lambda *args, **kwargs: json.dumps(plan, separators=(",", ":")).encode(),
    )

    assert isinstance(transport.producer, ProductionGoPlanningProducer)
    credentials = transport.producer.credentials
    profile = persisted["configuration_snapshot"]["configuration"]["resources"]["profiles"][0]
    auth_ref = profile["profile"]["auth_ref"]
    generation = credentials.current(run["project_id"], auth_ref, principal="owner")["generation"]
    original_save = transport.execution.planner._save
    revocations: list[Any] = []
    workers = ThreadPoolExecutor(max_workers=1)

    def save_after_source_guard(db: sqlite3.Connection, value: dict[str, Any]) -> None:
        if value["plans"] and not revocations:
            revocations.append(
                workers.submit(
                    credentials.revoke,
                    run["project_id"],
                    auth_ref,
                    generation,
                    principal="owner",
                    command_key="revoke-after-plan-linearization",
                )
            )
            assert not revocations[0].done(), "revoke crossed the held Project source guard"
        original_save(db, value)

    monkeypatch.setattr(transport.execution.planner, "_save", save_after_source_guard)
    try:
        result = transport.execute(execution["id"], principal="owner", command_key="execute")
        assert revocations, result
        revoked = revocations[0].result(timeout=5)
    finally:
        workers.shutdown(wait=True)

    assert result["state"] == "submitted"
    assert revoked["revoked"] is True
    assert transport.execution.planner.get(run["id"], principal="owner")["plans"] == [
        result["submission"]
    ]


@pytest.mark.skipif(sys.platform != "linux", reason="private deployment modes require Linux")
def test_transport_dispatch_claimant_produces_when_other_caller_pauses_after_admission(
    configured: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The caller that wins a real dispatch claim cannot return without producing."""
    control, _, run, execution = _persistent_production_transport_case(tmp_path, configured)
    first = PlanningTransport.from_trusted_factory(control)
    second = PlanningTransport.from_trusted_factory(control)
    persisted = first.execution.planner.get(run["id"], principal="owner")
    plan = submit_request(persisted, persisted["planning_intents"][0])["plan"]
    for task in plan["tasks"]:
        task["paths"] = ["original.txt"]
    sends: list[dict[str, Any]] = []

    def produce_once(*args: Any, **kwargs: Any) -> bytes:
        sends.append({"binding": kwargs["binding"], "admission": kwargs["admission"]})
        return json.dumps(plan, separators=(",", ":")).encode()

    monkeypatch.setattr(ProductionGoPlanningProducer, "produce", produce_once)
    admitted, release = Event(), Event()
    original_admit = PlanningExecution.admit

    def pause_first_after_admission(
        controller: PlanningExecution, *args: Any, **kwargs: Any
    ) -> dict[str, Any]:
        result = original_admit(controller, *args, **kwargs)
        if controller is first.execution and result["state"] == "awaiting_output":
            admitted.set()
            assert release.wait(30), "first transport did not resume after admission"
        return result

    monkeypatch.setattr(PlanningExecution, "admit", pause_first_after_admission)

    def execute_first() -> dict[str, Any]:
        try:
            return first.execute(execution["id"], principal="owner", command_key="execute")
        finally:
            release.set()

    with ThreadPoolExecutor(max_workers=1) as workers:
        pending = workers.submit(execute_first)
        assert admitted.wait(30), "first transport did not reach awaiting_output"
        claimed = second.execute(execution["id"], principal="owner", command_key="execute")
        release.set()
        replay = pending.result(timeout=30)

    assert claimed["state"] == "submitted"
    assert replay["state"] == "submitted"
    assert len(sends) == 1
    assert first.execution.planner.get(run["id"], principal="owner")["plans"] == [
        claimed["submission"]
    ]


def test_transport_keeps_admission_unknown_without_activation_receipt(
    configured: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absent original receipts remain unknown and never acquire a replacement."""
    _, authority, run, execution = _case(tmp_path, configured)
    outputs = PlanningOutputStore(tmp_path / "unknown-output.sqlite", authority_kind="fixture")
    controller = PlanningExecution(
        authority.execution_database,
        authority.planner,
        admissions=authority,
        outputs=outputs,
        capacity=authority.capacity,
        allow_fixture_authorities=True,
    )
    calls: list[str] = []

    class NoProducer:
        authority_kind = "fixture"

        def source(self, binding: dict[str, Any]) -> dict[str, Any]:
            del binding
            calls.append("source")
            raise AssertionError("unknown receipt recovery must not arm output")

        def produce(self, *args: Any, **kwargs: Any) -> bytes:
            del args, kwargs
            calls.append("produce")
            raise AssertionError("unknown receipt recovery must not produce")

    class NoAccounting:
        def source(self) -> dict[str, Any]:
            raise AssertionError("unknown receipt recovery must not compile input")

    transport = PlanningTransport(controller, NoAccounting(), NoProducer(), outputs)  # type: ignore[arg-type]
    def lose_capacity_reply(*args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        raise CapacityError("activation receipt unavailable")

    monkeypatch.setattr(authority.capacity, "activate", lose_capacity_reply)
    record = authority.advance(execution["id"], "owner", "original-admit")
    assert record["phase"] == "capacity_activate_unknown"

    # Record the ledger's original unknown receipt on the execution first;
    # the Transport calls below are the regression subject, not a synthetic
    # state injection.
    assert controller.reconcile(execution["id"], principal="owner")["state"] == "admission_unknown"
    before_capacity = authority.capacity.snapshot()
    first = transport.execute(execution["id"], principal="owner", command_key="execute")
    repeated = transport.execute(execution["id"], principal="owner", command_key="execute")

    assert first["state"] == repeated["state"] == "admission_unknown"
    assert first["reason_codes"] == repeated["reason_codes"] == ["PLANNING_ADMISSION_UNKNOWN"]
    assert controller.get(execution["id"], principal="owner")["state"] == "admission_unknown"
    assert authority.capacity.snapshot() == before_capacity
    assert calls == []
    with sqlite3.connect(outputs.database) as db:
        assert {
            table: db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
            for table in (
                "planning_execute_commands",
                "planning_output_sources",
                "planning_output_claims",
                "planning_outputs",
            )
        } == {
            # The caller's fixed identity is allowed; no output authority is
            # armed or dispatched while the original receipt is absent.
            "planning_execute_commands": 1,
            "planning_output_sources": 0,
            "planning_output_claims": 0,
            "planning_outputs": 0,
        }
    assert controller.planner.get(run["id"], principal="owner")["plans"] == []


@pytest.mark.skipif(sys.platform != "linux", reason="native planning requires Linux namespaces")
@pytest.mark.parametrize("reply_error", [RuntimeError, CapacityError])
def test_production_transport_recovers_unknown_from_committed_activation_receipt(
    configured: dict,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reply_error: type[Exception],
) -> None:
    """The fixed admission command recovers only its already committed receipt."""
    control, _, run, execution = _persistent_production_transport_case(tmp_path, configured)
    transport = PlanningTransport.from_trusted_factory(control)
    persisted = transport.execution.planner.get(run["id"], principal="owner")
    plan = submit_request(persisted, persisted["planning_intents"][0])["plan"]
    for task in plan["tasks"]:
        task["paths"] = ["original.txt"]
    sends: list[dict[str, Any]] = []

    def produce_once(*args: Any, **kwargs: Any) -> bytes:
        sends.append({"binding": kwargs["binding"], "admission": kwargs["admission"]})
        return json.dumps(plan, separators=(",", ":")).encode()

    monkeypatch.setattr(ProductionGoPlanningProducer, "produce", produce_once)
    assert isinstance(transport.execution.admissions, PlanningAdmissionAuthority)
    authority = transport.execution.admissions
    original_advance = authority.advance
    advance_keys: list[str] = []
    original_recover = authority.recover_original_receipt
    recovery_keys: list[str] = []

    def record_advance(execution_id: str, principal: str, command_key: str) -> dict[str, Any]:
        advance_keys.append(command_key)
        return original_advance(execution_id, principal, command_key)

    def record_recovery(execution_id: str, principal: str, command_key: str) -> dict[str, Any]:
        recovery_keys.append(command_key)
        return original_recover(execution_id, principal, command_key)

    monkeypatch.setattr(authority, "advance", record_advance)
    monkeypatch.setattr(authority, "recover_original_receipt", record_recovery)
    original_activate = transport.execution.capacity.activate
    activations: list[dict[str, Any]] = []

    def commit_then_lose_reply(*args: Any, **kwargs: Any) -> dict[str, Any]:
        receipt = original_activate(*args, **kwargs)
        activations.append(receipt)
        raise reply_error("activation reply lost")

    monkeypatch.setattr(transport.execution.capacity, "activate", commit_then_lose_reply)
    initial = transport.execute(execution["id"], principal="owner", command_key="execute")
    assert initial["state"] == "admission_unknown"
    unknown = transport.execution.get(execution["id"], principal="owner")
    assert unknown["state"] == "admission_unknown"
    assert len(activations) == 1
    capacity_before = transport.execution.capacity.snapshot()
    assert len(capacity_before["reservations"]) == 1
    original_binding = deepcopy(unknown["binding"])
    original_admission = deepcopy(unknown["admission"])
    original_source = transport.outputs.read_source(original_binding)

    def activation_must_not_repeat(*args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        raise AssertionError("receipt recovery must not activate Capacity again")

    monkeypatch.setattr(transport.execution.capacity, "activate", activation_must_not_repeat)
    recovered = transport.execute(execution["id"], principal="owner", command_key="execute")

    assert recovered["state"] == "submitted", recovered["reason_codes"]
    assert advance_keys == ["planning-admit:" + execution["id"]]
    assert recovery_keys == ["planning-admit:" + execution["id"]]
    assert len(sends) == 1
    restored = transport.execution.get(execution["id"], principal="owner")
    assert restored["binding"] == original_binding
    assert restored["output_source_sha256"] == original_source["source_sha256"]
    assert transport.outputs.read_source(restored["binding"]) == original_source
    assert restored["admission"]["capacity_request"] == original_admission["capacity_request"]
    assert (
        restored["admission"]["capacity_command_key"]
        == original_admission["capacity_command_key"]
    )
    assert (
        restored["admission"]["capacity_activation_command_key"]
        == original_admission["capacity_activation_command_key"]
    )
    assert (
        restored["admission"]["capacity_receipt"]["admission_id"]
        == activations[0]["admission_id"]
    )
    assert transport.execution.capacity.snapshot() == capacity_before
    assert transport.execution.planner.get(run["id"], principal="owner")["plans"] == [
        recovered["submission"]
    ]
    with sqlite3.connect(transport.execution.admissions.database) as db:
        assert db.execute("SELECT COUNT(*) FROM planning_estimates").fetchone()[0] == 1
    with sqlite3.connect(tmp_path / "protected-state" / "planning-output.sqlite") as db:
        assert {
            table: db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
            for table in (
                "planning_execute_commands",
                "planning_output_sources",
                "planning_output_claims",
                "planning_outputs",
            )
        } == {
            "planning_execute_commands": 1,
            "planning_output_sources": 1,
            "planning_output_claims": 1,
            "planning_outputs": 1,
        }


@pytest.mark.skipif(sys.platform != "linux", reason="native planning requires Linux namespaces")
@pytest.mark.parametrize("failure", ["journal", "cleanup"])
def test_production_native_output_requires_durable_completion_and_cleanup(
    configured: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """Delivered relay bytes never become output when durable proof is incomplete."""
    runtime = _prepared_runtime()
    tokenizer = _prepared_tokenizer()
    service, authority, run, execution = _case(tmp_path, configured)
    service.admissions = authority
    admitted = authority.advance(execution["id"], "owner", "native-admit")
    assert admitted["phase"] == "admitted", repr(admitted["reason_codes"])
    accounting = GoRequestAccounting(Path(tokenizer))
    journal = GoCallJournal(tmp_path / "production-journal.sqlite")

    class LocalRelay(GoRelay):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["client_factory"] = lambda: httpx.Client(
                transport=httpx.MockTransport(lambda _request: _local_response()), trust_env=False
            )
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("karajan.orchestration.planning_transport.GoRelay", LocalRelay)
    if failure == "journal":
        monkeypatch.setattr(
            journal,
            "complete_call",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("journal failed")),
        )
    else:
        from karajan.isolation.opencode_runtime import IsolatedOpenCode

        original_close = IsolatedOpenCode.close

        def incomplete_close(native: IsolatedOpenCode) -> dict[str, Any]:
            original_close(native)
            return {"local_stop": "unknown"}

        monkeypatch.setattr(IsolatedOpenCode, "close", incomplete_close)
    producer = ProductionGoPlanningProducer(
        service,
        accounting,
        journal,
        runtime,
        tmp_path / "native-work",
        _LocalPlanningCredentials(),
        "b" * 64,
    )
    native_admission = authority.read_admission(execution["binding"])

    expected = (
        "PLANNING_NATIVE_COMPLETION_UNKNOWN"
        if failure == "journal"
        else "PLANNING_NATIVE_LOG_EVIDENCE_UNAVAILABLE"
    )
    with pytest.raises(RunError, match=rf"^{expected}$"):
        producer.produce(
            _native_model_input(
                accounting
            ), binding=execution["binding"], admission=native_admission
        )
    snapshot = journal.snapshot("planning-native-" + execution["id"])
    if failure == "journal":
        assert snapshot["calls"][0]["state"] == "send_unknown"
    assert service.planner.get(run["id"], principal="owner")["plans"] == []


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


def test_missing_commander_fact_precedes_missing_estimate_without_reservation(
    configured: dict, tmp_path: Path
) -> None:
    _, authority, _, execution = _case(tmp_path, configured, register_estimate=False)
    authority.authority_kind = "production"

    denied = authority.advance(execution["id"], "owner", "qualification-before-estimate")

    assert denied["phase"] == "denied"
    assert denied["reason_codes"] == ["COMMANDER_QUALIFICATION_REQUIRED"]
    assert authority.capacity.snapshot()["reservations"] == []


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
    activations = 0

    def lose_reply(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal activations
        activations += 1
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
    recovered = reopened.advance(execution["id"], "owner", "advance")
    assert recovered["phase"] == "admitted"
    assert len(authority.capacity.snapshot()["reservations"]) == 1
    assert activations == 1
    assert authority.capacity.command_receipt(
        "activate",
        {"admission_id": authority.capacity.snapshot()["reservations"][0]["id"]},
        command_key="planning-activate:" + execution["id"],
    ) is not None


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


def _output_source_control(
    tmp_path: Path, authority: PlanningAdmissionAuthority, run: dict[str, Any]
) -> tuple[Path, Path]:
    """Install a real private Commander source without creating a qualification pass."""
    runtime = _prepared_runtime()
    tokenizer = _prepared_tokenizer()
    credential_private = tmp_path / "output-source-private"
    key = credential_private / "source.key"
    profile = run["configuration_snapshot"]["configuration"]["resources"]["profiles"][0]
    auth_ref = profile["profile"]["auth_ref"]
    credentials = CredentialSourceStore(
        authority.planner.projects,
        sources={(run["project_id"], auth_ref): LocalKeyFile("output-source", key)},
        private_directory=credential_private,
    )
    key.write_text("local-source-fixture\n", encoding="utf-8")
    key.chmod(0o600)
    credentials.register(
        run["project_id"], auth_ref, principal="owner", command_key="source-register"
    )
    # npm's installed package may expose the Linux binary through a hardlink
    # alias to opencode-ai/bin/opencode.  Production qualification rejects
    # aliased runtime paths, so stage a private standalone copy for this
    # fixture while proving its bytes and metadata remain identical.
    runtime_info = runtime.stat()
    staged_runtime = credential_private / "opencode"
    shutil.copy2(runtime, staged_runtime)
    staged_info = staged_runtime.stat()
    assert staged_info.st_nlink == 1
    assert staged_info.st_size == runtime_info.st_size
    assert staged_info.st_mode == runtime_info.st_mode
    assert staged_runtime.read_bytes() == runtime.read_bytes()
    runtime = staged_runtime
    control = _protected_factory_control(tmp_path, authority)
    journal_path = credential_private / "journal.sqlite"
    GoCallJournal(journal_path)
    journal_path.chmod(0o600)
    work_root = credential_private / "work"
    work_root.mkdir(mode=0o700)
    write_commander_qualification_settings(
        control,
        CommanderQualificationSettings(
            runtime,
            tokenizer,
            credential_private,
            (CommanderCredentialSource(run["project_id"], auth_ref, "output-source", key),),
            journal_path=journal_path,
            work_root=work_root,
        ),
    )
    return control, key


def _persistent_production_transport_case(
    tmp_path: Path, configured: dict, *, capacity_unit: str = "requests"
) -> tuple[Path, PlanningExecution, dict[str, Any], dict[str, Any]]:
    """Build the complete existing-only production factory fixture before use."""
    production_configuration = json.loads(
        (Path(__file__).parents[2] / "examples/projects/offline-configuration.json").read_text()
    )
    production_configuration["resources"]["quota_pools"][0]["unit"] = capacity_unit
    preview = configured["registry"].preview_configuration(
        configured["id"],
        production_configuration,
        command_key=capacity_unit + "-unit-preview",
        principal="owner",
    )
    configured = {
        **configured["registry"].apply_configuration(
            configured["id"],
            preview["preview_id"],
            expected_revision=configured["revision"],
            command_key=capacity_unit + "-unit-apply",
            principal="owner",
        ),
        "registry": configured["registry"],
    }
    _, authority, run, execution = _case(
        tmp_path,
        configured,
        register_estimate=False,
        paths=["original.txt"],
        execution_policy=_native_v2_policy(configured),
        clock=time.time,
        observation_at=time.time(),
        capacity_unit=capacity_unit,
    )
    # The production reader opens this existing-only qualification ledger after
    # its complete protected state, source and snapshot stores already exist.
    ProfileQualificationStore(authority.planner.projects)
    control, _ = _output_source_control(tmp_path, authority, run)
    state = tmp_path / "protected-state"
    outputs = PlanningOutputStore(state / "planning-output.sqlite", authority_kind="production")
    outputs.database.chmod(0o600)
    provision_planning_repository_snapshots(control)
    service = PlanningExecution.from_trusted_factory(control)
    assert isinstance(service.admissions, PlanningAdmissionAuthority)
    reader = service.admissions.qualifications
    assert isinstance(reader, PersistentCommanderQualificationReader)
    profile = run["configuration_snapshot"]["configuration"]["resources"]["profiles"][0]
    fixture_facts = authority.qualifications.read_commander(
        execution["binding"],
        scope=COMMANDER_QUALIFICATION_SCOPE,
        reader_version="karajan.commander-qualification-reader.v1",
    )
    assert fixture_facts is not None
    with reader.qualifications._owned(run["project_id"], "owner") as db:
        bound = reader.qualifications._binding(
            db, run["project_id"], {"id": profile["id"], "revision": profile["revision"]}
        )
        source = reader._current_source(db, run["project_id"], bound, "owner")
        profile_facts = deepcopy(fixture_facts["profile_facts"])
        profile_facts["valid_until"] = time.time() + 60
        start = {
            "qualification_scope": COMMANDER_QUALIFICATION_SCOPE,
            "profile_binding": bound,
            "source": source,
            "execution_start": {"test_only": "production-transport"},
        }
        record = {
            "id": "test-production-transport-qualification",
            "binding": start,
            "qualification_scope": COMMANDER_QUALIFICATION_SCOPE,
            "status": "passed",
            "provenance": "official",
            "observed_at": time.time(),
            "valid_until": time.time() + 60,
            "commander_facts": {
                "profile_facts": profile_facts,
                "capability_evidence": fixture_facts["capability_evidence"],
                "source_generation_sha256": digest(source),
            },
        }
        db.execute(
            "INSERT INTO profile_qualification_starts VALUES (?,?,?,?,?,?)",
            (
                record["id"],
                run["project_id"],
                "owner",
                "test-production-transport",
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
    return control, service, run, execution


@pytest.mark.skipif(sys.platform != "linux", reason="native planning requires Linux namespaces")
@pytest.mark.parametrize("capacity_unit", ["tokens", "percent"])
def test_production_transport_rejects_unsupported_estimate_unit_before_any_effect(
    configured: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capacity_unit: str
) -> None:
    """Production transport rejects a current non-request window before any send claim."""
    control, service, run, execution = _persistent_production_transport_case(
        tmp_path, configured, capacity_unit=capacity_unit
    )

    def unexpected_produce(*args: object, **kwargs: object) -> bytes:
        del args, kwargs
        raise AssertionError("unsupported estimates must not start native production")

    monkeypatch.setattr(ProductionGoPlanningProducer, "produce", unexpected_produce)
    transport = PlanningTransport.from_trusted_factory(control)
    output_database = tmp_path / "protected-state" / "planning-output.sqlite"
    assert isinstance(transport.execution.admissions, PlanningAdmissionAuthority)
    with pytest.raises(RunError, match="^PLANNING_ESTIMATE_UNIT_UNSUPPORTED$"):
        transport.execute(execution["id"], principal="owner", command_key="execute")

    with sqlite3.connect(output_database) as db:
        assert {
            table: db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
            for table in (
                "planning_execute_commands",
                "planning_output_sources",
                "planning_output_claims",
                "planning_outputs",
            )
        } == {
            "planning_execute_commands": 0,
            "planning_output_sources": 0,
            "planning_output_claims": 0,
            "planning_outputs": 0,
        }
    with sqlite3.connect(transport.execution.admissions.database) as db:
        assert db.execute("SELECT COUNT(*) FROM planning_estimates").fetchone()[0] == 0
    with sqlite3.connect(transport.execution.capacity.path) as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM commands WHERE key LIKE 'planning-activate:%'"
            ).fetchone()[0]
            == 0
        )
    assert transport.execution.capacity.snapshot()["reservations"] == []
    assert (
        transport.execution.get(execution["id"], principal="owner")["state"]
        == "awaiting_admission"
    )
    assert service.planner.get(run["id"], principal="owner")["plans"] == []


@pytest.mark.skipif(sys.platform != "linux", reason="native planning requires Linux namespaces")
def test_production_transport_rejects_conflicting_command_before_new_execution_effects(
    configured: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A command bound to A cannot freeze or estimate awaiting execution B."""
    control, service, run, first = _persistent_production_transport_case(tmp_path, configured)

    def unexpected_produce(*args: object, **kwargs: object) -> bytes:
        del args, kwargs
        raise AssertionError("a conflicting command must not start native production")

    monkeypatch.setattr(ProductionGoPlanningProducer, "produce", unexpected_produce)
    transport = PlanningTransport.from_trusted_factory(control)
    second_intent = transport.execution.planner.planning_intent(
        run["id"], term=1, command_key="conflicting-intent", principal="lead"
    )
    second = transport.execution.begin(
        run["id"], second_intent["id"], principal="owner", command_key="conflicting-begin"
    )
    assert second["state"] == "awaiting_admission"
    transport.outputs.claim_execute_command(
        first["binding"], principal="owner", command_key="conflicting-execute"
    )
    state = tmp_path / "protected-state"
    output_database = state / "planning-output.sqlite"
    snapshot_database_path = state / "planning-repository-snapshots.sqlite"
    assert isinstance(transport.execution.admissions, PlanningAdmissionAuthority)
    with pytest.raises(RunError, match="^IDEMPOTENCY_CONFLICT$"):
        transport.execute(
            second["id"], principal="owner", command_key="conflicting-execute"
        )

    with sqlite3.connect(snapshot_database_path) as db:
        assert {
            table: db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
            for table in ("snapshots", "files")
        } == {"snapshots": 0, "files": 0}
    with sqlite3.connect(transport.execution.admissions.database) as db:
        assert db.execute("SELECT COUNT(*) FROM planning_estimates").fetchone()[0] == 0
    with sqlite3.connect(output_database) as db:
        assert {
            table: db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
            for table in (
                "planning_execute_commands",
                "planning_output_sources",
                "planning_output_claims",
                "planning_outputs",
            )
        } == {
            "planning_execute_commands": 1,
            "planning_output_sources": 0,
            "planning_output_claims": 0,
            "planning_outputs": 0,
        }
    with sqlite3.connect(transport.execution.capacity.path) as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM commands WHERE key LIKE 'planning-activate:%'"
            ).fetchone()[0]
            == 0
        )
    assert transport.execution.capacity.snapshot()["reservations"] == []
    assert transport.execution.get(second["id"], principal="owner")["state"] == "awaiting_admission"
    assert service.planner.get(run["id"], principal="owner")["plans"] == []


@pytest.mark.skipif(sys.platform != "linux", reason="native planning requires Linux namespaces")
def test_transport_rejects_conflicting_key_before_unknown_receipt_reconcile(
    configured: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A key bound to A cannot even reconcile receipt-proven unknown execution B."""
    control, _, run, first = _persistent_production_transport_case(tmp_path, configured)
    transport = PlanningTransport.from_trusted_factory(control)
    second_intent = transport.execution.planner.planning_intent(
        run["id"], term=1, command_key="unknown-conflict-intent", principal="lead"
    )
    second = transport.execution.begin(
        run["id"], second_intent["id"], principal="owner", command_key="unknown-conflict-begin"
    )
    assert isinstance(transport.execution.admissions, PlanningAdmissionAuthority)
    authority = transport.execution.admissions
    original_activate = transport.execution.capacity.activate

    def commit_then_lose_capacity_reply(*args: Any, **kwargs: Any) -> dict[str, Any]:
        original_activate(*args, **kwargs)
        raise CapacityError("activation reply lost")

    monkeypatch.setattr(transport.execution.capacity, "activate", commit_then_lose_capacity_reply)
    unknown = transport.execute(second["id"], principal="owner", command_key="unknown-execute")
    assert unknown["state"] == "admission_unknown"
    # The Capacity write committed, but the controller is still genuinely
    # unknown.  Let the admission owner update only its original receipt while
    # leaving B's execution record stale for the command-conflict boundary.
    monkeypatch.setattr(transport.execution.capacity, "activate", original_activate)
    assert authority.recover_original_receipt(
        second["id"], "owner", "planning-admit:" + second["id"]
    )["phase"] == "admitted"
    assert transport.execution.get(second["id"], principal="owner")["state"] == "admission_unknown"
    transport.outputs.claim_execute_command(
        first["binding"], principal="owner", command_key="unknown-conflicting-execute"
    )
    state = tmp_path / "protected-state"
    output_database = state / "planning-output.sqlite"
    snapshot_database_path = state / "planning-repository-snapshots.sqlite"
    before_execution = transport.execution.get(second["id"], principal="owner")
    before_run = transport.execution.planner.get(run["id"], principal="owner")
    before_capacity = transport.execution.capacity.snapshot()
    with sqlite3.connect(output_database) as db:
        before_outputs = {
            table: db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
            for table in (
                "planning_execute_commands",
                "planning_output_sources",
                "planning_output_claims",
                "planning_outputs",
            )
        }
    with sqlite3.connect(snapshot_database_path) as db:
        before_snapshots = {
            table: db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
            for table in ("snapshots", "files")
        }

    with pytest.raises(RunError, match="^IDEMPOTENCY_CONFLICT$"):
        transport.execute(
            second["id"], principal="owner", command_key="unknown-conflicting-execute"
        )

    assert transport.execution.get(second["id"], principal="owner") == before_execution
    assert transport.execution.planner.get(run["id"], principal="owner") == before_run
    assert transport.execution.capacity.snapshot() == before_capacity
    with sqlite3.connect(output_database) as db:
        assert {
            table: db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
            for table in before_outputs
        } == before_outputs
    with sqlite3.connect(snapshot_database_path) as db:
        assert {
            table: db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
            for table in before_snapshots
        } == before_snapshots


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
def test_retained_factory_rejects_cancelled_execution_store_swap_during_git_prepare(
    configured: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale pre-cancel authority cannot publish after real Git preparation."""
    registry = configured["registry"]
    project = registry.get(configured["id"])
    root = Path(project["repository"]["root"])
    (root / "src").mkdir(exist_ok=True)
    (root / "tests").mkdir(exist_ok=True)
    source = root / "src" / "retained-authority.txt"
    source.write_bytes(b"registered base bytes\n")
    test_source = root / "tests" / "retained-authority-test.txt"
    test_source.write_bytes(b"registered test base bytes\n")
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "add",
            "src/retained-authority.txt",
            "tests/retained-authority-test.txt",
        ],
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
    _, authority, _, execution = _case(tmp_path, configured)
    ProfileQualificationStore(authority.planner.projects)
    control = _protected_factory_control(tmp_path, authority)
    ledger = provision_planning_repository_snapshots(control)
    retained = PlanningExecution.from_trusted_factory(control)
    assert retained.capacity is not None
    capacity_before = retained.capacity.snapshot()
    execution_store = tmp_path / "protected-state" / "planning-execution.sqlite"
    stale_copy = tmp_path / "pre-cancel-planning-execution.sqlite"
    shutil.copy2(execution_store, stale_copy)
    entered = Event()
    release = Event()
    original_git = PlanningRepositorySnapshotStore._git

    def slow_first_git(*args: Any, **kwargs: Any) -> bytes:
        result = original_git(*args, **kwargs)
        if not entered.is_set():
            entered.set()
            assert release.wait(timeout=5)
        return result

    monkeypatch.setattr(PlanningRepositorySnapshotStore, "_git", slow_first_git)
    held = tmp_path / "held-planning-execution.sqlite"
    with ThreadPoolExecutor(max_workers=1) as workers:
        publication = workers.submit(
            retained.freeze_repository_snapshot,
            execution["id"],
            principal="owner",
            command_key="freeze-after-cancel",
        )
        assert entered.wait(timeout=5)
        cancelled = retained.cancel(execution["id"], principal="owner", command_key="cancel")
        assert cancelled["cancel_requested"] is True
        execution_store.rename(held)
        execution_store.symlink_to(stale_copy)
        release.set()
        with pytest.raises(RunError, match="^PLANNING_ADMISSION_BOOTSTRAP_CHANGED$"):
            publication.result(timeout=10)
    with sqlite3.connect(ledger) as db:
        assert db.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 0
    assert stale_copy.read_bytes() != held.read_bytes()
    execution_store.unlink()
    held.rename(execution_store)
    reopened = PlanningExecution.from_trusted_factory(control)
    assert reopened.get(execution["id"], principal="owner")["cancel_requested"] is True
    assert reopened.capacity is not None
    assert reopened.capacity.snapshot() == capacity_before


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
            qualified = db.execute("SELECT count(*) FROM profile_qualification_records").fetchone()[
                0
            ]
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
    external_blobs_before = {item.name: item.read_bytes() for item in external_artifacts.iterdir()}
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
    assert process_call_sites == [(planning_snapshot.__name__, "_git")] * (
        1 + 2 * len(frozen["files"])
    )
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
    runtime = tmp_path / "runtime"
    runtime.write_bytes(b"synthetic-runtime")
    settings = CommanderQualificationSettings(
        runtime,
        Path(os.environ["KARAJAN_GO_TOKENIZER_DIRECTORY"]).resolve(),
        private,
        (CommanderCredentialSource(run["project_id"], auth_ref, "synthetic-current", key),),
    )
    write_commander_qualification_settings(control, settings)
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
    fixture_facts = authority.qualifications.read_commander(
        execution["binding"],
        scope=COMMANDER_QUALIFICATION_SCOPE,
        reader_version="karajan.commander-qualification-reader.v1",
    )
    assert fixture_facts is not None
    profile_record = run["configuration_snapshot"]["configuration"]["resources"]["profiles"][0]
    control, key = _output_source_control(tmp_path, authority, run)
    # Initialize the reader ledger before reopening the one existing source.
    ProfileQualificationStore(authority.planner.projects)
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
    # Both admission and its final effect guard use this one persistent reader.
    # A fixture-to-persistent swap would make a later rejection ambiguous.
    authority.qualifications = reader
    qualifications.commander_source = reader._current_source
    with qualifications._owned(run["project_id"], "owner") as db:
        bound = qualifications._binding(
            db,
            run["project_id"],
            {"id": profile_record["id"], "revision": profile_record["revision"]},
        )
        source = reader._current_source(db, run["project_id"], bound, "owner")
        start = {
            "qualification_scope": COMMANDER_QUALIFICATION_SCOPE,
            "profile_binding": bound,
            "source": source,
            "execution_start": {"synthetic": "capacity-boundary"},
        }
        profile_facts = deepcopy(fixture_facts["profile_facts"])
        profile_facts["valid_until"] = time.time() + 60
        record = {
            "id": "synthetic-capacity-boundary",
            "binding": start,
            "qualification_scope": COMMANDER_QUALIFICATION_SCOPE,
            "status": "passed",
            "provenance": "official",
            "observed_at": time.time(),
            "valid_until": time.time() + 60,
            "commander_facts": {
                "profile_facts": profile_facts,
                "capability_evidence": fixture_facts["capability_evidence"],
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
    admitted = authority.advance(execution["id"], "owner", "synthetic-boundary-advance")
    assert admitted["phase"] == "admitted", repr(admitted["reason_codes"])
    with authority.effect_guard(execution["id"], "owner", "unchanged-credential-effect"):
        pass
    original_boundary = authority._capture_final_boundary

    def mutate_inside_capacity_boundary(*args: Any, **kwargs: Any) -> dict[str, Any]:
        key.write_text("synthetic-boundary-key-changed\n", encoding="utf-8")
        return original_boundary(*args, **kwargs)

    monkeypatch.setattr(authority, "_capture_final_boundary", mutate_inside_capacity_boundary)
    with pytest.raises(RunError, match="^COMMANDER_QUALIFICATION_CHANGED$"):
        with authority.effect_guard(execution["id"], "owner", "changed-credential-effect"):
            pytest.fail("changed credentials entered a planning transport effect")


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
def test_factory_output_arm_preserves_missing_commander_as_durable_denial(
    configured: dict, tmp_path: Path
) -> None:
    """Output source observation must not bypass the ordinary Commander denial."""
    _, authority, run, execution = _case(tmp_path, configured)
    control, _ = _output_source_control(tmp_path, authority, run)
    ledger = tmp_path / "protected-state" / "planning-output.sqlite"
    PlanningOutputStore(ledger, authority_kind="production")
    ledger.chmod(0o600)

    reopened = PlanningExecution.from_trusted_factory(control)
    assert reopened.outputs is not None
    reader = reopened.outputs._source_reader
    assert callable(reader)
    expected_source = reader(execution["binding"])
    producer = ProductionGoPlanningProducer.from_trusted_factory(control, reopened)
    assert producer.source(execution["binding"]) == expected_source
    reopened.outputs.arm(execution["binding"], expected_source)
    denied = reopened.admissions.advance(execution["id"], "owner", "missing-commander")

    assert denied["phase"] == "denied"
    assert denied["reason_codes"] == ["COMMANDER_QUALIFICATION_REQUIRED"]
    assert reopened.capacity is not None
    assert reopened.capacity.snapshot()["reservations"] == []


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
def test_persistent_factory_rejects_output_ledger_alias_without_writes(
    configured: dict, tmp_path: Path, alias: str
) -> None:
    _, authority, _, _ = _case(tmp_path, configured)
    control = _protected_factory_control(tmp_path, authority)
    state = tmp_path / "protected-state"
    ledger = state / "planning-output.sqlite"
    PlanningOutputStore(ledger, authority_kind="production")
    external = tmp_path / "repository-controlled-output.sqlite"
    shutil.copy2(ledger, external)
    ledger.unlink()
    if alias == "symlink":
        ledger.symlink_to(external)
    else:
        os.link(external, ledger)
    before = external.read_bytes()
    with pytest.raises(RunError, match="^PLANNING_OUTPUT_AUTHORITY_UNAVAILABLE$"):
        PlanningExecution.from_trusted_factory(control)
    assert external.read_bytes() == before


@pytest.mark.skipif(sys.platform == "win32", reason="private deployment modes require Linux")
def test_retained_factory_rejects_replaced_output_ledger(
    configured: dict, tmp_path: Path
) -> None:
    """The output authority keeps its original private inode through recovery."""
    _, authority, _, execution = _case(tmp_path, configured)
    control = _protected_factory_control(tmp_path, authority)
    state = tmp_path / "protected-state"
    ledger = state / "planning-output.sqlite"
    PlanningOutputStore(ledger, authority_kind="production")
    ledger.chmod(0o600)
    retained = PlanningExecution.from_trusted_factory(control)
    replacement = tmp_path / "replacement-output.sqlite"
    shutil.copy2(ledger, replacement)
    replacement.replace(ledger)

    with pytest.raises(RunError, match="^PLANNING_ADMISSION_BOOTSTRAP_CHANGED$"):
        retained.get(execution["id"], principal="owner")


@pytest.mark.skipif(sys.platform == "win32", reason="private deployment modes require Linux")
@pytest.mark.parametrize("mutation", ["hardlink", "permissions"])
def test_retained_factory_rechecks_output_ledger_privacy(
    configured: dict, tmp_path: Path, mutation: str
) -> None:
    """A retained output authority rejects mutable privacy without an inode swap."""
    _, authority, _, execution = _case(tmp_path, configured)
    control = _protected_factory_control(tmp_path, authority)
    ledger = tmp_path / "protected-state" / "planning-output.sqlite"
    PlanningOutputStore(ledger, authority_kind="production")
    ledger.chmod(0o600)
    retained = PlanningExecution.from_trusted_factory(control)
    if mutation == "hardlink":
        os.link(ledger, tmp_path / "second-output-ledger-link.sqlite")
    else:
        ledger.chmod(0o644)

    with pytest.raises(RunError, match="^PLANNING_ADMISSION_BOOTSTRAP_CHANGED$"):
        retained.get(execution["id"], principal="owner")


@pytest.mark.skipif(sys.platform == "win32", reason="private deployment modes require Linux")
def test_direct_factory_submit_rechecks_live_output_source_before_claim(
    configured: dict, tmp_path: Path
) -> None:
    """A recovered execution cannot accept output armed under a stale source."""
    service, authority, run, execution = _case(tmp_path, configured)
    control, key = _output_source_control(tmp_path, authority, run)
    ledger = tmp_path / "protected-state" / "planning-output.sqlite"
    raw_outputs = PlanningOutputStore(ledger, authority_kind="production")
    ledger.chmod(0o600)
    recovered = PlanningExecution.from_trusted_factory(control)
    assert recovered.outputs is not None
    reader = recovered.outputs._source_reader
    assert callable(reader)
    source = reader(execution["binding"])
    raw_outputs.arm(execution["binding"], source)
    raw_outputs.publish(execution["binding"], b'{"summary":"stale"}')
    with recovered._transaction() as db:
        pending = recovered._load(db, execution["id"])
        pending["state"] = "awaiting_output"
        pending["output_source_sha256"] = digest(source)
        recovered._save(db, pending)
    key.write_text("local-source-changed\n", encoding="utf-8")
    key.chmod(0o600)
    result = recovered.submit(execution["id"], principal="owner", command_key="direct-stale")

    assert result["state"] == "blocked"
    assert result["reason_codes"] == ["PLANNING_OUTPUT_SOURCE_INVALID"]
    assert recovered.planner.get(run["id"], principal="owner")["plans"] == []


@pytest.mark.skipif(sys.platform == "win32", reason="private production source requires Linux")
def test_production_submit_rejects_a_valid_qualification_replacement_after_source_read(
    configured: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A replacement official record cannot authorize an already-armed output."""
    control, _, run, execution = _persistent_production_transport_case(tmp_path, configured)
    transport = PlanningTransport.from_trusted_factory(control)
    persisted = transport.execution.planner.get(run["id"], principal="owner")
    intent = persisted["planning_intents"][0]
    plan = submit_request(persisted, intent)["plan"]
    for task in plan["tasks"]:
        task["paths"] = ["original.txt"]

    replaced = False
    original_source = transport.execution._submission_source
    reader = transport.execution.admissions.qualifications
    qualifications = reader.qualifications

    def replace_after_source(execution_record: dict[str, Any]) -> dict[str, Any]:
        nonlocal replaced
        source = original_source(execution_record)
        if not replaced:
            with qualifications._owned(run["project_id"], "owner") as db:
                row = db.execute(
                    "SELECT id,record FROM profile_qualification_records "
                    "ORDER BY rowid DESC LIMIT 1"
                ).fetchone()
                assert row is not None
                record = json.loads(row[1])
                record["valid_until"] = time.time() + 120
                db.execute(
                    "UPDATE profile_qualification_records SET record=?,digest=? WHERE id=?",
                    (json.dumps(record), digest(record), row[0]),
                )
            current = reader.read_commander(
                execution_record["binding"],
                scope=COMMANDER_QUALIFICATION_SCOPE,
                reader_version="karajan.commander-qualification-reader.v1",
            )
            assert current is not None
            assert current["valid_until"] > time.time()
            assert current["record_sha256"] != source["qualification_record_sha256"]
            replaced = True
        return source

    monkeypatch.setattr(transport.execution, "_submission_source", replace_after_source)
    monkeypatch.setattr(
        ProductionGoPlanningProducer,
        "produce",
        lambda self, model_input, *, binding, admission: json.dumps(
            plan, separators=(",", ":")
        ).encode(),
    )

    result = transport.execute(execution["id"], principal="owner", command_key="replacement")
    assert replaced
    assert result["state"] == "blocked"
    assert result["reason_codes"] == ["PLANNING_OUTPUT_SOURCE_CHANGED"]
    assert transport.execution.planner.get(run["id"], principal="owner")["plans"] == []


@pytest.mark.skipif(sys.platform == "win32", reason="private production source requires Linux")
def test_production_submit_rechecks_credential_material_after_plan_validation(
    configured: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Credential material changing during validation blocks Plan persistence."""
    control, _, run, execution = _persistent_production_transport_case(tmp_path, configured)
    transport = PlanningTransport.from_trusted_factory(control)
    persisted = transport.execution.planner.get(run["id"], principal="owner")
    intent = persisted["planning_intents"][0]
    plan = submit_request(persisted, intent)["plan"]
    for task in plan["tasks"]:
        task["paths"] = ["original.txt"]
    from karajan.orchestration.go_commander_qualification import (
        read_commander_qualification_settings,
    )
    from karajan.runs import planning as runs_planning

    settings, _ = read_commander_qualification_settings(control)
    credential_path = Path(settings.credential_sources[0].path)
    original_validate = runs_planning.validate_plan

    def mutate_during_validation(value: dict[str, Any], ceiling: dict[str, Any]) -> None:
        original_validate(value, ceiling)
        credential_path.write_text("production-replacement-material\n", encoding="utf-8")

    monkeypatch.setattr(runs_planning, "validate_plan", mutate_during_validation)
    monkeypatch.setattr(
        ProductionGoPlanningProducer,
        "produce",
        lambda self, model_input, *, binding, admission: json.dumps(
            plan, separators=(",", ":")
        ).encode(),
    )

    result = transport.execute(execution["id"], principal="owner", command_key="credential-change")
    assert result["state"] == "blocked"
    assert result["reason_codes"] == ["PLANNING_OUTPUT_SOURCE_CHANGED"]
    assert transport.execution.planner.get(run["id"], principal="owner")["plans"] == []


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
    assert admitted["phase"] == "admitted", repr(admitted["reason_codes"])
    assert authority.read_admission(execution["binding"])["authority_kind"] == "fixture"
    control = _protected_factory_control(tmp_path, authority)
    provision_planning_repository_snapshots(control)
    production = PlanningExecution.from_trusted_factory(control)
    assert production.admissions is not None
    with pytest.raises(RunError, match="PLANNING_ADMISSION_PROVENANCE_FORBIDDEN"):
        production.admissions.advance(execution["id"], "owner", "production-admit")
