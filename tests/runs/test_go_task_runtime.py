"""Fixed bootstrap reconstruction; only synthetic local state, never provider material."""

import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from dataclasses import replace

import pytest
from karajan.orchestration import _go_task_runner
from karajan.orchestration.go_task_execution import ApprovedGoTaskExecution
from karajan.orchestration.go_task_runtime import (
    BOOTSTRAP_NAME,
    GoTaskSettings,
    load_go_task_services_from_fixed_bootstrap,
    open_go_task_services,
    write_go_task_bootstrap,
)
from karajan.runs import RunError
from karajan.storage import ExistingStoreError
from test_go_execution_intent import case, prepared, projected, ready, reservation

__all__ = ["case", "prepared", "projected", "ready", "reservation"]


@pytest.fixture(autouse=True)
def private_fixture_directories(tmp_path):
    for name in ("candidates", "host"):
        (tmp_path / name).mkdir(mode=0o700)


@pytest.fixture
def deployment(prepared, projected, tmp_path):
    controller, args = prepared
    state = tmp_path / "deployment-state"
    journal = tmp_path / "deployment-journal"
    control = tmp_path / "deployment-control"
    task = tmp_path / "deployment-tasks"
    for directory in (state, journal, control, task):
        directory.mkdir(mode=0o700)
    admission = controller.admissions
    sources = {
        "projects.sqlite": admission.routing.planner.projects.database,
        "runs.sqlite": admission.routing.planner.database,
        "capacity.sqlite": admission.routing.capacity.path,
        "task-admissions.sqlite": admission.database,
    }
    # Explicit fixture deployment of existing logical ledgers, not product recovery.
    for name, source in sources.items():
        with (
            closing(sqlite3.connect(source)) as existing,
            closing(sqlite3.connect(state / name)) as target,
        ):
            existing.backup(target)
    journal_file = journal / "go.sqlite"
    with (
        closing(sqlite3.connect(projected["suite"].journal.path)) as existing,
        closing(sqlite3.connect(journal_file)) as target,
    ):
        existing.backup(target)
    candidates = tmp_path / "candidates"
    if sys.platform != "win32":
        candidates.chmod(0o700)
        controller.host.directory.chmod(0o700)
    settings = GoTaskSettings(
        control,
        state,
        candidates,
        controller.host.directory,
        journal_file,
        tmp_path / "qualification-not-opened",
        task,
        tmp_path / "python-not-opened",
        tmp_path / "runtime-not-opened",
        tmp_path / "tokenizer-not-opened",
        tmp_path / "private-material-not-opened",
        tuple(admission.routing.planner.projects.allowed_roots),
        (),
    )
    write_go_task_bootstrap(settings)
    return settings, controller, args


def test_history_factory_uses_original_source_and_never_opens_execution_material(
    deployment, monkeypatch
):
    settings, original, args = deployment

    def forbidden(*_args, **_kwargs):
        pytest.fail("History reconstruction opened execution dependencies")

    monkeypatch.setattr("karajan.orchestration.go_task_runtime.GoRequestAccounting", forbidden)
    monkeypatch.setattr("karajan.orchestration.go_task_runtime.CredentialSourceStore", forbidden)
    monkeypatch.setattr("karajan.orchestration.go_task_runtime.FixedGoSuite", forbidden)
    paths = list(settings.state_directory.glob("*.sqlite")) + [
        settings.journal_path,
        original.host.database,
    ]
    before = {path: path.read_bytes() for path in paths}
    services = open_go_task_services(
        settings, run_id=args[0], operation_id=args[1], principal="owner"
    )
    assert services.intents.source == original.source
    assert services.credentials is None and services.accounting is None
    assert ApprovedGoTaskExecution(services).get(*args, principal="owner") == original.read(
        *args, principal="owner"
    )
    assert {path: path.read_bytes() for path in paths} == before
    assert not settings.credential_private_directory.exists()
    with pytest.raises(RunError, match="TASK_EXECUTION_SERVICES_REQUIRED"):
        services.fresh_source()


@pytest.mark.parametrize("change", ["missing", "empty"])
def test_factory_missing_original_admission_fails_without_replacement(deployment, change):
    settings, _, args = deployment
    path = settings.state_directory / "task-admissions.sqlite"
    path.rename(path.with_suffix(".retained"))
    if change == "empty":
        path.write_bytes(b"")
    with pytest.raises((ExistingStoreError, RunError)):
        open_go_task_services(settings, run_id=args[0], operation_id=args[1], principal="owner")
    assert not path.exists() or path.read_bytes() == b""


def test_loader_uses_fixed_cwd_and_rejects_a_moved_bootstrap(deployment, monkeypatch, tmp_path):
    settings, original, args = deployment
    monkeypatch.chdir(settings.control_directory)
    services = load_go_task_services_from_fixed_bootstrap(*args, "owner")
    assert services.intents.source == original.source
    elsewhere = tmp_path / "other-control"
    elsewhere.mkdir(mode=0o700)
    copy = elsewhere / BOOTSTRAP_NAME
    copy.write_bytes((settings.control_directory / BOOTSTRAP_NAME).read_bytes())
    if sys.platform != "win32":
        copy.chmod(0o600)
    monkeypatch.chdir(elsewhere)
    with pytest.raises(RunError, match="TASK_BOOTSTRAP_INVALID"):
        load_go_task_services_from_fixed_bootstrap(*args, "owner")


def test_bootstrap_configuration_cannot_add_transport_or_argv(deployment):
    settings, _, _ = deployment
    for key in ("endpoint", "client_factory", "argv", "prompt", "api_key"):
        value = settings.document()
        value[key] = "forbidden-canary"
        with pytest.raises(RunError, match="TASK_BOOTSTRAP_INVALID"):
            GoTaskSettings.from_document(value)
    with pytest.raises(FileExistsError):
        write_go_task_bootstrap(settings)


def test_bootstrap_change_cannot_redirect_a_live_settings_object(deployment):
    settings, _, args = deployment
    target = settings.control_directory / BOOTSTRAP_NAME
    document = replace(settings, task_work_root=settings.control_directory).document()
    target.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(RunError, match="TASK_BOOTSTRAP_CHANGED"):
        open_go_task_services(settings, run_id=args[0], operation_id=args[1], principal="owner")


def test_execution_factory_rejects_wrong_owner_before_execution_dependencies(
    deployment, monkeypatch
):
    settings, _, args = deployment

    def forbidden(*_args, **_kwargs):
        pytest.fail("Unowned operation opened execution dependencies")

    monkeypatch.setattr("karajan.orchestration.go_task_runtime.GoRequestAccounting", forbidden)
    monkeypatch.setattr("karajan.orchestration.go_task_runtime.CredentialSourceStore", forbidden)
    with pytest.raises(RunError, match="USER_DECISION_REQUIRED"):
        open_go_task_services(
            settings, run_id=args[0], operation_id=args[1], principal="intruder", for_execution=True
        )


def test_actual_fixed_entry_rejects_unowned_bootstrap_ids_without_repo_imports(deployment):
    settings, _, args = deployment
    fake_package = settings.control_directory / "karajan"
    fake_package.mkdir(mode=0o700)
    marker = settings.control_directory / "imported-untrusted"
    (fake_package / "__init__.py").write_text(f"open({str(marker)!r}, 'w').write('bad')")
    paths = list(settings.state_directory.glob("*.sqlite")) + [settings.journal_path]
    before = {path: path.read_bytes() for path in paths}
    result = subprocess.run(
        [sys.executable, "-I", _go_task_runner.__file__, *args, "intruder"],
        cwd=settings.control_directory,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 1
    assert result.stdout.strip() == "TASK_RUNNER_FAILED"
    # The tokenizer library can emit its optional-PyTorch warning on import.
    # The child must not expose its caught exception or import project code.
    assert "Traceback" not in result.stderr
    assert "USER_DECISION_REQUIRED" not in result.stderr
    assert str(settings.control_directory) not in result.stderr
    assert not marker.exists()
    assert {path: path.read_bytes() for path in paths} == before
    assert not settings.credential_private_directory.exists()
