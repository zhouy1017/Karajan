"""Owner predictions bind actual persisted approved Runs, not caller-made hashes."""

import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path

import pytest
from karajan.projects.demand import AttemptEstimateStore, DemandError
from karajan.projects.qualification import ProfileQualificationStore
from karajan.runs import RunPlanner
from test_qualification_store import apply, case

__all__ = ["case"]


@pytest.fixture
def approved(case: dict, tmp_path: Path, request: pytest.FixtureRequest) -> dict:
    projects = case["projects"]
    project = projects.get(case["project_id"])
    configuration = deepcopy(case["configuration"])
    configuration["resources"]["quota_pools"][0]["unit"] = "requests"
    if getattr(request, "param", None) == "multi":
        for name, unit in (("token-pool", "tokens"), ("weekly-percent", "percent")):
            configuration["resources"]["quota_pools"].append(
                {
                    "id": name,
                    "account_id": "fixture-account",
                    "kind": "service",
                    "unit": unit,
                    "limit": "100000",
                    "observation_state": "unknown",
                }
            )
            configuration["resources"]["profiles"][0]["quota_pool_refs"].append(name)
    preview = projects.preview_configuration(
        project["id"], configuration, command_key="demand-pools", principal="owner"
    )
    project = projects.apply_configuration(
        project["id"],
        preview["preview_id"],
        expected_revision=project["revision"],
        command_key="apply-demand-pools",
        principal="owner",
    )
    case["configuration"] = configuration
    case["registration"] = configuration["resources"]["profiles"][0]
    ref = {"id": "fixture-profile", "revision": 1}
    policy = projects.register_execution_policy(
        project["id"],
        {
            "schema_version": "karajan.execution-policy.v1",
            "id": "execution",
            "revision": 1,
            "configuration_digest": project["configuration"]["digest"],
            "constraints": {
                "profile_refs": [ref],
                "channel_ids": ["fixture-channel"],
                "tools": ["fixture-tools"],
                "data_destinations": ["local-fixture"],
                "required_capabilities": [],
                "min_isolation": "tool_sandboxed",
            },
            "risk_policy": {
                "id": "risk",
                "revision": 1,
                "mapping": {"standard": "T1", "critical": "T3"},
                "path_floors": [],
            },
            "channel_destinations": {"fixture-channel": "local-fixture"},
            "tool_policy": {
                "id": "tools",
                "revision": 1,
                "tool_permissions": {"fixture-tools": ["fixture-tools"]},
            },
            "context_policy": {
                "id": "context",
                "revision": 1,
                "input_accounting": "explicit_approved_upper_bound",
                "reserved_output_tokens": 1024,
            },
            "max_context_tokens": 8192,
        },
        command_key="execution-policy",
        principal="owner",
    )
    receipts: dict[str, dict] = {}
    planner = RunPlanner(tmp_path / "runs.sqlite", projects, admissions=receipts.__getitem__)
    run = planner.create(
        {
            "schema_version": "karajan.create-run.v2",
            "project_id": project["id"],
            "project_revision": project["revision"],
            "configuration_digest": project["configuration"]["digest"],
            "requirement": {"goal": "Produce report", "acceptance": ["Report is repeatable"]},
            "participants": [{"principal": "lead", "profile": ref, "purpose": "lead"}],
            "execution_policy": {key: policy[key] for key in ("id", "revision", "digest")},
            "authorization": {
                "profile_refs": [ref],
                "read_paths": ["src"],
                "write_paths": ["src"],
                "budget_ref": "run",
                "checks": ["tests", "independent_review"],
                "delivery": "pull_request",
                "target_branch": "main",
                "channel_ids": ["fixture-channel"],
                "tools": ["fixture-tools"],
                "data_destinations": ["local-fixture"],
                "required_capabilities": [],
                "min_isolation": "tool_sandboxed",
                "currency_limits": {"USD": "0", "CNY": "0"},
                "max_attempt_duration_seconds": 25,
                "max_quality_repair_rounds": 2,
                "stage_permissions": {
                    "bounded-worker": {"normal": True, "quality_indices": [0]},
                    "standard-review": {"normal": True, "quality_indices": []},
                },
            },
        },
        command_key="run",
        principal="owner",
    )
    intent = planner.planning_intent(run["id"], term=1, command_key="intent", principal="lead")
    receipts["fixture-receipt"] = {
        "receipt_ref": "fixture-receipt",
        "authority_revision": "fixture-v1",
        "run_id": run["id"],
        "intent_id": intent["id"],
        "term": 1,
        "principal": "lead",
        "profile": ref,
        "budget_ref": "planning",
        "state": "admitted",
        "provenance": "fixture",
    }
    planner.attach_planning_receipt(
        run["id"],
        intent["id"],
        receipt_ref="fixture-receipt",
        command_key="receipt",
        principal="owner",
    )
    task = {
        "id": "implement",
        "revision": 1,
        "role": "worker",
        "readiness": "ready",
        "complexity": "T2",
        "risk": "standard",
        "paths": ["src/report.py"],
        "depends_on": [],
        "acceptance": ["Report is repeatable"],
        "required": True,
        "purpose": None,
        "domains": ["code"],
        "required_capabilities": [],
        "tools": ["fixture-tools"],
        "context_tokens": 4096,
        "duration_seconds": 20,
    }
    submission = {
        "schema_version": "karajan.submit-plan.v2",
        "term": 1,
        "intent_id": intent["id"],
        "expected_plan_revision": 0,
        "plan": {
            "summary": "Produce and review",
            "authorization": run["authorization_ceiling"],
            "tasks": [
                task,
                {**task, "id": "review", "role": "reviewer", "depends_on": ["implement"]},
            ],
        },
    }
    plan = planner.submit_plan(run["id"], submission, command_key="plan", principal="lead")
    planner.approve_plan(run["id"], approval(plan), command_key="approve", principal="owner")
    return {
        **case,
        "planner": planner,
        "run_id": run["id"],
        "profile_ref": ref,
        "plan": plan,
        "submission": submission,
    }


def approval(plan: dict) -> dict:
    return {
        "schema_version": "karajan.approve-plan.v2",
        **{
            key: plan[key]
            for key in (
                "term",
                "plan_revision",
                "plan_digest",
                "authorization_digest",
                "configuration_digest",
                "routing_digest",
            )
        },
    }


def request() -> dict:
    return {
        "id": "report-prediction",
        "revision": 1,
        "source_kind": "owner_conservative_estimate",
        "validity_seconds": 60,
        "measurement_semantics": "window_independent_attempt",
        "demand": [
            {
                "pool_id": "service-fixture",
                "unit": "requests",
                "window_kind": "fixed",
                "amount": "3",
            }
        ],
        "completion_seconds": None,
        "basis": "Owner forecasts at most three requests per attempt.",
    }


def windows(window_id: str = "window-1") -> list[dict]:
    return [
        {
            "pool_id": "service-fixture",
            "account_id": "fixture-account",
            "kind": "service",
            "unit": "requests",
            "window_kind": "fixed",
            "window_id": window_id,
        }
    ]


def store(approved: dict) -> AttemptEstimateStore:
    return AttemptEstimateStore(approved["planner"], clock=lambda: approved["clock"][0])


def register(approved: dict, payload: dict | None = None, key: str = "estimate") -> dict:
    return store(approved).register(
        approved["run_id"],
        "implement",
        approved["profile_ref"],
        request() if payload is None else payload,
        principal="owner",
        command_key=key,
    )


def estimate(approved: dict, pool_windows: list[dict] | None = None) -> dict:
    return store(approved).estimate(
        approved["run_id"],
        "implement",
        approved["profile_ref"],
        principal="owner",
        pool_windows=windows() if pool_windows is None else pool_windows,
        as_of=approved["clock"][0],
    )


def test_registration_binds_approved_run_and_survives_restart_and_revocation(
    approved: dict,
) -> None:
    observed = register(approved)
    resolved = estimate(approved)
    assert resolved["reason_codes"] == []
    assert resolved["estimate"]["confidence"] == "unknown"
    assert resolved["estimate"]["price"] is None
    assert resolved["estimate"]["completion_seconds"] is None
    assert resolved["estimate"]["demand"] == [
        {"pool_id": "service-fixture", "unit": "requests", "window_id": "window-1", "amount": "3"}
    ]
    assert observed["binding"]["task_requirements"]["context_tokens"] == 4096
    assert observed["binding"]["context_policy"]["reserved_output_tokens"] == 1024
    assert register(approved) == observed
    store(approved).revoke(
        approved["project_id"],
        observed["id"],
        observed["revision"],
        principal="owner",
        reason="owner_withdrawn",
    )
    assert estimate(approved)["reason_codes"] == ["RESOURCE_ESTIMATE_REVOKED"]


def test_locked_consumer_works_inside_run_and_qualification_guards(approved: dict) -> None:
    register(approved)
    source = store(approved)
    with approved["planner"].activation_guard(approved["run_id"]) as run:
        with ProfileQualificationStore(approved["projects"]).routing_facts_guard(
            approved["project_id"], [approved["registration"]], principal="owner"
        ) as current:
            result = source.estimate_locked(
                run,
                "implement",
                approved["profile_ref"],
                current_catalog=current["catalog"],
                pool_windows=windows(),
                as_of=approved["clock"][0],
            )
            assert result["estimate"]["confidence"] == "unknown"


def test_missing_and_expired_estimates_are_not_replaced_by_task_limits(approved: dict) -> None:
    assert estimate(approved)["reason_codes"] == ["RESOURCE_ESTIMATE_MISSING"]
    register(approved)
    approved["clock"][0] += 60
    assert estimate(approved)["reason_codes"] == ["RESOURCE_ESTIMATE_EXPIRED"]


def test_unapproved_identity_cannot_register(approved: dict) -> None:
    with pytest.raises(DemandError, match="PROFILE_NOT_APPROVED"):
        store(approved).register(
            approved["run_id"],
            "implement",
            {"id": "made-up", "revision": 1},
            request(),
            principal="owner",
            command_key="invented",
        )


@pytest.mark.parametrize(
    "axis",
    [
        "zero",
        "negative",
        "fractional",
        "float",
        "overflow",
        "nan",
        "too_precise",
        "missing_pool",
        "duplicate_pool",
        "extra_pool",
        "wrong_unit",
        "cash_price",
        "known_confidence",
        "calibrated_source",
        "binding_hash",
        "huge_revision",
        "zero_ttl",
    ],
)
def test_bad_predictions_cannot_be_registered(approved: dict, axis: str) -> None:
    payload = request()
    invalid = {
        "zero": "0",
        "negative": "-1",
        "fractional": "0.5",
        "float": 1.0,
        "overflow": "9223372036854.775808",
        "nan": "NaN",
        "too_precise": "1.0000001",
    }
    if axis in invalid:
        payload["demand"][0]["amount"] = invalid[axis]
    elif axis == "missing_pool":
        payload["demand"] = []
    elif axis == "duplicate_pool":
        payload["demand"] *= 2
    elif axis == "extra_pool":
        payload["demand"].append({**payload["demand"][0], "pool_id": "unrelated"})
    elif axis == "wrong_unit":
        payload["demand"][0]["unit"] = "tokens"
    elif axis == "cash_price":
        payload["price"] = {"upper_bound": "0"}
    elif axis == "known_confidence":
        payload["confidence"] = "known"
    elif axis == "calibrated_source":
        payload["source_kind"] = "measured_calibration"
    elif axis == "binding_hash":
        payload["task_requirements_digest"] = "a" * 64
    elif axis == "huge_revision":
        payload["revision"] = 10**40
    else:
        payload["validity_seconds"] = 0
    with pytest.raises(DemandError):
        register(approved, payload)
    assert estimate(approved)["reason_codes"] == ["RESOURCE_ESTIMATE_MISSING"]


def test_revision_is_immutable_and_new_revision_supersedes_without_deleting(approved: dict) -> None:
    first = register(approved)
    changed = request()
    changed["demand"][0]["amount"] = "4"
    with pytest.raises(DemandError, match="IDEMPOTENCY_CONFLICT"):
        register(approved, changed)
    with pytest.raises(DemandError, match="ESTIMATE_REVISION_CONFLICT"):
        register(approved, changed, key="other-key")
    changed["revision"] = 2
    second = register(approved, changed, key="second-revision")
    assert estimate(approved)["estimate"]["demand"][0]["amount"] == "4"
    assert (
        store(approved).get(
            approved["project_id"], first["id"], first["revision"], principal="owner"
        )["record"]
        == first
    )
    store(approved).revoke(
        approved["project_id"], second["id"], 2, principal="owner", reason="stop"
    )
    assert estimate(approved)["reason_codes"] == ["RESOURCE_ESTIMATE_REVOKED"]


def test_actual_revised_approved_plan_invalidates_previous_prediction(approved: dict) -> None:
    register(approved)
    submission = deepcopy(approved["submission"])
    submission["expected_plan_revision"] = 1
    submission["plan"]["tasks"][0]["revision"] = 2
    submission["plan"]["tasks"][0]["context_tokens"] = 5000
    planner = approved["planner"]
    plan = planner.submit_plan(
        approved["run_id"], submission, command_key="plan-2", principal="lead"
    )
    # An unapproved proposal does not replace the currently active plan.
    assert estimate(approved)["estimate"] is not None
    planner.approve_plan(
        approved["run_id"], approval(plan), command_key="approve-2", principal="owner"
    )
    assert estimate(approved)["reason_codes"] == ["RESOURCE_ESTIMATE_BINDING_CHANGED"]
    prediction = request()
    prediction["revision"] = 2
    renewed = register(approved, prediction, "new-plan-prediction")
    assert renewed["binding"]["task_requirements"]["context_tokens"] == 5000
    assert estimate(approved)["estimate"] is not None


@pytest.mark.parametrize("axis", ["model", "runtime", "permissions", "pool_unit", "enabled"])
def test_current_profile_change_invalidates_prediction(approved: dict, axis: str) -> None:
    register(approved)
    configuration = deepcopy(approved["configuration"])
    registration = configuration["resources"]["profiles"][0]
    if axis == "model":
        registration["profile"]["binding"]["model_id"] = "different"
    elif axis == "runtime":
        registration["profile"]["binding"]["runtime_version"] = "2"
    elif axis == "permissions":
        registration["profile"]["required_permissions"] = ["shell"]
    elif axis == "pool_unit":
        configuration["resources"]["quota_pools"][0]["unit"] = "tokens"
    else:
        registration["enabled"] = False
    apply(approved, configuration)
    assert estimate(approved)["estimate"] is None


def test_window_rebinding_is_explicit_and_mismatched_pool_metadata_rejects(approved: dict) -> None:
    first = register(approved)
    current = estimate(approved, windows("window-2"))
    assert current["estimate"]["demand"][0]["window_id"] == "window-2"
    assert current["source_binding"] == first
    for key, value in (
        ("unit", "tokens"),
        ("account_id", "other"),
        ("kind", "platform_allowance"),
        ("window_kind", "rolling"),
    ):
        altered = windows()
        altered[0][key] = value
        assert estimate(approved, altered)["reason_codes"] == [
            "RESOURCE_ESTIMATE_WINDOW_BINDING_CHANGED"
        ]
    assert estimate(approved, windows() * 2)["reason_codes"] == ["PROFILE_POOL_VECTOR_INVALID"]


def test_owner_prediction_and_concurrent_idempotency_remain_explicit(approved: dict) -> None:
    payload = request()
    payload["completion_seconds"] = 7.5
    payload["demand"][0]["amount"] = "3e0"
    source = store(approved)
    with pytest.raises(DemandError, match="ESTIMATE_OWNER_REQUIRED"):
        source.register(
            approved["run_id"],
            "implement",
            approved["profile_ref"],
            payload,
            principal="lead",
            command_key="rogue",
        )
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda _: register(approved, payload), range(2)))
    assert results[0] == results[1]
    assert results[0]["completion_basis"] == "owner_prediction"
    resolved = estimate(approved)
    assert resolved["estimate"]["completion_seconds"] == 7.5
    assert resolved["estimate"]["confidence"] == "unknown"
    assert resolved["estimate"]["price"] is None


@pytest.mark.parametrize("approved", ["multi"], indirect=True)
def test_complete_multi_unit_vector_is_preserved_and_partial_vector_denied(approved: dict) -> None:
    with pytest.raises(DemandError, match="PROFILE_POOL_VECTOR_INVALID"):
        register(approved)
    payload = request()
    payload["demand"].extend(
        [
            {"pool_id": "token-pool", "unit": "tokens", "window_kind": "rolling", "amount": "8192"},
            {
                "pool_id": "weekly-percent",
                "unit": "percent",
                "window_kind": "fixed",
                "amount": "2.75",
            },
        ]
    )
    register(approved, payload)
    pool_windows = windows() + [
        {
            "pool_id": "token-pool",
            "unit": "tokens",
            "window_kind": "rolling",
            "window_id": "roll-1",
            "account_id": "fixture-account",
            "kind": "service",
        },
        {
            "pool_id": "weekly-percent",
            "unit": "percent",
            "window_kind": "fixed",
            "window_id": "week-1",
            "account_id": "fixture-account",
            "kind": "service",
        },
    ]
    result = estimate(approved, pool_windows)
    assert {d["pool_id"]: d["amount"] for d in result["estimate"]["demand"]} == {
        "service-fixture": "3",
        "token-pool": "8192",
        "weekly-percent": "2.75",
    }
    assert estimate(approved, pool_windows[:-1])["reason_codes"] == ["PROFILE_POOL_VECTOR_INVALID"]


def test_guard_keeps_estimate_revocation_stable_until_consumer_exits(approved: dict) -> None:
    observed = register(approved)
    source = store(approved)
    qualification = ProfileQualificationStore(approved["projects"])
    started = threading.Event()

    def revoke() -> dict:
        started.set()
        return source.revoke(
            approved["project_id"], observed["id"], 1, principal="owner", reason="stop"
        )

    with ThreadPoolExecutor(max_workers=1) as workers:
        with approved["planner"].activation_guard(approved["run_id"]) as run:
            with qualification.routing_facts_guard(
                approved["project_id"], [approved["registration"]], principal="owner"
            ) as view:
                future = workers.submit(revoke)
                assert started.wait(2)
                result = source.estimate_locked(
                    run,
                    "implement",
                    approved["profile_ref"],
                    current_catalog=view["catalog"],
                    pool_windows=windows(),
                    as_of=approved["clock"][0],
                )
                assert result["estimate"] is not None
                with pytest.raises(TimeoutError):
                    future.result(timeout=0.1)
        assert future.result(timeout=3)["reason"] == "stop"
    assert estimate(approved)["reason_codes"] == ["RESOURCE_ESTIMATE_REVOKED"]


def test_current_catalog_cannot_be_supplied_as_an_old_or_invented_snapshot(approved: dict) -> None:
    register(approved)
    source = store(approved)
    run = approved["planner"].get(approved["run_id"], principal="owner")
    catalog = approved["projects"].get_effective_resources(approved["project_id"])
    catalog["revision"] += 1
    assert source.estimate_locked(
        run,
        "implement",
        approved["profile_ref"],
        current_catalog=catalog,
        pool_windows=windows(),
        as_of=approved["clock"][0],
    )["reason_codes"] == ["ESTIMATE_CATALOG_CHANGED"]


@pytest.mark.parametrize("revision", [True, 0, -1, 10**40])
def test_invalid_revocation_revision_does_not_overflow_sqlite(
    approved: dict, revision: int
) -> None:
    observed = register(approved)
    with pytest.raises(DemandError, match="ESTIMATE_REVISION_INVALID"):
        store(approved).revoke(
            approved["project_id"], observed["id"], revision, principal="owner", reason="stop"
        )
