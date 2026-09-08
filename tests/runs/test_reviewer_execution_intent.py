"""C coverage for the pre-native Reviewer execution ledger."""

from pathlib import Path

import pytest
from karajan.execution import LaunchDenied, ProcessSpec, RunnerHost
from karajan.orchestration.reviewer_execution_intent import (
    ReviewerExecutionIntents,
    ReviewerExecutionSource,
    ReviewerLaunchSpec,
)
from karajan.runs import RunError
from test_reviewer_binding import _activate_reviewer_reservation, _passed_reviewer_subject

pytest_plugins = (
    "test_projected_qualification_store",
    "test_candidate_checks",
    "test_reviewer_binding",
)


def _service(tmp_path: Path, binding_case):
    intents, (run_id, _), candidates, _, _ = _passed_reviewer_subject(binding_case)
    reviewer = intents.admissions.advance(
        run_id,
        intents.admissions.enqueue(run_id, "review", principal="owner", command_key="review")["id"],
        principal="owner",
    )
    _activate_reviewer_reservation(intents, run_id, reviewer)
    source = ReviewerExecutionSource("1" * 64, "2" * 64)
    return (
        ReviewerExecutionIntents(
            tmp_path / "reviewer.sqlite",
            intents.admissions,
            candidates,
            source=source,
            host=RunnerHost(tmp_path / "host"),
            launch_compiler=lambda _: ReviewerLaunchSpec(
                ProcessSpec(("fixture",), tmp_path), "3" * 64
            ),
        ),
        run_id,
        reviewer["id"],
        intents,
    )


def test_prepare_is_durable_and_freezes_compiler_identity(tmp_path, binding_case):
    service, run_id, reviewer_id, intents = _service(tmp_path, binding_case)
    before = intents.admissions.routing.capacity.snapshot()
    intent = service.prepare(run_id, reviewer_id, principal="owner", command_key="prepare")
    assert intent["reviewer_operation_id"] == reviewer_id
    assert intent["worker_operation_id"] != reviewer_id
    assert intent["reviewer_input"]["schema_version"] == "karajan.reviewer-input.v2"
    assert intent["delivery"] == {"eligible": False, "state": "not_run"}
    assert service.prepare(run_id, reviewer_id, principal="owner", command_key="prepare") == intent
    assert intents.admissions.routing.capacity.snapshot() == before


def test_prepare_rejects_second_key_and_never_prepares_host(tmp_path, binding_case):
    service, run_id, reviewer_id, _ = _service(tmp_path, binding_case)
    intent = service.prepare(run_id, reviewer_id, principal="owner", command_key="prepare")
    with pytest.raises(RunError, match="REVIEWER_EXECUTION_ALREADY_PREPARED"):
        service.prepare(run_id, reviewer_id, principal="owner", command_key="other")
    with pytest.raises(KeyError):
        service.host.inspect(intent["planned_attempt_id"])


def test_fixed_host_prepare_is_replayable_without_starting_native(tmp_path, binding_case):
    service, run_id, reviewer_id, _ = _service(tmp_path, binding_case)
    service.prepare(run_id, reviewer_id, principal="owner", command_key="prepare")
    prepared = service.freeze_launch(run_id, reviewer_id, principal="owner")
    assert prepared["phase"] == "host_prepared"
    assert service.host.inspect(prepared["planned_attempt_id"]).state == "prepared"
    replay = service.freeze_launch(run_id, reviewer_id, principal="owner")
    assert replay["host_prepared_id"] == prepared["host_prepared_id"]
    assert replay["start_key"] == prepared["start_key"]


def test_cancelled_admission_prevents_new_host_preparation(tmp_path, binding_case):
    service, run_id, reviewer_id, intents = _service(tmp_path, binding_case)
    service.prepare(run_id, reviewer_id, principal="owner", command_key="prepare")
    cancelled = service.cancel(run_id, reviewer_id, principal="owner")
    assert cancelled is not None and cancelled["cancel_requested"] is True
    assert intents.admissions.get(run_id, reviewer_id, principal="owner")["cancel_requested"]
    with pytest.raises(RunError, match="REVIEWER_EXECUTION_CANCELLED"):
        service.freeze_launch(run_id, reviewer_id, principal="owner")


def test_unregistered_or_unstarted_host_cannot_claim_observer(tmp_path, binding_case):
    service, run_id, reviewer_id, _ = _service(tmp_path, binding_case)
    service.prepare(run_id, reviewer_id, principal="owner", command_key="prepare")
    service.freeze_launch(run_id, reviewer_id, principal="owner")
    with pytest.raises(LaunchDenied, match="RUNNER_REGISTRATION_UNPROVEN"):
        service.claim_registered_observer(
            run_id, reviewer_id, principal="owner", timeout_seconds=0.01
        )
    assert service.read(run_id, reviewer_id, principal="owner")["effect_claim"] is None
