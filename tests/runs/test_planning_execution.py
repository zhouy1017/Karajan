"""Planning execution uses real local SQLite and strictly read-only authorities."""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from karajan.capacity import CapacityStore
from karajan.orchestration.planning_execution import PlanningExecution
from karajan.runs import RunError, RunPlanner
from karajan.runs.planning import digest
from test_planning import create_request, proposal
from test_routing_authorization import policy_request, request_v2, submit_request

pytest_plugins = ["test_planning"]


class FixtureAuthorities:
    """Explicitly test-only adapters around a persisted CapacityStore receipt."""

    def __init__(self, capacity: CapacityStore) -> None:
        self.capacity = capacity
        self.request: dict[str, Any] | None = None
        self.key: str | None = None
        self.activation_request: dict[str, Any] | None = None
        self.activation_key: str | None = None
        self.content: bytes | None = None
        self.output_source = "b" * 64

    def prepare(self, binding: dict[str, Any], content: bytes) -> None:
        self.capacity.register_profile(
            {
                "id": binding["profile"]["id"],
                "revision": binding["profile"]["revision"],
                "account_id": "shared-account",
                "pool_ids": ["short", "weekly", "allowance"],
            },
            command_key="planning-profile:" + binding["execution_id"],
        )
        self.request = capacity_request(
            binding["attempt_id"],
            profile_id=binding["profile"]["id"],
            run_id=binding["run_id"],
        )
        self.key = "planning-admit:" + binding["execution_id"]
        self.capacity.admit(self.request, command_key=self.key)
        receipt = self.capacity.command_receipt("admit", self.request, command_key=self.key)
        assert receipt is not None
        self.activation_request = {"admission_id": receipt["admission_id"]}
        self.activation_key = "planning-activate:" + binding["execution_id"]
        self.content = content

    def activate(self) -> None:
        assert self.activation_request is not None and self.activation_key is not None
        self.capacity.activate(
            self.activation_request["admission_id"], command_key=self.activation_key
        )

    def read_admission(self, binding: dict[str, Any]) -> object:
        assert (
            self.request is not None
            and self.key is not None
            and self.activation_request is not None
            and self.activation_key is not None
        )
        return {
            "schema_version": "karajan.planning-admission-evidence.v1",
            "binding_sha256": digest(binding),
            "authority_kind": "fixture",
            "source_sha256": digest(self.request),
            "budget_ref": binding["budget_ref"],
            "capacity_request": self.request,
            "capacity_command_key": self.key,
            "capacity_receipt": self.capacity.command_receipt(
                "admit", self.request, command_key=self.key
            ),
            "capacity_activation_request": self.activation_request,
            "capacity_activation_command_key": self.activation_key,
            "capacity_activation_receipt": self.capacity.command_receipt(
                "activate", self.activation_request, command_key=self.activation_key
            ),
            "state": "admitted",
        }

    def read_output(self, execution_id: str, binding: dict[str, Any]) -> object:
        assert self.content is not None
        return {
            "schema_version": "karajan.planning-output-evidence.v1",
            "execution_id": execution_id,
            "binding_sha256": digest(binding),
            "authority_kind": "fixture",
            "source_sha256": self.output_source,
            "completed": True,
            "artifact_sha256": hashlib.sha256(self.content).hexdigest(),
            "artifact_size": len(self.content),
            "content": self.content,
        }

    def read_source(self, binding: dict[str, Any]) -> object:
        return {
            "schema_version": "karajan.planning-output-source.v1",
            "binding_sha256": digest(binding),
            "authority_kind": "fixture",
            "source_sha256": self.output_source,
        }


def capacity_request(attempt_id: str, *, profile_id: str, run_id: str) -> dict[str, Any]:
    return {
        "attempt_id": attempt_id,
        "run_id": run_id,
        "profile_id": profile_id,
        "profile_revision": 1,
        "role": "commander",
        "purpose": "lead",
        "authorization_ref": "fixture-planning-scope",
        "rulebook_revision": "fixture-planning-rules",
        "duration_seconds": 30,
        "demand": {"short": "1", "weekly": "1", "allowance": "1"},
    }


def capacity_store(directory: Path) -> CapacityStore:
    directory.mkdir()
    store = CapacityStore(directory / "capacity.sqlite", clock=lambda: 1000.0)
    for pool_id, kind in (
        ("short", "service"),
        ("weekly", "service"),
        ("allowance", "platform_allowance"),
    ):
        store.register_pool(
            {
                "id": pool_id,
                "account_id": "shared-account",
                "kind": kind,
                "unit": "requests",
                "window_kind": "fixed",
            },
            command_key="pool:" + pool_id,
        )
        store.observe(
            {
                "pool_id": pool_id,
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
            command_key="observe:" + pool_id,
        )
    store.activate_policy(
        {
            "account_id": "shared-account",
            "max_active_attempts": 4,
            "max_attempt_duration_seconds": 60,
            "observation_max_age_seconds": 30,
            "require_official_observation": False,
            "safety_margin": {},
            "lead_reserve": {},
            "lead_reserved_slots": 0,
            "conservative_mode": None,
        },
        expected_revision=0,
        command_key="policy",
    )
    return store


def planning_case(
    tmp_path: Path, configured: dict
) -> tuple[PlanningExecution, dict, dict, FixtureAuthorities]:
    planner = RunPlanner(tmp_path / "runs.sqlite", configured.pop("registry"))
    run = planner.create(create_request(configured), command_key="run", principal="owner")
    intent = planner.planning_intent(run["id"], term=1, command_key="intent", principal="lead")
    authorities = FixtureAuthorities(capacity_store(tmp_path / "capacity"))
    service = PlanningExecution(
        tmp_path / "planning-execution.sqlite",
        planner,
        admissions=authorities,
        outputs=authorities,
        capacity=authorities.capacity,
        allow_fixture_authorities=True,
    )
    return service, run, intent, authorities


@pytest.fixture
def configured(project: tuple[Any, dict[str, Any], Path]) -> dict[str, Any]:
    registry, value, _ = project
    return {**deepcopy(value), "registry": registry}


def begin(
    service: PlanningExecution, run: dict, intent: dict, authorities: FixtureAuthorities
) -> dict:
    execution = service.begin(run["id"], intent["id"], principal="owner", command_key="begin")
    content = json.dumps(proposal(run, intent)["plan"], separators=(",", ":")).encode()
    authorities.prepare(execution["binding"], content)
    return execution


def test_id_only_output_consumption_reopens_exact_capacity_receipt_and_plan(
    configured: dict, tmp_path: Path
) -> None:
    service, run, intent, authorities = planning_case(tmp_path, configured)
    execution = begin(service, run, intent, authorities)
    authorities.activate()

    admitted = service.reconcile(execution["id"], principal="owner")
    assert admitted["state"] == "awaiting_output"
    before = authorities.capacity.snapshot()
    reopened = PlanningExecution(
        service.database,
        service.planner,
        admissions=authorities,
        outputs=authorities,
        capacity=authorities.capacity,
        allow_fixture_authorities=True,
    )
    assert reopened.reconcile(execution["id"], principal="owner") == admitted
    assert authorities.capacity.snapshot() == before

    submitted = reopened.submit(execution["id"], principal="owner", command_key="submit")
    assert submitted["state"] == "submitted"
    assert submitted["output"]["content_sha256"] == hashlib.sha256(authorities.content).hexdigest()
    assert submitted["submission"]["plan_revision"] == 1
    assert reopened.submit(execution["id"], principal="owner", command_key="submit") == submitted
    assert service.planner.get(run["id"], principal="owner")["plans"] == [submitted["submission"]]


@pytest.mark.parametrize("change", ["missing", "wrong_profile", "unknown", "changed_output"])
def test_missing_mismatched_or_changed_evidence_never_submits(
    configured: dict, tmp_path: Path, change: str
) -> None:
    service, run, intent, authorities = planning_case(tmp_path, configured)
    execution = begin(service, run, intent, authorities)
    if change == "missing":
        service.admissions = None
    elif change == "wrong_profile":
        original = authorities.read_admission

        def changed(binding: dict[str, Any]) -> object:
            result = original(binding)
            result["capacity_request"] = {**result["capacity_request"], "profile_id": "other"}
            result["source_sha256"] = digest(result["capacity_request"])
            return result

        service.admissions = type("BadAdmissions", (), {"read_admission": staticmethod(changed)})()
    elif change == "unknown":
        original = authorities.read_admission

        def unknown(binding: dict[str, Any]) -> object:
            result = original(binding)
            result["state"] = "unknown"
            return result

        service.admissions = type(
            "UnknownAdmissions", (), {"read_admission": staticmethod(unknown)}
        )()
    else:
        authorities.activate()
        service.reconcile(execution["id"], principal="owner")
        authorities.content = b'{"summary":"changed"}'
    result = service.submit(execution["id"], principal="owner", command_key="submit")
    assert result["submission"] is None
    assert service.planner.get(run["id"], principal="owner")["plans"] == []


def test_cancel_before_output_wins_and_owner_and_intent_are_not_replaceable(
    configured: dict, tmp_path: Path
) -> None:
    service, run, intent, authorities = planning_case(tmp_path, configured)
    execution = begin(service, run, intent, authorities)
    with pytest.raises(RunError, match="RUN_NOT_FOUND"):
        service.get(execution["id"], principal="lead")
    cancelled = service.cancel(execution["id"], principal="owner", command_key="cancel")
    assert cancelled["state"] == "cancelled"
    assert service.submit(execution["id"], principal="owner", command_key="submit") == cancelled
    assert service.planner.get(run["id"], principal="owner")["plans"] == []
    with pytest.raises(RunError, match="PLANNING_EXECUTION_PENDING"):
        service.begin(run["id"], intent["id"], principal="owner", command_key="new-begin")


def test_production_default_rejects_fixture_authorities(configured: dict, tmp_path: Path) -> None:
    service, run, intent, authorities = planning_case(tmp_path, configured)
    service.allow_fixture_authorities = False
    execution = begin(service, run, intent, authorities)
    blocked = service.reconcile(execution["id"], principal="owner")
    assert blocked["reason_codes"] == ["PLANNING_FIXTURE_AUTHORITY_FORBIDDEN"]
    assert blocked["submission"] is None


def test_production_label_cannot_promote_a_test_double(configured: dict, tmp_path: Path) -> None:
    service, run, intent, authorities = planning_case(tmp_path, configured)
    execution = begin(service, run, intent, authorities)
    original = authorities.read_admission

    def mislabeled(binding: dict[str, Any]) -> object:
        result = original(binding)
        result["authority_kind"] = "production"
        return result

    service.admissions = type(
        "MislabeledAdmissions", (), {"read_admission": staticmethod(mislabeled)}
    )()
    blocked = service.reconcile(execution["id"], principal="owner")
    assert blocked["reason_codes"] == ["PLANNING_PRODUCTION_AUTHORITY_UNAVAILABLE"]


def test_capacity_reader_and_frozen_output_source_reject_substitution(
    configured: dict, tmp_path: Path
) -> None:
    registry = configured["registry"]
    service, run, intent, authorities = planning_case(tmp_path, configured)
    execution = begin(service, run, intent, authorities)
    original = authorities.read_admission

    def unrelated_receipt(binding: dict[str, Any]) -> object:
        value = original(binding)
        value["capacity_command_key"] = "never-issued-command"
        value["capacity_receipt"] = {"unrelated": "receipt"}
        return value

    authorities.read_admission = unrelated_receipt  # type: ignore[method-assign]
    rejected = service.reconcile(execution["id"], principal="owner")
    assert rejected["submission"] is None
    assert rejected["reason_codes"] == ["PLANNING_CAPACITY_RECEIPT_MISMATCH"]

    (tmp_path / "source").mkdir()
    service, run, intent, authorities = planning_case(
        tmp_path / "source", {**configured, "registry": registry}
    )
    execution = begin(service, run, intent, authorities)
    authorities.activate()
    assert service.reconcile(execution["id"], principal="owner")["state"] == "awaiting_output"
    authorities.output_source = "c" * 64
    rejected = service.submit(execution["id"], principal="owner", command_key="submit")
    assert rejected["submission"] is None
    assert rejected["reason_codes"] == ["PLANNING_OUTPUT_SOURCE_CHANGED"]
    assert service.planner.get(run["id"], principal="owner")["plans"] == []


def test_missing_or_changed_activation_receipt_never_submits(
    configured: dict, tmp_path: Path
) -> None:
    service, run, intent, authorities = planning_case(tmp_path, configured)
    execution = begin(service, run, intent, authorities)
    original = authorities.read_admission

    def missing_activation(binding: dict[str, Any]) -> object:
        value = original(binding)
        value["capacity_activation_command_key"] = "unissued-activation"
        value["capacity_activation_receipt"] = None
        return value

    authorities.read_admission = missing_activation  # type: ignore[method-assign]
    result = service.submit(execution["id"], principal="owner", command_key="submit")
    assert result["submission"] is None
    assert result["reason_codes"] == ["PLANNING_CAPACITY_ACTIVATION_MISMATCH"]
    assert service.planner.get(run["id"], principal="owner")["plans"] == []


def test_exact_run_receipt_recovers_lost_reply_without_resubmission(
    configured: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, run, intent, authorities = planning_case(tmp_path, configured)
    execution = begin(service, run, intent, authorities)
    authorities.activate()
    original = service.planner._submit_planning_execution_plan

    def lose_reply(*args: Any, **kwargs: Any) -> dict[str, Any]:
        original(*args, **kwargs)
        raise RuntimeError("reply lost after RunPlanner commit")

    monkeypatch.setattr(service.planner, "_submit_planning_execution_plan", lose_reply)
    with pytest.raises(RuntimeError, match="reply lost"):
        service.submit(execution["id"], principal="owner", command_key="submit")
    recovered = PlanningExecution(service.database, service.planner).submit(
        execution["id"], principal="owner", command_key="submit"
    )
    assert recovered["state"] == "submitted"
    assert service.planner.get(run["id"], principal="owner")["plans"] == [
        recovered["submission"]
    ]


def test_reopened_unstarted_claim_without_receipt_never_submits(
    configured: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash after the SQLite claim is uncertain, never a fresh submission."""
    service, run, intent, authorities = planning_case(tmp_path, configured)
    execution = begin(service, run, intent, authorities)
    authorities.activate()

    def crash_after_claim(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise SystemExit("claim persisted before submit")

    monkeypatch.setattr(service, "_recover_or_submit", crash_after_claim)
    with pytest.raises(SystemExit, match="claim persisted"):
        service.submit(execution["id"], principal="owner", command_key="submit")
    claimed = service.get(execution["id"], principal="owner")
    assert claimed["state"] == "submit_claimed"
    assert claimed["submission_started"] is False
    assert service.planner.get(run["id"], principal="owner")["plans"] == []

    # A new controller has no live authorities to re-authorize the persisted
    # claim. It may read its exact Run receipt, but cannot make a new Plan.
    reopened = PlanningExecution(service.database, service.planner)
    recovered = reopened.submit(execution["id"], principal="owner", command_key="submit")
    assert recovered["state"] == "submission_unknown"
    assert recovered["reason_codes"] == ["PLANNING_EXECUTION_SUBMISSION_UNKNOWN"]
    assert service.planner.get(run["id"], principal="owner")["plans"] == []


def test_concurrent_submitters_create_at_most_one_plan(configured: dict, tmp_path: Path) -> None:
    service, run, intent, authorities = planning_case(tmp_path, configured)
    execution = begin(service, run, intent, authorities)
    authorities.activate()
    reopened = PlanningExecution(
        service.database,
        service.planner,
        admissions=authorities,
        outputs=authorities,
        capacity=authorities.capacity,
        allow_fixture_authorities=True,
    )

    with ThreadPoolExecutor(max_workers=2) as workers:
        submissions = list(
            workers.map(
                lambda subject: subject.submit(
                    execution["id"], principal="owner", command_key="submit"
                ),
                (service, reopened),
            )
        )

    assert all(submission["state"] == "submitted" for submission in submissions)
    assert submissions[0]["submission"] == submissions[1]["submission"]
    assert service.planner.get(run["id"], principal="owner")["plans"] == [
        submissions[0]["submission"]
    ]


def test_cancellation_observed_before_run_submit_prevents_plan(
    configured: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, run, intent, authorities = planning_case(tmp_path, configured)
    execution = begin(service, run, intent, authorities)
    authorities.activate()
    original = service.planner._submit_planning_execution_plan

    def cancel_at_boundary(*args: Any, **kwargs: Any) -> dict[str, Any]:
        result = service.cancel(execution["id"], principal="owner", command_key="cancel")
        assert result["cancel_requested"]
        return original(*args, **kwargs)

    monkeypatch.setattr(service.planner, "_submit_planning_execution_plan", cancel_at_boundary)
    result = service.submit(execution["id"], principal="owner", command_key="submit")
    assert result["submission"] is None
    assert service.planner.get(run["id"], principal="owner")["plans"] == []


def test_source_drift_after_capture_blocks_reopened_claim(
    configured: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, run, intent, authorities = planning_case(tmp_path, configured)
    execution = begin(service, run, intent, authorities)
    authorities.activate()

    def stop_before_claim(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise SystemExit("output captured")

    monkeypatch.setattr(service, "_claim_submission", stop_before_claim)
    with pytest.raises(SystemExit, match="output captured"):
        service.submit(execution["id"], principal="owner", command_key="submit")
    assert service.get(execution["id"], principal="owner")["state"] == "output_captured"
    authorities.output_source = "c" * 64
    reopened = PlanningExecution(
        service.database,
        service.planner,
        admissions=authorities,
        outputs=authorities,
        capacity=authorities.capacity,
        allow_fixture_authorities=True,
    )
    result = reopened.submit(execution["id"], principal="owner", command_key="submit")
    assert result["submission"] is None
    assert result["reason_codes"] == ["PLANNING_OUTPUT_SOURCE_CHANGED"]
    assert reopened.planner.get(run["id"], principal="owner")["plans"] == []


def test_begin_replay_survives_original_intent_state_change(
    configured: dict, tmp_path: Path
) -> None:
    service, run, intent, authorities = planning_case(tmp_path, configured)
    execution = begin(service, run, intent, authorities)
    authorities.activate()
    service.submit(execution["id"], principal="owner", command_key="submit")
    assert (
        service.begin(run["id"], intent["id"], principal="owner", command_key="begin")
        == execution
    )


def test_v2_output_uses_the_versioned_parser_and_run_submission(
    project: tuple[Any, dict[str, Any], Path], tmp_path: Path
) -> None:
    registry, configured, _ = project
    policy = registry.register_execution_policy(
        configured["id"], policy_request(configured), command_key="policy", principal="owner"
    )
    planner = RunPlanner(tmp_path / "v2-runs.sqlite", registry)
    run = planner.create(request_v2(configured, policy), command_key="run", principal="owner")
    intent = planner.planning_intent(run["id"], term=1, command_key="intent", principal="lead")
    authorities = FixtureAuthorities(capacity_store(tmp_path / "v2-capacity"))
    service = PlanningExecution(
        tmp_path / "v2-planning-execution.sqlite",
        planner,
        admissions=authorities,
        outputs=authorities,
        capacity=authorities.capacity,
        allow_fixture_authorities=True,
    )
    execution = service.begin(run["id"], intent["id"], principal="owner", command_key="begin")
    content = json.dumps(submit_request(run, intent)["plan"], separators=(",", ":")).encode()
    authorities.prepare(execution["binding"], content)
    authorities.activate()
    submitted = service.submit(execution["id"], principal="owner", command_key="submit")
    assert submitted["state"] == "submitted"
    assert submitted["submission"]["routing_binding"]["execution_policy"]["id"] == policy["id"]
