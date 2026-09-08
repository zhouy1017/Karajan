"""Private bootstrap for reopening Reviewer execution state without provisioning it."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from karajan.runs import RunError

if TYPE_CHECKING:
    from .reviewer_execution_intent import ReviewerExecutionIntents

_NAME = "reviewer-execution-bootstrap.json"
_SCHEMA = "karajan.reviewer-execution-bootstrap.v1"


@dataclass(frozen=True)
class ReviewerExecutionSettings:
    control_directory: Path
    execution_database: Path
    state_directory: Path
    candidate_directory: Path
    host_directory: Path

    def document(self) -> dict[str, str]:
        return {
            "schema_version": _SCHEMA,
            "control_directory": str(self.control_directory),
            "execution_database": str(self.execution_database),
            "state_directory": str(self.state_directory),
            "candidate_directory": str(self.candidate_directory),
            "host_directory": str(self.host_directory),
        }

    @classmethod
    def from_document(cls, value: object) -> ReviewerExecutionSettings:
        keys = {
            "schema_version",
            "control_directory",
            "execution_database",
            "state_directory",
            "candidate_directory",
            "host_directory",
        }
        try:
            if (
                not isinstance(value, dict)
                or set(value) != keys
                or value["schema_version"] != _SCHEMA
            ):
                raise ValueError
            paths = {key: Path(value[key]) for key in keys - {"schema_version"}}
            if any(not path.is_absolute() or ".." in path.parts for path in paths.values()):
                raise ValueError
            return cls(**paths)
        except (KeyError, TypeError, ValueError):
            raise RunError("REVIEWER_EXECUTION_BOOTSTRAP_INVALID") from None


def _private(path: Path, *, directory: bool = False) -> None:
    try:
        info = path.stat()
        if (directory and not stat.S_ISDIR(info.st_mode)) or (
            not directory and not stat.S_ISREG(info.st_mode)
        ):
            raise ValueError
        if path.is_symlink() or (not directory and info.st_nlink != 1):
            raise ValueError
    except (OSError, ValueError):
        raise RunError("REVIEWER_EXECUTION_BOOTSTRAP_INVALID") from None


def provision_reviewer_execution_bootstrap(settings: ReviewerExecutionSettings) -> Path:
    settings = ReviewerExecutionSettings.from_document(settings.document())
    _private(settings.control_directory, directory=True)
    path = settings.control_directory / _NAME
    raw = (json.dumps(settings.document(), sort_keys=True, separators=(",", ":")) + "\n").encode()
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        raise RunError("REVIEWER_EXECUTION_BOOTSTRAP_PROVISION_FAILED") from None
    return path


def open_existing_reviewer_execution_bootstrap(
    control_directory: Path,
) -> tuple[ReviewerExecutionSettings, str]:
    _private(control_directory, directory=True)
    path = control_directory / _NAME
    _private(path)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            raw = stream.read(32769)
        if len(raw) > 32768:
            raise ValueError
        settings = ReviewerExecutionSettings.from_document(json.loads(raw))
        if (
            settings.control_directory != control_directory
            or not settings.execution_database.is_file()
        ):
            raise ValueError
        return settings, hashlib.sha256(raw).hexdigest()
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        raise RunError("REVIEWER_EXECUTION_BOOTSTRAP_INVALID") from None


def open_reviewer_execution_intents(control_directory: Path) -> ReviewerExecutionIntents:
    """Open the fixed Reviewer execution facade from provisioned controller state.

    This deliberately has no run, principal, profile, argv, or source arguments.
    It is a reader/composer: every store must already exist and it never starts a
    Host process.  The Go-task deployment descriptor is the second, fixed seal
    for the shared controller stores and executable provenance.
    """
    # Imports stay here to keep the descriptor reader usable during bootstrap
    # diagnostics without opening qualification assets.
    from karajan.adapters.opencode.go_context import GoRequestAccounting
    from karajan.candidates import CandidateStore
    from karajan.capacity import CapacityStore
    from karajan.execution import ProcessSpec, RunnerHost
    from karajan.projects import ProjectRegistry
    from karajan.runs import RunPlanner

    from .admission import ApprovedTaskAdmission
    from .go_task_runtime import _plain, _read_bootstrap
    from .qualification_services import GoQualificationSettings, open_go_qualification_store
    from .reviewer_binding import ApprovedReviewerBindings
    from .reviewer_execution_intent import (
        ReviewerExecutionIntents,
        ReviewerExecutionSource,
        ReviewerLaunchSpec,
    )
    from .routing import ApprovedRunRouting

    reviewer, _ = open_existing_reviewer_execution_bootstrap(control_directory)
    task, task_digest = _read_bootstrap(control_directory)
    if (
        task.control_directory != reviewer.control_directory
        or task.state_directory != reviewer.state_directory
        or task.candidate_directory != reviewer.candidate_directory
        or task.host_directory != reviewer.host_directory
    ):
        raise RunError("REVIEWER_EXECUTION_BOOTSTRAP_MISMATCH")
    # A current qualification composition is intentionally real, but only
    # constructs existing sealed stores.  It does not qualify, call a provider,
    # or turn any test fixture into an official qualification.
    for path in (reviewer.state_directory, reviewer.candidate_directory, reviewer.host_directory):
        _plain(path, directory=True)
    for name in ("projects.sqlite", "runs.sqlite", "capacity.sqlite", "task-admissions.sqlite"):
        _plain(reviewer.state_directory / name)
    _plain(reviewer.host_directory / "runnerhost.sqlite3")
    projects = ProjectRegistry(
        reviewer.state_directory / "projects.sqlite", task.allowed_roots, existing_only=True
    )
    planner = RunPlanner(reviewer.state_directory / "runs.sqlite", projects, existing_only=True)
    capacity = CapacityStore(reviewer.state_directory / "capacity.sqlite", existing_only=True)
    qualification = GoQualificationSettings(
        task.runtime,
        task.tokenizer_directory,
        task.journal_path,
        task.qualification_work_root,
        task.qualification_work_root,
        task.credential_private_directory,
        task.credential_sources,
    )
    qualifications = open_go_qualification_store(projects, qualification, for_current=True)
    routing = ApprovedRunRouting(planner, qualifications, capacity)
    admissions = ApprovedTaskAdmission(
        reviewer.state_directory / "task-admissions.sqlite", routing, existing_only=True
    )
    candidates = CandidateStore(reviewer.candidate_directory, existing_only=True)
    host = RunnerHost(reviewer.host_directory, existing_only=True)
    ApprovedReviewerBindings(admissions, candidates, qualifications)

    def current_source() -> ReviewerExecutionSource:
        """Bind the fixed deployment and this exact non-native child entry."""
        # Unlike the Go descriptor, this descriptor was previously captured by
        # the enclosing factory call.  Reopen it at every effect guard too: a
        # valid replacement must not silently inherit this facade's identity.
        current_reviewer, current_reviewer_digest = open_existing_reviewer_execution_bootstrap(
            control_directory
        )
        if current_reviewer != reviewer:
            raise RunError("REVIEWER_EXECUTION_BOOTSTRAP_CHANGED")
        accounting = GoRequestAccounting(task.tokenizer_directory)
        # deployment_source performs the fixed bootstrap/platform/interpreter
        # checks.  Hashing this source file prevents a changed child from
        # inheriting a previously prepared identity.
        from karajan.isolation.go_probe import source_digest

        from .go_task_runtime import deployment_source

        deployment = deployment_source(task, accounting)
        entry = Path(__file__).with_name("reviewer_execution_runner.py").resolve()
        _plain(entry)
        envelope = {
            "schema_version": "karajan.reviewer-execution-runner-source.v1",
            "deployment": deployment,
            "reviewer_bootstrap_sha256": current_reviewer_digest,
            "task_bootstrap_sha256": task_digest,
            "entry_path": str(entry),
            "entry_sha256": hashlib.sha256(entry.read_bytes()).hexdigest(),
            "transport": "fixed_reviewer_observer_no_native",
        }
        return ReviewerExecutionSource(source_digest(envelope), source_digest(deployment))

    def launch(intent: dict[str, object]) -> ReviewerLaunchSpec:
        current_source()
        values = tuple(intent[key] for key in ("run_id", "reviewer_operation_id", "principal"))
        if not all(isinstance(value, str) for value in values):
            raise RunError("REVIEWER_EXECUTION_LAUNCH_INVALID")
        run_id, reviewer_operation_id, principal = (cast(str, value) for value in values)
        return ReviewerLaunchSpec(
            ProcessSpec(
                (
                    str(task.python_executable),
                    "-I",
                    str(Path(__file__).with_name("reviewer_execution_runner.py").resolve()),
                    run_id,
                    reviewer_operation_id,
                    principal,
                ),
                reviewer.control_directory,
            ),
            task_digest,
        )

    return ReviewerExecutionIntents(
        reviewer.execution_database,
        admissions,
        candidates,
        source=current_source(),
        host=host,
        launch_compiler=launch,
        current_source=current_source,
        existing_only=True,
    )
