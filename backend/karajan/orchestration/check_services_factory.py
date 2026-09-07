"""Private fixed Check deployment; reopening does not provision state or images."""

import hashlib
import json
import math
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from karajan.candidates import CandidateStore
from karajan.capacity import CapacityStore
from karajan.execution import ProcessSpec, RunnerHost
from karajan.projects import ProjectRegistry
from karajan.projects.credential_sources import _private
from karajan.projects.qualification import ProfileQualificationStore
from karajan.runs import RunError, RunPlanner
from karajan.runs.planning import identifier

from .admission import ApprovedTaskAdmission
from .go_execution_intent import GoExecutionIntents
from .routing import ApprovedRunRouting

if TYPE_CHECKING:
    from .candidate_checks import ApprovedCandidateChecks, CheckLaunchSpec

BOOTSTRAP_NAME = "candidate-check-bootstrap.json"
_SCHEMA = "karajan.candidate-check-bootstrap.v1"
_PATHS = (
    "control_directory",
    "state_directory",
    "candidate_directory",
    "host_directory",
    "check_work_root",
    "python_executable",
)


@dataclass(frozen=True)
class CheckEnvironmentSource:
    id: str
    revision: int
    directory: Path


@dataclass(frozen=True, repr=False)
class CheckSettings:
    control_directory: Path
    state_directory: Path
    candidate_directory: Path
    host_directory: Path
    check_work_root: Path
    python_executable: Path
    allowed_roots: tuple[Path, ...]
    environment_sources: tuple[CheckEnvironmentSource, ...] = ()

    def document(self) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA,
            **{name: str(getattr(self, name)) for name in _PATHS},
            "allowed_roots": [str(path) for path in self.allowed_roots],
            "environment_sources": [
                {"id": row.id, "revision": row.revision, "directory": str(row.directory)}
                for row in self.environment_sources
            ],
        }

    @classmethod
    def from_document(cls, value: object) -> "CheckSettings":
        try:
            if (
                not isinstance(value, dict)
                or set(value) != {"schema_version", *_PATHS, "allowed_roots", "environment_sources"}
                or value["schema_version"] != _SCHEMA
            ):
                raise ValueError()

            def path(raw: object) -> Path:
                if not isinstance(raw, str) or not raw or "\x00" in raw:
                    raise ValueError()
                item = Path(raw)
                if not item.is_absolute() or ".." in item.parts or str(item) != raw:
                    raise ValueError()
                return item

            roots = value["allowed_roots"]
            sources = value["environment_sources"]
            if not isinstance(roots, list) or not roots or not isinstance(sources, list):
                raise ValueError()
            entries = []
            for row in sources:
                if not isinstance(row, dict) or set(row) != {"id", "revision", "directory"}:
                    raise ValueError()
                identifier(row["id"])
                if type(row["revision"]) is not int or row["revision"] <= 0:
                    raise ValueError()
                entries.append(
                    CheckEnvironmentSource(row["id"], row["revision"], path(row["directory"]))
                )
            if len({(row.id, row.revision) for row in entries}) != len(entries):
                raise ValueError()
            return cls(
                **{name: path(value[name]) for name in _PATHS},
                allowed_roots=tuple(path(raw) for raw in roots),
                environment_sources=tuple(entries),
            )
        except (ValueError, TypeError, KeyError, OSError):
            raise RunError("CHECK_BOOTSTRAP_INVALID") from None


def _plain(path: Path, *, directory: bool = False) -> None:
    try:
        for part in (path, *path.parents):
            info = part.lstat()
            if stat.S_ISLNK(info.st_mode) or (
                getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
            ):
                raise ValueError()
        info = path.stat()
        if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
            raise ValueError()
        if not directory and info.st_nlink != 1:
            raise ValueError()
    except (OSError, ValueError):
        raise RunError("CHECK_DEPLOYMENT_PATH_INVALID") from None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate bootstrap field")
        result[name] = value
    return result


def _read_bootstrap(directory: Path) -> tuple[CheckSettings, str]:
    path = directory / BOOTSTRAP_NAME
    _plain(directory, directory=True)
    _private(directory, directory=True)
    _plain(path)
    _private(path)
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if info.st_size > 32768 or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError()
            raw = stream.read(32769)
        settings = CheckSettings.from_document(json.loads(raw, object_pairs_hook=_unique_object))
        if settings.control_directory != directory:
            raise ValueError()
        return settings, hashlib.sha256(raw).hexdigest()
    except (OSError, ValueError, TypeError):
        raise RunError("CHECK_BOOTSTRAP_INVALID") from None


def write_check_bootstrap(settings: CheckSettings) -> Path:
    """Explicit controller provisioning: one private file, no ledgers or images."""
    settings = CheckSettings.from_document(settings.document())
    _plain(settings.control_directory, directory=True)
    _private(settings.control_directory, directory=True)
    raw = (json.dumps(settings.document(), sort_keys=True, separators=(",", ":")) + "\n").encode()
    if len(raw) > 32768:
        raise RunError("CHECK_BOOTSTRAP_INVALID")
    path = settings.control_directory / BOOTSTRAP_NAME
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    _private(path)
    return path


def check_controller_source(settings: CheckSettings) -> dict[str, Any]:
    actual, bootstrap_sha = _read_bootstrap(settings.control_directory)
    if actual.document() != settings.document():
        raise RunError("CHECK_BOOTSTRAP_CHANGED")
    if sys.platform != "linux":
        raise RunError("CHECK_EXECUTION_PLATFORM_UNSUPPORTED")
    executable = settings.python_executable.resolve(strict=True)
    _plain(executable)
    venv_config = settings.python_executable.parent.parent / "pyvenv.cfg"
    _plain(venv_config)
    package = Path(__file__).resolve().parents[1]
    # Include transitive policy/identity code: a changed controller cannot reuse
    # a previously frozen execution claim just because its entry file is equal.
    files = [
        {
            "path": path.relative_to(package).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in sorted(package.rglob("*.py"))
    ]
    return {
        "schema_version": "karajan.check-controller-source.v1",
        "files": files,
        "bootstrap_sha256": bootstrap_sha,
        "bootstrap_path": str(settings.control_directory / BOOTSTRAP_NAME),
        "entry_path": str(Path(__file__).with_name("_candidate_check_runner.py").resolve()),
        "python_path": str(settings.python_executable),
        "python_resolved_path": str(executable),
        "python_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "python_environment_sha256": hashlib.sha256(venv_config.read_bytes()).hexdigest(),
    }


def open_check_services(
    settings: CheckSettings,
    *,
    run_id: str,
    operation_id: str,
    principal: str,
    for_execution: bool = False,
) -> "ApprovedCandidateChecks":
    from karajan.isolation.check_runner import FixedCheckRunner, PythonCheckEnvironment

    from .candidate_checks import ApprovedCandidateChecks, CheckLaunchSpec

    settings = CheckSettings.from_document(settings.document())
    actual, bootstrap_sha = _read_bootstrap(settings.control_directory)
    if actual.document() != settings.document():
        raise RunError("CHECK_BOOTSTRAP_CHANGED")
    _plain(settings.state_directory, directory=True)
    _private(settings.state_directory, directory=True)
    for name in ("projects.sqlite", "runs.sqlite", "capacity.sqlite", "task-admissions.sqlite"):
        _plain(settings.state_directory / name)
    projects = ProjectRegistry(
        settings.state_directory / "projects.sqlite", settings.allowed_roots, existing_only=True
    )
    planner = RunPlanner(settings.state_directory / "runs.sqlite", projects, existing_only=True)
    capacity = CapacityStore(settings.state_directory / "capacity.sqlite", existing_only=True)
    routing = ApprovedRunRouting(planner, ProfileQualificationStore(projects), capacity)
    admissions = ApprovedTaskAdmission(
        settings.state_directory / "task-admissions.sqlite", routing, existing_only=True
    )
    operation = GoExecutionIntents.read_operation(
        admissions, run_id, operation_id, principal=principal
    )
    try:
        repositories = {
            Path(operation["workspace"]["source_binding"]["repository"]["root"]).resolve(),
            *(Path(row["repository"]["root"]).resolve() for row in projects.list()),
        }
    except (KeyError, TypeError, ValueError):
        raise RunError("CHECK_WORKSPACE_REQUIRED") from None
    protected = (
        settings.control_directory,
        settings.state_directory,
        settings.candidate_directory,
        settings.host_directory,
        *(row.directory for row in settings.environment_sources),
    )
    for path in (*protected, settings.check_work_root):
        if any(path.is_relative_to(root) for root in repositories):
            raise RunError("CHECK_CONTROL_STATE_IN_REPOSITORY")
    if any(
        settings.check_work_root.is_relative_to(path)
        or path.is_relative_to(settings.check_work_root)
        for path in protected
    ):
        raise RunError("CHECK_WORK_ROOT_MUST_BE_SEPARATE")
    if for_execution:
        for path in (*protected, settings.check_work_root):
            _plain(path, directory=True)
            _private(path, directory=True)
        _plain(settings.host_directory / "runnerhost.sqlite3")
    candidates = CandidateStore(
        settings.candidate_directory, existing_only=True, defer_validation=not for_execution
    )
    host = RunnerHost(
        settings.host_directory, existing_only=True, defer_validation=not for_execution
    )
    environments = (
        {
            (row.id, row.revision): PythonCheckEnvironment(row.directory)
            for row in settings.environment_sources
        }
        if for_execution
        else {}
    )
    runner = FixedCheckRunner(settings.check_work_root, candidates, environments=environments)

    def current_source() -> dict[str, Any]:
        if not for_execution:
            raise RunError("CHECK_EXECUTION_SERVICES_REQUIRED")
        return check_controller_source(settings)

    def launch(execution: dict[str, Any]) -> "CheckLaunchSpec":
        current_source()
        identifiers = tuple(
            execution[key] for key in ("run_id", "operation_id", "check_run_id", "principal")
        )
        for value in identifiers:
            identifier(value)
        timeout = execution["timeout_seconds"]
        if (
            type(timeout) not in {int, float}
            or not math.isfinite(timeout)
            or not 0 < timeout <= 86400
        ):
            raise RunError("CHECK_RUNNER_TIMEOUT_INVALID")
        spec = ProcessSpec(
            (
                str(settings.python_executable),
                "-I",
                str(Path(__file__).with_name("_candidate_check_runner.py").resolve()),
                *identifiers,
            ),
            settings.control_directory,
            float(timeout),
        )
        return CheckLaunchSpec(spec, bootstrap_sha)

    return ApprovedCandidateChecks(
        admissions,
        candidates,
        runner=runner,
        host=host,
        launch_compiler=launch,
        controller_source=current_source,
    )


def load_check_services_from_fixed_bootstrap(
    run_id: str,
    operation_id: str,
    principal: str,
    *,
    for_execution: bool = False,
) -> "ApprovedCandidateChecks":
    settings, _ = _read_bootstrap(Path.cwd())
    return open_check_services(
        settings,
        run_id=run_id,
        operation_id=operation_id,
        principal=principal,
        for_execution=for_execution,
    )
