"""Original operation lifecycle; synthetic qualification, no provider effects."""

import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Event

import pytest
from karajan.adapters.opencode.go_journal import GoCallJournal
from karajan.candidates import CandidateStore
from karajan.contracts.probe import AttemptManifest
from karajan.execution import ProcessSpec, RunnerHost
from karajan.orchestration.go_execution_intent import GoExecutionIntents, GoExecutionSource
from karajan.routing.compiler import digest
from karajan.runs import RunError
from test_go_execution_intent import case, projected, ready, reservation
from test_go_task_collector import captured_case, collection_case, workspace_case

__all__ = [
    "case",
    "projected",
    "ready",
    "reservation",
    "captured_case",
    "collection_case",
    "workspace_case",
]


@pytest.fixture
def lifecycle(ready, tmp_path):
    from karajan.orchestration.go_execution_intent import GoLaunchSpec

    admissions, _, run, operation, _ = ready

    def compile_launch(operation):
        return GoLaunchSpec(ProcessSpec((sys.executable, "-c", "pass"), tmp_path), "c" * 64)

    service = GoExecutionIntents(
        admissions,
        source=GoExecutionSource("a" * 64, "b" * 64),
        host=RunnerHost(tmp_path / "lifecycle-host"),
        launch_compiler=compile_launch,
        journal=GoCallJournal(tmp_path / "lifecycle-journal.sqlite", clock=lambda: 1000.0),
        candidates=CandidateStore(tmp_path / "candidates"),
    )
    args = (run["id"], operation["id"])
    service.prepare_intent(*args, principal="owner", command_key="lifecycle")
    return service, args


def test_activation_guard_holds_original_prepared_intent(lifecycle):
    service, args = lifecycle
    before = service.admissions.database.read_bytes()
    with service.activation_guard(*args, principal="owner") as operation:
        assert operation["execution"]["phase"] == "prepared"
        assert operation["request"]["attempt_id"] == operation["planned_attempt_id"]
    assert service.admissions.database.read_bytes() == before


def activated(lifecycle):
    service, args = lifecycle
    with service.activation_guard(*args, principal="owner") as op:
        intent = op["execution"]["intent"]
        service.admissions.routing.capacity.activate(
            intent["admission_id"], command_key=intent["activation_key"]
        )
    return service.activation_recorded(*args, principal="owner")


def test_launch_replay_freezes_original_expiry_and_does_not_touch_resource_ledgers(lifecycle):
    service, args = lifecycle
    op = activated(lifecycle)
    paths = (service.admissions.routing.capacity.path, service.journal.path, service.host.database)
    before = {p: p.read_bytes() for p in paths}
    first = service.freeze_launch(*args, principal="owner")
    original = service.admissions.database.read_bytes()
    second = service.freeze_launch(*args, principal="owner")
    assert first == second
    launch = second["execution"]["launch"]
    assert (
        launch["activation"]["expires_at"] == op["execution"]["capacity_activation"]["expires_at"]
    )
    assert (
        digest({key: value for key, value in launch.items() if key != "digest"}) == launch["digest"]
    )
    assert service.admissions.database.read_bytes() == original
    assert all(path.read_bytes() == body for path, body in before.items())


def test_launch_cannot_change_controller_compiler_after_freeze(lifecycle, tmp_path):
    from karajan.orchestration.go_execution_intent import GoLaunchSpec

    service, args = lifecycle
    activated(lifecycle)
    service.freeze_launch(*args, principal="owner")
    before = service.admissions.database.read_bytes()
    service.launch_compiler = lambda operation: GoLaunchSpec(
        ProcessSpec((sys.executable, "-c", "print('different')"), tmp_path), "c" * 64
    )
    with pytest.raises(RunError, match="TASK_LAUNCH_BINDING_CONFLICT"):
        service.freeze_launch(*args, principal="owner")
    assert service.admissions.database.read_bytes() == before


def test_launch_preparation_serializes_cancel_then_cannot_rearm(lifecycle):
    service, args = lifecycle
    activated(lifecycle)
    service.freeze_launch(*args, principal="owner")
    entered = Event()

    def cancel():
        entered.set()
        return service.cancel_intent(*args, principal="owner")

    with ThreadPoolExecutor(max_workers=1) as pool:
        with service.launch_preparation_guard(*args, principal="owner") as op:
            pending = pool.submit(cancel)
            assert entered.wait(2) and not pending.done()
            launch = op["execution"]["launch"]
            spec = service.launch_compiler(op).process_spec
            service.host.prepare(
                AttemptManifest.model_validate(launch["manifest"]),
                op["execution"]["intent"]["start_key"],
                spec,
            )
            intent = op["execution"]["intent"]
            service.host.initialize_control_once(
                intent["attempt_id"],
                prepared_id=intent["start_key"],
                fence=intent["fence"],
                authorization_ref=intent["authorization_ref"],
            )
        assert pending.result(timeout=3)["cancel_requested"]
    before = service.host.database.read_bytes()
    with pytest.raises(RunError, match="TASK_EXECUTION_CANCEL_REQUESTED"):
        with service.launch_preparation_guard(*args, principal="owner"):
            pytest.fail("Cancellation reopened Host preparation")
    assert service.host.database.read_bytes() == before


@pytest.mark.parametrize("guard", ["activation_guard", "launch_preparation_guard"])
def test_cancelled_operation_blocks_both_pre_effect_stages(lifecycle, guard):
    service, args = lifecycle
    service.cancel_intent(*args, principal="owner")
    before = service.admissions.database.read_bytes()
    with pytest.raises(RunError, match="TASK_EXECUTION_CANCEL_REQUESTED"):
        with getattr(service, guard)(*args, principal="owner"):
            pytest.fail("Cancelled stage reopened")
    assert service.admissions.database.read_bytes() == before


def test_prepared_host_stage_needs_activation_and_frozen_launch(lifecycle):
    service, args = lifecycle
    for _ in range(2):
        with pytest.raises(RunError, match="TASK_EXECUTION_LAUNCH_PREPARATION_REQUIRED"):
            with service.launch_preparation_guard(*args, principal="owner"):
                pytest.fail("Missing launch accepted")
        if service.read(*args, principal="owner")["execution"]["phase"] == "prepared":
            activated(lifecycle)
    with pytest.raises(RunError, match="TASK_EXECUTION_PREPARED_REQUIRED"):
        with service.activation_guard(*args, principal="owner"):
            pytest.fail("Activated admission allowed a second activation stage")


def test_cleanup_uses_original_history_after_cancel_and_source_change(lifecycle):
    service, args = lifecycle
    activated(lifecycle)
    op = service.freeze_launch(*args, principal="owner")
    service.cancel_intent(*args, principal="owner")
    service.source = GoExecutionSource("d" * 64, "e" * 64)
    service.launch_compiler = lambda operation: pytest.fail("Cleanup recompiled launch")
    before = service.admissions.database.read_bytes()
    restored = service.cleanup_binding(*args, principal="owner")
    assert restored["launch"] == op["execution"]["launch"]
    restored["launch"]["manifest"]["id"] = "reader-mutated"
    assert service.cleanup_binding(*args, principal="owner")["launch"] == op["execution"]["launch"]
    assert service.admissions.database.read_bytes() == before


def test_existing_reconstruction_does_not_call_fresh_source_and_retains_owner_gate(lifecycle):
    service, args = lifecycle

    def lazy():
        pytest.fail("History required fresh executable/source")

    before = service.admissions.database.read_bytes()
    reopened = GoExecutionIntents.open_existing(
        service.admissions,
        run_id=args[0],
        operation_id=args[1],
        principal="owner",
        host=service.host,
        source_if_unprepared=lazy,
    )
    assert reopened.source == service.source
    assert reopened.read(*args, principal="owner") == service.read(*args, principal="owner")
    with pytest.raises(RunError, match="USER_DECISION_REQUIRED"):
        GoExecutionIntents.open_existing(
            service.admissions,
            run_id=args[0],
            operation_id=args[1],
            principal="other",
            host=service.host,
            source_if_unprepared=lazy,
        )
    assert service.admissions.database.read_bytes() == before


def test_unprepared_reconstruction_calls_lazy_source_once_and_does_not_prepare(ready, tmp_path):
    admissions, _, run, op, _ = ready
    calls = []

    def source():
        calls.append(True)
        return GoExecutionSource("a" * 64, "b" * 64)

    service = GoExecutionIntents.open_existing(
        admissions,
        run_id=run["id"],
        operation_id=op["id"],
        principal="owner",
        host=RunnerHost(tmp_path / "new-host"),
        source_if_unprepared=source,
    )
    assert calls == [True]
    assert "execution" not in service.read(run["id"], op["id"], principal="owner")


def test_observation_keeps_send_unknown_and_never_revokes_or_refunds(lifecycle):
    service, args = lifecycle
    activated(lifecycle)
    op = service.freeze_launch(*args, principal="owner")
    intent = op["execution"]["intent"]
    binding = op["execution"]["launch"]["grant_binding"]
    grant = service.journal.create_grant(binding, grant_id=intent["grant_id"])
    service.journal.begin_call(
        intent["grant_id"], "lost-send", capability=grant["capability"], binding=binding
    )
    paths = (service.journal.path, service.admissions.routing.capacity.path, service.host.database)
    before = {p: p.read_bytes() for p in paths}
    observed = service.observe_execution(*args, principal="owner", failure="runner")
    evidence = observed["execution"]["observation"]
    assert evidence["grant"]["state"] == "active"
    assert evidence["grant"]["request_count"] == evidence["grant"]["unknown_calls"] == 1
    assert evidence["native_stop"] == evidence["provider_remote_stop"] == "unknown"
    assert observed["state"] == "execution_unknown"
    after = service.admissions.database.read_bytes()
    assert service.observe_execution(*args, principal="owner", failure="runner") == observed
    assert service.admissions.database.read_bytes() == after
    assert all(p.read_bytes() == body for p, body in before.items())


@pytest.mark.parametrize("state", ["missing", "mismatch", "unavailable"])
def test_cleanup_observation_distinguishes_unowned_or_unavailable_grant(lifecycle, state):
    service, args = lifecycle
    activated(lifecycle)
    op = service.freeze_launch(*args, principal="owner")
    intent = op["execution"]["intent"]
    if state == "mismatch":
        binding = deepcopy(op["execution"]["launch"]["grant_binding"])
        binding["attempt_id"] = "unowned-attempt"
        service.journal.create_grant(binding, grant_id=intent["grant_id"])
    elif state == "unavailable":
        service.journal.path.rename(service.journal.path.with_suffix(".saved"))
    service.cancel_intent(*args, principal="owner")
    service.source = GoExecutionSource("f" * 64, "e" * 64)
    observed = service.record_cleanup(*args, principal="owner")
    assert observed["cancel_requested"] and observed["state"] == "cancellation_pending"
    assert (
        observed["execution"]["observation"]["grant"]["state"]
        == {
            "missing": "not_created",
            "mismatch": "binding_mismatch",
            "unavailable": "unknown",
        }[state]
    )
    if state == "unavailable":
        assert not service.journal.path.exists()


def test_existing_corrupt_source_never_falls_back_to_fresh_source(lifecycle):
    service, args = lifecycle
    op = service.read(*args, principal="owner")
    op["execution"]["intent"]["runner_source_sha256"] = "not-a-digest"
    with sqlite3.connect(service.admissions.database) as db:
        service.admissions._save(db, op)
    with pytest.raises(RunError):
        GoExecutionIntents.open_existing(
            service.admissions,
            run_id=args[0],
            operation_id=args[1],
            principal="owner",
            host=service.host,
            source_if_unprepared=lambda: pytest.fail("Corrupt source fallback"),
        )


def capture_receipt(collection_case):
    from karajan.orchestration.go_task_collector import compile_go_capture

    service, args, candidates, journal, result = collection_case
    operation = service.read(*args, principal="owner")
    return compile_go_capture(operation, result, candidates, journal)


def test_capture_commit_lost_reply_is_exact_and_detached_without_candidate_effects(collection_case):
    service, args, candidates, journal, _ = collection_case
    receipt = capture_receipt(collection_case)
    untouched = {
        p: p.read_bytes() for p in (candidates.directory / "candidates.sqlite", journal.path)
    }
    result = service.capture_recorded(
        *args, principal="owner", runner=service.host.runner, capture=receipt
    )
    before = service.admissions.database.read_bytes()
    result["execution"]["collection"]["capture"]["report"]["status"] = "reader-mutated"
    replay = service.capture_recorded(
        *args, principal="owner", runner=service.host.runner, capture=receipt
    )
    assert replay["execution"]["collection"]["capture"] == receipt.as_dict()
    assert replay["execution"]["collection"]["candidate"] is None
    assert replay["execution"]["phase"] == "effect_claimed"
    assert service.admissions.database.read_bytes() == before
    assert all(path.read_bytes() == body for path, body in untouched.items())


@pytest.mark.parametrize("fault", ["runner", "series", "operation", "source", "cancel"])
def test_capture_first_commit_requires_current_exact_identity(collection_case, fault):
    from karajan.execution import ProcessIdentity
    from karajan.orchestration.go_execution_intent import GoTaskCaptureReceipt

    service, args, _, _, _ = collection_case
    receipt = capture_receipt(collection_case)
    runner = service.host.runner
    if fault == "runner":
        runner = ProcessIdentity(runner.pid, "another-birth")
    elif fault == "series":
        document = receipt.as_dict()
        document["freeze_request"]["series_id"] = "unrelated-series"
        receipt = GoTaskCaptureReceipt(document)
    elif fault == "operation":
        document = receipt.as_dict()
        document["intent_digest"] = "f" * 64
        # Rebuild internally consistent DTO; the durable original intent must
        # still reject it, instead of trusting its self-consistent hash alone.
        evidence = {
            key: value
            for key, value in document.items()
            if key not in {"schema_version", "freeze_request", "evidence_digest"}
        }
        document["evidence_digest"] = digest(evidence)
        document["freeze_request"]["writer"]["observation_ref"] = "go-task-stop:" + digest(evidence)
        document["freeze_request"]["authors"][0]["provenance_ref"] = "go-task-author:" + digest(
            evidence
        )
        receipt = GoTaskCaptureReceipt(document)
    elif fault == "source":
        service.source = GoExecutionSource("f" * 64, "d" * 64)
    else:
        service.cancel_intent(*args, principal="owner")
    before = service.admissions.database.read_bytes()
    with pytest.raises(RunError):
        service.capture_recorded(*args, principal="owner", runner=runner, capture=receipt)
    assert service.admissions.database.read_bytes() == before
    assert "collection" not in service.read(*args, principal="owner")["execution"]


def test_same_capture_replay_after_cancel_is_history_but_collection_guard_denies(collection_case):
    service, args, _, _, _ = collection_case
    capture = capture_receipt(collection_case)
    service.capture_recorded(*args, principal="owner", runner=service.host.runner, capture=capture)
    service.cancel_intent(*args, principal="owner")
    before = service.admissions.database.read_bytes()
    assert service.capture_recorded(
        *args, principal="owner", runner=service.host.runner, capture=capture
    )["cancel_requested"]
    with pytest.raises(RunError, match="TASK_EXECUTION_CANCEL_REQUESTED"):
        with service.collection_guard(
            *args,
            principal="owner",
            runner=service.host.runner,
            capture_digest=digest(capture.as_dict()),
        ):
            pytest.fail("Cancelled historical receipt permitted a new freeze")
    assert service.admissions.database.read_bytes() == before


def test_collection_guard_holds_exact_receipt_and_wrong_digest_has_no_effect(collection_case):
    service, args, _, _, _ = collection_case
    capture = capture_receipt(collection_case)
    service.capture_recorded(*args, principal="owner", runner=service.host.runner, capture=capture)
    before = service.admissions.database.read_bytes()
    with service.collection_guard(
        *args,
        principal="owner",
        runner=service.host.runner,
        capture_digest=digest(capture.as_dict()),
    ) as operation:
        assert operation["execution"]["collection"]["capture"] == capture.as_dict()
    with pytest.raises(RunError, match="TASK_CAPTURE_IDENTITY_CONFLICT"):
        with service.collection_guard(
            *args, principal="owner", runner=service.host.runner, capture_digest="f" * 64
        ):
            pytest.fail("Another capture allowed")
    assert service.admissions.database.read_bytes() == before


def test_candidate_late_link_uses_exact_owned_history_and_preserves_cancellation(collection_case):
    service, args, candidates, journal, result = collection_case
    receipt = capture_receipt(collection_case)
    capture = receipt.as_dict()
    service.capture_recorded(*args, principal="owner", runner=service.host.runner, capture=receipt)
    candidate = candidates.freeze_projection(
        capture["projection"], dict(result.capture.files), capture["freeze_request"]
    )
    # Simulate lost Candidate commit response followed by controller cancellation
    # and code deployment. The original immutable candidate is still linkable.
    service.cancel_intent(*args, principal="owner")
    service.source = GoExecutionSource("e" * 64, "f" * 64)
    paths = (
        candidates.directory / "candidates.sqlite",
        journal.path,
        service.admissions.routing.capacity.path,
    )
    before = {p: p.read_bytes() for p in paths}
    linked = service.candidate_recorded(
        *args, principal="owner", capture_digest=digest(capture), candidate_id=candidate["id"]
    )
    assert linked["state"] == "cancellation_pending" and linked["cancel_requested"]
    assert linked["execution"]["phase"] == "candidate_recorded"
    assert linked["execution"]["collection"]["candidate"]["id"] == candidate["id"]
    assert all(path.read_bytes() == body for path, body in before.items())
    before_operation = service.admissions.database.read_bytes()
    assert (
        service.candidate_recorded(
            *args, principal="owner", capture_digest=digest(capture), candidate_id=candidate["id"]
        )
        == linked
    )
    assert service.admissions.database.read_bytes() == before_operation


def test_candidate_id_cannot_substitute_an_unrelated_candidate_or_create_one(collection_case):
    service, args, candidates, _, _ = collection_case
    receipt = capture_receipt(collection_case)
    service.capture_recorded(*args, principal="owner", runner=service.host.runner, capture=receipt)
    before = {
        p: p.read_bytes()
        for p in (service.admissions.database, candidates.directory / "candidates.sqlite")
    }
    with pytest.raises(RunError, match="TASK_CANDIDATE_BINDING_MISMATCH"):
        service.candidate_recorded(
            *args,
            principal="owner",
            capture_digest=digest(receipt.as_dict()),
            candidate_id="invented-candidate",
        )
    assert all(path.read_bytes() == body for path, body in before.items())


def test_capture_dto_freezes_nested_input_and_rejects_unbounded_extra_content(collection_case):
    from karajan.orchestration.go_execution_intent import GoTaskCaptureReceipt

    original = capture_receipt(collection_case).as_dict()
    receipt = GoTaskCaptureReceipt(original)
    original["report"]["status"] = "changed-after-construction"
    assert receipt.as_dict()["report"]["status"] == "completed"
    invalid = receipt.as_dict() | {"raw_file_bytes": "not-an-accepted-field"}
    with pytest.raises(RunError, match="TASK_CAPTURE_RECEIPT_INVALID"):
        GoTaskCaptureReceipt(invalid)
