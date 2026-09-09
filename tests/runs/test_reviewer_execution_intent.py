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
from types import SimpleNamespace

import karajan.execution.host as runner_host_module
import karajan.orchestration.reviewer_execution_intent as reviewer_execution_intent
import karajan.orchestration.reviewer_input as reviewer_input
import pytest
from karajan.capacity.store import CapacityEffectCapability
from karajan.execution import Activation, LaunchDenied, ProcessSpec, RunnerHost
from karajan.orchestration.reviewer_execution_binding import host_manifest
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


def _record_new_passed_check(service, worker):
    """Record a later successful evidence row for one already-passed Check."""
    check = worker["validation"]["checks"]["runs"][0]
    original = check["evidence"]
    request = deepcopy(check["evidence_request"])
    request["evidence_key"] += ":new"
    request["observation_ref"] += ":new"
    replacement = service.candidates.record_check(request, log=b"replacement check passed\n")
    return original["id"], replacement["id"]


@pytest.mark.parametrize("boundary", ["prepare", "claim"])
def test_payload_preparation_precedes_the_final_expiry_check(
    tmp_path, binding_case, monkeypatch, boundary
):
    """A UUID/JSON payload step cannot cross an already-final deadline."""
    service, run_id, reviewer_id, _ = _service(tmp_path, binding_case)
    if boundary == "claim":
        service.prepare(run_id, reviewer_id, principal="owner", command_key="prepare")
        service.freeze_launch(run_id, reviewer_id, principal="owner")
        from karajan.execution._platform import process_identity

        identity = process_identity(os.getpid())
        assert identity is not None

        @contextmanager
        def current_runner(*args, **kwargs):
            yield identity

        service.host.wait_for_runner_registration = lambda *args, **kwargs: identity
        service.host.current_runner_guard = current_runner

    admission_id = service.admissions.get(
        run_id, reviewer_id, principal="owner"
    )["capacity_receipt"]["admission_id"]
    reservation = next(
        row
        for row in service.admissions.routing.capacity.snapshot()["reservations"]
        if row["id"] == admission_id
    )
    now = [1000.0]
    service.admissions.routing.capacity.clock = lambda: now[0]
    armed = [False]

    if boundary == "prepare":
        original_db = service._db

        @contextmanager
        def writer(*, write=True):
            with original_db(write=write) as db:
                if write:
                    armed[0] = True
                yield db

        service._db = writer
        original_uuid4 = reviewer_execution_intent.uuid.uuid4

        def expire_while_building_payload():
            value = original_uuid4()
            if armed[0]:
                now[0] = reservation["expires_at"] + 0.001
            return value

        monkeypatch.setattr(reviewer_execution_intent.uuid, "uuid4", expire_while_building_payload)
        def action():
            return service.prepare(
                run_id, reviewer_id, principal="owner", command_key="payload-expiry"
            )
    else:
        original_dumps = reviewer_execution_intent.json.dumps

        def expire_while_serializing(*args, **kwargs):
            result = original_dumps(*args, **kwargs)
            import inspect

            if any(
                frame.filename.endswith("reviewer_execution_intent.py")
                and frame.function in {"_save", "_serialized_save"}
                for frame in inspect.stack()
            ):
                now[0] = reservation["expires_at"] + 0.001
            return result

        monkeypatch.setattr(reviewer_execution_intent.json, "dumps", expire_while_serializing)
        def action():
            return service.claim_registered_observer(
                run_id, reviewer_id, principal="owner", timeout_seconds=0.01
            )

    with pytest.raises(RunError, match="REVIEWER_CAPACITY_REVALIDATION_FAILED"):
        action()
    current = service.read(run_id, reviewer_id, principal="owner")
    if boundary == "prepare":
        assert current is None
    else:
        assert current is not None and current["effect_claim"] is None


@pytest.mark.parametrize(
    ("boundary", "expired", "reason"),
    [
        ("new_intent", "run", "RUN_DURATION_LIMIT"),
        ("host", "run", "RUN_DURATION_LIMIT"),
        ("control", "run", "RUN_DURATION_LIMIT"),
        ("claim", "run", "RUN_DURATION_LIMIT"),
        ("new_intent", "qualification", "REVIEWER_QUALIFICATION_EXPIRED"),
        ("new_intent", "quota", "REVIEWER_CAPACITY_REVALIDATION_FAILED"),
        ("host", "quota", "REVIEWER_CAPACITY_REVALIDATION_FAILED"),
        ("control", "quota", "REVIEWER_CAPACITY_REVALIDATION_FAILED"),
        ("claim", "quota", "REVIEWER_CAPACITY_REVALIDATION_FAILED"),
    ],
)
def test_final_input_compilation_expiry_blocks_each_actual_effect(
    tmp_path, binding_case, monkeypatch, boundary, expired, reason
):
    """Final input materialization cannot outlive non-Capacity authority."""
    if expired == "qualification":
        original_facts = binding_case[1].original._facts

        def short_lived_facts(*args, **kwargs):
            observed = original_facts(*args, **kwargs)
            observed["facts"]["valid_until"] = 1001.0
            return observed

        monkeypatch.setattr(binding_case[1].original, "_facts", short_lived_facts)
    service, run_id, reviewer_id, _ = _service(
        tmp_path,
        binding_case,
        conservative_observation_age=5 if expired == "quota" else None,
    )
    capacity = service.admissions.routing.capacity
    now = [1000.0]
    capacity.clock = lambda: now[0]
    service.admissions.routing.planner.clock = lambda: now[0]
    if expired == "run":
        with sqlite3.connect(service.admissions.database) as db:
            row = db.execute(
                "SELECT data FROM run_execution_budgets WHERE run_id=?", (run_id,)
            ).fetchone()
            budget = json.loads(row[0])
            budget.update(started_at=1000.0, max_duration_seconds=1.0)
            db.execute(
                "UPDATE run_execution_budgets SET data=? WHERE run_id=?",
                (json.dumps(budget), run_id),
            )
    if boundary != "new_intent":
        service.prepare(run_id, reviewer_id, principal="owner", command_key="prepare")
    if boundary == "claim":
        service.freeze_launch(run_id, reviewer_id, principal="owner")
        from karajan.execution._platform import process_identity

        identity = process_identity(os.getpid())
        assert identity is not None

        @contextmanager
        def current_runner(*args, **kwargs):
            yield identity

        service.host.wait_for_runner_registration = lambda *args, **kwargs: identity
        service.host.current_runner_guard = current_runner

    admission_id = service.admissions.get(
        run_id, reviewer_id, principal="owner"
    )["capacity_receipt"]["admission_id"]
    reservation = next(
        row for row in capacity.snapshot()["reservations"] if row["id"] == admission_id
    )

    original_compile = reviewer_input.compile_reviewer_input_from_records
    compilation_count = 0

    def expire_after_real_materialization(*args, **kwargs):
        nonlocal compilation_count
        compiled = original_compile(*args, **kwargs)
        compilation_count += 1
        if compilation_count < (2 if boundary == "new_intent" else 1):
            return compiled
        if expired == "run":
            now[0] = 1001.0
        elif expired == "qualification":
            now[0] = 1001.0
        else:
            now[0] = 1006.0
        assert now[0] < reservation["expires_at"]
        return compiled

    monkeypatch.setattr(
        reviewer_input, "compile_reviewer_input_from_records", expire_after_real_materialization
    )
    if boundary == "new_intent":
        def action():
            return service.prepare(
                run_id, reviewer_id, principal="owner", command_key="compiled-expiry"
            )
    elif boundary in {"host", "control"}:
        def action():
            return service.freeze_launch(run_id, reviewer_id, principal="owner")
    else:
        def action():
            return service.claim_registered_observer(
                run_id, reviewer_id, principal="owner", timeout_seconds=0.01
            )

    with pytest.raises(RunError, match=reason):
        action()
    current = service.read(run_id, reviewer_id, principal="owner")
    if boundary == "new_intent":
        assert current is None
        return
    assert current is not None and current["effect_claim"] is None
    if boundary == "host":
        with pytest.raises(KeyError):
            service.host.inspect(current["planned_attempt_id"])
    if boundary == "control":
        with sqlite3.connect(service.host.database) as db:
            assert db.execute("SELECT COUNT(*) FROM controls").fetchone()[0] == 0


def test_host_nonce_expiry_prevents_host_control_and_claim_effects(
    tmp_path, binding_case, monkeypatch
):
    """The actual Host nonce must be prepared before its final authority check."""
    service, run_id, reviewer_id, _ = _service(tmp_path, binding_case)
    service.prepare(run_id, reviewer_id, principal="owner", command_key="prepare")
    admission_id = service.admissions.get(
        run_id, reviewer_id, principal="owner"
    )["capacity_receipt"]["admission_id"]
    reservation = next(
        row
        for row in service.admissions.routing.capacity.snapshot()["reservations"]
        if row["id"] == admission_id
    )
    now = [1000.0]
    service.admissions.routing.capacity.clock = lambda: now[0]
    original_uuid4 = runner_host_module.uuid.uuid4

    def expire_during_host_nonce():
        now[0] = reservation["expires_at"] + 0.001
        return original_uuid4()

    monkeypatch.setattr(
        runner_host_module, "uuid", SimpleNamespace(uuid4=expire_during_host_nonce)
    )
    with pytest.raises(RunError, match="REVIEWER_CAPACITY_REVALIDATION_FAILED"):
        service.freeze_launch(run_id, reviewer_id, principal="owner")
    current = service.read(run_id, reviewer_id, principal="owner")
    assert current is not None and current["effect_claim"] is None
    with pytest.raises(KeyError):
        service.host.inspect(current["planned_attempt_id"])
    with sqlite3.connect(service.host.database) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM executions WHERE start_key=?", (current["start_key"],)
        ).fetchone()[0] == 0
        assert db.execute(
            "SELECT COUNT(*) FROM controls WHERE attempt_id=?", (current["planned_attempt_id"],)
        ).fetchone()[0] == 0


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
    before = service.database.read_bytes()
    with pytest.raises(RunError, match="REVIEWER_EXECUTION_SOURCE_CHANGED"):
        service.prepare(run_id, reviewer_id, principal="owner", command_key="stale-new-intent")
    assert service.database.read_bytes() == before
    assert service.read(run_id, reviewer_id, principal="owner") is None


@pytest.mark.parametrize("boundary", ["new_intent", "host_prepare", "initialize_control"])
def test_current_source_change_after_actual_receiver_writer_wait_blocks_effect(
    tmp_path, binding_case, boundary
):
    """The final source read follows, rather than precedes, each writer wait."""
    service, run_id, reviewer_id, _ = _service(tmp_path, binding_case)
    changed = [False]
    replacement = ReviewerExecutionSource("3" * 64, "4" * 64)
    service.current_source = lambda: replacement if changed[0] else service.source
    host_before = service.host.database.read_bytes()
    errors: list[BaseException] = []
    reached, release = threading.Event(), threading.Event()
    holder: sqlite3.Connection | None = None
    holder_thread: threading.Thread | None = None

    if boundary == "new_intent":
        database = service.database
        original_db = service._db

        @contextmanager
        def writer(*, write=True):
            if write:
                reached.set()
            with original_db(write=write) as db:
                yield db

        service._db = writer

        def action():
            return service.prepare(
                run_id, reviewer_id, principal="owner", command_key="writer-wait"
            )

    else:
        intent = service.prepare(run_id, reviewer_id, principal="owner", command_key="prepare")
        database = service.host.database
        if boundary == "host_prepare":
            original_prepare = service.host.prepare

            def prepare(*args, **kwargs):
                reached.set()
                return original_prepare(*args, **kwargs)

            service.host.prepare = prepare
        else:
            launch = service.launch_compiler(intent)
            service.host.prepare(host_manifest(intent), intent["start_key"], launch.process_spec)
            host_before = service.host.database.read_bytes()
            original_control = service.host.initialize_control_once
            holder_ready = threading.Event()

            def hold_control_writer():
                nonlocal holder
                holder = sqlite3.connect(database, isolation_level=None, timeout=5)
                holder.execute("BEGIN IMMEDIATE")
                holder_ready.set()
                assert release.wait(5)
                holder.commit()
                holder.close()

            def initialize_control(*args, **kwargs):
                nonlocal holder_thread
                holder_thread = threading.Thread(target=hold_control_writer)
                holder_thread.start()
                assert holder_ready.wait(5)
                reached.set()
                return original_control(*args, **kwargs)

            service.host.initialize_control_once = initialize_control

        def action():
            return service.freeze_launch(run_id, reviewer_id, principal="owner")

    if boundary != "initialize_control":
        holder = sqlite3.connect(database, isolation_level=None, timeout=5)
        holder.execute("BEGIN IMMEDIATE")

    def invoke():
        try:
            action()
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=invoke)
    thread.start()
    assert reached.wait(5)
    changed[0] = True
    assert holder is not None
    if boundary != "initialize_control":
        holder.commit()
        holder.close()
    else:
        release.set()
    thread.join(10)
    if holder_thread is not None:
        holder_thread.join(10)
        assert not holder_thread.is_alive()
    assert not thread.is_alive()
    assert errors and isinstance(errors[0], RunError)
    assert "REVIEWER_EXECUTION_SOURCE_CHANGED" in str(errors[0])
    current = service.read(run_id, reviewer_id, principal="owner")
    if boundary == "new_intent":
        assert current is None
    else:
        assert current is not None and current["host_prepared_id"] is None
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


@pytest.mark.parametrize("workspace_case", ["large_candidate"], indirect=True)
def test_large_candidate_materialization_precedes_all_shared_producer_writers(
    tmp_path, binding_case, monkeypatch
):
    """A small Reviewer scope never holds shared writers over full CAS copies."""
    service, run_id, reviewer_id, _ = _service(tmp_path, binding_case)
    reviewer = service.admissions.get(run_id, reviewer_id, principal="owner")
    worker = service.admissions.get(
        run_id, reviewer["depends_on_operation_id"], principal="owner"
    )
    candidate = service.candidates.get(
        worker["validation"]["checks"]["runs"][0]["evidence_request"]["candidate_id"]
    )
    task = next(
        item
        for plan in service.admissions.routing.planner.get(run_id, principal="owner")["plans"]
        for item in plan["plan"]["tasks"]
        if item["id"] == "review"
    )
    assert task["paths"] == ["src/report.py"]
    assert len(candidate["manifest"]) >= 28

    writer_depths = {
        "admission": 0,
        "run": 0,
        "project": 0,
        "capacity": 0,
        "candidate": 0,
    }
    materialization_depths = []
    original_materialize = reviewer_input._materialize_content

    @contextmanager
    def tracked(original, name, *args, **kwargs):
        with original(*args, **kwargs) as value:
            writer_depths[name] += 1
            try:
                yield value
            finally:
                writer_depths[name] -= 1

    def track_transaction(owner, name):
        original = owner._transaction

        @contextmanager
        def transaction(*args, **kwargs):
            with tracked(original, name, *args, **kwargs) as value:
                yield value

        monkeypatch.setattr(owner, "_transaction", transaction)

    track_transaction(service.admissions, "admission")
    track_transaction(service.admissions.routing.planner, "run")
    track_transaction(service.admissions.routing.planner.projects, "project")
    track_transaction(service.admissions.routing.capacity, "capacity")
    original_publication_guard = service.candidates.check_publication_guard

    @contextmanager
    def publication_guard():
        with tracked(original_publication_guard, "candidate"):
            yield

    def materialize(*args, **kwargs):
        materialization_depths.append(dict(writer_depths))
        assert not any(writer_depths.values()), (
            "full Candidate snapshots/diff ran inside a shared producer writer"
        )
        return original_materialize(*args, **kwargs)

    monkeypatch.setattr(service.candidates, "check_publication_guard", publication_guard)
    monkeypatch.setattr(reviewer_input, "_materialize_content", materialize)
    service.prepare(run_id, reviewer_id, principal="owner", command_key="prepare")
    assert materialization_depths == [{name: 0 for name in writer_depths}]


@pytest.mark.parametrize(
    ("column", "replacement", "request_ids"),
    [
        ("run_id", "tampered-run", lambda run_id, reviewer_id: ("tampered-run", reviewer_id)),
        (
            "reviewer_operation_id",
            "tampered-reviewer",
            lambda run_id, reviewer_id: (run_id, "tampered-reviewer"),
        ),
        ("execution_id", "tampered-execution", lambda run_id, reviewer_id: (run_id, reviewer_id)),
        ("principal", "tampered-owner", lambda run_id, reviewer_id: (run_id, reviewer_id)),
        ("command_key", "tampered-command", lambda run_id, reviewer_id: (run_id, reviewer_id)),
        ("state", "tampered-state", lambda run_id, reviewer_id: (run_id, reviewer_id)),
    ],
)
def test_persisted_identity_column_tampering_is_rejected_at_every_reader_and_effect(
    tmp_path, binding_case, column, replacement, request_ids
):
    """A digest-valid JSON body cannot outlive its SQLite identity columns."""
    service, run_id, reviewer_id, _ = _service(tmp_path, binding_case)
    intent = service.prepare(run_id, reviewer_id, principal="owner", command_key="prepare")
    before_host = service.host.database.read_bytes()
    with sqlite3.connect(service.database) as db:
        stored = db.execute(
            "SELECT intent FROM reviewer_executions WHERE execution_id=?", (intent["execution_id"],)
        ).fetchone()[0]
        db.execute(
            f"UPDATE reviewer_executions SET {column}=? WHERE execution_id=?",
            (replacement, intent["execution_id"]),
        )
        db.commit()
    requested_run, requested_reviewer = request_ids(run_id, reviewer_id)
    history = reviewer_execution_intent.ReviewerExecutionHistory(service.database)
    for action in (
        lambda: service.read(requested_run, requested_reviewer, principal="owner"),
        lambda: service.prepare(
            requested_run, requested_reviewer, principal="owner", command_key="prepare"
        ),
        lambda: history.read(requested_run, requested_reviewer, principal="owner"),
        lambda: service.freeze_launch(requested_run, requested_reviewer, principal="owner"),
        lambda: service.cancel(requested_run, requested_reviewer, principal="owner"),
    ):
        with pytest.raises(RunError, match="REVIEWER_EXECUTION_BINDING_INVALID"):
            action()
    with sqlite3.connect(service.database) as db:
        assert db.execute(
            "SELECT intent FROM reviewer_executions WHERE intent=?", (stored,)
        ).fetchone()[0] == stored
    assert service.host.database.read_bytes() == before_host


def test_original_embedded_ids_reject_new_command_after_run_index_tamper(tmp_path, binding_case):
    """An altered lookup column cannot free an embedded execution identity."""
    service, run_id, reviewer_id, _ = _service(tmp_path, binding_case)
    intent = service.prepare(run_id, reviewer_id, principal="owner", command_key="prepare")
    before_host = service.host.database.read_bytes()
    with sqlite3.connect(service.database) as db:
        db.execute(
            "UPDATE reviewer_executions SET run_id=? WHERE execution_id=?",
            ("tampered-run", intent["execution_id"]),
        )
        db.commit()
    history = reviewer_execution_intent.ReviewerExecutionHistory(service.database)
    for action in (
        lambda: service.read(run_id, reviewer_id, principal="owner"),
        lambda: history.read(run_id, reviewer_id, principal="owner"),
        lambda: service.prepare(
            run_id, reviewer_id, principal="owner", command_key="new-command-key"
        ),
        lambda: service.freeze_launch(run_id, reviewer_id, principal="owner"),
    ):
        with pytest.raises(RunError, match="REVIEWER_EXECUTION_BINDING_INVALID"):
            action()
    with sqlite3.connect(service.database) as db:
        assert db.execute("SELECT COUNT(*) FROM reviewer_executions").fetchone()[0] == 1
    assert service.host.database.read_bytes() == before_host


def test_missing_check_log_after_host_writer_wait_blocks_prepare(tmp_path, binding_case):
    """A Check CAS object must still exist when Host receives its write turn."""
    service, run_id, reviewer_id, _ = _service(tmp_path, binding_case)
    intent = service.prepare(run_id, reviewer_id, principal="owner", command_key="prepare")
    worker = service.admissions.get(
        run_id, intent["worker_operation_id"], principal="owner"
    )
    log = Path(worker["validation"]["checks"]["runs"][0]["evidence"]["log"]["path"])
    assert log.is_file()
    reached, outcome = threading.Event(), []
    original_prepare = service.host.prepare

    def prepare(*args, **kwargs):
        reached.set()
        return original_prepare(*args, **kwargs)

    service.host.prepare = prepare
    holder = sqlite3.connect(service.host.database, isolation_level=None, timeout=5)
    holder.execute("BEGIN IMMEDIATE")

    def freeze():
        try:
            service.freeze_launch(run_id, reviewer_id, principal="owner")
        except BaseException as error:
            outcome.append(error)

    thread = threading.Thread(target=freeze)
    thread.start()
    assert reached.wait(5)
    log.unlink()
    holder.commit()
    holder.close()
    thread.join(10)
    assert not thread.is_alive()
    assert outcome and isinstance(outcome[0], RunError)
    with pytest.raises(KeyError):
        service.host.inspect(intent["planned_attempt_id"])


@pytest.mark.parametrize("boundary", ["prepare", "claim"])
def test_missing_check_log_after_receiving_writer_wait_blocks_prepare_and_claim(
    tmp_path, binding_case, boundary
):
    """The same receiver-writer recheck applies to ledger prepare and claim."""
    service, run_id, reviewer_id, _ = _service(tmp_path, binding_case)
    reviewer = service.admissions.get(run_id, reviewer_id, principal="owner")
    worker = service.admissions.get(
        run_id, reviewer["depends_on_operation_id"], principal="owner"
    )
    log = Path(worker["validation"]["checks"]["runs"][0]["evidence"]["log"]["path"])
    assert log.is_file()
    reached, outcome = threading.Event(), []

    if boundary == "prepare":
        original_db = service._db

        @contextmanager
        def writer(*, write=True):
            if write:
                reached.set()
            with original_db(write=write) as db:
                yield db

        service._db = writer

        def action():
            return service.prepare(run_id, reviewer_id, principal="owner", command_key="prepare")

        database = service.database
    else:
        service.prepare(run_id, reviewer_id, principal="owner", command_key="prepare")
        service.freeze_launch(run_id, reviewer_id, principal="owner")
        from karajan.execution._platform import process_identity

        identity = process_identity(os.getpid())
        assert identity is not None

        @contextmanager
        def current_runner(*args, **kwargs):
            reached.set()
            yield identity

        def wait_for_runner(*args, **kwargs):
            return identity

        service.host.wait_for_runner_registration = wait_for_runner
        service.host.current_runner_guard = current_runner

        def action():
            return service.claim_registered_observer(
                run_id, reviewer_id, principal="owner", timeout_seconds=0.01
            )

        database = service.database

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
    log.unlink()
    holder.commit()
    holder.close()
    thread.join(10)
    assert not thread.is_alive()
    assert outcome and isinstance(outcome[0], RunError)
    if boundary == "prepare":
        assert service.read(run_id, reviewer_id, principal="owner") is None
    else:
        assert service.read(run_id, reviewer_id, principal="owner")["effect_claim"] is None


def test_later_passed_check_before_receiver_guard_blocks_full_input_identity(
    tmp_path, binding_case
):
    """A Check committed before the receiver guard cannot replace its input."""
    service, run_id, reviewer_id, _ = _service(tmp_path, binding_case)
    reviewer = service.admissions.get(run_id, reviewer_id, principal="owner")
    worker = service.admissions.get(
        run_id, reviewer["depends_on_operation_id"], principal="owner"
    )
    initial_guard_done, allow_final_guard, outcome = threading.Event(), threading.Event(), []
    original_guard = service.admissions.reviewer_reserved_effect_guard
    guard_calls = 0

    @contextmanager
    def guard(*args, **kwargs):
        nonlocal guard_calls
        guard_calls += 1
        with original_guard(*args, **kwargs) as held:
            yield held
        if guard_calls == 1:
            initial_guard_done.set()
            assert allow_final_guard.wait(5)

    service.admissions.reviewer_reserved_effect_guard = guard

    def invoke():
        try:
            service.prepare(run_id, reviewer_id, principal="owner", command_key="prepare")
        except BaseException as error:
            outcome.append(error)

    thread = threading.Thread(target=invoke)
    thread.start()
    assert initial_guard_done.wait(5)
    _record_new_passed_check(service, worker)
    allow_final_guard.set()
    thread.join(10)
    assert not thread.is_alive()
    assert outcome and isinstance(outcome[0], RunError)
    assert service.read(run_id, reviewer_id, principal="owner") is None


def test_baseline_artifact_change_after_preparation_blocks_final_input(tmp_path, binding_case):
    """Cached snapshot bytes never hide a later physical baseline CAS change."""
    service, run_id, reviewer_id, _ = _service(tmp_path, binding_case)
    reviewer = service.admissions.get(run_id, reviewer_id, principal="owner")
    worker = service.admissions.get(
        run_id, reviewer["depends_on_operation_id"], principal="owner"
    )
    candidate = service.candidates.get(
        worker["validation"]["checks"]["runs"][0]["evidence_request"]["candidate_id"]
    )
    baseline = service.candidates.get_baseline(candidate["request"]["baseline_id"])
    candidate_artifacts = {row["artifact"]["sha256"] for row in candidate["manifest"]}
    artifact = Path(
        next(
            row["artifact"]["path"]
            for row in baseline["manifest"]
            if row["artifact"]["sha256"] not in candidate_artifacts
        )
    )
    assert artifact.is_file()
    prepared, release, errors = threading.Event(), threading.Event(), []
    original_guard = service.admissions.reviewer_reserved_effect_guard
    guard_calls = 0

    @contextmanager
    def guard(*args, **kwargs):
        nonlocal guard_calls
        guard_calls += 1
        with original_guard(*args, **kwargs) as held:
            yield held
        if guard_calls == 1:
            prepared.set()
            assert release.wait(5)

    service.admissions.reviewer_reserved_effect_guard = guard

    def prepare():
        try:
            service.prepare(run_id, reviewer_id, principal="owner", command_key="prepare")
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=prepare)
    thread.start()
    assert prepared.wait(5)
    artifact.write_bytes(b"baseline artifact replaced after compiler preparation")
    release.set()
    thread.join(10)
    assert not thread.is_alive()
    assert errors and isinstance(errors[0], RunError)
    assert "ARTIFACT_UNAVAILABLE" in str(errors[0])
    assert service.read(run_id, reviewer_id, principal="owner") is None


def test_new_check_publication_waits_for_complete_input_comparison_and_prepare_commit(
    tmp_path, binding_case, monkeypatch
):
    """The Candidate producer lock covers later material I/O and the ledger effect."""
    service, run_id, reviewer_id, _ = _service(tmp_path, binding_case)
    reviewer = service.admissions.get(run_id, reviewer_id, principal="owner")
    worker = service.admissions.get(
        run_id, reviewer["depends_on_operation_id"], principal="owner"
    )
    full_input_compiled, later_material, release_scalar = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    publish_started, publish_done, prepare_done, scalar_reached = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    compiled_calls = 0
    original_compile = reviewer_input.compile_reviewer_input_from_records
    original_facts = binding_case[1]._facts

    def compiled(*args, **kwargs):
        nonlocal compiled_calls
        value = original_compile(*args, **kwargs)
        compiled_calls += 1
        if compiled_calls == 2:
            assert later_material.is_set()
            full_input_compiled.set()
        return value

    def material(*args, **kwargs):
        if not later_material.is_set():
            later_material.set()
        return original_facts(*args, **kwargs)

    original_scalar = CapacityEffectCapability.assert_current

    def pause_after_full_input(self):
        value = original_scalar(self)
        if full_input_compiled.is_set():
            scalar_reached.set()
            assert release_scalar.wait(5)
        return value

    monkeypatch.setattr(reviewer_input, "compile_reviewer_input_from_records", compiled)
    monkeypatch.setattr(CapacityEffectCapability, "assert_current", pause_after_full_input)
    binding_case[1]._facts = material
    errors: list[BaseException] = []

    def prepare():
        try:
            service.prepare(run_id, reviewer_id, principal="owner", command_key="prepare")
        except BaseException as error:
            errors.append(error)
        finally:
            prepare_done.set()

    def publish():
        assert full_input_compiled.wait(5)
        publish_started.set()
        _record_new_passed_check(service, worker)
        publish_done.set()

    prepare_thread = threading.Thread(target=prepare)
    prepare_thread.start()
    assert scalar_reached.wait(5)
    publisher = threading.Thread(target=publish)
    publisher.start()
    assert publish_started.wait(5)
    try:
        assert not publish_done.wait(0.2)
    finally:
        release_scalar.set()
    prepare_thread.join(10)
    publisher.join(10)
    assert not prepare_thread.is_alive() and not publisher.is_alive()
    assert not errors
    assert prepare_done.is_set() and publish_done.is_set()


@pytest.mark.parametrize("boundary", ["new_intent", "host", "control", "claim"])
def test_reservation_only_expiry_after_real_sqlite_writer_wait_blocks_the_actual_effect(
    tmp_path, binding_case, boundary
):
    service, run_id, reviewer_id, _ = _service(tmp_path, binding_case)
    if boundary != "new_intent":
        service.prepare(run_id, reviewer_id, principal="owner", command_key="prepare")
    admission_id = service.admissions.get(
        run_id, reviewer_id, principal="owner"
    )["capacity_receipt"]["admission_id"]
    reservation = next(
        row
        for row in service.admissions.routing.capacity.snapshot()["reservations"]
        if row["id"] == admission_id
    )
    reached = threading.Event()
    release = threading.Event()
    outcome: list[BaseException] = []

    if boundary == "new_intent":
        original_db = service._db

        @contextmanager
        def signal_writer(*, write=True):
            if write:
                reached.set()
            with original_db(write=write) as db:
                yield db

        service._db = signal_writer

        def action():
            return service.prepare(
                run_id, reviewer_id, principal="owner", command_key="reservation-new-intent"
            )

        database = service.database
    elif boundary == "host":
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
    # Cross only the held reservation deadline. Qualification, estimate, quota
    # observation/reset and Run windows remain valid, so another temporal fence
    # cannot mask a missing retained reservation authority.
    service.admissions.routing.capacity.clock = lambda: reservation["expires_at"] + 0.001
    assert holder is not None
    # The real Host control transaction is now blocked behind this writer.
    time.sleep(0.05)
    holder.commit()
    holder.close()
    thread.join(10)
    assert not thread.is_alive()
    assert outcome and isinstance(outcome[0], RunError)
    assert "REVIEWER_CAPACITY_REVALIDATION_FAILED" in str(outcome[0])
    current = service.read(run_id, reviewer_id, principal="owner")
    if boundary == "new_intent":
        assert current is None
        return
    assert current is not None and current["effect_claim"] is None
    if boundary == "host":
        with pytest.raises(KeyError):
            service.host.inspect(current["planned_attempt_id"])
    if boundary == "control":
        with sqlite3.connect(service.host.database) as db:
            assert db.execute("SELECT COUNT(*) FROM controls").fetchone()[0] == 0


def test_reviewer_material_recheck_prepares_before_capacity_scalar_callback(
    tmp_path, binding_case, monkeypatch
):
    """Credential material I/O completes before Capacity's deferred O(1) check."""
    service, run_id, reviewer_id, _ = _service(tmp_path, binding_case)
    service.prepare(run_id, reviewer_id, principal="owner", command_key="prepare")
    qualification = binding_case[1]
    source_reads = 0
    original_facts = qualification._facts

    def count_source_reads(*args, **kwargs):
        nonlocal source_reads
        source_reads += 1
        return original_facts(*args, **kwargs)

    qualification._facts = count_source_reads
    capacity = service.admissions.routing.capacity
    original_guard = capacity.pre_effect_guard
    preparation_reads: list[tuple[int, int]] = []

    @contextmanager
    def inspect_final_callback(*args, **kwargs):
        callback = kwargs["before_effect_yield"]

        def prepare_scalar_check():
            before = source_reads
            result = callback()
            preparation_reads.append((before, source_reads))
            return result

        with original_guard(
            *args, **{**kwargs, "before_effect_yield": prepare_scalar_check}
        ) as held:
            yield held

    monkeypatch.setattr(capacity, "pre_effect_guard", inspect_final_callback)
    service.freeze_launch(run_id, reviewer_id, principal="owner")
    assert preparation_reads and preparation_reads[0][1] > preparation_reads[0][0]


@pytest.mark.parametrize("boundary", ["new_intent", "host", "control", "claim"])
def test_actual_credential_material_change_after_real_writer_wait_blocks_each_effect(
    tmp_path, binding_case, boundary
):
    """The held qualification reader, not a frozen generation double, seals bytes."""
    service, run_id, reviewer_id, _ = _service(tmp_path, binding_case)
    if boundary != "new_intent":
        service.prepare(run_id, reviewer_id, principal="owner", command_key="prepare")
    reached = threading.Event()
    release = threading.Event()
    outcome: list[BaseException] = []

    if boundary == "new_intent":
        original_db = service._db

        @contextmanager
        def signal_writer(*, write=True):
            if write:
                reached.set()
            with original_db(write=write) as db:
                yield db

        service._db = signal_writer

        def action():
            return service.prepare(
                run_id, reviewer_id, principal="owner", command_key="credential-new-intent"
            )

        database = service.database
    elif boundary == "host":
        original = service.host.prepare

        def call(*args, **kwargs):
            reached.set()
            return original(*args, **kwargs)

        service.host.prepare = call

        def action():
            return service.freeze_launch(run_id, reviewer_id, principal="owner")

        database = service.host.database
    elif boundary == "control":
        original = service.host.initialize_control_once

        def call(*args, **kwargs):
            reached.set()
            assert release.wait(5)
            return original(*args, **kwargs)

        service.host.initialize_control_once = call

        def action():
            return service.freeze_launch(run_id, reviewer_id, principal="owner")

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
    credentials = binding_case[1].original.credentials
    credential_path = next(iter(credentials._sources.values())).path
    # This is the configured reader's actual temporary file. Keep its source
    # identity/path and every SQLite generation unchanged; replace bytes only.
    credential_path.write_text("replacement-material-not-a-provider-key", encoding="ascii")
    assert holder is not None
    time.sleep(0.05)
    holder.commit()
    holder.close()
    thread.join(10)
    assert not thread.is_alive()
    assert outcome and isinstance(outcome[0], RunError)
    assert "REVIEWER_QUALIFICATION_SOURCE_CHANGED" in str(outcome[0])

    current = service.read(run_id, reviewer_id, principal="owner")
    if boundary == "new_intent":
        assert current is None
        return
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


def test_successful_inspect_host_is_read_only_across_store_reopen(tmp_path, binding_case):
    """A normal fixed Host preparation never records a read observation."""
    service, run_id, reviewer_id, _ = _service(tmp_path, binding_case)
    prepared = service.prepare(run_id, reviewer_id, principal="owner", command_key="prepare")
    service.freeze_launch(run_id, reviewer_id, principal="owner")
    with sqlite3.connect(service.database) as db:
        before = db.execute(
            "SELECT intent FROM reviewer_executions WHERE execution_id=?",
            (prepared["execution_id"],),
        ).fetchone()[0]
    mtime_before = service.database.stat().st_mtime_ns
    writes = []
    original_db = service._db

    @contextmanager
    def observe_db(*, write=True):
        writes.append(write)
        with original_db(write=write) as db:
            yield db

    service._db = observe_db
    observed = service.inspect_host(run_id, reviewer_id, principal="owner")
    assert observed["host_observation"]["prepared_id"] == prepared["start_key"]
    assert writes == [False]
    assert service.database.stat().st_mtime_ns == mtime_before
    with sqlite3.connect(service.database) as db:
        assert db.execute(
            "SELECT intent FROM reviewer_executions WHERE execution_id=?",
            (prepared["execution_id"],),
        ).fetchone()[0] == before
    reopened = ReviewerExecutionIntents(
        service.database,
        service.admissions,
        service.candidates,
        source=service.source,
        host=RunnerHost(service.host.directory, existing_only=True),
        launch_compiler=service.launch_compiler,
        existing_only=True,
    )
    again = reopened.inspect_host(run_id, reviewer_id, principal="owner")
    assert again["host_observation"] == observed["host_observation"]
    with sqlite3.connect(reopened.database) as db:
        assert db.execute(
            "SELECT intent FROM reviewer_executions WHERE execution_id=?",
            (prepared["execution_id"],),
        ).fetchone()[0] == before


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


def _write_direct_child_port(
    directory: Path,
    service: ReviewerExecutionIntents,
    source: ReviewerExecutionSource,
    qualification_facts: dict[object, object],
    *,
    changed_source: ReviewerExecutionSource | None = None,
) -> None:
    (directory / "reviewer-execution-test-port.json").write_text(
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
                "changed_source": (
                    {
                        "runner_source_sha256": changed_source.runner_source_sha256,
                        "native_source_sha256": changed_source.native_source_sha256,
                    }
                    if changed_source is not None
                    else None
                ),
                "qualification_facts": qualification_facts,
            }
        )
    )


@pytest.mark.skipif(
    sys.platform == "win32", reason="Host direct-child identity is Linux P evidence"
)
@pytest.mark.parametrize("mode", ["reply", "lost-reply", "concurrent", "lifecycle"])
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
    _write_direct_child_port(tmp_path, service, source, observed_facts[-1])
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
    if mode == "lifecycle":
        assert result["replay_allowed"] is False
        assert result["cancel_requested"] is True
        assert result["cancel_error"] == "REVIEWER_EXECUTION_CANCELLED"
    effects_path = tmp_path / "reviewer-execution-test-child-effects.json"
    while not effects_path.exists():
        assert time.monotonic() < deadline, "direct child did not persist forbidden-effect counters"
        time.sleep(0.02)
    effects = json.loads(effects_path.read_text())
    assert effects["claim_observed"] is True
    assert effects["counters"] == {
        "observer": 0,
        "host_start": 0,
        "native_start": 0,
        "http_send": 0,
        "parser_suite": 0,
        "parser_output": 0,
        "journal_grant": 0,
        "journal_call": 0,
        "evidence_write": 0,
        "quality_effect": 0,
    }
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
    if mode == "lifecycle":
        with pytest.raises(RunError, match="REVIEWER_EXECUTION_CANCELLED"):
            reopened.claim_registered_observer(run_id, reviewer_id, principal="owner")
    else:
        assert (
            reopened.claim_registered_observer(
                run_id, reviewer_id, principal="owner"
            )["claim_allowed"]
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


@pytest.mark.skipif(
    sys.platform == "win32", reason="Host direct-child identity is Linux P evidence"
)
@pytest.mark.parametrize("field", ["profile_id", "permissions", "timeout_seconds"])
def test_registered_direct_child_rejects_tampered_original_host_binding(
    tmp_path, binding_case, field
):
    """A registered child cannot claim a Host row changed after preparation."""
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
            (sys.executable, "-I", str(child), run_id, reviewer_id, "owner", "paused"),
            tmp_path,
            20,
        ),
        "3" * 64,
    )
    intent = service.prepare(run_id, reviewer_id, principal="owner", command_key="prepare")
    prepared = service.freeze_launch(run_id, reviewer_id, principal="owner")
    _write_direct_child_port(tmp_path, service, source, observed_facts[-1])
    activation = Activation(
        "reviewer-tamper-activation",
        intent["planned_attempt_id"],
        intent["fence"],
        intent["authorization_ref"],
        intent["budget_ref"],
        time.time() + 30,
    )
    service.host.start(prepared["start_key"], activation)
    ready = tmp_path / "reviewer-execution-test-child-ready"
    deadline = time.monotonic() + 30
    while not ready.exists():
        assert time.monotonic() < deadline, "direct child did not reach claim boundary"
        time.sleep(0.02)
    while True:
        with sqlite3.connect(service.host.database) as db:
            registered = db.execute(
                "SELECT runner_pid FROM executions WHERE attempt_id=?",
                (intent["planned_attempt_id"],),
            ).fetchone()
        if registered is not None and registered[0] is not None:
            break
        assert time.monotonic() < deadline, "direct child did not register with Host"
        time.sleep(0.02)
    with sqlite3.connect(service.host.database) as db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT manifest,spec FROM executions WHERE start_key=?", (prepared["start_key"],)
        ).fetchone()
        assert row is not None
        if field == "timeout_seconds":
            spec = json.loads(row["spec"])
            spec["timeout_seconds"] = 19
            db.execute(
                "UPDATE executions SET spec=? WHERE start_key=?",
                (json.dumps(spec, sort_keys=True, separators=(",", ":")), prepared["start_key"]),
            )
        else:
            manifest = json.loads(row["manifest"])
            if field == "profile_id":
                manifest["profile_id"] = "tampered-profile"
            else:
                manifest["permissions"] = ["read", "tampered-permission"]
            db.execute(
                "UPDATE executions SET manifest=? WHERE start_key=?",
                (
                    json.dumps(manifest, sort_keys=True, separators=(",", ":")),
                    prepared["start_key"],
                ),
            )
        db.commit()
    (tmp_path / "reviewer-execution-test-child-release").write_text("release")
    result_path = tmp_path / "reviewer-execution-test-child-result.json"
    while not result_path.exists():
        assert time.monotonic() < deadline, "tampered direct child did not reply"
        time.sleep(0.02)
    result = json.loads(result_path.read_text())
    assert "CAPTURE_PREPARED_BINDING_MISMATCH" in result.get("error", "")
    effects = json.loads((tmp_path / "reviewer-execution-test-child-effects.json").read_text())
    assert effects["claim_observed"] is False
    assert all(value == 0 for value in effects["counters"].values())
    assert service.read(run_id, reviewer_id, principal="owner")["effect_claim"] is None


@pytest.mark.skipif(
    sys.platform == "win32", reason="Host direct-child identity is Linux P evidence"
)
def test_registered_direct_child_rechecks_source_after_final_writer_wait(tmp_path, binding_case):
    """The registered child rereads source only after its final ledger wait."""
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
    replacement = ReviewerExecutionSource("3" * 64, "4" * 64)
    child = Path(__file__).with_name("reviewer_execution_test_child.py").resolve()
    service.launch_compiler = lambda _: ReviewerLaunchSpec(
        ProcessSpec(
            (sys.executable, "-I", str(child), run_id, reviewer_id, "owner", "source-wait"),
            tmp_path,
            20,
        ),
        "3" * 64,
    )
    intent = service.prepare(run_id, reviewer_id, principal="owner", command_key="prepare")
    prepared = service.freeze_launch(run_id, reviewer_id, principal="owner")
    _write_direct_child_port(
        tmp_path, service, source, observed_facts[-1], changed_source=replacement
    )
    activation = Activation(
        "reviewer-source-wait-activation",
        intent["planned_attempt_id"],
        intent["fence"],
        intent["authorization_ref"],
        intent["budget_ref"],
        time.time() + 30,
    )
    service.host.start(prepared["start_key"], activation)
    service_ready = tmp_path / "reviewer-execution-test-child-service-ready"
    deadline = time.monotonic() + 30
    while not service_ready.exists():
        assert time.monotonic() < deadline, "direct child did not compose existing stores"
        time.sleep(0.02)
    holder = sqlite3.connect(service.database, isolation_level=None, timeout=5)
    holder.execute("BEGIN IMMEDIATE")
    (tmp_path / "reviewer-execution-test-child-final-writer-begin").write_text("begin")
    ready = tmp_path / "reviewer-execution-test-child-final-writer-ready"
    attempt = tmp_path / "reviewer-execution-test-child-final-writer-attempt"
    release = tmp_path / "reviewer-execution-test-child-final-writer-release"
    while not ready.exists():
        assert time.monotonic() < deadline, "direct child did not reach final ledger boundary"
        time.sleep(0.02)
    release.write_text("release")
    while not attempt.exists():
        assert time.monotonic() < deadline, "direct child did not attempt final ledger writer"
        time.sleep(0.02)
    (tmp_path / "reviewer-execution-test-source-changed").write_text("changed")
    holder.commit()
    holder.close()
    result_path = tmp_path / "reviewer-execution-test-child-result.json"
    while not result_path.exists():
        assert time.monotonic() < deadline, "source-changed child did not reply"
        time.sleep(0.02)
    result = json.loads(result_path.read_text())
    assert "REVIEWER_EXECUTION_SOURCE_CHANGED" in result.get("error", "")
    effects = json.loads((tmp_path / "reviewer-execution-test-child-effects.json").read_text())
    assert effects["claim_observed"] is False
    assert all(value == 0 for value in effects["counters"].values())
    assert service.read(run_id, reviewer_id, principal="owner")["effect_claim"] is None
