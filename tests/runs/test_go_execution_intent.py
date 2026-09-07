"""One original operation; synthetic qualification only, no provider effects."""

import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event

import pytest
from karajan.candidates import CandidateStore
from karajan.contracts.probe import AttemptManifest
from karajan.execution import Activation, ProbeCrash, ProcessIdentity, ProcessSpec, RunnerHost
from karajan.orchestration.go_execution_intent import GoExecutionIntents, GoExecutionSource
from karajan.orchestration.workspace import ApprovedTaskWorkspace
from karajan.runs import RunError
from test_projected_go_routing import approved_task, case, projected

__all__ = ["case", "projected"]


@pytest.fixture
def reservation(projected, tmp_path: Path):
    repository = projected["repository"]
    for relative, body in {
        "src/report.py": "print('approved task')\n",
        "tests/test_report.py": "assert True\n",
    }.items():
        path = repository / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    for args in (
        ["add", "."],
        [
            "-c",
            "user.name=Intent Fixture",
            "-c",
            "user.email=intent@example.invalid",
            "commit",
            "-qm",
            "Task files",
        ],
    ):
        subprocess.run(["git", "-C", str(repository), *args], check=True, capture_output=True)
    projects = projected["projects"]
    current = projects.get(projected["project_id"])
    projects.update(
        current["id"],
        {
            "name": current["name"],
            "base_ref": "main",
            "target_branch": "main",
            "allowed_target_branches": ["main"],
        },
        expected_revision=current["revision"],
        command_key="new-baseline",
        principal="owner",
    )
    admissions, routing, run, _ = approved_task(projected, tmp_path)
    operation = admissions.enqueue(run["id"], "implement", principal="owner", command_key="enqueue")
    operation = admissions.advance(run["id"], operation["id"], principal="owner")
    assert operation["state"] == "reserved"
    return admissions, routing, run, operation


@pytest.fixture
def ready(reservation, tmp_path):
    admissions, routing, run, operation = reservation
    workspace = ApprovedTaskWorkspace(admissions, CandidateStore(tmp_path / "candidates"))
    frozen = workspace.prepare(run["id"], operation["id"], principal="owner")
    return admissions, routing, run, operation, frozen


def controller(admissions, tmp_path):
    return GoExecutionIntents(
        admissions,
        source=GoExecutionSource(runner_source_sha256="a" * 64, native_source_sha256="b" * 64),
        host=RunnerHost(tmp_path / "host"),
    )


@pytest.fixture
def prepared(ready, tmp_path):
    admissions, _, run, operation, _ = ready
    service = controller(admissions, tmp_path)
    service.prepare_intent(run["id"], operation["id"], principal="owner", command_key="prepare")
    return service, (run["id"], operation["id"])


def activate(service, args):
    current = service.read(*args, principal="owner")
    intent = current["execution"]["intent"]
    receipt = service.admissions.routing.capacity.activate(
        intent["admission_id"], command_key=intent["activation_key"]
    )
    assert receipt["decision"] == "capacity_revalidated"
    return receipt


def host_prepare(service, args, tmp_path, *, wrong_key=False):
    current = service.read(*args, principal="owner")
    intent = current["execution"]["intent"]
    registration = current["workspace"]["source_binding"]["profile_registration"]
    manifest = AttemptManifest(
        id=intent["attempt_id"],
        fence=intent["fence"],
        role="worker",
        profile_id=registration["id"],
        profile_revision=registration["revision"],
        authorization_ref=intent["authorization_ref"],
        budget_ref=intent["budget_ref"],
        permissions=["read", "edit"],
        requested_binding=registration["profile"]["binding"],
    )
    return service.host.prepare(
        manifest,
        "wrong-key" if wrong_key else intent["start_key"],
        ProcessSpec((sys.executable, "-c", "pass"), tmp_path),
    )


@pytest.fixture
def launched_intent(prepared, tmp_path):
    service, args = prepared
    activate(service, args)
    service.activation_recorded(*args, principal="owner")
    host_prepare(service, args, tmp_path)
    service.record_host_prepared(*args, principal="owner")
    service.mark_start_unknown(*args, principal="owner")
    # This layer's trusted ProcessIdentity input substitutes the Host port only.
    # It is not evidence of a running Host child, native start, or a model effect.
    return service, args, ProcessIdentity(12345, "synthetic-owned-birth")


def test_prepare_fixes_original_identity_without_activation_or_new_effect(
    ready, projected, tmp_path
):
    admissions, routing, run, operation, workspace = ready
    source = GoExecutionSource(runner_source_sha256="a" * 64, native_source_sha256="b" * 64)
    service = GoExecutionIntents(admissions, source=source, host=RunnerHost(tmp_path / "host"))
    before = {
        path: path.read_bytes() for path in (routing.capacity.path, projected["suite"].journal.path)
    }
    result = service.prepare_intent(
        run["id"], operation["id"], principal="owner", command_key="prepare"
    )
    assert result["id"] == operation["id"]
    assert result["state"] == "execution_pending"
    execution = result["execution"]
    assert execution["schema_version"] == "karajan.go-task-execution-intent.v1"
    assert execution["phase"] == "prepared"
    assert execution["intent"]["attempt_id"] == operation["planned_attempt_id"]
    assert execution["intent"]["context_id"] == operation["planned_context_id"]
    assert execution["intent"]["workspace_digest"] == workspace["digest"]
    assert execution["intent"]["admission_id"] == operation["capacity_receipt"]["admission_id"]
    assert execution["intent"]["runner_source_sha256"] == source.runner_source_sha256
    assert result["activation_allowed"] is False and result["dispatch_enabled"] is False
    assert all(path.read_bytes() == body for path, body in before.items())


def test_missing_workspace_does_not_reserve_or_capture_implicitly(reservation, tmp_path):
    admissions, routing, run, operation = reservation
    before = {p: p.read_bytes() for p in (admissions.database, routing.capacity.path)}
    with pytest.raises(RunError, match="TASK_WORKSPACE_NOT_PREPARED"):
        controller(admissions, tmp_path).prepare_intent(
            run["id"], operation["id"], principal="owner", command_key="prepare"
        )
    assert all(path.read_bytes() == body for path, body in before.items())


def test_cancelled_reservation_cannot_become_an_intent(ready, tmp_path):
    admissions, _, run, operation, _ = ready
    admissions.cancel(run["id"], operation["id"], principal="owner")
    with pytest.raises(RunError, match="TASK_EXECUTION_RESERVATION_REQUIRED"):
        controller(admissions, tmp_path).prepare_intent(
            run["id"], operation["id"], principal="owner", command_key="prepare"
        )


def test_prepare_replay_same_or_other_key_preserves_single_operation(prepared):
    service, args = prepared
    original = service.read(*args, principal="owner")
    for key in ("prepare", "another-key"):
        assert service.prepare_intent(*args, principal="owner", command_key=key) == original
    with sqlite3.connect(service.admissions.database) as db:
        assert db.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 1
    with pytest.raises(RunError, match="IDEMPOTENCY_CONFLICT"):
        service.prepare_intent(*args, principal="owner", command_key="enqueue")


def test_pending_intent_blocks_another_enqueue_and_capacity_refresh_does_not_erase_it(prepared):
    service, args = prepared
    activate(service, args)
    assert service.admissions.get(*args, principal="owner")["state"] == "execution_pending"
    with pytest.raises(RunError, match="TASK_ADMISSION_PENDING"):
        service.admissions.enqueue(args[0], "implement", principal="owner", command_key="duplicate")


@pytest.mark.parametrize("identity", ["owner", "operation", "run"])
def test_exact_owner_and_operation_are_required(prepared, identity):
    service, args = prepared
    run_id, operation_id = args
    principal = "other" if identity == "owner" else "owner"
    if identity == "operation":
        operation_id = "other"
    if identity == "run":
        run_id = "other"
    with pytest.raises(
        RunError, match="USER_DECISION_REQUIRED" if identity == "owner" else "NOT_FOUND"
    ):
        service.read(run_id, operation_id, principal=principal)


def test_read_and_reconcile_are_detached_no_clock_no_write_history(prepared, monkeypatch):
    service, args = prepared
    routing = service.admissions.routing
    paths = (
        service.admissions.database,
        routing.planner.database,
        routing.capacity.path,
        routing.planner.projects.database,
        service.host.database,
    )
    before = {p: p.read_bytes() for p in paths}

    def forbidden():
        pytest.fail("Historical read called a clock")

    monkeypatch.setattr(routing.capacity, "clock", forbidden)
    monkeypatch.setattr(routing.planner, "clock", forbidden)
    original = service.read(*args, principal="owner")
    original["execution"]["intent"]["attempt_id"] = "changed-by-reader"
    assert service.reconcile(*args, principal="owner")["planned_attempt_id"] != "changed-by-reader"
    assert all(path.read_bytes() == body for path, body in before.items())


@pytest.mark.parametrize("missing", ["admission", "run"])
def test_missing_ledger_is_not_recreated_by_status(prepared, missing):
    service, args = prepared
    path = (
        service.admissions.database
        if missing == "admission"
        else service.admissions.routing.planner.database
    )
    path.rename(path.with_suffix(".saved"))
    with pytest.raises(sqlite3.OperationalError):
        service.read(*args, principal="owner")
    assert not path.exists()


def test_source_change_allows_historical_read_but_not_new_execution_action(prepared):
    service, args = prepared
    changed = GoExecutionIntents(
        service.admissions,
        host=service.host,
        source=GoExecutionSource(runner_source_sha256="c" * 64, native_source_sha256="b" * 64),
    )
    assert changed.read(*args, principal="owner") == service.read(*args, principal="owner")
    with pytest.raises(RunError, match="TASK_EXECUTION_SOURCE_CHANGED"):
        changed.prepare_intent(*args, principal="owner", command_key="new-source")


def test_lost_activation_response_recovers_original_expiry_without_another_activation(prepared):
    service, args = prepared
    receipt = activate(service, args)  # response deliberately not recorded in operation
    reopened = GoExecutionIntents(service.admissions, source=service.source, host=service.host)
    capacity = service.admissions.routing.capacity
    before = capacity.path.read_bytes()
    result = reopened.activation_recorded(*args, principal="owner")
    assert result["execution"]["capacity_activation"] == receipt
    assert result["execution"]["phase"] == "activated"
    operation_before = service.admissions.database.read_bytes()
    assert reopened.activation_recorded(*args, principal="owner") == result
    assert service.admissions.database.read_bytes() == operation_before
    assert capacity.path.read_bytes() == before


def test_missing_activation_receipt_never_activates(prepared):
    service, args = prepared
    before = {
        p: p.read_bytes()
        for p in (service.admissions.database, service.admissions.routing.capacity.path)
    }
    assert service.activation_recorded(*args, principal="owner")["execution"]["phase"] == "prepared"
    assert all(path.read_bytes() == body for path, body in before.items())


def test_prepared_only_is_not_an_effect_claim(prepared):
    service, args = prepared
    with pytest.raises(RunError, match="TASK_EXECUTION_START_INTENT_REQUIRED"):
        service.effect_start_claim(*args, principal="owner", runner=ProcessIdentity(1, "fixture"))


def test_wrong_owned_host_start_key_cannot_be_registered(prepared, tmp_path):
    service, args = prepared
    activate(service, args)
    service.activation_recorded(*args, principal="owner")
    host_prepare(service, args, tmp_path, wrong_key=True)
    with pytest.raises(RunError, match="TASK_EXECUTION_HOST_BINDING_MISMATCH"):
        service.record_host_prepared(*args, principal="owner")


def test_racing_claims_commit_only_once_and_lost_return_cannot_reclaim(launched_intent):
    service, args, identity = launched_intent
    barrier = Barrier(2)

    def claim():
        barrier.wait()
        return service.effect_start_claim(*args, principal="owner", runner=identity)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: claim(), range(2)))
    assert sorted(result["claim_allowed"] for result in results) == [False, True]
    assert results[0]["execution"]["effect_claim"] == results[1]["execution"]["effect_claim"]
    before = service.admissions.database.read_bytes()
    reopened = GoExecutionIntents(service.admissions, source=service.source, host=service.host)
    assert not reopened.effect_start_claim(*args, principal="owner", runner=identity)[
        "claim_allowed"
    ]
    assert not reopened.effect_start_claim(
        *args, principal="owner", runner=ProcessIdentity(identity.pid + 1, "replacement")
    )["claim_allowed"]
    assert service.admissions.database.read_bytes() == before
    assert "claim_allowed" not in reopened.read(*args, principal="owner")


def test_late_host_reply_and_prepare_replay_never_undo_claim_or_cancel(launched_intent):
    service, args, identity = launched_intent
    service.effect_start_claim(*args, principal="owner", runner=identity)
    observed = service.host_started(*args, principal="owner")
    assert observed["state"] == "executing"
    assert observed["execution"]["phase"] == "effect_claimed"
    service.admissions.cancel(*args, principal="owner")
    observed = service.host_started(*args, principal="owner")
    replay = service.prepare_intent(*args, principal="owner", command_key="prepare")
    assert replay == observed
    assert observed["state"] == "cancellation_pending"
    assert observed["cancel_requested"] is True
    assert observed["execution"]["effect_claim"] is not None
    with pytest.raises(RunError, match="TASK_EXECUTION_CANCEL_REQUESTED"):
        service.effect_start_claim(*args, principal="owner", runner=identity)


def test_cancellation_is_persisted_without_claiming_remote_stop_or_refunding(prepared):
    service, args = prepared
    before = service.admissions.routing.capacity.path.read_bytes()
    result = service.cancel_intent(*args, principal="owner")
    assert result["state"] == "cancellation_pending"
    assert result["cancellation_receipt"] is None
    assert result["cancel_requested"] is result["execution"]["cancel_requested"] is True
    assert service.admissions.routing.capacity.path.read_bytes() == before
    assert service.reconcile(*args, principal="owner") == result


def test_current_claim_guard_is_readonly_and_serializes_cancellation(launched_intent):
    service, args, identity = launched_intent
    service.effect_start_claim(*args, principal="owner", runner=identity)
    before = service.admissions.database.read_bytes()
    attempted = Event()

    def cancel():
        attempted.set()
        return service.cancel_intent(*args, principal="owner")

    with ThreadPoolExecutor(max_workers=1) as pool:
        with service.effect_claim_guard(*args, principal="owner", runner=identity) as held:
            assert held["execution"]["effect_claim"]["runner"]["birth"] == identity.birth
            future = pool.submit(cancel)
            assert attempted.wait(2)
            assert not future.done()
            assert service.admissions.database.read_bytes() == before
        assert future.result(timeout=3)["cancel_requested"] is True
    with pytest.raises(RunError, match="TASK_EXECUTION_CANCEL_REQUESTED"):
        with service.effect_claim_guard(*args, principal="owner", runner=identity):
            pytest.fail("Cancelled claim granted")


def test_claim_guard_rejects_another_incarnation(launched_intent):
    service, args, identity = launched_intent
    service.effect_start_claim(*args, principal="owner", runner=identity)
    with pytest.raises(RunError, match="TASK_EXECUTION_CLAIM_NOT_CURRENT"):
        with service.effect_claim_guard(
            *args, principal="owner", runner=ProcessIdentity(identity.pid, "later-birth")
        ):
            pytest.fail("Old PID accepted as another incarnation")


def test_startup_guard_is_readonly_and_cannot_follow_a_claim(launched_intent):
    service, args, identity = launched_intent
    before = service.admissions.database.read_bytes()
    with service.startup_guard(*args, principal="owner") as current:
        assert current["execution"]["phase"] == "start_unknown"
    assert service.admissions.database.read_bytes() == before
    service.effect_start_claim(*args, principal="owner", runner=identity)
    with pytest.raises(RunError, match="TASK_EXECUTION_START_INTENT_REQUIRED"):
        with service.startup_guard(*args, principal="owner"):
            pytest.fail("Claimed native start became available again")


def test_lost_host_acceptance_reply_is_observation_not_native_authority(
    launched_intent, monkeypatch
):
    service, args, identity = launched_intent
    current = service.read(*args, principal="owner")
    intent = current["execution"]["intent"]
    # Match the synthetic Capacity clock, and stop before any process launch.
    monkeypatch.setattr("karajan.execution.host.time.time", lambda: 1000.0)
    activation = Activation(
        id=intent["admission_id"],
        attempt_id=intent["attempt_id"],
        fence=intent["fence"],
        authorization_ref=intent["authorization_ref"],
        budget_ref=intent["budget_ref"],
        expires_at=current["execution"]["capacity_activation"]["expires_at"],
    )
    service.host.set_control(
        intent["attempt_id"],
        fence=intent["fence"],
        authorization_ref=intent["authorization_ref"],
        dispatch_enabled=True,
    )
    with service.startup_guard(*args, principal="owner"):
        with pytest.raises(ProbeCrash, match="after_accept"):
            service.host.start(intent["start_key"], activation, crash_at="after_accept")
    current = service.host_started(*args, principal="owner")
    assert current["state"] == "execution_unknown"
    assert current["execution"]["phase"] == "start_unknown"
    assert current["execution"]["effect_claim"] is None
    assert current["execution"]["host_observation"]["launch_phase"] != "prepared"
    with pytest.raises(RunError, match="TASK_EXECUTION_CLAIM_NOT_CURRENT"):
        with service.effect_claim_guard(*args, principal="owner", runner=identity):
            pytest.fail("Unknown Host acceptance conferred native authority")
    # The real Host replays its original accepted activation without a new spawn.
    before = service.host.database.read_bytes()
    replay = service.host.start(intent["start_key"], activation)
    assert replay.supervisor is None
    assert service.host.database.read_bytes() == before
