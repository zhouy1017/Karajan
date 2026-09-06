"""Independent estimate store checks using the Spec-owned public approval fixture."""

import copy
import importlib.util
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pathlib import Path
from threading import Event

import pytest
from karajan.projects.demand import AttemptEstimateStore, DemandError

WORKTREE = next(
    parent for parent in Path(__file__).resolve().parents if (parent / "pyproject.toml").is_file()
)
spec = importlib.util.spec_from_file_location(
    "independent_approval_fixture",
    WORKTREE / "examples/approved-routing/routing/spec/fixture.py",
)
assert spec is not None and spec.loader is not None
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)


@pytest.fixture
def case(tmp_path):
    return fixtures.seeded(tmp_path)


def request(identity="prediction", revision=1):
    return {
        "id": identity,
        "revision": revision,
        "source_kind": "owner_conservative_estimate",
        "validity_seconds": 60,
        "measurement_semantics": "window_independent_attempt",
        "demand": [
            {
                "pool_id": "service-fixture",
                "unit": "percent",
                "window_kind": "fixed",
                "amount": "2.5e0",
            }
        ],
        "completion_seconds": None,
        "basis": "Independent explicit owner forecast, no calibrated claim",
    }


def register(case, value=None, key="prediction"):
    return case["estimates"].register(
        case["run"]["id"],
        "implement",
        fixtures.REF,
        value or request(),
        principal="owner",
        command_key=key,
    )


def windows(case):
    pool = case["capacity"].routing_facts().as_dict()["accounts"][0]["pools"][0]
    return [
        {
            "pool_id": pool["id"],
            **{key: pool[key] for key in ("account_id", "kind", "unit", "window_kind")},
            "window_id": pool["observation"]["observation"]["window_id"],
        }
    ]


def project(case, **kwargs):
    return case["estimates"].estimate(
        case["run"]["id"],
        "implement",
        fixtures.REF,
        principal="owner",
        pool_windows=kwargs.get("windows", windows(case)),
        as_of=kwargs.get("as_of", case["now"][0]),
    )


def test_public_registration_binds_exact_approved_sources_and_preserves_prediction_semantics(case):
    record = register(case)
    result = project(case)
    bound = record["binding"]
    assert bound["plan_digest"] == case["plan"]["plan_digest"]
    assert bound["authorization_digest"] == case["plan"]["authorization_digest"]
    assert bound["task_requirements"]["context_tokens"] == 3072
    assert bound["task_requirements"]["duration_seconds"] == 21
    assert bound["context_policy"]["reserved_output_tokens"] == 1024
    assert result["estimate"]["demand"][0]["amount"] == "2.5e0"
    assert result["estimate"]["confidence"] == "unknown"
    assert result["estimate"]["price"] is None
    assert result["estimate"]["completion_seconds"] is None
    assert result["source_binding"] == record
    reopened = AttemptEstimateStore(case["planner"], clock=lambda: case["now"][0])
    assert (
        reopened.get(case["project"], record["id"], record["revision"], principal="owner")["record"]
        == record
    )
    assert register(case) == record
    with pytest.raises(DemandError, match="^IDEMPOTENCY_CONFLICT$"):
        register(case, {**request(), "completion_seconds": 15.0})


@pytest.mark.parametrize(
    "field,value",
    [
        ("confidence", "known"),
        ("price", {"upper_bound": "0"}),
        ("binding", {"plan_digest": "0" * 64}),
    ],
)
def test_owner_cannot_inject_calibration_price_or_source_binding(case, field, value):
    with pytest.raises(DemandError, match="^RESOURCE_ESTIMATE_INPUT_INVALID$"):
        register(case, {**request(), field: value})
    assert project(case)["reason_codes"] == ["RESOURCE_ESTIMATE_MISSING"]


def test_newer_revoked_record_does_not_fall_back_to_older_valid_forecast(case):
    first = register(case)
    second = register(case, request(revision=2), key="second")
    case["estimates"].revoke(
        case["project"], second["id"], second["revision"], principal="owner", reason="stop-new"
    )
    assert project(case)["reason_codes"] == ["RESOURCE_ESTIMATE_REVOKED"]
    assert (
        case["estimates"].get(case["project"], first["id"], first["revision"], principal="owner")[
            "record"
        ]
        == first
    )


def test_controller_clock_expiry_cannot_be_hidden_by_old_capacity_as_of(case):
    register(case)
    case["now"][0] = 1060.0
    assert project(case, as_of=1000.0)["reason_codes"] == ["RESOURCE_ESTIMATE_EXPIRED"]
    case["now"][0] = 999.0
    assert project(case, as_of=1000.0)["reason_codes"] == ["RESOURCE_ESTIMATE_EXPIRED"]


@pytest.mark.parametrize("axis", ["account_id", "unit", "window_kind", "omitted"])
def test_complete_current_window_identity_must_match_pool_binding(case, axis):
    register(case)
    changed = windows(case)
    if axis == "omitted":
        changed = []
    else:
        changed[0][axis] = {
            "account_id": "other-account",
            "unit": "tokens",
            "window_kind": "rolling",
        }[axis]
    result = project(case, windows=changed)
    assert result["estimate"] is None
    assert result["reason_codes"] == [
        "PROFILE_POOL_VECTOR_INVALID"
        if axis == "omitted"
        else "RESOURCE_ESTIMATE_WINDOW_BINDING_CHANGED"
    ]


def test_window_independent_forecast_rebinds_only_after_new_real_capacity_observation(case):
    value = request()
    value["validity_seconds"] = 500
    record = register(case, value)
    first = project(case)
    case["now"][0] = 1201.0
    case["capacity"].observe(
        {
            "pool_id": "service-fixture",
            "window_id": "next-window",
            "observed_at": 1201.0,
            "reset_at": 1500.0,
            "source": "fixture",
            "source_ref": "spec-next-window",
            "metric": "remaining",
            "amount": "30",
            "limit": "100",
            "covered_usage_ids": [],
        },
        command_key="window-reset",
    )
    current = project(case)
    assert first["estimate"]["demand"][0]["window_id"] == "fixed-current"
    assert current["estimate"]["demand"][0]["window_id"] == "next-window"
    assert current["estimate"]["demand"][0]["amount"] == first["estimate"]["demand"][0]["amount"]
    assert current["source_binding"] == record


def test_new_approved_task_revision_invalidates_forecast_for_prior_approval(case):
    register(case)
    proposal = copy.deepcopy(case["proposal"])
    proposal["expected_plan_revision"] = 1
    proposal["plan"]["tasks"][0].update(revision=2, context_tokens=2048)
    plan = case["planner"].submit_plan(
        case["run"]["id"], proposal, principal="lead", command_key="new-plan"
    )
    assert project(case)["estimate"] is not None
    case["planner"].approve_plan(
        case["run"]["id"], fixtures.approval(plan), principal="owner", command_key="new-approval"
    )
    assert project(case)["reason_codes"] == ["RESOURCE_ESTIMATE_BINDING_CHANGED"]


def test_internal_projection_under_real_project_guard_blocks_revocation_until_exit(case):
    record = register(case)
    supplied_windows = windows(case)
    started = Event()

    def revoke():
        started.set()
        return case["estimates"].revoke(
            case["project"],
            record["id"],
            record["revision"],
            principal="owner",
            reason="guard-stop",
        )

    with ThreadPoolExecutor(max_workers=1) as executor:
        with case["planner"].activation_guard(case["run"]["id"]) as run:
            with case["qualifications"].routing_facts_guard(
                case["project"], case["config"]["resources"]["profiles"], principal="owner"
            ) as qualified:
                future = executor.submit(revoke)
                assert started.wait(timeout=2)
                with pytest.raises(TimeoutError):
                    future.result(timeout=0.15)
                result = case["estimates"].estimate_locked(
                    run,
                    "implement",
                    fixtures.REF,
                    current_catalog=qualified["catalog"],
                    pool_windows=supplied_windows,
                    as_of=case["now"][0],
                )
                assert result["source_binding"] == record
            assert future.result(timeout=3)["reason"] == "guard-stop"
    assert project(case)["reason_codes"] == ["RESOURCE_ESTIMATE_REVOKED"]
