"""C coverage for the pre-native Reviewer execution ledger."""

import json
import os
import sqlite3
import sys
import threading
import time
from copy import deepcopy
from pathlib import Path

import pytest
from karajan.execution import Activation, LaunchDenied, ProcessSpec, RunnerHost
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


@pytest.mark.parametrize("contents", [b"", b"not a sqlite ledger"])
def test_existing_only_rejects_empty_or_malformed_ledger_without_repair(
    tmp_path, binding_case, contents
):
    service, _, _, _ = _service(tmp_path, binding_case)
    database = tmp_path / "invalid-reviewer.sqlite"
    database.write_bytes(contents)
    with pytest.raises(RunError, match="REVIEWER_EXECUTION_LEDGER_UNAVAILABLE"):
        ReviewerExecutionIntents(
            database,
            service.admissions,
            service.candidates,
            source=service.source,
            host=RunnerHost(service.host.directory, existing_only=True),
            launch_compiler=service.launch_compiler,
            existing_only=True,
        )
    assert database.read_bytes() == contents


def test_existing_only_rejects_deleted_ledger_before_reopening_or_creating_it(
    tmp_path, binding_case
):
    service, run_id, reviewer_id, _ = _service(tmp_path, binding_case)
    service.prepare(run_id, reviewer_id, principal="owner", command_key="prepare")
    reopened = ReviewerExecutionIntents(
        service.database,
        service.admissions,
        service.candidates,
        source=service.source,
        host=RunnerHost(service.host.directory, existing_only=True),
        launch_compiler=service.launch_compiler,
        existing_only=True,
    )
    service.database.unlink()
    with pytest.raises(RunError, match="REVIEWER_EXECUTION_LEDGER_MISSING"):
        reopened.read(run_id, reviewer_id, principal="owner")
    assert not service.database.exists()


def test_existing_only_rejects_hardlinked_ledger_before_opening_it(tmp_path, binding_case):
    service, _, _, _ = _service(tmp_path, binding_case)
    alias = tmp_path / "reviewer-alias.sqlite"
    os.link(service.database, alias)
    before = service.database.read_bytes()
    with pytest.raises(RunError, match="REVIEWER_EXECUTION_LEDGER_UNAVAILABLE"):
        ReviewerExecutionIntents(
            alias,
            service.admissions,
            service.candidates,
            source=service.source,
            host=RunnerHost(service.host.directory, existing_only=True),
            launch_compiler=service.launch_compiler,
            existing_only=True,
        )
    assert service.database.read_bytes() == before


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


def test_concurrent_independent_facades_replay_one_prepared_identity(tmp_path, binding_case):
    service, run_id, reviewer_id, _ = _service(tmp_path, binding_case)
    other = ReviewerExecutionIntents(
        service.database, service.admissions, service.candidates, source=service.source,
        host=service.host, launch_compiler=service.launch_compiler,
    )
    barrier = threading.Barrier(2)
    original = service._compiled

    def synchronized(*args):
        result = original(*args)
        barrier.wait(timeout=5)
        return result

    service._compiled = synchronized
    other._compiled = synchronized
    results: list[dict[str, object]] = []

    def prepare(facade):
        results.append(facade.prepare(run_id, reviewer_id, principal="owner", command_key="same"))

    threads = [threading.Thread(target=prepare, args=(facade,)) for facade in (service, other)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert len(results) == 2
    assert {result["execution_id"] for result in results} == {results[0]["execution_id"]}
    with pytest.raises(RunError, match="REVIEWER_EXECUTION_ALREADY_PREPARED"):
        service.prepare(run_id, reviewer_id, principal="owner", command_key="different")


def test_ledger_uses_wal_and_compiler_does_not_hold_its_writer(tmp_path, binding_case):
    service, run_id, reviewer_id, _ = _service(tmp_path, binding_case)
    observed = []

    def compiler(value):
        db = sqlite3.connect(service.database, timeout=0.1, isolation_level=None)
        try:
            db.execute("BEGIN IMMEDIATE")
            observed.append(db.execute("PRAGMA journal_mode").fetchone()[0])
            db.commit()
        finally:
            db.close()
        return ReviewerLaunchSpec(ProcessSpec(("fixture",), tmp_path), "3" * 64)

    service.launch_compiler = compiler
    service.prepare(run_id, reviewer_id, principal="owner", command_key="prepare")
    service.freeze_launch(run_id, reviewer_id, principal="owner")
    assert observed == ["wal"]


def test_tampered_persisted_intent_is_rejected_without_a_host_effect(tmp_path, binding_case):
    service, run_id, reviewer_id, _ = _service(tmp_path, binding_case)
    intent = service.prepare(run_id, reviewer_id, principal="owner", command_key="prepare")
    db = sqlite3.connect(service.database)
    try:
        stored = json.loads(
            db.execute(
                "SELECT intent FROM reviewer_executions WHERE execution_id=?",
                (intent["execution_id"],),
            ).fetchone()[0]
        )
        stored["candidate"]["tree_sha"] = "f" * 64
        db.execute(
            "UPDATE reviewer_executions SET intent=? WHERE execution_id=?",
            (json.dumps(stored, sort_keys=True), intent["execution_id"]),
        )
        db.commit()
    finally:
        db.close()
    with pytest.raises(RunError, match="REVIEWER_EXECUTION_BINDING_INVALID"):
        service.freeze_launch(run_id, reviewer_id, principal="owner")
    with pytest.raises(KeyError):
        service.host.inspect(intent["planned_attempt_id"])


def test_current_qualification_change_blocks_next_host_prepare(tmp_path, binding_case):
    service, run_id, reviewer_id, _ = _service(tmp_path, binding_case)
    service.prepare(run_id, reviewer_id, principal="owner", command_key="prepare")
    binding_case[1].generation = 2
    with pytest.raises(RunError):
        service.freeze_launch(run_id, reviewer_id, principal="owner")
    with pytest.raises(KeyError):
        current = service.read(run_id, reviewer_id, principal="owner")
        assert current is not None
        service.host.inspect(current["planned_attempt_id"])


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


@pytest.mark.skipif(
    sys.platform == "win32", reason="Host direct-child identity is Linux P evidence"
)
@pytest.mark.parametrize("mode", ["reply", "lost-reply", "concurrent"])
def test_existing_store_direct_child_claim_is_one_shot_and_cancelled_recovery_stays_blocked(
    tmp_path, binding_case, mode
):
    """A registered Host child, rather than the controller, owns the claim."""
    service, run_id, reviewer_id, _ = _service(tmp_path, binding_case)
    qualification = binding_case[1]
    observed_facts = []
    original_facts = qualification._facts

    def record_facts(*args):
        value = original_facts(*args)
        observed_facts.append(deepcopy(value))
        return value

    qualification._facts = record_facts
    source = service.source
    child = Path(__file__).with_name("reviewer_execution_test_child.py").resolve()
    service.launch_compiler = lambda _: ReviewerLaunchSpec(
        ProcessSpec(
            (
                sys.executable,
                "-I",
                str(child),
                run_id,
                reviewer_id,
                "owner",
                *((mode,) if mode != "reply" else ()),
            ),
            tmp_path,
            20,
        ),
        "3" * 64,
    )
    intent = service.prepare(run_id, reviewer_id, principal="owner", command_key="prepare")
    prepared = service.freeze_launch(run_id, reviewer_id, principal="owner")
    assert observed_facts
    (tmp_path / "reviewer-execution-test-port.json").write_text(
        json.dumps(
            {
                "stores": {
                    "projects": str(service.admissions.routing.planner.projects.database),
                    "runs": str(service.admissions.routing.planner.database),
                    "capacity": str(service.admissions.routing.capacity.path),
                    "admissions": str(service.admissions.database),
                },
                "candidate_directory": str(service.candidates.directory),
                "host_directory": str(service.host.directory),
                "execution_database": str(service.database),
                "allowed_roots": [
                    str(path) for path in service.admissions.routing.planner.projects.allowed_roots
                ],
                "project_clock": service.admissions.routing.planner.projects.clock(),
                "planner_clock": service.admissions.routing.planner.clock(),
                "capacity_clock": service.admissions.routing.capacity.clock(),
                "estimate_clock": service.admissions.routing.estimates.clock(),
                "source": {
                    "runner_source_sha256": source.runner_source_sha256,
                    "native_source_sha256": source.native_source_sha256,
                },
                "qualification_facts": observed_facts[-1],
            }
        )
    )
    activation = Activation(
        "reviewer-test-activation",
        intent["planned_attempt_id"],
        intent["fence"],
        intent["authorization_ref"],
        intent["budget_ref"],
        time.time() + 30,
    )
    service.host.start(prepared["start_key"], activation)
    result_path = tmp_path / "reviewer-execution-test-child-result.json"
    deadline = time.monotonic() + 30
    while (
        mode == "lost-reply"
        and service.read(run_id, reviewer_id, principal="owner")["effect_claim"] is None
    ):
        assert time.monotonic() < deadline, "lost-reply direct child did not commit"
        time.sleep(0.02)
    while mode != "lost-reply" and not result_path.exists():
        assert time.monotonic() < deadline, "registered direct child did not reply"
        time.sleep(0.02)
    result = {} if mode == "lost-reply" else json.loads(result_path.read_text())
    assert result.get("claim_allowed", True) is True, result
    if mode == "concurrent":
        assert sorted(result["claims"]) == [False, True]
    claimed = service.read(run_id, reviewer_id, principal="owner")
    if mode != "lost-reply":
        assert claimed["effect_claim"]["runner"]["pid"] == result["pid"]
    else:
        assert not result_path.exists()
    reopened = ReviewerExecutionIntents(
        service.database,
        service.admissions,
        service.candidates,
        source=source,
        host=RunnerHost(service.host.directory, existing_only=True),
        launch_compiler=service.launch_compiler,
        current_source=lambda: source,
        existing_only=True,
    )
    assert (
        reopened.claim_registered_observer(run_id, reviewer_id, principal="owner")["claim_allowed"]
        is False
    )
    before_replay = reopened.host.inspect(prepared["planned_attempt_id"])
    replayed = reopened.host.start(prepared["start_key"], activation)
    # A lost controller reply may observe the original child already exited.
    # Its terminal state is canonical; replay identifies the original launch
    # and never accepts a second supervisor/process identity.
    assert replayed == reopened.host.inspect(prepared["planned_attempt_id"])
    assert replayed.prepared_id == prepared["start_key"]
    assert replayed.attempt_id == prepared["planned_attempt_id"]
    assert replayed.launch_phase == before_replay.launch_phase == "acknowledged"
    assert replayed.supervisor == before_replay.supervisor
    assert replayed.processes == before_replay.processes
    assert reopened.cancel(run_id, reviewer_id, principal="owner")["cancel_requested"] is True
    with pytest.raises(RunError, match="REVIEWER_EXECUTION_CANCELLED"):
        reopened.claim_registered_observer(run_id, reviewer_id, principal="owner")
