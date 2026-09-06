"""Recovery reconstructs the original ledgers and never provisions substitutes."""

import sqlite3
from functools import partial
from pathlib import Path

import pytest
from karajan.adapters.opencode.go_journal import GoCallJournal
from karajan.capacity import CapacityStore
from karajan.execution import RunnerHost
from karajan.orchestration.admission import ApprovedTaskAdmission
from karajan.orchestration.go_execution_intent import GoExecutionIntents, GoExecutionSource
from karajan.orchestration.routing import ApprovedRunRouting
from karajan.projects import ProjectRegistry
from karajan.projects.qualification import ProfileQualificationStore
from karajan.runs import RunPlanner
from karajan.storage import ExistingStoreError
from test_go_execution_intent import case, prepared, projected, ready, reservation

__all__ = ["case", "prepared", "projected", "ready", "reservation"]


def test_reopen_original_prepared_operation_without_initializing_or_mutating(prepared):
    original, args = prepared
    admission = original.admissions
    route = admission.routing
    paths = [
        admission.database,
        route.planner.database,
        route.planner.projects.database,
        route.capacity.path,
        original.host.database,
    ]
    before = {path: path.read_bytes() for path in paths}
    projects = ProjectRegistry(
        route.planner.projects.database, route.planner.projects.allowed_roots, existing_only=True
    )
    planner = RunPlanner(route.planner.database, projects, existing_only=True)
    capacity = CapacityStore(route.capacity.path, existing_only=True)
    routing = ApprovedRunRouting(planner, ProfileQualificationStore(projects), capacity)
    admissions = ApprovedTaskAdmission(admission.database, routing, existing_only=True)
    reopened = GoExecutionIntents(
        admissions,
        source=GoExecutionSource("a" * 64, "b" * 64),
        host=RunnerHost(original.host.directory, existing_only=True),
    )
    assert reopened.read(*args, principal="owner") == original.read(*args, principal="owner")
    assert {path: path.read_bytes() for path in paths} == before


@pytest.mark.parametrize("kind", ["project", "capacity", "journal", "host"])
@pytest.mark.parametrize("state", ["missing", "empty"])
def test_existing_constructor_does_not_create_or_migrate(kind, state, tmp_path):
    path = tmp_path / "state.sqlite"
    if kind == "host":
        path = tmp_path / "host" / "runnerhost.sqlite3"
    if state == "empty":
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"")
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    open_store = {
        "project": lambda: ProjectRegistry(path, [tmp_path], existing_only=True),
        "capacity": lambda: CapacityStore(path, existing_only=True),
        "journal": lambda: GoCallJournal(path, existing_only=True),
        "host": lambda: RunnerHost(path.parent, existing_only=True),
    }[kind]
    with pytest.raises(ExistingStoreError):
        open_store()
    assert sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*")) == before
    assert not path.exists() or path.read_bytes() == b""


@pytest.mark.parametrize("kind", ["project", "capacity", "journal", "host"])
def test_reconnect_after_missing_ledger_never_recreates_it(kind, tmp_path):
    path = tmp_path / "state.sqlite"
    if kind == "project":
        ProjectRegistry(path, [tmp_path])
        store = ProjectRegistry(path, [tmp_path], existing_only=True)
        read = partial(store.get, "absent")
    elif kind == "capacity":
        CapacityStore(path)
        store = CapacityStore(path, existing_only=True)
        read = store.snapshot
    elif kind == "journal":
        GoCallJournal(path)
        store = GoCallJournal(path, existing_only=True)
        read = partial(store.snapshot, "absent")
    else:
        host = RunnerHost(tmp_path / "host")
        path = host.database
        store = RunnerHost(host.directory, existing_only=True)
        read = partial(store.inspect, "absent")
    retained = path.with_suffix(".retained")
    path.rename(retained)
    with pytest.raises((ExistingStoreError, sqlite3.Error)):
        read()
    assert not path.exists()
    assert retained.exists()


def test_legacy_host_schema_is_rejected_without_migration(tmp_path: Path):
    host = RunnerHost(tmp_path / "host")
    with sqlite3.connect(host.database) as db:
        db.execute("ALTER TABLE executions DROP COLUMN runner_birth")
    before = host.database.read_bytes()
    with pytest.raises(ExistingStoreError, match="EXISTING_STORE_SCHEMA_UNSUPPORTED"):
        RunnerHost(host.directory, existing_only=True)
    assert host.database.read_bytes() == before
