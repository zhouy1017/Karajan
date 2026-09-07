"""Approved Checks facade against real Run, operation, Candidate and Evidence stores.

The inherited producer/qualification is an explicit synthetic boundary fixture;
it does not claim a real Commander or native model observation.
"""

import hashlib
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest
from karajan.execution import LaunchDenied, ProcessIdentity, ProcessSpec, RunnerHost
from karajan.orchestration.go_task_collector import ApprovedGoCollector
from karajan.runs import RunError
from test_go_task_collector import (
    captured_case,
    case,
    collection_case,
    projected,
    workspace_case,
)

__all__ = ["captured_case", "case", "collection_case", "projected", "workspace_case"]


@pytest.fixture
def check_case(collection_case):
    intents, args, candidates, journal, result = collection_case
    collector = ApprovedGoCollector(intents, candidates, journal, source_check=lambda: None)
    candidate = collector.collect(
        *args, principal="owner", runner=intents.host.runner, result=result
    )
    return intents, args, candidates, candidate


def test_unprepared_checks_history_is_read_only_and_does_not_require_a_runner(check_case):
    from karajan.orchestration.candidate_checks import ApprovedCandidateChecks

    intents, args, candidates, _ = check_case
    before = intents.read(*args, principal="owner")
    checks = ApprovedCandidateChecks(intents.admissions, candidates)
    assert checks.get(*args, principal="owner") is None
    assert intents.read(*args, principal="owner") == before


def test_checks_history_rejects_other_owner(check_case):
    from karajan.orchestration.candidate_checks import ApprovedCandidateChecks

    intents, args, candidates, _ = check_case
    checks = ApprovedCandidateChecks(intents.admissions, candidates)
    with pytest.raises(RunError, match="USER_DECISION_REQUIRED"):
        checks.get(*args, principal="other-owner")


@pytest.mark.parametrize("ledger", ["operation", "run"])
def test_checks_history_does_not_replace_a_missing_ledger(check_case, ledger):
    from karajan.orchestration.candidate_checks import ApprovedCandidateChecks

    intents, args, candidates, _ = check_case
    checks = ApprovedCandidateChecks(intents.admissions, candidates)
    path = (
        intents.admissions.database
        if ledger == "operation"
        else intents.admissions.routing.planner.database
    )
    path.rename(path.with_suffix(".retained"))
    with pytest.raises(sqlite3.OperationalError):
        checks.get(*args, principal="owner")
    assert not path.exists()


def test_writer_enqueue_claims_shared_run_budget_once_without_replay_reset(check_case):
    from karajan.orchestration.execution_budget import RunExecutionBudget

    intents, args, _, _ = check_case
    budgets = RunExecutionBudget(intents.admissions)
    budget = budgets.get(args[0], principal="owner")
    assert budget["started_at"] == 1000.0
    assert len(budget["claims"]) == 1
    assert budget["claims"][0]["scope"] == "writer"
    assert budget["claims"][0]["operation_id"] == args[1]
    intents.prepare_intent(*args, principal="owner", command_key="prepare")
    assert budgets.get(args[0], principal="owner") == budget


class SourceFixture:
    """Explicit runtime source boundary double; no process or qualification claim."""

    def source(self, environment):
        return {
            "schema_version": "synthetic.check-source.v1",
            "environment_sha256": "e" * 64,
        }

    def inspect(self, execution):
        return None


def test_prepare_compiles_check_from_original_approval_and_full_candidate(check_case):
    from karajan.orchestration.candidate_checks import ApprovedCandidateChecks
    from karajan.orchestration.execution_budget import RunExecutionBudget

    intents, args, candidates, captured = check_case
    checks = ApprovedCandidateChecks(
        intents.admissions,
        candidates,
        runner=SourceFixture(),
        controller_source=lambda: {"schema_version": "synthetic.check-controller.v1"},
    )
    prepared = checks.advance(*args, principal="owner")
    assert prepared["checks"]["phase"] == "prepared"
    assert prepared["subject"]["candidate"]["id"] == captured["id"]
    execution = prepared["checks"]["runs"][0]
    assert execution["check"]["id"] == "tests"
    assert execution["check"]["argv"] == ["python", "-m", "pytest"]
    assert execution["environment"]["network"] == "none"
    assert execution["phase"] == "prepared"
    assert (
        len(RunExecutionBudget(intents.admissions).get(args[0], principal="owner")["claims"]) == 1
    )
    reopened = ApprovedCandidateChecks(intents.admissions, candidates)
    assert reopened.get(*args, principal="owner") == prepared
    assert prepared["delivery_eligible"] is False


def test_check_claim_commits_original_identity_and_shared_time_before_host_start(
    check_case, tmp_path
):
    from karajan.orchestration.candidate_checks import ApprovedCandidateChecks, CheckLaunchSpec
    from karajan.orchestration.execution_budget import RunExecutionBudget

    intents, args, candidates, _ = check_case
    checks = ApprovedCandidateChecks(
        intents.admissions,
        candidates,
        runner=SourceFixture(),
        controller_source=lambda: {"schema_version": "synthetic.check-controller.v1"},
        launch_compiler=lambda execution: CheckLaunchSpec(
            ProcessSpec((sys._base_executable, "-c", "pass"), tmp_path), "b" * 64
        ),
    )
    before = checks.advance(*args, principal="owner")
    claimed = checks.advance(*args, principal="owner")
    old, new = before["checks"]["runs"][0], claimed["checks"]["runs"][0]
    assert new["phase"] == "claimed"
    assert new["check_run_id"] == old["check_run_id"]
    assert new["attempt_id"] == old["attempt_id"]
    assert new["claimed_at"] == 1000.0
    assert new["deadline"] <= 1060.0
    assert new["host_prepared_id"] is None
    budget = RunExecutionBudget(intents.admissions).get(args[0], principal="owner")
    assert budget["started_at"] == 1000.0
    assert [item["scope"] for item in budget["claims"]] == ["writer", "check"]


def test_cancel_prepared_checks_keeps_capture_and_does_not_require_current_source(check_case):
    from karajan.orchestration.candidate_checks import ApprovedCandidateChecks

    intents, args, candidates, captured = check_case
    writer = ApprovedCandidateChecks(
        intents.admissions,
        candidates,
        runner=SourceFixture(),
        controller_source=lambda: {"schema_version": "synthetic.check-controller.v1"},
    )
    writer.advance(*args, principal="owner")
    history = ApprovedCandidateChecks(intents.admissions, candidates)
    cancelled = history.cancel(*args, principal="owner")
    assert cancelled["checks"]["phase"] == "cancelled"
    assert all(row["phase"] == "cancelled" for row in cancelled["checks"]["runs"])
    original = intents.read(*args, principal="owner")
    assert original["cancel_requested"] is True
    assert original["execution"]["collection"]["candidate"] == captured
    assert history.reconcile(*args, principal="owner") == cancelled


def test_all_approved_check_ids_are_compiled_from_new_public_policy(projected, tmp_path):
    from candidate_checks_case import approved_check_candidate
    from karajan.orchestration.candidate_checks import ApprovedCandidateChecks

    reference = {"id": "checks-python", "revision": 1}
    environment = {
        **reference,
        "runtime_kind": "python312-stdlib",
        "platform": "linux_x64",
        "source_sha256": "e" * 64,
        "filesystem": "candidate_copy",
        "network": "none",
        "env": {},
        "max_log_bytes": 65536,
    }
    definitions = [
        {
            "id": "content-contract",
            "revision": 1,
            "argv": ["python", "-c", "print('first')"],
            "environment_ref": reference,
            "timeout_seconds": 20,
        },
        {
            "id": "syntax-contract",
            "revision": 1,
            "argv": ["python", "-m", "compileall", "src"],
            "environment_ref": reference,
            "timeout_seconds": 20,
        },
    ]
    case = approved_check_candidate(
        projected, tmp_path / "two-checks", environment=environment, checks=definitions
    )
    prepared = ApprovedCandidateChecks(
        case.admissions,
        case.candidates,
        runner=SourceFixture(),
        controller_source=lambda: {"schema_version": "synthetic.check-controller.v1"},
    ).advance(*case.args, principal="owner")
    actual = prepared["checks"]["runs"]
    assert [row["check"] for row in actual] == definitions
    assert len({row["attempt_id"] for row in actual}) == 2


def test_check_prepares_real_host_without_spawning_and_keeps_local_identity(check_case, tmp_path):
    from karajan.orchestration.candidate_checks import ApprovedCandidateChecks, CheckLaunchSpec

    intents, args, candidates, _ = check_case
    host = RunnerHost(tmp_path / "check-host")
    checks = ApprovedCandidateChecks(
        intents.admissions,
        candidates,
        host=host,
        runner=SourceFixture(),
        controller_source=lambda: {"schema_version": "synthetic.check-controller.v1"},
        launch_compiler=lambda execution: CheckLaunchSpec(
            ProcessSpec((sys._base_executable, "-c", "pass"), tmp_path), "b" * 64
        ),
    )
    checks.advance(*args, principal="owner")
    checks.advance(*args, principal="owner")
    prepared = checks.advance(*args, principal="owner")
    row = prepared["checks"]["runs"][0]
    assert row["phase"] == "host_prepared"
    snapshot = host.inspect(row["attempt_id"])
    assert snapshot.state == "prepared"
    assert snapshot.supervisor is None
    assert row["launch"]["manifest"]["role"] == "check"
    assert "requested_binding" not in row["launch"]["manifest"]


def test_fixed_host_child_starts_once_and_reopen_never_restarts_it(projected, tmp_path):
    from candidate_checks_case import approved_check_candidate
    from karajan.orchestration.candidate_checks import ApprovedCandidateChecks, CheckLaunchSpec

    ref = {"id": "checks-python", "revision": 1}
    environment = {
        **ref,
        "runtime_kind": "python312-stdlib",
        "platform": "linux_x64",
        "source_sha256": "e" * 64,
        "filesystem": "candidate_copy",
        "network": "none",
        "env": {},
        "max_log_bytes": 65536,
    }
    case = approved_check_candidate(
        projected,
        tmp_path / "host-case",
        environment=environment,
        checks=[
            {
                "id": "local-check",
                "revision": 1,
                "argv": ["python", "-c", "print('ok')"],
                "environment_ref": ref,
                "timeout_seconds": 20,
            }
        ],
    )
    marker = tmp_path / "host-child-started"
    host = RunnerHost(tmp_path / "actual-check-host")

    def source():
        return {"schema_version": "synthetic.check-controller.v1"}

    service = ApprovedCandidateChecks(
        case.admissions,
        case.candidates,
        runner=SourceFixture(),
        host=host,
        controller_source=source,
        launch_compiler=lambda execution: CheckLaunchSpec(
            ProcessSpec(
                (
                    sys._base_executable,
                    "-c",
                    "from pathlib import Path; Path("
                    + repr(str(marker))
                    + ").open('x').write('once')",
                ),
                tmp_path,
            ),
            "b" * 64,
        ),
    )
    for _ in range(4):
        state = service.advance(*case.args, principal="owner")
    row = state["checks"]["runs"][0]
    until = time.monotonic() + 8
    while not marker.exists() and time.monotonic() < until:
        time.sleep(0.05)
    assert marker.read_text() == "once"
    reopened = ApprovedCandidateChecks(
        case.admissions, case.candidates, runner=SourceFixture(), host=host
    )
    for _ in range(2):
        recovered = reopened.reconcile(*case.args, principal="owner")
    assert recovered["checks"]["runs"][0]["attempt_id"] == row["attempt_id"]
    assert marker.read_text() == "once"
    with pytest.raises(LaunchDenied, match="RUNNER_IDENTITY_NOT_CURRENT"):
        service.consume_check(
            *case.args,
            row["check_run_id"],
            principal="owner",
            runner_identity=ProcessIdentity(123, "not-the-child"),
        )


class CheckProcessBoundary(SourceFixture):
    """Explicit native observation double; persistence and Candidate CAS are real.

    Real Host-child and Linux namespace integration lives in the separate P suite.
    These tests isolate fault ordering at the public trusted runner port.
    """

    def __init__(self, exits=(0,)):
        self.exits = iter(exits)
        self.observations = {}
        self.logs = {}
        self.starts = 0

    def run(self, execution, *, start_guard, cancelled):
        from karajan.isolation.check_runner import CheckObservation
        from karajan.routing.compiler import digest

        assert not cancelled()
        with start_guard():
            self.starts += 1
        log = b"synthetic trusted-runner boundary observation\n"
        observed = CheckObservation(
            digest(execution),
            "completed",
            next(self.exits),
            "confirmed",
            True,
            hashlib.sha256(log).hexdigest(),
            len(log),
            "synthetic-observation:" + execution["check_run_id"],
            "synthetic-check-runner",
            execution["environment"]["source_sha256"],
        )
        self.observations[execution["check_run_id"]] = observed
        self.logs[execution["check_run_id"]] = log
        return observed

    def inspect(self, execution):
        return self.observations.get(execution["check_run_id"])

    def read_log(self, execution, observation):
        return self.logs.get(execution["check_run_id"])


class CheckHostIdentityBoundary(RunnerHost):
    """Only direct-child authority is synthetic; prepare/start/ledger are actual Host."""

    identity = ProcessIdentity(42, "synthetic-check-runner")

    def wait_for_runner_registration(self, attempt_id, *, timeout_seconds=10):
        return self.identity

    @contextmanager
    def current_runner_guard(self, attempt_id, *, fence, authorization_ref):
        yield self.identity


def check_workflow(projected, tmp_path, exits=(0,)):
    from candidate_checks_case import approved_check_candidate
    from karajan.orchestration.candidate_checks import ApprovedCandidateChecks, CheckLaunchSpec

    ref = {"id": "checks-python", "revision": 1}
    environment = {
        **ref,
        "runtime_kind": "python312-stdlib",
        "platform": "linux_x64",
        "source_sha256": "e" * 64,
        "filesystem": "candidate_copy",
        "network": "none",
        "env": {},
        "max_log_bytes": 65536,
    }
    case = approved_check_candidate(
        projected,
        tmp_path / "check-workflow",
        environment=environment,
        checks=[
            {
                "id": "check-" + str(index),
                "revision": 1,
                "argv": ["python", "-c", "pass"],
                "environment_ref": ref,
                "timeout_seconds": 20,
            }
            for index in range(len(exits))
        ],
    )
    runner = CheckProcessBoundary(exits)
    host = CheckHostIdentityBoundary(tmp_path / "boundary-host")
    service = ApprovedCandidateChecks(
        case.admissions,
        case.candidates,
        runner=runner,
        host=host,
        controller_source=lambda: {"schema_version": "synthetic.check-controller.v1"},
        launch_compiler=lambda execution: CheckLaunchSpec(
            ProcessSpec((sys._base_executable, "-c", "pass"), tmp_path), "b" * 64
        ),
    )
    return case, service, runner, host


def run_next_boundary_check(case, service, host):
    for _ in range(5):
        state = service.advance(*case.args, principal="owner")
        row = next(row for row in state["checks"]["runs"] if row["phase"] != "recorded")
        if row["phase"] == "host_started":
            return service.consume_check(
                *case.args, row["check_run_id"], principal="owner", runner_identity=host.identity
            )
    pytest.fail("The approved check did not reach its one-shot Host start")


def test_nonzero_check_does_not_omit_other_approved_checks_and_review_stays_missing(
    projected, tmp_path
):
    case, service, runner, host = check_workflow(projected, tmp_path, exits=(7, 0))
    run_next_boundary_check(case, service, host)
    service.advance(*case.args, principal="owner")
    run_next_boundary_check(case, service, host)
    final = service.advance(*case.args, principal="owner")
    assert runner.starts == 2
    assert [row["evidence"]["status"] for row in final["checks"]["runs"]] == ["failed", "passed"]
    assert final["checks"]["phase"] == "blocked"
    assert final["review"] == "not_run"
    assert final["local_gate_passed"] is False
    assert final["delivery_eligible"] is False


def test_lost_evidence_response_recovers_exact_history_after_cancel_without_a_runner(
    projected, tmp_path, monkeypatch
):
    from karajan.orchestration.candidate_checks import ApprovedCandidateChecks

    case, service, runner, host = check_workflow(projected, tmp_path)
    run_next_boundary_check(case, service, host)
    record = case.candidates.record_check
    committed = []

    def lose_reply(request, *, log):
        committed.append(record(request, log=log))
        raise OSError("synthetic response loss")

    monkeypatch.setattr(case.candidates, "record_check", lose_reply)
    pending = service.advance(*case.args, principal="owner")
    assert pending["checks"]["runs"][0]["evidence"] is None
    assert len(committed) == 1
    history = ApprovedCandidateChecks(case.admissions, case.candidates)
    recovered = history.cancel(*case.args, principal="owner")
    assert recovered["checks"]["runs"][0]["evidence"] == committed[0]
    assert recovered["checks"]["phase"] == "cancelled"
    assert history.reconcile(*case.args, principal="owner") == recovered
    assert len(committed) == runner.starts == 1


def test_uncommitted_evidence_reply_remains_unknown_without_another_submission(
    projected, tmp_path, monkeypatch
):
    case, service, runner, host = check_workflow(projected, tmp_path)
    run_next_boundary_check(case, service, host)
    attempts = []

    def unavailable(request, *, log):
        attempts.append(request)
        raise OSError("synthetic unavailable database")

    monkeypatch.setattr(case.candidates, "record_check", unavailable)
    pending = service.advance(*case.args, principal="owner")
    for _ in range(3):
        recovered = service.advance(*case.args, principal="owner")
    assert len(attempts) == runner.starts == 1
    assert recovered["checks"]["runs"][0]["evidence"] is None
    assert (
        recovered["checks"]["runs"][0]["evidence_submit_claim"]
        == pending["checks"]["runs"][0]["evidence_submit_claim"]
    )
    assert recovered["local_gate_passed"] is False


@pytest.mark.parametrize("failure", ["unknown_stop", "missing_log", "incomplete_log", "timed_out"])
def test_incomplete_observation_never_passes_or_starts_next_check(
    projected, tmp_path, monkeypatch, failure
):
    from dataclasses import replace

    case, service, runner, host = check_workflow(projected, tmp_path, exits=(0, 0))
    run = runner.run

    def incomplete(*args, **kwargs):
        result = run(*args, **kwargs)
        if failure == "unknown_stop":
            return replace(result, local_stop="unknown")
        if failure == "incomplete_log":
            return replace(result, log_complete=False)
        if failure == "timed_out":
            return replace(result, outcome="timed_out")
        runner.logs.clear()
        return result

    monkeypatch.setattr(runner, "run", incomplete)
    run_next_boundary_check(case, service, host)
    final = service.advance(*case.args, principal="owner")
    assert final["checks"]["runs"][0]["evidence"]["status"] == "inconclusive"
    assert final["checks"]["phase"] == "blocked"
    service.advance(*case.args, principal="owner")
    assert runner.starts == 1


def test_native_result_commit_reply_loss_recovers_without_new_native_start(
    projected, tmp_path, monkeypatch
):
    case, service, runner, host = check_workflow(projected, tmp_path)
    run = runner.run

    def lose_reply(*args, **kwargs):
        run(*args, **kwargs)
        raise OSError("synthetic result response loss")

    monkeypatch.setattr(runner, "run", lose_reply)
    with pytest.raises(RunError, match="CANDIDATE_CHECK_EXECUTION_FAILED"):
        run_next_boundary_check(case, service, host)
    recovered = service.reconcile(*case.args, principal="owner")
    assert recovered["checks"]["runs"][0]["phase"] == "observed"
    final = service.advance(*case.args, principal="owner")
    assert final["checks"]["phase"] == "checks_passed"
    assert runner.starts == 1


def test_concurrent_evidence_advances_use_one_committed_submission(
    projected, tmp_path, monkeypatch
):
    case, service, runner, host = check_workflow(projected, tmp_path)
    run_next_boundary_check(case, service, host)
    entered, release = threading.Event(), threading.Event()
    record, calls = case.candidates.record_check, []

    def held(request, *, log):
        calls.append(request)
        entered.set()
        assert release.wait(10)
        return record(request, log=log)

    monkeypatch.setattr(case.candidates, "record_check", held)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(service.advance, *case.args, principal="owner")
        try:
            assert entered.wait(10)
            pending = pool.submit(service.advance, *case.args, principal="owner").result(10)
            assert pending["checks"]["runs"][0]["evidence"] is None
        finally:
            release.set()
        final = first.result(10)
    assert final["checks"]["phase"] == "checks_passed"
    assert len(calls) == runner.starts == 1


def test_cancel_after_host_prepare_prevents_any_host_or_native_start(
    projected, tmp_path, monkeypatch
):
    case, service, runner, host = check_workflow(projected, tmp_path)
    for _ in range(3):
        prepared = service.advance(*case.args, principal="owner")
    attempt = prepared["checks"]["runs"][0]["attempt_id"]
    assert host.inspect(attempt).supervisor is None
    cancelled = service.cancel(*case.args, principal="owner")
    service.advance(*case.args, principal="owner")
    assert cancelled["checks"]["phase"] == "cancelled"
    assert host.inspect(attempt).supervisor is None
    assert runner.starts == 0


@pytest.mark.parametrize("stage", ["prepared", "host_prepared"])
def test_queued_check_expiry_blocks_next_process_and_preserves_history(
    projected, tmp_path, monkeypatch, stage
):
    from karajan.orchestration.execution_budget import RunExecutionBudget

    case, service, runner, host = check_workflow(projected, tmp_path)
    for _ in range(1 if stage == "prepared" else 3):
        original = service.advance(*case.args, principal="owner")
    budget = RunExecutionBudget(case.admissions).get(case.args[0], principal="owner")
    deadline = budget["started_at"] + budget["max_duration_seconds"]
    monkeypatch.setattr(case.admissions.routing.planner, "clock", lambda: deadline)
    with pytest.raises(RunError, match="RUN_DURATION_LIMIT"):
        service.advance(*case.args, principal="owner")
    assert service.get(*case.args, principal="owner") == original
    assert runner.starts == 0
    if stage == "host_prepared":
        assert host.inspect(original["checks"]["runs"][0]["attempt_id"]).supervisor is None


def test_new_owner_approval_prevents_start_of_prior_prepared_check(projected, tmp_path):
    from copy import deepcopy

    from test_routing_authorization import approve_request

    case, service, runner, host = check_workflow(projected, tmp_path)
    for _ in range(3):
        original = service.advance(*case.args, principal="owner")
    planner = case.admissions.routing.planner
    run = planner.get(case.args[0], principal="owner")
    previous = run["plans"][-1]
    plan = deepcopy(previous["plan"])
    plan["summary"] = "Changed plan requires a fresh owner approval"
    replacement = planner.submit_plan(
        case.args[0],
        {
            "schema_version": "karajan.submit-plan.v2",
            "term": previous["term"],
            "intent_id": previous["intent_id"],
            "expected_plan_revision": previous["plan_revision"],
            "plan": plan,
        },
        principal="lead",
        command_key="replacement-plan",
    )
    # A proposal alone deliberately retains the prior approved Plan. The new
    # owner approval is the actual authority transition this boundary tests.
    planner.approve_plan(
        case.args[0],
        approve_request(replacement),
        principal="owner",
        command_key="replacement-approval",
    )
    with pytest.raises(RunError, match="APPROVED_PLAN_REQUIRED|APPROVAL_BINDING_MISMATCH"):
        service.advance(*case.args, principal="owner")
    assert runner.starts == 0
    assert service.get(*case.args, principal="owner") == original
    assert host.inspect(original["checks"]["runs"][0]["attempt_id"]).supervisor is None


@pytest.mark.parametrize("changed", ["environment", "controller"])
def test_current_source_change_blocks_start_without_replacing_frozen_identity(
    projected, tmp_path, monkeypatch, changed
):
    case, service, runner, host = check_workflow(projected, tmp_path)
    for _ in range(3):
        original = service.advance(*case.args, principal="owner")
    if changed == "environment":
        monkeypatch.setattr(runner, "source", lambda _: {"environment_sha256": "f" * 64})
    else:
        monkeypatch.setattr(service, "controller_source", lambda: {"changed": True})
    with pytest.raises(
        RunError, match="CANDIDATE_CHECK_ENVIRONMENT_CHANGED|CANDIDATE_CHECKS_BINDING_CHANGED"
    ):
        service.advance(*case.args, principal="owner")
    assert runner.starts == 0
    assert service.get(*case.args, principal="owner") == original
    assert host.inspect(original["checks"]["runs"][0]["attempt_id"]).supervisor is None
