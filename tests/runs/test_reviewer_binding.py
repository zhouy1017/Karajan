"""ID-only binding over real approved stores/CAS; no real Reviewer qualification."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from threading import Event

import pytest
from karajan.orchestration.candidate_checks import ApprovedCandidateChecks
from karajan.projects.qualification import ProfileQualificationStore, QualificationError
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
