"""C coverage for the pre-native Reviewer execution ledger."""

import json
import os
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
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
from test_reviewer_binding import (
    _activate_reviewer_reservation,
    _passed_reviewer_subject,
    _tighten_reviewer_conservative_capacity,
)

pytest_plugins = (
    "test_projected_qualification_store",
    "test_candidate_checks",
    "test_reviewer_binding",
)


def _service(tmp_path: Path, binding_case, *, conservative_observation_age: float | None = None):
    intents, (run_id, _), candidates, _, _ = _passed_reviewer_subject(binding_case)
    if conservative_observation_age is not None:
        _tighten_reviewer_conservative_capacity(
            intents.admissions.routing.capacity,
            maximum=2,
            observation_age=conservative_observation_age,
        )
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


def test_rejects_schema_valid_ledger_inside_actual_registered_repository_without_writing_it(
    tmp_path, binding_case
):
    service, _, _, _ = _service(tmp_path, binding_case)
    root = Path(service.admissions.routing.planner.projects.list()[0]["repository"]["root"])
    ledger = root / "existing-reviewer-ledger.sqlite"
    ledger.write_bytes(service.database.read_bytes())
    ledger.chmod(0o600)
    before, mode = ledger.read_bytes(), ledger.stat().st_mode
    with pytest.raises(RunError, match="REVIEWER_EXECUTION_LEDGER_IN_REPOSITORY"):
        ReviewerExecutionIntents(
            ledger,
            service.admissions,
            service.candidates,
            source=service.source,
            host=service.host,
            launch_compiler=service.launch_compiler,
            existing_only=True,
        )
    assert ledger.read_bytes() == before
    assert ledger.stat().st_mode == mode


def test_rejects_parent_symlink_alias_of_actual_registered_repository(tmp_path, binding_case):
    service, _, _, _ = _service(tmp_path, binding_case)
    root = Path(service.admissions.routing.planner.projects.list()[0]["repository"]["root"])
    ledger = root / "symlink-reviewer-ledger.sqlite"
    ledger.write_bytes(service.database.read_bytes())
    alias = tmp_path / "repository-alias"
    try:
        os.symlink(root, alias, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlinks unavailable: {error}")
    before, mode = ledger.read_bytes(), ledger.stat().st_mode
    with pytest.raises(RunError, match="REVIEWER_EXECUTION_LEDGER_IN_REPOSITORY"):
        ReviewerExecutionIntents(
            alias / ledger.name,
            service.admissions,
            service.candidates,
            source=service.source,
            host=service.host,
            launch_compiler=service.launch_compiler,
            existing_only=True,
        )
    assert ledger.read_bytes() == before
    assert ledger.stat().st_mode == mode


def test_rejects_repository_hardlink_without_changing_original_ledger_or_mode(
    tmp_path, binding_case
):
    service, _, _, _ = _service(tmp_path, binding_case)
    root = Path(service.admissions.routing.planner.projects.list()[0]["repository"]["root"])
    linked = root / "reviewer-ledger-hardlink.sqlite"
    try:
        os.link(service.database, linked)
    except OSError as error:
        pytest.skip(f"hard links unavailable: {error}")
    before, mode = service.database.read_bytes(), service.database.stat().st_mode
    with pytest.raises(RunError, match="REVIEWER_EXECUTION_LEDGER_IN_REPOSITORY"):
        ReviewerExecutionIntents(
            service.database,
            service.admissions,
            service.candidates,
            source=service.source,
            host=service.host,
            launch_compiler=service.launch_compiler,
            existing_only=True,
        )
    assert service.database.read_bytes() == before
    assert service.database.stat().st_mode == mode


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


def test_new_intent_rechecks_current_deployment_source_at_its_writer_boundary(
    tmp_path, binding_case
):
    service, run_id, reviewer_id, _ = _service(tmp_path, binding_case)
    changed = ReviewerExecutionSource("3" * 64, "4" * 64)
    service.current_source = lambda: changed
    before, host_before = service.database.read_bytes(), service.host.database.read_bytes()
    with pytest.raises(RunError, match="REVIEWER_EXECUTION_SOURCE_CHANGED"):
        service.prepare(run_id, reviewer_id, principal="owner", command_key="stale-new-intent")
    assert service.database.read_bytes() == before
    assert service.read(run_id, reviewer_id, principal="owner") is None
    assert service.host.database.read_bytes() == host_before


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


@pytest.mark.parametrize("boundary", ["host", "control", "claim"])
def test_expiry_after_real_sqlite_writer_wait_blocks_the_actual_effect(
    tmp_path, binding_case, boundary
):
    service, run_id, reviewer_id, _ = _service(tmp_path, binding_case)
    service.prepare(run_id, reviewer_id, principal="owner", command_key="prepare")
    reached = threading.Event()
    release = threading.Event()
    outcome: list[BaseException] = []

    if boundary == "host":
        target, original = service.host, service.host.prepare

        def call(*args, **kwargs):
            reached.set()
            return original(*args, **kwargs)

        target.prepare = call
        def action():
            return service.freeze_launch(run_id, reviewer_id, principal="owner")
        database = service.host.database
    elif boundary == "control":
        target, original = service.host, service.host.initialize_control_once

        def call(*args, **kwargs):
            reached.set()
            assert release.wait(5)
            return original(*args, **kwargs)

        target.initialize_control_once = call
        def action():
            return service.freeze_launch(run_id, reviewer_id, principal="owner")
        database = service.host.database
    else:
        service.freeze_launch(run_id, reviewer_id, principal="owner")
        # This focused ledger-boundary regression supplies the already-held
        # Host identity; it still waits on the real execution SQLite writer.
        from karajan.execution._platform import process_identity

        identity = process_identity(os.getpid())
        assert identity is not None

        @contextmanager
        def current_runner(*args, **kwargs):
            reached.set()
            yield identity

        service.host.wait_for_runner_registration = lambda *args, **kwargs: identity
        service.host.current_runner_guard = current_runner
        def action():
            return service.claim_registered_observer(
                run_id, reviewer_id, principal="owner", timeout_seconds=0.01
            )
        database = service.database

    holder = None
    if boundary != "control":
        holder = sqlite3.connect(database, isolation_level=None, timeout=5)
        holder.execute("BEGIN IMMEDIATE")

    def invoke():
        try:
            action()
        except BaseException as error:
            outcome.append(error)

    thread = threading.Thread(target=invoke)
    thread.start()
    assert reached.wait(5)
    if boundary == "control":
        holder = sqlite3.connect(database, isolation_level=None, timeout=5)
        holder.execute("BEGIN IMMEDIATE")
        release.set()
    service.admissions.routing.capacity.clock = lambda: time.time() + 10_000
    assert holder is not None
    # The real Host control transaction is now blocked behind this writer.
    time.sleep(0.05)
    holder.commit()
    holder.close()
    thread.join(10)
    assert not thread.is_alive()
    assert outcome and isinstance(outcome[0], RunError)
    current = service.read(run_id, reviewer_id, principal="owner")
    assert current is not None and current["effect_claim"] is None
    if boundary == "host":
        with pytest.raises(KeyError):
            service.host.inspect(current["planned_attempt_id"])
    if boundary == "control":
        with sqlite3.connect(service.host.database) as db:
            assert db.execute("SELECT COUNT(*) FROM controls").fetchone()[0] == 0


@pytest.mark.parametrize("boundary", ["host", "control", "claim"])
def test_quota_freshness_after_real_writer_wait_blocks_each_actual_effect(
    tmp_path, binding_case, boundary
):
    """A live reservation may outlast a required fresh quota observation."""
    service, run_id, reviewer_id, _ = _service(
        tmp_path, binding_case, conservative_observation_age=5
    )
    capacity = service.admissions.routing.capacity
    service.prepare(run_id, reviewer_id, principal="owner", command_key="prepare")
    reached, release = threading.Event(), threading.Event()
    outcome: list[BaseException] = []

    if boundary == "host":
        original = service.host.prepare

        def action():
            return service.freeze_launch(run_id, reviewer_id, principal="owner")

        def call(*args, **kwargs):
            reached.set()
            return original(*args, **kwargs)

        service.host.prepare = call
        database = service.host.database
    elif boundary == "control":
        original = service.host.initialize_control_once

        def action():
            return service.freeze_launch(run_id, reviewer_id, principal="owner")

        def call(*args, **kwargs):
            reached.set()
            assert release.wait(5)
            return original(*args, **kwargs)

        service.host.initialize_control_once = call
        database = service.host.database
    else:
        service.freeze_launch(run_id, reviewer_id, principal="owner")
        from karajan.execution._platform import process_identity

        identity = process_identity(os.getpid())
        assert identity is not None

        @contextmanager
        def current_runner(*args, **kwargs):
            reached.set()
            yield identity

        service.host.wait_for_runner_registration = lambda *args, **kwargs: identity
        service.host.current_runner_guard = current_runner

        def action():
            return service.claim_registered_observer(
                run_id, reviewer_id, principal="owner", timeout_seconds=0.01
            )

        database = service.database

    holder = None
    if boundary != "control":
        holder = sqlite3.connect(database, isolation_level=None, timeout=5)
        holder.execute("BEGIN IMMEDIATE")

    def invoke():
        try:
            action()
        except BaseException as error:
            outcome.append(error)

    thread = threading.Thread(target=invoke)
    thread.start()
    assert reached.wait(5)
    if boundary == "control":
        holder = sqlite3.connect(database, isolation_level=None, timeout=5)
        holder.execute("BEGIN IMMEDIATE")
        release.set()
    # Reservation, qualification and estimate windows remain valid.  Only the
    # conservative quota observation age crosses while the real writer waits.
    capacity.clock = lambda: 1006.0
    assert holder is not None
    time.sleep(0.05)
    holder.commit()
    holder.close()
    thread.join(10)
    assert not thread.is_alive()
    assert outcome and isinstance(outcome[0], RunError)
    assert "REVIEWER_CAPACITY_REVALIDATION_FAILED" in str(outcome[0])
    current = service.read(run_id, reviewer_id, principal="owner")
    assert current is not None and current["effect_claim"] is None
    if boundary == "host":
        with pytest.raises(KeyError):
            service.host.inspect(current["planned_attempt_id"])
    if boundary == "control":
        with sqlite3.connect(service.host.database) as db:
            assert db.execute("SELECT COUNT(*) FROM controls").fetchone()[0] == 0


def test_deployment_source_read_before_final_deadline_check_blocks_host_write(
    tmp_path, binding_case
):
    service, run_id, reviewer_id, _ = _service(tmp_path, binding_case)
    prepared = service.prepare(run_id, reviewer_id, principal="owner", command_key="prepare")
    with sqlite3.connect(service.admissions.database) as db:
        row = db.execute(
            "SELECT data FROM run_execution_budgets WHERE run_id=?", (run_id,)
        ).fetchone()
        budget = json.loads(row[0])
        budget.update(started_at=1000.0, max_duration_seconds=10)
        db.execute(
            "UPDATE run_execution_budgets SET data=? WHERE run_id=?", (json.dumps(budget), run_id)
        )
    calls = 0

    def current_source():
        nonlocal calls
        calls += 1
        if calls == 2:
            service.admissions.routing.planner.clock = lambda: 1010.0
        return service.source

    service.current_source = current_source
    service.admissions.routing.planner.clock = lambda: 1001.0
    before = service.host.database.read_bytes()
    with pytest.raises(RunError, match="RUN_DURATION_LIMIT"):
        service.freeze_launch(run_id, reviewer_id, principal="owner")
    assert calls >= 2
    assert service.host.database.read_bytes() == before
    with pytest.raises(KeyError):
        service.host.inspect(prepared["planned_attempt_id"])


def test_lost_host_prepare_reply_reopens_for_historical_read_only_correlation(
    tmp_path, binding_case
):
    service, run_id, reviewer_id, _ = _service(tmp_path, binding_case)
    original = service.prepare(run_id, reviewer_id, principal="owner", command_key="prepare")
    prepare = service.host.prepare
    counts = {"prepare": 0, "control": 0, "start": 0, "claim": 0}

    def lose_reply(*args, **kwargs):
        counts["prepare"] += 1
        prepare(*args, **kwargs)
        raise ConnectionError("reply lost after Host commit")

    def count_control(*args, **kwargs):
        counts["control"] += 1
        return pytest.fail("lost Host reply must not initialize control")

    service.host.prepare = lose_reply
    service.host.initialize_control_once = count_control
    with pytest.raises(ConnectionError, match="reply lost"):
        service.freeze_launch(run_id, reviewer_id, principal="owner")
    assert counts == {"prepare": 1, "control": 0, "start": 0, "claim": 0}
    assert service.read(run_id, reviewer_id, principal="owner")["host_prepared_id"] is None
    host_before, ledger_before = service.host.database.read_bytes(), service.database.read_bytes()
    reopened = ReviewerExecutionIntents(
        service.database,
        service.admissions,
        service.candidates,
        source=service.source,
        host=RunnerHost(service.host.directory, existing_only=True),
        launch_compiler=service.launch_compiler,
        existing_only=True,
    )
    observed = reopened.inspect_host(run_id, reviewer_id, principal="owner")
    assert observed["host_prepared_id"] is None
    assert observed["host_observation"]["prepared_id"] == original["start_key"]
    assert reopened.host.database.read_bytes() == host_before
    assert reopened.database.read_bytes() == ledger_before
    with sqlite3.connect(reopened.host.database) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM executions WHERE start_key=?", (original["start_key"],)
        ).fetchone()[0] == 1
        assert db.execute(
            "SELECT COUNT(*) FROM controls WHERE attempt_id=?", (original["planned_attempt_id"],)
        ).fetchone()[0] == 0
    with pytest.raises(RunError, match="REVIEWER_EXECUTION_HOST_PREPARE_REQUIRED"):
        reopened.claim_registered_observer(run_id, reviewer_id, principal="owner")


def test_cancel_serializes_with_actual_inspect_host_writer_and_stays_durable(
    tmp_path, binding_case
):
    service, run_id, reviewer_id, _ = _service(tmp_path, binding_case)
    service.prepare(run_id, reviewer_id, principal="owner", command_key="prepare")
    service.freeze_launch(run_id, reviewer_id, principal="owner")
    entered, continue_inspect = threading.Event(), threading.Event()
    original = service.host.inspect

    def delayed(*args, **kwargs):
        entered.set()
        assert continue_inspect.wait(5)
        return original(*args, **kwargs)

    service.host.inspect = delayed
    inspect_errors: list[BaseException] = []
    thread = threading.Thread(
        target=lambda: _record_error(
            inspect_errors, lambda: service.inspect_host(run_id, reviewer_id, principal="owner")
        )
    )
    thread.start()
    assert entered.wait(5)
    cancelled = service.cancel(run_id, reviewer_id, principal="owner")
    continue_inspect.set()
    thread.join(10)
    assert not thread.is_alive() and not inspect_errors
    assert cancelled is not None and cancelled["cancel_requested"]
    current = service.read(run_id, reviewer_id, principal="owner")
    assert current is not None and current["cancel_requested"]


def _record_error(errors, call):
    try:
        call()
    except BaseException as error:
        errors.append(error)


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
    # The lost-reply child deliberately writes no reply.  Observe its actual
    # Host terminal record before retrying the controller operation, so a
    # natural None -> 0 exit-code update cannot race a snapshot comparison.
    deadline = time.monotonic() + 5
    before_replay = reopened.host.inspect(prepared["planned_attempt_id"])
    while before_replay.state != "exited":
        assert time.monotonic() < deadline, "lost-reply direct child did not exit"
        time.sleep(0.02)
        before_replay = reopened.host.inspect(prepared["planned_attempt_id"])
    with sqlite3.connect(reopened.host.database) as database:
        persisted_before = database.execute(
            "SELECT start_key, attempt_id, supervisor_pid, supervisor_birth, "
            "runner_pid, runner_birth, launch_phase FROM executions WHERE start_key=?",
            (prepared["start_key"],),
        ).fetchone()
        launch_count_before = database.execute(
            "SELECT COUNT(*) FROM executions WHERE start_key=?", (prepared["start_key"],)
        ).fetchone()[0]
        child_count_before = database.execute(
            "SELECT COUNT(*) FROM executions WHERE start_key=? AND runner_pid IS NOT NULL",
            (prepared["start_key"],),
        ).fetchone()[0]
    replayed = reopened.host.start(prepared["start_key"], activation)
    after_replay = reopened.host.inspect(prepared["planned_attempt_id"])
    with sqlite3.connect(reopened.host.database) as database:
        persisted_after = database.execute(
            "SELECT start_key, attempt_id, supervisor_pid, supervisor_birth, "
            "runner_pid, runner_birth, launch_phase FROM executions WHERE start_key=?",
            (prepared["start_key"],),
        ).fetchone()
        launch_count_after = database.execute(
            "SELECT COUNT(*) FROM executions WHERE start_key=?", (prepared["start_key"],)
        ).fetchone()[0]
        child_count_after = database.execute(
            "SELECT COUNT(*) FROM executions WHERE start_key=? AND runner_pid IS NOT NULL",
            (prepared["start_key"],),
        ).fetchone()[0]
    # Completion and usage fields are mutable observations.  Recovery instead
    # proves it replayed the one persisted launch and its one registered child.
    assert replayed.prepared_id == prepared["start_key"]
    assert replayed.attempt_id == prepared["planned_attempt_id"]
    assert after_replay.prepared_id == before_replay.prepared_id == prepared["start_key"]
    assert after_replay.attempt_id == before_replay.attempt_id == prepared["planned_attempt_id"]
    assert (
        replayed.launch_phase
        == after_replay.launch_phase
        == before_replay.launch_phase
        == "acknowledged"
    )
    assert replayed.supervisor == after_replay.supervisor == before_replay.supervisor
    assert replayed.processes == after_replay.processes == before_replay.processes == ()
    assert persisted_after == persisted_before
    assert launch_count_after == launch_count_before == 1
    assert child_count_after == child_count_before == 1
    assert reopened.cancel(run_id, reviewer_id, principal="owner")["cancel_requested"] is True
    with pytest.raises(RunError, match="REVIEWER_EXECUTION_CANCELLED"):
        reopened.claim_registered_observer(run_id, reviewer_id, principal="owner")
