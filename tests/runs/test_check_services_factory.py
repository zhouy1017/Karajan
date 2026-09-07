"""Fixed Check bootstrap cannot accept executable requests or provision ledgers."""

import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from karajan.orchestration import _candidate_check_runner
from karajan.orchestration.check_services_factory import (
    BOOTSTRAP_NAME,
    CheckEnvironmentSource,
    CheckSettings,
    _read_bootstrap,
    open_check_services,
    write_check_bootstrap,
)
from karajan.runs import RunError
from test_go_execution_intent import case, prepared, projected, ready, reservation
from test_go_task_runtime import deployment as go_deployment
from test_go_task_runtime import private_fixture_directories

__all__ = [
    "case",
    "go_deployment",
    "prepared",
    "private_fixture_directories",
    "projected",
    "ready",
    "reservation",
]


@pytest.fixture
def settings(tmp_path):
    control = tmp_path / "control"
    control.mkdir(mode=0o700)
    return CheckSettings(
        control,
        tmp_path / "state",
        tmp_path / "candidates",
        tmp_path / "host",
        tmp_path / "checks",
        Path(sys.executable),
        (tmp_path / "repositories",),
        (CheckEnvironmentSource("python-checks", 1, tmp_path / "image"),),
    )


def test_bootstrap_roundtrip_writes_only_explicit_file(settings):
    result = write_check_bootstrap(settings)
    opened, digest = _read_bootstrap(settings.control_directory)
    assert opened == settings
    assert result == settings.control_directory / BOOTSTRAP_NAME
    assert len(digest) == 64
    assert not settings.state_directory.exists()
    assert not settings.environment_sources[0].directory.exists()
    with pytest.raises(FileExistsError):
        write_check_bootstrap(settings)


@pytest.mark.parametrize("field", ["argv", "prompt", "endpoint", "credential", "result"])
def test_deployment_cannot_smuggle_execution_payload(settings, field):
    with pytest.raises(RunError, match="CHECK_BOOTSTRAP_INVALID"):
        CheckSettings.from_document(settings.document() | {field: "forbidden-input"})


def test_duplicate_environment_identity_and_noncanonical_path_rejected(settings):
    with pytest.raises(RunError, match="CHECK_BOOTSTRAP_INVALID"):
        CheckSettings.from_document(
            replace(settings, environment_sources=settings.environment_sources * 2).document()
        )
    document = settings.document()
    document["check_work_root"] = str(settings.check_work_root.parent / ".." / "outside")
    with pytest.raises(RunError, match="CHECK_BOOTSTRAP_INVALID"):
        CheckSettings.from_document(document)


def test_duplicate_json_fields_are_not_silently_replaced(settings):
    path = write_check_bootstrap(settings)
    original = path.read_text()
    path.write_text(original.rstrip()[:-1] + ',"check_work_root":"another-root"}', encoding="utf-8")
    with pytest.raises(RunError, match="CHECK_BOOTSTRAP_INVALID"):
        _read_bootstrap(settings.control_directory)


def test_bootstrap_moved_to_another_control_directory_is_rejected(settings, tmp_path):
    path = write_check_bootstrap(settings)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    copy = elsewhere / BOOTSTRAP_NAME
    copy.write_bytes(path.read_bytes())
    if sys.platform != "win32":
        copy.chmod(0o600)
    with pytest.raises(RunError, match="CHECK_BOOTSTRAP_INVALID"):
        _read_bootstrap(elsewhere)


@pytest.mark.parametrize(
    "arguments", [[], ["run", "operation", "check", "bad owner"], ["r", "o", "c", "p", "extra"]]
)
def test_actual_isolated_python_entry_rejects_bad_ids_before_opening_state(tmp_path, arguments):
    result = subprocess.run(
        [sys.executable, "-I", str(Path(_candidate_check_runner.__file__).resolve()), *arguments],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 2
    assert result.stdout.strip() == "CHECK_RUNNER_IDENTITIES_REQUIRED"
    assert not (tmp_path / BOOTSTRAP_NAME).exists()


@pytest.fixture
def check_deployment(go_deployment):
    original_settings, controller, args = go_deployment
    settings = CheckSettings(
        original_settings.control_directory,
        original_settings.state_directory,
        original_settings.candidate_directory,
        original_settings.host_directory,
        original_settings.task_work_root,
        original_settings.python_executable,
        original_settings.allowed_roots,
        (
            CheckEnvironmentSource(
                "stdlib", 1, original_settings.control_directory.parent / "missing-image"
            ),
        ),
    )
    write_check_bootstrap(settings)
    return settings, controller, args


def test_history_factory_reopens_without_images_or_controller_runtime(check_deployment):
    settings, _, args = check_deployment
    database = settings.state_directory / "task-admissions.sqlite"
    before = database.read_bytes()
    service = open_check_services(settings, run_id=args[0], operation_id=args[1], principal="owner")
    assert service.get(*args, principal="owner") is None
    assert database.read_bytes() == before
    assert not settings.environment_sources[0].directory.exists()
    assert not settings.python_executable.exists()
    with pytest.raises(RunError, match="CHECK_EXECUTION_SERVICES_REQUIRED"):
        service.controller_source()


@pytest.mark.parametrize(
    "missing", ["projects.sqlite", "runs.sqlite", "capacity.sqlite", "task-admissions.sqlite"]
)
def test_history_factory_never_recreates_missing_authority(check_deployment, missing):
    settings, _, args = check_deployment
    target = settings.state_directory / missing
    target.unlink()
    with pytest.raises(RunError, match="CHECK_DEPLOYMENT_PATH_INVALID"):
        open_check_services(settings, run_id=args[0], operation_id=args[1], principal="owner")
    assert not target.exists()


def test_history_factory_rejects_changed_bootstrap(check_deployment):
    settings, _, args = check_deployment
    target = settings.control_directory / BOOTSTRAP_NAME
    replacement = replace(settings, check_work_root=settings.control_directory)
    import json

    target.write_text(json.dumps(replacement.document()), encoding="utf-8")
    with pytest.raises(RunError, match="CHECK_BOOTSTRAP_CHANGED"):
        open_check_services(settings, run_id=args[0], operation_id=args[1], principal="owner")
