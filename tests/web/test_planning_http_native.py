"""Authenticated C/P planning flow with the native Go fixture as the only double."""

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from karajan.adapters.opencode.go_context import GoRequestAccounting
from karajan.adapters.opencode.go_journal import GoCallJournal
from karajan.capacity import CapacityStore
from karajan.orchestration.planning_admission import PlanningAdmissionAuthority
from karajan.orchestration.planning_execution import PlanningExecution
from karajan.orchestration.planning_snapshot import PlanningRepositorySnapshotStore
from karajan.orchestration.planning_transport import (
    FixtureGoPlanningProducer,
    PlanningOutputStore,
    PlanningTransport,
)
from karajan.projects import ProjectRegistry
from karajan.runs import RunPlanner
from karajan.runs.planning import digest
from karajan.web import create_app

_RUNS_TEST_ROOT = str(Path(__file__).parents[1] / "runs")
if _RUNS_TEST_ROOT not in sys.path:
    sys.path.insert(0, _RUNS_TEST_ROOT)

from test_planning_admission import FixtureCommander, planning_capacity  # noqa: E402
from test_routing_authorization import (  # noqa: E402
    policy_request,
    request_v2,
    submit_request,
)

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux namespaces required")
pytest_plugins = ["test_planning"]

ORIGIN = "http://127.0.0.1:8765"


def _v2_policy(configured: dict[str, Any]) -> dict[str, Any]:
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
            "id": "native-fixture-validation",
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


def _headers(client: TestClient, token: str) -> dict[str, str]:
    login = client.post(
        "/v1/session/bootstrap", json={"token": token}, headers={"Origin": ORIGIN}
    )
    assert login.status_code == 200
    return {"Origin": ORIGIN, "X-CSRF-Token": login.json()["csrf_token"]}


def _runtime() -> Path:
    runtime = Path(
        os.environ.get(
            "KARAJAN_OPENCODE_LINUX_BINARY",
            str(
                Path(__file__).resolve().parents[2]
                / "runtimes/opencode/node_modules/opencode-linux-x64/bin/opencode"
            ),
        )
    )
    if runtime.is_file():
        return runtime
    if os.environ.get("KARAJAN_REQUIRE_OPENCODE_ISOLATION") == "1":
        pytest.fail("Prepared fixed Linux OpenCode artifact is required")
    pytest.skip("Prepared Linux OpenCode artifact is not available")


def test_authenticated_v2_planning_executes_one_native_fixture_send_and_persists_plan(
    tmp_path: Path, project: tuple[ProjectRegistry, dict[str, Any], Path]
) -> None:
    registry, configured, repository = project
    profile_document = registry.get_configuration(configured["id"])["configuration"]
    policy = registry.register_execution_policy(
        configured["id"], _v2_policy(configured), command_key="policy", principal="owner"
    )
    planner = RunPlanner(tmp_path / "runs.sqlite", registry)
    capacity = planning_capacity(tmp_path / "capacity", clock=lambda: 1000.0)
    capacity.register_profile(
        {
            "id": "fixture-profile",
            "revision": 1,
            "account_id": "fixture-account",
            "pool_ids": ["service-fixture"],
        },
        command_key="profile",
    )
    execution_database = tmp_path / "planning.sqlite"
    authority = PlanningAdmissionAuthority(
        tmp_path / "planning-admissions.sqlite",
        execution_database,
        planner,
        capacity,
        FixtureCommander(
            {
                "profile": {"id": "fixture-profile", "revision": 1},
                "profile_digest": digest(
                    profile_document["resources"]["profiles"][0]["profile"]
                ),
                "runtime_version": "1",
                "roles": ["commander"],
                "tools": ["fixture-tools"],
                "context_tokens": 16384,
                "data_destination": "local-fixture",
                "budget_enforcement": "bounded_calls",
                "provenance": "fixture",
                "evidence_ref": "fixture:commander",
                "observed_at": 0.0,
                "valid_until": 2_000_000_000.0,
            }
        ),
        authority_kind="fixture",
    )
    outputs = PlanningOutputStore(tmp_path / "planning-outputs.sqlite", authority_kind="fixture")
    snapshots = PlanningRepositorySnapshotStore(tmp_path / "planning-snapshots.sqlite")
    execution = PlanningExecution(
        execution_database,
        planner,
        admissions=authority,
        outputs=outputs,
        capacity=capacity,
        snapshots=snapshots,
        allow_fixture_authorities=True,
    )
    accounting = GoRequestAccounting(Path(os.environ["KARAJAN_GO_TOKENIZER_DIRECTORY"]))
    calls: list[dict[str, Any]] = []
    plan: dict[str, Any] | None = None

    def upstream(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        assert plan is not None
        response = {
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
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=("data: " + json.dumps(response) + "\n\ndata: [DONE]\n\n").encode(),
        )

    transport = PlanningTransport(
        execution,
        accounting,
        FixtureGoPlanningProducer(
            GoCallJournal(tmp_path / "journal.sqlite"),
            accounting,
            upstream=upstream,
            runtime=_runtime(),
            work_root=tmp_path / "native-work",
        ),
        outputs,
    )
    app = create_app(
        tmp_path / "web-state",
        origin=ORIGIN,
        bootstrap_token="first",
        planning_execution=execution,
        planning_transport=transport,
    )
    original = (repository / "original.txt").read_bytes()
    with TestClient(app, base_url=ORIGIN) as client:
        headers = _headers(client, "first")
        request = request_v2(configured, policy)
        request["authorization"].update(
            read_paths=["original.txt"], write_paths=["original.txt"]
        )
        run = client.post(
            "/v1/runs",
            json=request,
            headers={**headers, "Idempotency-Key": "create-v2"},
        ).json()
        assert run["schema_version"] == "karajan.run-planning.v2"
        plan = submit_request(run, {"id": "not-used"})["plan"]
        for task in plan["tasks"]:
            task["paths"] = ["original.txt"]
        started = client.post(
            f"/v1/runs/{run['id']}/planning-start",
            json={},
            headers={**headers, "Idempotency-Key": "prepare"},
        )
        assert started.status_code == 200
        binding = execution.get(
            started.json()["planning"]["execution"]["id"], principal="owner"
        )["binding"]
        authority.register_estimate(
            run["id"],
            binding["budget_ref"],
            binding["profile"],
            demand={"service-fixture": "1"},
            expected_capacity={
                "policy_revision": 1,
                "pool_windows": {"service-fixture": "fixture-window"},
                "lead_reserve_access": True,
            },
            duration_seconds=25,
            max_requests=1,
            max_duration_seconds=25,
            principal="owner",
            command_key="fixture-estimate",
        )
        executed = client.post(
            f"/v1/runs/{run['id']}/planning-execute",
            json={},
            headers={**headers, "Idempotency-Key": "execute"},
        )
        assert executed.status_code == 202, executed.json()
        assert executed.json()["command"]["state"] == "accepted"
        deadline = time.monotonic() + 10
        observed = executed.json()
        while time.monotonic() < deadline:
            observed = client.get(f"/v1/runs/{run['id']}/planning", headers=headers).json()
            if observed["run"]["plans"]:
                break
            time.sleep(0.05)
        assert observed["run"]["plans"], (
            observed,
            execution.get(binding["execution_id"], principal="owner"),
        )
        assert observed["run"]["plans"][0]["plan"] == plan
        retried = client.post(
            f"/v1/runs/{run['id']}/planning-execute",
            json={},
            headers={**headers, "Idempotency-Key": "execute"},
        )
        assert retried.status_code == 202
        assert retried.json()["command"]["id"] == executed.json()["command"]["id"]
        assert len(calls) == 1
        run_id = run["id"]
    assert (repository / "original.txt").read_bytes() == original

    reopened_registry = ProjectRegistry(registry.database, [tmp_path], existing_only=True)
    reopened_planner = RunPlanner(
        planner.database, reopened_registry, existing_only=True
    )
    reopened_capacity = CapacityStore(
        tmp_path / "capacity" / "capacity.sqlite", existing_only=True, clock=lambda: 1000.0
    )
    reopened = PlanningExecution(
        execution_database,
        reopened_planner,
        admissions=authority,
        outputs=outputs,
        capacity=reopened_capacity,
        snapshots=PlanningRepositorySnapshotStore(
            tmp_path / "planning-snapshots.sqlite", existing_only=True
        ),
        allow_fixture_authorities=True,
        existing_only=True,
    )
    app = create_app(
        tmp_path / "web-state",
        origin=ORIGIN,
        bootstrap_token="second",
        planning_execution=reopened,
        planning_transport=PlanningTransport(reopened, accounting, transport.producer, outputs),
    )
    with TestClient(app, base_url=ORIGIN) as client:
        headers = _headers(client, "second")
        restored = client.get(f"/v1/runs/{run_id}/planning", headers=headers)
        assert restored.status_code == 200
        assert restored.json()["run"]["plans"][0]["plan"] == plan

def test_unobserved_capacity_persists_commander_qualification_block_without_send(
    tmp_path: Path, project: tuple[ProjectRegistry, dict[str, Any], Path]
) -> None:
    registry, configured, _ = project
    profile_document = registry.get_configuration(configured["id"])["configuration"]
    policy = registry.register_execution_policy(
        configured["id"], _v2_policy(configured), command_key="policy", principal="owner"
    )
    planner = RunPlanner(tmp_path / "runs.sqlite", registry)
    capacity = CapacityStore(tmp_path / "capacity.sqlite", clock=lambda: 1000.0)
    capacity.register_pool(
        {
            "id": "service-fixture",
            "account_id": "fixture-account",
            "kind": "service",
            "unit": "percent",
            "window_kind": "unknown",
        },
        command_key="pool",
    )
    capacity.register_profile(
        {
            "id": "fixture-profile",
            "revision": 1,
            "account_id": "fixture-account",
            "pool_ids": ["service-fixture"],
        },
        command_key="profile",
    )
    capacity.activate_policy(
        {
            "account_id": "fixture-account",
            "max_active_attempts": 1,
            "max_attempt_duration_seconds": 60,
            "observation_max_age_seconds": 30,
            "require_official_observation": False,
            "safety_margin": {},
            "lead_reserve": {},
            "lead_reserved_slots": 0,
            "conservative_mode": {
                "enabled": True,
                "max_local_active_attempts": 1,
                "max_attempt_duration_seconds": 60,
                "observation_max_age_seconds": 30,
                "cooldown_seconds": 10,
            },
        },
        expected_revision=0,
        command_key="policy",
    )
    execution_database = tmp_path / "planning.sqlite"
    authority = PlanningAdmissionAuthority(
        tmp_path / "planning-admissions.sqlite",
        execution_database,
        planner,
        capacity,
        FixtureCommander(
            {
                "profile": {"id": "fixture-profile", "revision": 1},
                "profile_digest": digest(profile_document["resources"]["profiles"][0]["profile"]),
                "runtime_version": "1",
                "roles": ["commander"],
                "tools": ["fixture-tools"],
                "context_tokens": 16384,
                "data_destination": "local-fixture",
                "budget_enforcement": "bounded_calls",
                "provenance": "fixture",
                "evidence_ref": "fixture:commander",
                "observed_at": 0.0,
                "valid_until": 2_000_000_000.0,
            },
            available=False,
        ),
        authority_kind="production",
    )
    outputs = PlanningOutputStore(tmp_path / "planning-outputs.sqlite", authority_kind="production")
    execution = PlanningExecution(
        execution_database,
        planner,
        admissions=authority,
        outputs=outputs,
        capacity=capacity,
        snapshots=PlanningRepositorySnapshotStore(tmp_path / "planning-snapshots.sqlite"),
        _trusted_authority_ids=frozenset({id(authority)}),
    )

    class NoProducer:
        authority_kind = "production"

        def source(self, binding: dict[str, Any]) -> dict[str, Any]:
            del binding
            return {"kind": "read-only-source"}

        def produce(self, *args: object, **kwargs: object) -> bytes:
            raise AssertionError("denied admission must not send a producer request")

    transport = PlanningTransport(
        execution,
        GoRequestAccounting(Path(os.environ["KARAJAN_GO_TOKENIZER_DIRECTORY"])),
        NoProducer(),
        outputs,
    )
    app = create_app(
        tmp_path / "web-state",
        origin=ORIGIN,
        bootstrap_token="unobserved",
        planning_execution=execution,
        planning_transport=transport,
    )
    with TestClient(app, base_url=ORIGIN) as client:
        headers = _headers(client, "unobserved")
        request = request_v2(configured, policy)
        request["authorization"].update(read_paths=["original.txt"], write_paths=["original.txt"])
        run = client.post(
            "/v1/runs", json=request, headers={**headers, "Idempotency-Key": "create"}
        ).json()
        client.post(
            f"/v1/runs/{run['id']}/planning-start",
            json={},
            headers={**headers, "Idempotency-Key": "start"},
        )
        result = client.post(
            f"/v1/runs/{run['id']}/planning-execute",
            json={},
            headers={**headers, "Idempotency-Key": "execute"},
        )

    assert result.status_code == 202
    assert result.json()["command"]["state"] == "accepted"
    planning = result.json()["planning"]
    assert planning["execution"] == {
        "id": planning["execution"]["id"],
        "binding_sha256": planning["execution"]["binding_sha256"],
        "state": "blocked",
        "cancel_requested": False,
        "reason_codes": ["COMMANDER_QUALIFICATION_REQUIRED"],
    }
    assert planning["availability"] == {
        "state": "blocked",
        "reason_code": "COMMANDER_QUALIFICATION_REQUIRED",
    }
    assert capacity.snapshot()["reservations"] == []
