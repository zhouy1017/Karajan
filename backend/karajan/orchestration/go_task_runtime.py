"""Fixed private deployment and existing-store composition for the Go Task child.

This is controller configuration, not an HTTP payload or a project config file.
Provisioning writes one explicit bootstrap; reconstruction never creates a ledger.
"""

import hashlib
import json
import math
import os
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from karajan.adapters.opencode.go_context import GoRequestAccounting
from karajan.adapters.opencode.go_journal import GoCallJournal
from karajan.candidates import CandidateStore
from karajan.capacity import CapacityStore
from karajan.execution import ProcessSpec, RunnerHost
from karajan.projects import ProjectRegistry
from karajan.projects.credential_sources import CredentialSourceStore, LocalKeyFile, _private
from karajan.projects.go_suite import V2_SUITE_REF, FixedGoSuite
from karajan.projects.qualification import ProfileQualificationStore
from karajan.runs import RunError, RunPlanner
from karajan.runs.planning import identifier

from .admission import ApprovedTaskAdmission
from .go_execution_intent import GoExecutionIntents, GoLaunchSpec
from .go_task_binding import execution_source, task_runner_source
from .go_task_execution import GoTaskServices
from .routing import ApprovedRunRouting

BOOTSTRAP_NAME = "go-task-bootstrap.json"
_SCHEMA = "karajan.go-task-bootstrap.v1"
_PATHS = (
    "control_directory",
    "state_directory",
    "candidate_directory",
    "host_directory",
    "journal_path",
    "qualification_work_root",
    "task_work_root",
    "python_executable",
    "runtime",
    "tokenizer_directory",
    "credential_private_directory",
)


@dataclass(frozen=True)
class GoTaskCredentialSource:
    project_id: str
    auth_ref: str
    source_id: str
    path: Path = field(repr=False)


@dataclass(frozen=True, repr=False)
class GoTaskSettings:
    control_directory: Path
    state_directory: Path
    candidate_directory: Path
    host_directory: Path
    journal_path: Path
    qualification_work_root: Path
    task_work_root: Path
    python_executable: Path
    runtime: Path
    tokenizer_directory: Path
    credential_private_directory: Path
    allowed_roots: tuple[Path, ...]
    credential_sources: tuple[GoTaskCredentialSource, ...] = field(repr=False)

    def document(self) -> dict[str, Any]:
        document = {name: str(getattr(self, name)) for name in _PATHS}
        return {
            "schema_version": _SCHEMA,
            **document,
            "allowed_roots": [str(path) for path in self.allowed_roots],
            "credential_sources": [
                {
                    "project_id": row.project_id,
                    "auth_ref": row.auth_ref,
                    "source_id": row.source_id,
                    "path": str(row.path),
                }
                for row in self.credential_sources
            ],
        }

    @classmethod
    def from_document(cls, value: object) -> "GoTaskSettings":
        try:
            if (
                not isinstance(value, dict)
                or set(value) != {"schema_version", *_PATHS, "allowed_roots", "credential_sources"}
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

            if not isinstance(value["allowed_roots"], list) or not value["allowed_roots"]:
                raise ValueError()
            if not isinstance(value["credential_sources"], list):
                raise ValueError()
            sources = []
            for row in value["credential_sources"]:
                if not isinstance(row, dict) or set(row) != {
                    "project_id",
                    "auth_ref",
                    "source_id",
                    "path",
                }:
                    raise ValueError()
                for name in ("project_id", "auth_ref", "source_id"):
                    identifier(row[name])
                sources.append(
                    GoTaskCredentialSource(
                        row["project_id"], row["auth_ref"], row["source_id"], path(row["path"])
                    )
                )
            if len({(row.project_id, row.auth_ref) for row in sources}) != len(sources):
                raise ValueError()
            return cls(
                **{name: path(value[name]) for name in _PATHS},
                allowed_roots=tuple(path(raw) for raw in value["allowed_roots"]),
                credential_sources=tuple(sources),
            )
        except (ValueError, TypeError, KeyError, OSError):
            raise RunError("TASK_BOOTSTRAP_INVALID") from None


def _plain(path: Path, *, directory: bool = False) -> None:
    try:
        for item in (path, *path.parents):
            info = item.lstat()
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
        raise RunError("TASK_DEPLOYMENT_PATH_INVALID") from None


def _read_bootstrap(directory: Path) -> tuple[GoTaskSettings, str]:
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
        settings = GoTaskSettings.from_document(json.loads(raw))
        if settings.control_directory != directory:
            raise ValueError()
        return settings, hashlib.sha256(raw).hexdigest()
    except (OSError, ValueError, TypeError):
        raise RunError("TASK_BOOTSTRAP_INVALID") from None


def write_go_task_bootstrap(settings: GoTaskSettings) -> Path:
    """Explicit provisioning only; create one private file, never databases/keys."""
    settings = GoTaskSettings.from_document(settings.document())
    _plain(settings.control_directory, directory=True)
    _private(settings.control_directory, directory=True)
    target = settings.control_directory / BOOTSTRAP_NAME
    raw = (json.dumps(settings.document(), sort_keys=True, separators=(",", ":")) + "\n").encode()
    if len(raw) > 32768:
        raise RunError("TASK_BOOTSTRAP_INVALID")
    descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    _private(target)
    return target


def _state_paths(settings: GoTaskSettings, *, for_execution: bool) -> None:
    for path in (
        settings.control_directory,
        settings.state_directory,
    ):
        _plain(path, directory=True)
        _private(path, directory=True)
    for name in ("projects.sqlite", "runs.sqlite", "capacity.sqlite", "task-admissions.sqlite"):
        _plain(settings.state_directory / name)
    if not for_execution:
        # Optional execution ledgers may be unavailable during cleanup. Their
        # path handles still use existing-only connections; each owned observation
        # independently records unavailable, while other cleanup can proceed.
        return
    for path in (settings.candidate_directory, settings.host_directory, settings.task_work_root):
        _plain(path, directory=True)
        _private(path, directory=True)
    _plain(settings.host_directory / "runnerhost.sqlite3")
    _plain(settings.journal_path)
    _private(settings.journal_path.parent, directory=True)
    task = settings.task_work_root
    protected = (
        settings.state_directory,
        settings.candidate_directory,
        settings.host_directory,
        settings.control_directory,
        settings.credential_private_directory,
        settings.journal_path.parent,
        settings.qualification_work_root,
    )
    if any(task.is_relative_to(path) or path.is_relative_to(task) for path in protected):
        raise RunError("TASK_WORK_ROOT_MUST_BE_SEPARATE")


def deployment_source(settings: GoTaskSettings, accounting: GoRequestAccounting) -> dict[str, Any]:
    current, bootstrap_sha = _read_bootstrap(settings.control_directory)
    if current.document() != settings.document():
        raise RunError("TASK_BOOTSTRAP_CHANGED")
    if sys.platform != "linux":
        raise RunError("TASK_EXECUTION_PLATFORM_UNSUPPORTED")
    _plain(settings.runtime)
    executable = settings.python_executable.resolve(strict=True)
    _plain(executable)
    venv_config = settings.python_executable.parent.parent / "pyvenv.cfg"
    if not venv_config.is_file():
        raise RunError("TASK_PYTHON_ENVIRONMENT_UNPROVEN")
    result = task_runner_source(settings.runtime, accounting)
    result["deployment"] = {
        "bootstrap_sha256": bootstrap_sha,
        "bootstrap_path": str(settings.control_directory / BOOTSTRAP_NAME),
        "entry_path": str(Path(__file__).with_name("_go_task_runner.py").resolve()),
        "python_path": str(settings.python_executable),
        "python_resolved_path": str(executable),
        "python_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "python_environment_sha256": hashlib.sha256(venv_config.read_bytes()).hexdigest(),
        "transport": "fixed_official_go",
    }
    return result


def open_go_task_services(
    settings: GoTaskSettings,
    *,
    run_id: str,
    operation_id: str,
    principal: str,
    for_execution: bool = False,
) -> GoTaskServices:
    settings = GoTaskSettings.from_document(settings.document())
    actual, bootstrap_sha = _read_bootstrap(settings.control_directory)
    if actual.document() != settings.document():
        raise RunError("TASK_BOOTSTRAP_CHANGED")
    _state_paths(settings, for_execution=for_execution)
    projects = ProjectRegistry(
        settings.state_directory / "projects.sqlite", settings.allowed_roots, existing_only=True
    )
    planner = RunPlanner(settings.state_directory / "runs.sqlite", projects, existing_only=True)
    capacity = CapacityStore(settings.state_directory / "capacity.sqlite", existing_only=True)
    journal = GoCallJournal(
        settings.journal_path, existing_only=True, defer_validation=not for_execution
    )
    host = RunnerHost(
        settings.host_directory, existing_only=True, defer_validation=not for_execution
    )
    candidates = CandidateStore(
        settings.candidate_directory, existing_only=True, defer_validation=not for_execution
    )
    routing = ApprovedRunRouting(planner, ProfileQualificationStore(projects), capacity)
    admissions = ApprovedTaskAdmission(
        settings.state_directory / "task-admissions.sqlite", routing, existing_only=True
    )
    operation = GoExecutionIntents.read_operation(
        admissions, run_id, operation_id, principal=principal
    )
    try:
        repository = Path(operation["workspace"]["source_binding"]["repository"]["root"]).resolve()
    except (KeyError, TypeError, ValueError):
        raise RunError("TASK_WORKSPACE_REQUIRED") from None
    repositories = {
        repository,
        *(Path(row["repository"]["root"]).resolve() for row in projects.list()),
    }
    for path in (
        settings.control_directory,
        settings.state_directory,
        settings.candidate_directory,
        settings.host_directory,
        settings.journal_path,
        settings.task_work_root,
        settings.credential_private_directory,
    ):
        if any(path.is_relative_to(root) for root in repositories):
            raise RunError("TASK_CONTROL_STATE_IN_REPOSITORY")
    accounting = GoRequestAccounting(settings.tokenizer_directory) if for_execution else None
    credentials = (
        CredentialSourceStore(
            projects,
            sources={
                (row.project_id, row.auth_ref): LocalKeyFile(row.source_id, row.path)
                for row in settings.credential_sources
            },
            private_directory=settings.credential_private_directory,
            existing_only=True,
        )
        if for_execution
        else None
    )
    suite = (
        FixedGoSuite(
            settings.runtime,
            settings.qualification_work_root,
            journal,
            suite_ref=V2_SUITE_REF,
            accounting=accounting,
        )
        if for_execution
        else None
    )
    if for_execution:
        routing.qualifications = ProfileQualificationStore(
            projects,
            credentials=credentials,
            go_suite=suite,
        )

    def fresh_source() -> dict[str, Any]:
        if accounting is None:
            raise RunError("TASK_EXECUTION_SERVICES_REQUIRED")
        return deployment_source(settings, accounting)

    def fixed_runner_spec(run: str, op: str, owner: str, timeout: float) -> ProcessSpec:
        for value in (run, op, owner):
            identifier(value)
        if (
            not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or not math.isfinite(timeout)
            or not 0 < timeout <= 86400
        ):
            raise RunError("TASK_RUNNER_TIMEOUT_INVALID")
        fresh_source()
        return ProcessSpec(
            (
                str(settings.python_executable),
                "-I",
                str(Path(__file__).with_name("_go_task_runner.py").resolve()),
                run,
                op,
                owner,
            ),
            settings.control_directory,
            float(timeout),
        )

    def launch(owned: dict[str, Any]) -> GoLaunchSpec:
        intent = owned["execution"]["intent"]
        tasks = owned["workspace"]["source_binding"]["plan"]["plan"]["tasks"]
        selected = [task for task in tasks if task["id"] == owned["task_id"]]
        if len(selected) != 1:
            raise RunError("TASK_INPUT_UNIQUE_TASK_REQUIRED")
        return GoLaunchSpec(
            fixed_runner_spec(
                owned["run_id"], owned["id"], intent["owner"], selected[0]["duration_seconds"]
            ),
            bootstrap_sha,
        )

    intents = GoExecutionIntents.open_existing(
        admissions,
        run_id=run_id,
        operation_id=operation_id,
        principal=principal,
        source_if_unprepared=lambda: execution_source(fresh_source()),
        host=host,
        launch_compiler=launch,
        journal=journal,
        candidates=candidates,
    )
    return GoTaskServices(
        intents,
        candidates,
        journal,
        credentials,
        settings.runtime,
        accounting,
        settings.task_work_root,
        fresh_source,
        fixed_runner_spec,
    )


def load_go_task_services_from_fixed_bootstrap(
    run_id: str,
    operation_id: str,
    principal: str,
    *,
    for_execution: bool = False,
) -> GoTaskServices:
    settings, _ = _read_bootstrap(Path.cwd())
    return open_go_task_services(
        settings,
        run_id=run_id,
        operation_id=operation_id,
        principal=principal,
        for_execution=for_execution,
    )
