"""Subject consumer C tests: real ledgers/CAS, explicit qualification producer double."""

from copy import deepcopy

import pytest
from karajan.routing.compiler import digest
from karajan.runs import RunError
from test_candidate_checks import (
    check_workflow,
    run_next_boundary_check,
)
from test_go_execution_intent import case, projected

__all__ = ["case", "projected"]


def synthetic_transition(case, *, ready=True, persist=True):
    """Controller producer test double; never a public binding/qualification API."""
    from karajan.orchestration.candidate_subjects import (
        candidate_identity,
        current_subject,
        mark_ready,
        stage_transition,
    )
    from karajan.orchestration.go_execution_intent import _connection

    with _connection(case.admissions.database, readonly=False) as db:
        operation = case.admissions._load(db, *case.args)
        source = operation["workspace"]["source_binding"]
        current = current_subject(operation, case.candidates)
        tasks = source["plan"]["plan"]["tasks"]
        reviewer_task = next(task for task in tasks if task["role"] == "reviewer")
        revision = operation["validation"].get("review_binding", {}).get("revision", 0) + 1
        binding = {
            "schema_version": "karajan.reviewer-binding.v1",
            "revision": revision,
            "source_candidate": candidate_identity(current["candidate"]),
            "run_id": case.args[0],
            "operation_id": case.args[1],
            "reviewer_task_id": reviewer_task["id"],
            "capture_digest": operation["execution"]["collection"]["capture_digest"],
            "approval_digest": digest(source["approval"]),
            "plan_digest": source["plan"]["plan_digest"],
            "execution_policy_digest": source["execution_policy"]["digest"],
            "reviewer_task_digest": digest(reviewer_task),
            "rulebook_digest": "d" * 64,
            "reviewer_sources": [
                {
                    "reviewer": {
                        "profile_id": "synthetic-independent-reviewer",
                        "profile_revision": 1,
                        "model_family": "synthetic-other-family",
                        "qualification_ref": "synthetic-role-qualification",
                    },
                    "qualification_source_digest": "a" * 64,
                    "authentication_source_digest": "b" * 64,
                }
            ],
        }
        transition = stage_transition(
            operation,
            binding,
            transition_id=f"synthetic-transition-{revision}",
            command_key=f"synthetic-rebind-{revision}",
            semantic_digest="c" * 64,
        )
        if ready:
            transition["phase"] = "rebind_claimed"
        if persist:
            case.admissions._save(db, operation)
    if ready:
        candidate = case.candidates.rebind_reviewers(binding, command_key=transition["command_key"])
        if not persist:
            return deepcopy(transition)
        with _connection(case.admissions.database, readonly=False) as db:
            operation = case.admissions._load(db, *case.args)
            mark_ready(operation, candidate)
            case.admissions._save(db, operation)
    return deepcopy(transition)


def synthetic_current(project_db, run, operation, transition, *, principal):
    """Explicit current-qualification boundary fixture; no real Reviewer qualification."""
    assert project_db.in_transaction
    assert principal == "owner"
    assert transition["binding"]["run_id"] == run["id"] == operation["run_id"]


def test_ready_subject_replaces_terminal_cycle_without_reusing_evidence(projected, tmp_path):
    case, service, runner, host = check_workflow(projected, tmp_path)
    run_next_boundary_check(case, service, host)
    before = service.advance(*case.args, principal="owner")
    assert before["checks"]["phase"] == "checks_passed"
    synthetic_transition(case)
    service.subject_validator = synthetic_current
    after = service.advance(*case.args, principal="owner")
    assert after["subject"]["revision"] == 2
    assert after["subject"]["source_candidate"] == before["subject"]["source_candidate"]
    assert after["subject"]["candidate"]["id"] != before["subject"]["candidate"]["id"]
    assert after["history"][0]["checks"] == before["checks"]
    assert after["checks"]["runs"][0]["phase"] == "prepared"
    assert after["checks"]["runs"][0]["evidence_key"] != before["checks"]["runs"][0]["evidence_key"]
    assert after["review"] == "not_run"
    assert not after["local_gate_passed"]
    assert runner.starts == 1


def test_every_check_reruns_and_shared_budget_and_original_capture_are_preserved(
    projected, tmp_path
):
    from karajan.orchestration.execution_budget import RunExecutionBudget
    from karajan.orchestration.go_execution_intent import GoExecutionIntents

    case, service, runner, host = check_workflow(projected, tmp_path, exits=(0, 0))
    for _ in range(2):
        run_next_boundary_check(case, service, host)
        old = service.advance(*case.args, principal="owner")
    original = GoExecutionIntents.read_operation(case.admissions, *case.args, principal="owner")
    budget = RunExecutionBudget(case.admissions).get(case.args[0], principal="owner")
    synthetic_transition(case)
    service.subject_validator = synthetic_current
    service.advance(*case.args, principal="owner")
    runner.exits = iter((0, 0))
    for _ in range(2):
        run_next_boundary_check(case, service, host)
        final = service.advance(*case.args, principal="owner")
    assert runner.starts == 4
    assert final["checks"]["phase"] == "checks_passed"
    assert final["history"][0]["checks"] == old["checks"]
    assert {r["evidence"]["id"] for r in old["checks"]["runs"]}.isdisjoint(
        r["evidence"]["id"] for r in final["checks"]["runs"]
    )
    retained = GoExecutionIntents.read_operation(case.admissions, *case.args, principal="owner")
    assert retained["execution"]["collection"] == original["execution"]["collection"]
    after = RunExecutionBudget(case.admissions).get(case.args[0], principal="owner")
    assert after["started_at"] == budget["started_at"]
    assert len(after["claims"]) == len(budget["claims"]) + 2
    assert final["review"] == "not_run" and not final["delivery_eligible"]


def test_ready_receipt_without_configured_current_authority_never_installs(projected, tmp_path):
    case, service, runner, _ = check_workflow(projected, tmp_path)
    service.advance(*case.args, principal="owner")
    synthetic_transition(case)
    with pytest.raises(RunError, match="REVIEW_BINDING_PRODUCER_REQUIRED"):
        service.advance(*case.args, principal="owner")
    assert service.get(*case.args, principal="owner")["subject"]["revision"] == 1
    assert runner.starts == 0


def test_pending_transition_fences_prepared_old_check_without_consuming_budget(projected, tmp_path):
    from karajan.orchestration.execution_budget import RunExecutionBudget

    case, service, runner, _ = check_workflow(projected, tmp_path)
    service.advance(*case.args, principal="owner")
    budget = RunExecutionBudget(case.admissions).get(case.args[0], principal="owner")
    synthetic_transition(case, ready=False)
    current = service.advance(*case.args, principal="owner")
    assert current["checks"]["runs"][0]["phase"] == "prepared"
    assert RunExecutionBudget(case.admissions).get(case.args[0], principal="owner") == budget
    assert runner.starts == 0


def test_unknown_native_stop_prevents_switch_even_when_old_evidence_is_recorded(
    projected, tmp_path
):
    from dataclasses import replace

    case, service, runner, host = check_workflow(projected, tmp_path)
    original_run = runner.run
    runner.run = lambda *args, **kwargs: replace(
        original_run(*args, **kwargs), local_stop="unknown"
    )
    run_next_boundary_check(case, service, host)
    old = service.advance(*case.args, principal="owner")
    assert old["checks"]["runs"][0]["phase"] == "recorded"
    synthetic_transition(case)
    service.subject_validator = synthetic_current
    with pytest.raises(RunError, match="REVIEW_SUBJECT_CHECK_STOP_REQUIRED"):
        service.advance(*case.args, principal="owner")
    assert service.get(*case.args, principal="owner")["subject"]["revision"] == 1


def test_current_qualification_withdrawal_blocks_rebound_check_but_not_history(projected, tmp_path):
    from karajan.orchestration.candidate_checks import ApprovedCandidateChecks

    case, service, runner, _ = check_workflow(projected, tmp_path)
    service.advance(*case.args, principal="owner")
    synthetic_transition(case)
    service.subject_validator = synthetic_current
    installed = service.advance(*case.args, principal="owner")

    def revoked(*args, **kwargs):
        raise RunError("SYNTHETIC_REVIEWER_REVOKED")

    service.subject_validator = revoked
    with pytest.raises(RunError, match="SYNTHETIC_REVIEWER_REVOKED"):
        service.advance(*case.args, principal="owner")
    history = ApprovedCandidateChecks(case.admissions, case.candidates)
    assert history.get(*case.args, principal="owner") == installed
    assert history.reconcile(*case.args, principal="owner") == installed
    assert runner.starts == 0


def test_third_revision_keeps_worker_anchor_and_exact_immediate_predecessor(projected, tmp_path):
    case, service, runner, _ = check_workflow(projected, tmp_path)
    original = service.advance(*case.args, principal="owner")
    service.subject_validator = synthetic_current
    synthetic_transition(case)
    second = service.advance(*case.args, principal="owner")
    synthetic_transition(case)
    third = service.advance(*case.args, principal="owner")
    assert third["subject"]["revision"] == 3
    assert len(third["history"]) == 2
    assert third["subject"]["source_candidate"] == original["subject"]["candidate"]
    assert third["review_binding"]["binding"]["source_candidate"] == second["subject"]["candidate"]
    assert runner.starts == 0


def test_old_child_replay_is_history_only_even_without_current_runner(projected, tmp_path):
    from karajan.execution import ProcessIdentity
    from karajan.orchestration.candidate_checks import ApprovedCandidateChecks

    case, service, runner, _ = check_workflow(projected, tmp_path)
    before = service.advance(*case.args, principal="owner")
    old_id = before["checks"]["runs"][0]["check_run_id"]
    service.subject_validator = synthetic_current
    synthetic_transition(case)
    installed = service.advance(*case.args, principal="owner")
    history = ApprovedCandidateChecks(case.admissions, case.candidates)
    assert (
        history.consume_check(
            *case.args, old_id, principal="owner", runner_identity=ProcessIdentity(1, "untrusted")
        )
        == installed
    )
    assert runner.starts == 0


def test_stopped_old_observation_can_record_late_evidence_without_overwriting_new_cycle(
    projected, tmp_path
):
    case, service, runner, host = check_workflow(projected, tmp_path)
    observed = run_next_boundary_check(case, service, host)
    assert observed["checks"]["runs"][0]["phase"] == "observed"
    synthetic_transition(case)
    service.subject_validator = synthetic_current
    installed = service.advance(*case.args, principal="owner")
    assert installed["history"][0]["checks"]["runs"][0]["evidence"] is None
    recovered = service.reconcile(*case.args, principal="owner")
    assert recovered["history"][0]["checks"]["runs"][0]["evidence"]["status"] == "passed"
    assert recovered["checks"] == installed["checks"]
    assert recovered["subject"] == installed["subject"]
    assert runner.starts == 1


def test_lost_candidate_receipt_read_retries_only_lookup_and_preserves_ready_intent(
    projected, tmp_path, monkeypatch
):
    case, service, runner, _ = check_workflow(projected, tmp_path)
    service.advance(*case.args, principal="owner")
    transition = synthetic_transition(case)
    service.subject_validator = synthetic_current
    lookup = case.candidates.lookup_review_rebind
    observed = []

    def lost(*args, **kwargs):
        observed.append(lookup(*args, **kwargs))
        raise OSError("Synthetic controller lookup response loss")

    monkeypatch.setattr(case.candidates, "lookup_review_rebind", lost)
    with pytest.raises(OSError):
        service.advance(*case.args, principal="owner")
    assert service.get(*case.args, principal="owner")["subject_transition"]["phase"] == "ready"
    monkeypatch.setattr(case.candidates, "lookup_review_rebind", lookup)

    def forbidden(*args, **kwargs):
        pytest.fail("The consumer cannot submit another Candidate rebind")

    monkeypatch.setattr(case.candidates, "rebind_reviewers", forbidden)
    recovered = service.advance(*case.args, principal="owner")
    assert recovered["subject"]["candidate"]["id"] == observed[0]["id"]
    assert recovered["subject_transition"]["id"] == transition["id"]
    assert runner.starts == 0


def test_cancel_ready_subject_does_not_install_or_clear_cancellation(projected, tmp_path):
    case, service, runner, _ = check_workflow(projected, tmp_path)
    service.advance(*case.args, principal="owner")
    synthetic_transition(case)
    service.subject_validator = synthetic_current
    cancelled = service.cancel(*case.args, principal="owner")
    with pytest.raises(RunError, match="CANDIDATE_CHECKS_CANCELLED"):
        service.advance(*case.args, principal="owner")
    current = service.get(*case.args, principal="owner")
    assert current["subject"] == cancelled["subject"]
    assert current["checks"]["phase"] == "cancelled"
    assert runner.starts == 0


def test_new_subject_does_not_reset_expired_run_budget(projected, tmp_path, monkeypatch):
    from karajan.orchestration.execution_budget import RunExecutionBudget

    case, service, runner, _ = check_workflow(projected, tmp_path)
    service.advance(*case.args, principal="owner")
    synthetic_transition(case)
    service.subject_validator = synthetic_current
    installed = service.advance(*case.args, principal="owner")
    budget = RunExecutionBudget(case.admissions).get(case.args[0], principal="owner")
    monkeypatch.setattr(
        case.admissions.routing.planner,
        "clock",
        lambda: budget["started_at"] + budget["max_duration_seconds"],
    )
    with pytest.raises(RunError, match="RUN_DURATION_LIMIT"):
        service.advance(*case.args, principal="owner")
    assert service.get(*case.args, principal="owner") == installed
    assert runner.starts == 0


def test_old_evidence_commit_lost_response_links_only_archived_cycle(
    projected, tmp_path, monkeypatch
):
    from karajan.orchestration.candidate_checks import ApprovedCandidateChecks

    case, service, runner, host = check_workflow(projected, tmp_path)
    run_next_boundary_check(case, service, host)
    original = case.candidates.record_check
    committed = []

    def lost(request, *, log):
        committed.append(original(request, log=log))
        raise OSError("Synthetic reply loss after actual Evidence commit")

    monkeypatch.setattr(case.candidates, "record_check", lost)
    service.advance(*case.args, principal="owner")
    synthetic_transition(case)
    service.subject_validator = synthetic_current
    installed = service.advance(*case.args, principal="owner")
    history = ApprovedCandidateChecks(case.admissions, case.candidates)
    recovered = history.reconcile(*case.args, principal="owner")
    assert recovered["history"][0]["checks"]["runs"][0]["evidence"] == committed[0]
    assert recovered["checks"] == installed["checks"]
    assert len(committed) == runner.starts == 1


def test_two_concurrent_installations_keep_one_new_cycle_and_stable_ids(projected, tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    case, service, runner, _ = check_workflow(projected, tmp_path)
    service.advance(*case.args, principal="owner")
    synthetic_transition(case)
    service.subject_validator = synthetic_current
    barrier = threading.Barrier(2)

    def advance():
        barrier.wait(10)
        return service.advance(*case.args, principal="owner")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [f.result(20) for f in [pool.submit(advance), pool.submit(advance)]]
    assert all(r["subject"]["revision"] == 2 and len(r["history"]) == 1 for r in results)
    assert (
        results[0]["checks"]["runs"][0]["check_run_id"]
        == results[1]["checks"]["runs"][0]["check_run_id"]
    )
    assert runner.starts == 0


def test_cas_rebind_alone_cannot_select_or_authorize_a_validation_subject(projected, tmp_path):
    case, service, runner, host = check_workflow(projected, tmp_path)
    run_next_boundary_check(case, service, host)
    previous = service.advance(*case.args, principal="owner")
    synthetic_transition(case, persist=False)
    service.subject_validator = synthetic_current
    assert service.advance(*case.args, principal="owner") == previous
    assert runner.starts == 1


def test_historical_rebind_read_needs_no_current_artifacts_or_source(projected, tmp_path):
    from karajan.orchestration.candidate_checks import ApprovedCandidateChecks

    case, service, runner, _ = check_workflow(projected, tmp_path)
    service.advance(*case.args, principal="owner")
    synthetic_transition(case)
    service.subject_validator = synthetic_current
    installed = service.advance(*case.args, principal="owner")
    case.candidates.objects.rename(case.candidates.directory / "retained-artifacts")
    history = ApprovedCandidateChecks(case.admissions, case.candidates)
    assert history.get(*case.args, principal="owner") == installed
    assert history.reconcile(*case.args, principal="owner") == installed
    assert not case.candidates.objects.exists()
    assert runner.starts == 0
