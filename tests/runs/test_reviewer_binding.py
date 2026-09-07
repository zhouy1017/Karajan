"""ID-only binding over real approved stores/CAS; no real Reviewer qualification."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from contextlib import contextmanager
from copy import deepcopy
from threading import Event

import pytest
from karajan.orchestration.candidate_checks import ApprovedCandidateChecks
from karajan.projects.qualification import ProfileQualificationStore, QualificationError
from karajan.routing import evaluate_route
from karajan.routing.compiler import digest
from karajan.runs import RunError
from test_candidate_checks import (
    SourceFixture,
    captured_case,
    case,
    check_case,
    collection_case,
    projected,
    workspace_case,
)

__all__ = ["captured_case", "case", "check_case", "collection_case", "projected", "workspace_case"]


def prepare(check_case):
    intents, args, candidates, captured = check_case
    checks = ApprovedCandidateChecks(
        intents.admissions,
        candidates,
        runner=SourceFixture(),
        controller_source=lambda: {"schema_version": "synthetic.check-controller.v1"},
    )
    checks.advance(*args, principal="owner")
    return intents, args, candidates, captured, checks


def test_current_worker_qualification_cannot_create_a_reviewer_binding(check_case):
    from karajan.orchestration.reviewer_binding import ApprovedReviewerBindings

    intents, args, candidates, captured, checks = prepare(check_case)
    admissions = intents.admissions
    service = ApprovedReviewerBindings(admissions, candidates, admissions.routing.qualifications)
    before = admissions.routing.capacity.path.read_bytes()
    result = service.advance(*args, principal="owner")
    assert result["state"] == "blocked"
    assert "REVIEWER_QUALIFICATION_REQUIRED" in result["reason_codes"]
    assert result["transition"] is None
    assert checks.get(*args, principal="owner")["subject"]["candidate"]["id"] == captured["id"]
    assert candidates.get(captured["id"])["request"]["policy"]["review"]["approved_reviewers"] == []
    assert admissions.routing.capacity.path.read_bytes() == before


class ReviewerQualificationFixture(ProfileQualificationStore):
    """Explicit trusted-source double for C only; never used by production factories."""

    generation = 1
    enabled = True
    mutate = None

    def __init__(self, original):
        self.projects = original.projects
        self.original = original

    def _facts(self, db, project_id, registration, scope, fixture_root):
        if not self.enabled:
            raise QualificationError("QUALIFICATION_REVOKED")
        observed = deepcopy(self.original._facts(db, project_id, registration, scope, fixture_root))
        observed["facts"].update(roles=["reviewer"], tools=["read"])
        # This explicit C double now supplies the readonly scope consumed by
        # production binding. It remains no evidence for this Worker's actual role.
        observed["qualification_scope"] = "readonly_reviewer_tools"
        observed["executor_scope"].update(
            schema_version="karajan.go-readonly-reviewer-executor-scope.v1",
            suite_ref={"id": "opencode-go-readonly-review-linux", "revision": 1},
            tools=["read"],
            supported_roles=["reviewer"],
            candidate_capture=False,
            output_policy="fixed_native_limit",
            output_parser_revision="karajan.review-output-parser.v1",
        )
        observed["observation"]["binding"]["synthetic_reviewer_generation"] = self.generation
        observed["observation"]["binding"]["execution_start"]["authentication_source"][
            "generation"
        ] = f"synthetic-generation-{self.generation}"
        for capability in ("code_review", "structured_findings"):
            observed["capability_evidence"].append(
                {
                    "capability": capability,
                    "status": "passed",
                    "profile_digest": digest(registration["profile"]),
                    "runtime_version": registration["profile"]["binding"]["runtime_version"],
                    "provenance": "fixture",
                    "evidence_ref": "synthetic-reviewer-source",
                }
            )
        if self.mutate is not None:
            self.mutate(observed)
        return observed


@pytest.fixture
def binding_case(check_case):
    from karajan.orchestration.reviewer_binding import ApprovedReviewerBindings

    intents, args, candidates, captured, checks = prepare(check_case)
    qualifications = ReviewerQualificationFixture(intents.admissions.routing.qualifications)
    service = ApprovedReviewerBindings(intents.admissions, candidates, qualifications)
    checks.subject_validator = service.current_locked
    return service, qualifications, intents, args, candidates, captured, checks


def test_public_ids_compile_original_authors_and_create_ready_without_model_admission(binding_case):
    service, _, intents, args, candidates, captured, checks = binding_case
    capacity_before = intents.admissions.routing.capacity.path.read_bytes()
    original = intents.read(*args, principal="owner")
    first = service.advance(*args, principal="owner")
    assert first["state"] == "prepared", first
    assert first["transition"]["phase"] == "prepared"
    binding = first["transition"]["binding"]
    assert binding["source_candidate"]["id"] == captured["id"]
    assert binding["reviewer_task_id"] == "review"
    assert binding["reviewer_sources"][0]["reviewer"]["profile_id"] == "fixture-profile"
    task = first["assessment"]["membership"]["snapshots"]["task"]
    writer = candidates.get(captured["id"])["request"]["authors"][0]
    assert task["authors"][0]["attempt_id"] == writer["attempt_id"]
    assert task["authors"][0]["context_id"] == writer["context_id"]
    assert task["authors"][0]["complexity"] == "T1"
    assert task["authorization"]["allowed_stages"] == ["normal"]
    assert task["authorization"]["approved_quality_stage_indices"] == []
    assert first["assessment"]["actual_reviewer_attempt"] is None
    ready = service.advance(*args, principal="owner")
    assert ready["state"] == "ready", ready
    assert ready["transition"]["receipt"]["revision"] == 2
    assert checks.get(*args, principal="owner")["subject"]["candidate"]["id"] == captured["id"]
    assert (
        intents.read(*args, principal="owner")["execution"]["collection"]
        == original["execution"]["collection"]
    )
    assert intents.admissions.routing.capacity.path.read_bytes() == capacity_before


def _passed_reviewer_subject(binding_case):
    service, qualification, intents, args, candidates, captured, checks = binding_case
    # Binding preparation/commit is separate from the later current check cycle.
    service.advance(*args, principal="owner")
    service.advance(*args, principal="owner")
    checks.advance(*args, principal="owner")  # Install the current bound subject.
    # This fixture has no native Check runner.  Make the already-controller-owned
    # current Check cycle explicit C evidence, then exercise real admission and
    # Capacity persistence below.
    with sqlite3.connect(intents.admissions.database) as db:
        row = db.execute("SELECT data FROM operations WHERE id=?", (args[1],)).fetchone()
        worker = json.loads(row[0])
        for check in worker["validation"]["checks"]["runs"]:
            check.update(phase="recorded", evidence={"status": "passed"})
        worker["validation"]["checks"]["phase"] = "checks_passed"
        db.execute("UPDATE operations SET data=? WHERE id=?", (json.dumps(worker), args[1]))
    routing = intents.admissions.routing
    routing.qualifications = qualification
    run_id, worker_operation_id = args
    run = routing.planner.get(run_id, principal="owner")
    demand = [
        {"pool_id": pool["id"], "unit": pool["unit"], "window_kind": "fixed", "amount": "3"}
        for pool in run["configuration_snapshot"]["configuration"]["resources"]["quota_pools"]
    ]
    routing.estimates.register(
        run_id,
        "review",
        {"id": "fixture-profile", "revision": 1},
        {
            "id": "checks-reviewer-estimate",
            "revision": 1,
            "source_kind": "owner_conservative_estimate",
            "validity_seconds": 600,
            "measurement_semantics": "window_independent_attempt",
            "demand": demand,
            "completion_seconds": None,
            "basis": "Synthetic reviewer forecast for this exact approved task.",
        },
        principal="owner",
        command_key="reviewer-estimate",
    )
    return intents, args, candidates, captured, checks


def test_reviewer_admission_uses_distinct_operation_identity_and_capacity_request(binding_case):
    intents, (run_id, worker_operation_id), _, _, _ = _passed_reviewer_subject(binding_case)
    queued = intents.admissions.enqueue(
        run_id, "review", principal="owner", command_key="reviewer-admission"
    )
    assert queued["state"] == "queued", queued
    assert queued["id"] != worker_operation_id
    assert queued["depends_on_operation_id"] == worker_operation_id
    assert queued["planned_attempt_id"] != queued["assessment"]["route"]["snapshots"]["task"][
        "authors"
    ][0]["attempt_id"]
    assert queued["assessment"]["route"]["snapshots"]["task"]["authors"]
    reserved = intents.admissions.advance(run_id, queued["id"], principal="owner")
    assert reserved["state"] == "reserved", reserved
    assert reserved["request"]["attempt_id"] == queued["planned_attempt_id"]
    assert reserved["request"]["role"] == "reviewer"


def _activate_reviewer_reservation(intents, run_id, operation):
    receipt = intents.admissions.routing.capacity.activate(
        operation["capacity_receipt"]["admission_id"],
        command_key="reviewer-activate:" + operation["id"],
    )
    assert receipt["decision"] == "capacity_revalidated", receipt


def test_reviewer_effect_guard_uses_only_the_stored_operation_and_active_hold(binding_case):
    intents, (run_id, worker_operation_id), _, _, _ = _passed_reviewer_subject(binding_case)
    reviewer = intents.admissions.advance(
        run_id,
        intents.admissions.enqueue(
            run_id, "review", principal="owner", command_key="effect-guard"
        )["id"],
        principal="owner",
    )
    _activate_reviewer_reservation(intents, run_id, reviewer)
    before = intents.admissions.routing.capacity.snapshot()
    with intents.admissions.reviewer_reserved_effect_guard(
        run_id, reviewer["id"], principal="owner"
    ) as held:
        assert held["operation"]["id"] == reviewer["id"]
        assert held["operation"]["id"] != worker_operation_id
        assert held["operation"]["depends_on_operation_id"] == worker_operation_id
        assert held["revalidation"]["reviewer_operation_id"] == reviewer["id"]
        assert held["capacity"]["admission_id"] == reviewer["capacity_receipt"]["admission_id"]
        assert held["capacity"]["request"] == reviewer["request"]
    assert intents.admissions.routing.capacity.snapshot() == before


def test_reviewer_effect_guard_keeps_project_binding_locked_through_capacity(binding_case):
    intents, (run_id, _), _, _, _ = _passed_reviewer_subject(binding_case)
    reviewer = intents.admissions.advance(
        run_id,
        intents.admissions.enqueue(
            run_id, "review", principal="owner", command_key="effect-project-lock"
        )["id"],
        principal="owner",
    )
    _activate_reviewer_reservation(intents, run_id, reviewer)
    project_id = intents.admissions.routing.planner.get(run_id, principal="owner")["project_id"]
    started = Event()

    def read_project():
        started.set()
        return intents.admissions.routing.planner.projects.get_configuration(project_id)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with intents.admissions.reviewer_reserved_effect_guard(
            run_id, reviewer["id"], principal="owner"
        ) as held:
            assert held["capacity"]["state"] == "active"
            future = pool.submit(read_project)
            assert started.wait(2)
            with pytest.raises(TimeoutError):
                future.result(timeout=0.15)
        assert future.result(timeout=5)


def test_reviewer_effect_guard_rejects_cancelled_or_changed_binding_without_new_admission(
    binding_case,
):
    service, qualification, intents, (run_id, _), _, _, _ = binding_case
    intents, _, _, _, _ = _passed_reviewer_subject(binding_case)
    reviewer = intents.admissions.advance(
        run_id,
        intents.admissions.enqueue(
            run_id, "review", principal="owner", command_key="effect-change"
        )["id"],
        principal="owner",
    )
    _activate_reviewer_reservation(intents, run_id, reviewer)
    before = intents.admissions.routing.capacity.snapshot()
    qualification.generation = 2
    with pytest.raises(RunError, match="REVIEWER_RESERVED_ROUTE_NOT_CURRENT"):
        with intents.admissions.reviewer_reserved_effect_guard(
            run_id, reviewer["id"], principal="owner"
        ):
            pytest.fail("changed reviewer binding entered the effect guard")
    assert intents.admissions.routing.capacity.snapshot() == before
    cancelled = intents.admissions.cancel(run_id, reviewer["id"], principal="owner")
    assert cancelled["cancel_requested"] is True
    with pytest.raises(RunError, match="REVIEWER_OPERATION_CANCELLED"):
        with intents.admissions.reviewer_reserved_effect_guard(
            run_id, reviewer["id"], principal="owner"
        ):
            pytest.fail("cancelled reviewer entered the effect guard")


@pytest.mark.parametrize(
    ("change", "reason"),
    [("attempts", "RUN_ATTEMPT_LIMIT"), ("duration", "RUN_DURATION_LIMIT")],
)
def test_reviewer_admission_honors_existing_run_cumulative_budget_before_capacity(
    binding_case, change, reason
):
    intents, (run_id, _), _, _, _ = _passed_reviewer_subject(binding_case)
    with sqlite3.connect(intents.admissions.database) as db:
        row = db.execute(
            "SELECT data FROM run_execution_budgets WHERE run_id=?", (run_id,)
        ).fetchone()
        budget = json.loads(row[0])
        if change == "attempts":
            budget["max_total_attempts"] = len(budget["claims"])
        else:
            budget["started_at"] = 0.0
            budget["max_duration_seconds"] = 1
        db.execute(
            "UPDATE run_execution_budgets SET data=? WHERE run_id=?",
            (json.dumps(budget), run_id),
        )
    reviewer = intents.admissions.enqueue(
        run_id, "review", principal="owner", command_key="budget-" + change
    )
    before = intents.admissions.routing.capacity.path.read_bytes()
    blocked = intents.admissions.advance(run_id, reviewer["id"], principal="owner")
    assert blocked["state"] == "blocked"
    assert blocked["reason_codes"] == [reason]
    assert intents.admissions.routing.capacity.path.read_bytes() == before


def test_multiple_credible_worker_operations_are_rejected_before_capacity(binding_case):
    intents, (run_id, worker_operation_id), _, _, _ = _passed_reviewer_subject(binding_case)
    with sqlite3.connect(intents.admissions.database) as db:
        row = db.execute(
            "SELECT data FROM operations WHERE id=?", (worker_operation_id,)
        ).fetchone()
        duplicate = json.loads(row[0])
        duplicate["id"] = "second-credible-worker-operation"
        db.execute(
            "INSERT INTO operations VALUES (?,?,?,?,?)",
            (
                duplicate["id"],
                duplicate["run_id"],
                duplicate["task_id"],
                duplicate["state"],
                json.dumps(duplicate),
            ),
        )
    before = intents.admissions.routing.capacity.path.read_bytes()
    with pytest.raises(RunError, match="REVIEW_WORKER_LINEAGE_REQUIRED"):
        intents.admissions.enqueue(run_id, "review", principal="owner", command_key="ambiguous")
    assert intents.admissions.routing.capacity.path.read_bytes() == before


def test_current_check_change_blocks_reviewer_admission_before_capacity(binding_case):
    intents, (run_id, worker_operation_id), _, _, _ = _passed_reviewer_subject(binding_case)
    queued = intents.admissions.enqueue(
        run_id, "review", principal="owner", command_key="current-checks"
    )
    with sqlite3.connect(intents.admissions.database) as db:
        row = db.execute(
            "SELECT data FROM operations WHERE id=?", (worker_operation_id,)
        ).fetchone()
        worker = json.loads(row[0])
        worker["validation"]["checks"]["runs"][0]["evidence"]["status"] = "failed"
        worker["validation"]["checks"]["phase"] = "blocked"
        db.execute(
            "UPDATE operations SET data=? WHERE id=?", (json.dumps(worker), worker_operation_id)
        )
    before = intents.admissions.routing.capacity.path.read_bytes()
    result = intents.admissions.advance(run_id, queued["id"], principal="owner")
    assert result["state"] == "blocked"
    assert result["reason_codes"] == ["REVIEW_SUBJECT_CHECKS_REQUIRED"]
    assert intents.admissions.routing.capacity.path.read_bytes() == before


def test_reviewer_admission_reuses_one_exact_request_after_lost_reply(binding_case, monkeypatch):
    intents, (run_id, _), _, _, _ = _passed_reviewer_subject(binding_case)
    queued = intents.admissions.enqueue(run_id, "review", principal="owner", command_key="lost")
    capacity = intents.admissions.routing.capacity
    actual, calls = capacity.admit, []

    def admit_then_lose(request, *, command_key):
        calls.append((request, command_key))
        actual(request, command_key=command_key)
        raise ConnectionResetError("fixture response lost after Capacity commit")

    monkeypatch.setattr(capacity, "admit", admit_then_lose)
    with pytest.raises(ConnectionResetError, match="response lost"):
        intents.admissions.advance(run_id, queued["id"], principal="owner")
    recovered = intents.admissions.advance(run_id, queued["id"], principal="owner")
    assert recovered["state"] == "reserved"
    assert calls == [(queued["request"], "task-admit:" + queued["id"])]
    assert recovered["request"] == queued["request"]
    assert recovered["capacity_receipt"] == capacity.command_receipt(
        "admit", queued["request"], command_key="task-admit:" + queued["id"]
    )


def test_concurrent_reviewer_advance_has_one_capacity_admission(binding_case, monkeypatch):
    intents, (run_id, _), _, _, _ = _passed_reviewer_subject(binding_case)
    queued = intents.admissions.enqueue(
        run_id, "review", principal="owner", command_key="concurrent-reviewer"
    )
    capacity = intents.admissions.routing.capacity
    actual, calls = capacity.admit, []

    def observed(request, *, command_key):
        calls.append((request, command_key))
        return actual(request, command_key=command_key)

    monkeypatch.setattr(capacity, "admit", observed)
    reservations_before = len(capacity.snapshot()["reservations"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: intents.admissions.advance(run_id, queued["id"], principal="owner"),
                range(2),
            )
        )
    assert [result["state"] for result in results] == ["reserved", "reserved"]
    assert calls == [(queued["request"], "task-admit:" + queued["id"])]
    assert len(capacity.snapshot()["reservations"]) == reservations_before + 1


def test_lost_reviewer_activate_reply_requires_reconciliation_without_new_claim(
    binding_case, monkeypatch
):
    intents, (run_id, _), _, _, _ = _passed_reviewer_subject(binding_case)
    queued = intents.admissions.enqueue(run_id, "review", principal="owner", command_key="activate")
    reserved = intents.admissions.advance(run_id, queued["id"], principal="owner")
    capacity = intents.admissions.routing.capacity
    actual, calls = capacity.activate, []

    def activate_then_lose(admission_id, *, command_key):
        calls.append((admission_id, command_key))
        actual(admission_id, command_key=command_key)
        raise ConnectionResetError("fixture activate response lost after Capacity commit")

    monkeypatch.setattr(capacity, "activate", activate_then_lose)
    with pytest.raises(ConnectionResetError, match="activate response lost"):
        capacity.activate(
            reserved["capacity_receipt"]["admission_id"], command_key="external-activate"
        )
    current = intents.admissions.get(run_id, queued["id"], principal="owner")
    assert current["state"] == "reconciliation_required"
    assert current["request"] == queued["request"]
    assert calls == [(reserved["capacity_receipt"]["admission_id"], "external-activate")]
    assert intents.admissions.advance(run_id, queued["id"], principal="owner")["state"] == (
        "reconciliation_required"
    )


def test_reviewer_generation_change_blocks_current_effect_before_capacity(binding_case):
    service, qualification, intents, (run_id, _), _, _, _ = binding_case
    intents, _, _, _, _ = _passed_reviewer_subject(binding_case)
    queued = intents.admissions.enqueue(
        run_id, "review", principal="owner", command_key="changed-generation"
    )
    qualification.generation = 2
    before = intents.admissions.routing.capacity.path.read_bytes()
    result = intents.admissions.advance(run_id, queued["id"], principal="owner")
    assert result["state"] == "blocked"
    assert result["reason_codes"] == ["REVIEWER_BINDING_CHANGED"]
    assert intents.admissions.routing.capacity.path.read_bytes() == before


def test_reviewer_cancelled_before_advance_never_creates_a_reservation(binding_case):
    intents, (run_id, _), _, _, _ = _passed_reviewer_subject(binding_case)
    queued = intents.admissions.enqueue(run_id, "review", principal="owner", command_key="cancel")
    before = intents.admissions.routing.capacity.path.read_bytes()
    cancelled = intents.admissions.cancel(run_id, queued["id"], principal="owner")
    assert cancelled["state"] == "cancelled"
    advanced = intents.admissions.advance(run_id, queued["id"], principal="owner")
    assert advanced["state"] == "cancelled"
    assert intents.admissions.routing.capacity.path.read_bytes() == before


def test_reviewer_window_change_blocks_the_stored_request(binding_case):
    intents, (run_id, _), _, _, _ = _passed_reviewer_subject(binding_case)
    queued = intents.admissions.enqueue(run_id, "review", principal="owner", command_key="window")
    capacity = intents.admissions.routing.capacity
    pool = intents.admissions.routing.planner.get(run_id, principal="owner")[
        "configuration_snapshot"
    ]["configuration"]["resources"]["quota_pools"][0]
    reservations_before = len(capacity.snapshot()["reservations"])
    capacity.clock = lambda: 2001.0
    capacity.observe(
        {
            "pool_id": pool["id"],
            "window_id": "new-current-window",
            "observed_at": 2001.0,
            "reset_at": 2601.0,
            "source": "fixture",
            "source_ref": "changed-window-observer",
            "metric": "remaining",
            "amount": "80",
            "limit": "100",
            "covered_usage_ids": [],
        },
        command_key="changed-window-" + pool["id"],
    )
    result = intents.admissions.advance(run_id, queued["id"], principal="owner")
    assert result["state"] == "blocked"
    assert result["revalidation"]["route"]["snapshots"]["capacity"]["pools"][0][
        "window_id"
    ] == "new-current-window"
    assert len(capacity.snapshot()["reservations"]) == reservations_before


def test_public_reviewer_admission_rejects_uploaded_candidate_or_complexity(binding_case):
    intents, (run_id, _), _, _, _ = _passed_reviewer_subject(binding_case)
    before = intents.admissions.database.read_bytes()
    with pytest.raises(TypeError):
        intents.admissions.enqueue(
            run_id,
            "review",
            principal="owner",
            command_key="uploaded-candidate",
            candidate={"id": "forged"},
        )
    with pytest.raises(TypeError):
        intents.admissions.enqueue(
            run_id,
            "review",
            principal="owner",
            command_key="uploaded-complexity",
            complexity="T1",
        )
    assert intents.admissions.database.read_bytes() == before


def test_reviewer_route_checks_independence_against_every_captured_author(binding_case):
    intents, (run_id, _), _, _, _ = _passed_reviewer_subject(binding_case)
    queued = intents.admissions.enqueue(run_id, "review", principal="owner", command_key="authors")
    route = queued["assessment"]["route"]
    task = deepcopy(route["snapshots"]["task"])
    first = deepcopy(task["authors"][0])
    first.update(attempt_id="another-author-attempt", context_id="another-author-context")
    task["authors"].append(first)
    task["planned_attempt_id"] = "independent-reviewer-attempt"
    task["planned_context_id"] = "independent-reviewer-context"
    before = intents.admissions.routing.capacity.path.read_bytes()
    result = evaluate_route(task, route["snapshots"]["policy"], route["snapshots"]["capacity"])
    assert result["selected_profile"] is not None
    assert all(
        author["attempt_id"] != task["planned_attempt_id"]
        and author["context_id"] != task["planned_context_id"]
        for author in result["snapshots"]["task"]["authors"]
    )
    assert intents.admissions.routing.capacity.path.read_bytes() == before


@pytest.mark.parametrize("field", ["attempt_id", "context_id"])
def test_second_author_collision_rejects_reviewer_before_capacity(binding_case, field):
    intents, (run_id, _), _, _, _ = _passed_reviewer_subject(binding_case)
    queued = intents.admissions.enqueue(run_id, "review", principal="owner", command_key="second")
    route = queued["assessment"]["route"]
    task = deepcopy(route["snapshots"]["task"])
    second = deepcopy(task["authors"][0])
    second.update(attempt_id="second-author-attempt", context_id="second-author-context")
    second[field] = task["planned_" + field]
    task["authors"].append(second)
    before = intents.admissions.routing.capacity.path.read_bytes()
    result = evaluate_route(task, route["snapshots"]["policy"], route["snapshots"]["capacity"])
    assert result["selected_profile"] is None
    assert "REVIEW_NOT_INDEPENDENT" in result["candidates"][0]["reason_codes"]
    assert intents.admissions.routing.capacity.path.read_bytes() == before


@pytest.mark.parametrize("family", ["same", None])
def test_t3_reviewer_same_or_unknown_family_is_rejected_before_capacity(binding_case, family):
    intents, (run_id, _), _, _, _ = _passed_reviewer_subject(binding_case)
    queued = intents.admissions.enqueue(run_id, "review", principal="owner", command_key="t3")
    route = queued["assessment"]["route"]
    task, policy = deepcopy(route["snapshots"]["task"]), deepcopy(route["snapshots"]["policy"])
    task["complexity"] = "T3"
    profile = task["authors"][0]["profile"]
    author_family = task["authors"][0]["model_family"]
    for registration in policy["resources"]["profiles"]:
        if {"id": registration["id"], "revision": registration["revision"]} == profile:
            registration["model_family"] = author_family if family == "same" else None
    if family is None:
        task["authors"][0]["model_family"] = None
    before = intents.admissions.routing.capacity.path.read_bytes()
    result = evaluate_route(task, policy, route["snapshots"]["capacity"])
    assert result["selected_profile"] is None
    assert "REVIEW_FAMILY_NOT_INDEPENDENT" in result["candidates"][0]["reason_codes"]
    assert intents.admissions.routing.capacity.path.read_bytes() == before


def test_installed_binding_is_stable_over_time_and_next_generation_uses_direct_predecessor(
    binding_case,
):
    service, qualifications, intents, args, candidates, captured, checks = binding_case
    service.advance(*args, principal="owner")
    first = service.advance(*args, principal="owner")
    second_subject = checks.advance(*args, principal="owner")
    assert second_subject["subject"]["revision"] == 2
    assert second_subject["subject"]["source_candidate"]["id"] == captured["id"]
    first_id = first["transition"]["id"]
    intents.admissions.routing.planner.clock = lambda: 1001.0
    stable = service.advance(*args, principal="owner")
    assert stable["state"] == "installed", stable
    assert stable["transition"]["id"] == first_id
    qualifications.generation = 2
    next_intent = service.advance(*args, principal="owner")
    assert next_intent["state"] == "prepared", next_intent
    assert (
        next_intent["transition"]["binding"]["source_candidate"] == first["transition"]["receipt"]
    )
    next_ready = service.advance(*args, principal="owner")
    third_subject = checks.advance(*args, principal="owner")
    assert next_ready["transition"]["receipt"]["revision"] == 3
    assert third_subject["subject"]["revision"] == 3
    assert third_subject["subject"]["source_candidate"]["id"] == captured["id"]
    assert candidates.get(captured["id"])["request"]["policy"]["review"]["approved_reviewers"] == []


def test_prepared_source_change_archives_unclaimed_intent_with_a_new_key(binding_case):
    service, qualifications, intents, args, candidates, _, _ = binding_case
    first = service.advance(*args, principal="owner")
    qualifications.generation = 2
    second = service.advance(*args, principal="owner")
    assert second["state"] == "prepared"
    assert first["transition"]["id"] != second["transition"]["id"]
    assert first["transition"]["command_key"] != second["transition"]["command_key"]
    assert first["transition"]["revision"] == second["transition"]["revision"] == 1
    validation = intents.read(*args, principal="owner")["validation"]
    assert validation["intent_history"] == [first["transition"]]
    assert (
        candidates.lookup_review_rebind(
            first["transition"]["binding"], command_key=first["transition"]["command_key"]
        )
        is None
    )
    assert service.advance(*args, principal="owner")["state"] == "ready"


def test_lost_cas_reply_recovers_exact_history_without_current_sources_or_assets(
    binding_case, monkeypatch
):
    service, qualifications, intents, args, candidates, _, _ = binding_case
    service.advance(*args, principal="owner")
    original = candidates.rebind_reviewers
    effects = []

    def lost_reply(binding, *, command_key):
        result = original(binding, command_key=command_key)
        effects.append(result["id"])
        raise ConnectionResetError("synthetic commit return lost")

    monkeypatch.setattr(candidates, "rebind_reviewers", lost_reply)
    ready = service.advance(*args, principal="owner")
    assert ready["state"] == "ready"
    assert effects == [ready["transition"]["receipt"]["id"]]
    candidates.objects.rename(candidates.objects.with_name("retained-artifacts"))
    candidates.git_directory.rename(candidates.git_directory.with_name("retained-git"))

    def forbidden(*args, **kwargs):
        raise AssertionError("history must not inspect current qualification")

    monkeypatch.setattr(qualifications, "_facts", forbidden)
    monkeypatch.setattr(intents.admissions.routing.planner, "clock", forbidden)
    assert service.get(*args, principal="owner") == ready
    assert service.reconcile(*args, principal="owner") == ready
    assert service.advance(*args, principal="owner") == ready
    assert len(effects) == 1


@pytest.mark.parametrize("fault", ["lost_claim", "guard_after_claim"])
def test_consumed_claim_never_reissues_cas_or_replaces_intent(binding_case, monkeypatch, fault):
    import karajan.orchestration.reviewer_binding as module

    service, qualifications, intents, args, candidates, _, _ = binding_case
    prepared = service.advance(*args, principal="owner")
    original_connection = module._connection
    injected = []

    @contextmanager
    def after_commit(path, *, readonly):
        with original_connection(path, readonly=readonly) as db:
            yield db
        if path == intents.admissions.database and not readonly and not injected:
            injected.append(True)
            if fault == "lost_claim":
                raise ConnectionResetError("synthetic claim commit reply lost")
            qualifications.enabled = False

    calls = []
    monkeypatch.setattr(candidates, "rebind_reviewers", lambda *a, **k: calls.append(k))
    with monkeypatch.context() as patch:
        patch.setattr(module, "_connection", after_commit)
        if fault == "lost_claim":
            with pytest.raises(ConnectionResetError, match="synthetic claim commit reply lost"):
                service.advance(*args, principal="owner")
        else:
            assert service.advance(*args, principal="owner")["state"] == "reconciliation_required"
    qualifications.enabled = True
    qualifications.generation = 3
    recovered = service.advance(*args, principal="owner")
    assert recovered["state"] == "reconciliation_required"
    assert recovered["transition"]["phase"] == "rebind_claimed"
    assert recovered["transition"]["id"] == prepared["transition"]["id"]
    assert calls == []
    assert service.reconcile(*args, principal="owner") == recovered


def test_two_advances_share_exactly_one_claim_and_cas(binding_case, monkeypatch):
    service, _, _, args, candidates, _, _ = binding_case
    service.advance(*args, principal="owner")
    original = candidates.rebind_reviewers
    calls = []

    def observed(binding, *, command_key):
        calls.append(command_key)
        return original(binding, command_key=command_key)

    monkeypatch.setattr(candidates, "rebind_reviewers", observed)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: service.advance(*args, principal="owner"), range(2)))
    assert len(calls) == 1
    assert all(result["transition"]["command_key"] == calls[0] for result in results)
    assert service.reconcile(*args, principal="owner")["state"] == "ready"


def test_owner_ids_and_no_payload_authority_before_any_state_write(binding_case):
    service, _, intents, args, candidates, _, _ = binding_case
    paths = [intents.admissions.database, candidates.directory / "candidates.sqlite"]
    before = [path.read_bytes() for path in paths]
    for method in (service.advance, service.get, service.reconcile):
        with pytest.raises(RunError):
            method(*args, principal="different-owner")
        with pytest.raises(RunError, match="TASK_ADMISSION_NOT_FOUND"):
            method(args[0], "another-operation", principal="owner")
        with pytest.raises(TypeError):
            method(*args, principal="owner", profile={"id": "injected"})
    assert [path.read_bytes() for path in paths] == before


def test_cancelled_or_unknown_old_check_stop_never_rebinds(binding_case):
    service, _, intents, args, candidates, _, _ = binding_case
    with sqlite3.connect(intents.admissions.database) as db:
        import json

        operation = json.loads(
            db.execute("SELECT data FROM operations WHERE id=?", (args[1],)).fetchone()[0]
        )
        operation["validation"]["checks"]["runs"][0].update(
            phase="native_claimed", native_claim={"claim": "synthetic"}
        )
        db.execute("UPDATE operations SET data=? WHERE id=?", (json.dumps(operation), args[1]))
    before = (candidates.directory / "candidates.sqlite").read_bytes()
    result = service.advance(*args, principal="owner")
    assert result["state"] == "blocked"
    assert "REVIEW_SUBJECT_CHECK_STOP_REQUIRED" in result["reason_codes"]
    assert result["transition"] is None
    intents.admissions.cancel(*args, principal="owner")
    assert "REVIEW_BINDING_CANCELLED" in service.advance(*args, principal="owner")["reason_codes"]
    assert (candidates.directory / "candidates.sqlite").read_bytes() == before


@pytest.mark.parametrize(
    "change", ["missing", "foreign_project", "foreign_auth", "unknown_generation"]
)
def test_unknown_or_foreign_authentication_source_does_not_compile_a_binding(
    binding_case, monkeypatch, change
):
    service, qualifications, _, args, _, _, _ = binding_case
    original = qualifications._facts

    def altered(*args, **kwargs):
        observed = original(*args, **kwargs)
        start = observed["observation"]["binding"]["execution_start"]
        if change == "missing":
            start["authentication_source"] = None
        elif change == "foreign_project":
            start["authentication_source"]["project_id"] = "another-project"
        elif change == "foreign_auth":
            start["authentication_source"]["auth_ref"] = "another-auth"
        else:
            start["authentication_source"]["generation"] = None
        return observed

    monkeypatch.setattr(qualifications, "_facts", altered)
    result = service.advance(*args, principal="owner")
    assert result["state"] == "blocked", result
    assert result["transition"] is None
    assert "REVIEW_QUALIFICATION_AUTHENTICATION_MISMATCH" in {
        issue["reason_code"] for issue in result["assessment"]["qualification_issues"]
    }


@pytest.mark.parametrize("winner", ["claim", "replace"])
def test_concurrent_source_change_cannot_replace_a_consumed_claim_or_send_old_intent(
    binding_case, monkeypatch, winner
):
    import karajan.orchestration.reviewer_binding as module

    service, qualifications, intents, args, candidates, _, _ = binding_case
    prepared = service.advance(*args, principal="owner")
    original_connection = module._connection
    committed, release = Event(), Event()

    @contextmanager
    def pause_after_first_commit(path, *, readonly):
        with original_connection(path, readonly=readonly) as db:
            yield db
        if path == intents.admissions.database and not readonly and not committed.is_set():
            committed.set()
            assert release.wait(10), "second public advance did not complete"

    calls = []
    original_cas = candidates.rebind_reviewers

    def counted(binding, *, command_key):
        calls.append(command_key)
        return original_cas(binding, command_key=command_key)

    monkeypatch.setattr(candidates, "rebind_reviewers", counted)
    monkeypatch.setattr(module, "_connection", pause_after_first_commit)
    if winner == "replace":
        qualifications.generation = 2
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(service.advance, *args, principal="owner")
        try:
            assert committed.wait(10), "first public advance did not commit"
            qualifications.generation = 2
            second = service.advance(*args, principal="owner")
        finally:
            release.set()
        first.result(timeout=10)
    final = service.reconcile(*args, principal="owner")
    if winner == "claim":
        assert second["state"] == final["state"] == "reconciliation_required"
        assert final["transition"]["id"] == prepared["transition"]["id"]
        assert "REVIEWER_BINDING_CHANGED" in final["reason_codes"]
        assert calls == []
        assert not intents.read(*args, principal="owner")["validation"].get("intent_history")
    else:
        assert final["state"] == "ready"
        assert final["transition"]["id"] != prepared["transition"]["id"]
        assert calls == [final["transition"]["command_key"]]
        assert intents.read(*args, principal="owner")["validation"]["intent_history"] == [
            prepared["transition"]
        ]


def test_ready_receipt_is_history_and_cannot_install_after_qualification_change(binding_case):
    service, qualifications, _, args, _, captured, checks = binding_case
    service.advance(*args, principal="owner")
    ready = service.advance(*args, principal="owner")
    qualifications.generation = 2
    assert service.advance(*args, principal="owner") == ready
    with pytest.raises(RunError, match="REVIEWER_BINDING_CHANGED"):
        checks.advance(*args, principal="owner")
    assert checks.get(*args, principal="owner")["subject"]["candidate"]["id"] == captured["id"]
    assert service.get(*args, principal="owner")["transition"] == ready["transition"]


def test_installed_subject_requires_current_qualification_for_new_checks(binding_case):
    service, qualifications, _, args, _, _, checks = binding_case
    service.advance(*args, principal="owner")
    service.advance(*args, principal="owner")
    checks.advance(*args, principal="owner")
    before = checks.get(*args, principal="owner")
    qualifications.enabled = False
    result = service.advance(*args, principal="owner")
    assert result["state"] == "blocked"
    assert result["transition"]["phase"] == "installed"
    with pytest.raises(RunError, match="REVIEWER_QUALIFICATION_REQUIRED"):
        checks.advance(*args, principal="owner")
    after = checks.get(*args, principal="owner")
    assert after["checks"] == before["checks"]
    assert after["subject"] == before["subject"]


def test_issue107_identity_precedes_real_ready_reply_loss(binding_case, tmp_path, monkeypatch):
    """A C-only operator recovery can read the exact CAS after its ready reply is lost."""
    import importlib.util
    import json
    import shutil
    from pathlib import Path

    from karajan.orchestration.candidate_subjects import candidate_identity

    service, _, intents, args, candidates, _, _ = binding_case
    source = (
        Path(__file__).parents[2]
        / "examples/go-readonly-reviewer-qualification-20260907/prepare_issue107_consumer.py"
    )
    spec = importlib.util.spec_from_file_location("issue107_ready_loss", source)
    assert spec is not None and spec.loader is not None
    consumer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(consumer)
    identity = {"command": "issue107", "qualification_ref": "explicit-c-boundary"}
    monkeypatch.setattr(consumer, "FIXTURE_OWNER", "owner")
    monkeypatch.setattr(consumer, "ensure_fixture", lambda _root: (service, args, {}))
    original_advance = service.advance

    def ready_then_lost(*values, **kwargs):
        result = original_advance(*values, **kwargs)
        if result["state"] == "ready":
            raise OSError("controlled C reply loss after CandidateStore commit")
        return result

    monkeypatch.setattr(service, "advance", ready_then_lost)
    with pytest.raises(OSError):
        consumer.positive_result(tmp_path, identity)
    operation = intents.read(*args, principal="owner")
    transition = operation["validation"]["subject_transition"]
    assert operation["validation"]["issue107_recovery_identity"] == identity
    assert transition["phase"] == "ready"
    exact = candidates.lookup_review_rebind(
        transition["binding"], command_key=transition["command_key"]
    )
    assert candidate_identity(exact) == transition["receipt"]
    fixture = tmp_path / "consumer-fixture"
    fixture.mkdir()
    shutil.copyfile(service.admissions.database, fixture / "admission.sqlite")
    shutil.copytree(candidates.directory, fixture / "candidates")
    with sqlite3.connect(fixture / "admission.sqlite") as database:
        database.execute("UPDATE operations SET id=?", ("issue107-fixed-consumer-operation",))
        row = database.execute("SELECT data FROM operations").fetchone()
        assert row is not None
        assert json.loads(row[0])["validation"]["issue107_recovery_identity"] == identity
    assert consumer.positive_history(tmp_path, identity) is not None
