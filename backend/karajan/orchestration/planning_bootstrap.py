"""Protected, read-only bootstrap for the production planning authority.

The planning authority is rebuilt from a controller-owned descriptor.  This
module intentionally has no authority or reservation logic: it only proves
that the descriptor and its existing stores are a private deployment and that
the descriptor is still the one the caller originally observed.
"""

import hashlib
import json
import os
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from karajan.projects.credential_sources import _private
from karajan.runs import RunError
from karajan.storage import ExistingStoreError, require_schema

from .go_task_runtime import _plain

PLANNING_ADMISSION_BOOTSTRAP = "planning-admission-bootstrap.json"
SCHEMA_VERSION = "karajan.planning-admission-bootstrap.v1"
MAX_DESCRIPTOR_BYTES = 32 * 1024

_PATH_FIELDS = (
    "state_directory",
    "planning_execution_database",
    "planning_admission_database",
    "capacity_database",
    "projects_database",
)
_REQUIRED_FIELDS = {"schema_version", *_PATH_FIELDS, "allowed_roots"}
_PROJECT_SCHEMA = {
    "projects": ["id", "snapshot"],
    "commands": ["principal", "key", "digest", "result"],
    "previews": ["id", "project_id", "configuration", "result"],
    "execution_policies": ["project_id", "id", "revision", "record"],
    "project_owners": ["project_id", "principal"],
    "rulebook_versions": ["project_id", "id", "revision", "digest", "result"],
    "rulebook_publications": ["sequence", "project_id", "result"],
    "effective_catalogs": ["project_id", "result"],
    "rulebook_conflicts": ["project_id", "id", "revision", "digests"],
    "publication_migrations": ["version"],
}


class _DuplicateKey(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey()
        result[key] = value
    return result


@dataclass(frozen=True, slots=True, repr=False)
class PlanningBootstrapSettings:
    """Validated paths from a planning bootstrap descriptor."""

    control_directory: Path
    state_directory: Path
    planning_execution_database: Path
    planning_admission_database: Path
    capacity_database: Path
    projects_database: Path
    allowed_roots: tuple[Path, ...]

    def document(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "state_directory": str(self.state_directory),
            "planning_execution_database": str(self.planning_execution_database),
            "planning_admission_database": str(self.planning_admission_database),
            "capacity_database": str(self.capacity_database),
            "projects_database": str(self.projects_database),
            "allowed_roots": [str(root) for root in self.allowed_roots],
        }

    def __repr__(self) -> str:
        return "PlanningBootstrapSettings(<protected paths>)"


def _invalid() -> RunError:
    return RunError("PLANNING_ADMISSION_BOOTSTRAP_INVALID")


def _canonical_existing(raw: object, *, directory: bool) -> Path:
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise ValueError()
    path = Path(raw)
    # Do not silently canonicalize a descriptor.  A spelling containing a
    # relative component, a duplicate separator, or a symlink is an alias.
    if not path.is_absolute() or any(part in {".", ".."} for part in path.parts):
        raise ValueError()
    if str(path) != raw:
        raise ValueError()
    resolved = path.resolve(strict=True)
    if resolved != path:
        raise ValueError()
    _plain(path, directory=directory)
    return path


def _read_descriptor(path: Path) -> tuple[bytes, dict[str, Any]]:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_size > MAX_DESCRIPTOR_BYTES
            ):
                raise ValueError()
            raw = stream.read(MAX_DESCRIPTOR_BYTES + 1)
        if len(raw) > MAX_DESCRIPTOR_BYTES:
            raise ValueError()
        value = json.loads(raw, object_pairs_hook=_unique_object)
        if not isinstance(value, dict):
            raise ValueError()
        return raw, value
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        raise _invalid() from None


def _parse_settings(control: Path, value: dict[str, Any]) -> PlanningBootstrapSettings:
    if set(value) != _REQUIRED_FIELDS or value.get("schema_version") != SCHEMA_VERSION:
        raise _invalid()
    roots_value = value.get("allowed_roots")
    if not isinstance(roots_value, list) or not roots_value:
        raise _invalid()
    try:
        paths = {
            name: _canonical_existing(value[name], directory=name == "state_directory")
            for name in _PATH_FIELDS
        }
        roots = tuple(_canonical_existing(raw, directory=True) for raw in roots_value)
    except (OSError, TypeError, ValueError, RunError):
        raise _invalid() from None
    if len(set(paths.values())) != len(paths) or len(set(roots)) != len(roots):
        raise _invalid()
    if paths["state_directory"] == control or control.is_relative_to(paths["state_directory"]):
        raise _invalid()
    state = paths["state_directory"]
    # ``runs.sqlite`` is not named by the small planning descriptor, but the
    # production authority necessarily opens it. Subject it to the same exact
    # spelling/no-alias/private-state checks as the explicitly named ledgers.
    # Otherwise a symlink below an otherwise valid state directory could make
    # a repository-controlled compatible Run store authoritative.
    _canonical_existing(str(state / "runs.sqlite"), directory=False)
    if any(not paths[name].is_relative_to(state) for name in _PATH_FIELDS[1:]):
        raise _invalid()
    if any(not path.is_file() for path in paths.values() if path != state):
        raise _invalid()
    if any(root == control or root == state for root in roots):
        # A broad allowed root is not a privacy proof, and must not become a
        # second spelling for one of the protected controller directories.
        raise _invalid()
    return PlanningBootstrapSettings(
        control,
        paths["state_directory"],
        paths["planning_execution_database"],
        paths["planning_admission_database"],
        paths["capacity_database"],
        paths["projects_database"],
        roots,
    )


def _registered_repositories(database: Path, roots: tuple[Path, ...]) -> tuple[Path, ...]:
    """Read only the known ProjectRegistry table and its saved repository roots."""
    try:
        require_schema(database, _PROJECT_SCHEMA)
        connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            rows = connection.execute("SELECT snapshot FROM projects ORDER BY rowid").fetchall()
        finally:
            connection.close()
    except (ExistingStoreError, OSError, sqlite3.Error):
        raise _invalid() from None
    result: list[Path] = []
    try:
        for row in rows:
            if len(row) != 1 or not isinstance(row[0], str):
                raise ValueError()
            snapshot = json.loads(row[0], object_pairs_hook=_unique_object)
            if (
                not isinstance(snapshot, dict)
                or snapshot.get("schema_version") != "karajan.project.v1"
            ):
                raise ValueError()
            repository = snapshot["repository"]
            root = _canonical_existing(repository["root"], directory=True)
            if not any(root.is_relative_to(allowed) for allowed in roots):
                raise ValueError()
            result.append(root)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, _DuplicateKey):
        raise _invalid() from None
    return tuple(result)


def _assert_repository_isolation(settings: PlanningBootstrapSettings, descriptor: Path) -> None:
    repositories = _registered_repositories(settings.projects_database, settings.allowed_roots)
    protected = (settings.control_directory, descriptor, settings.state_directory)
    if any(
        protected_path.is_relative_to(repository) or repository.is_relative_to(protected_path)
        for protected_path in protected
        for repository in repositories
    ):
        raise _invalid()


def read_planning_bootstrap(control_directory: Path) -> tuple[PlanningBootstrapSettings, str]:
    """Validate and read the fixed existing planning deployment descriptor."""
    try:
        if not isinstance(control_directory, Path):
            raise ValueError()
        control = _canonical_existing(str(control_directory), directory=True)
        descriptor = control / PLANNING_ADMISSION_BOOTSTRAP
        _private(control, directory=True)
        _plain(descriptor)
        _private(descriptor)
        raw, value = _read_descriptor(descriptor)
        settings = _parse_settings(control, value)
        _private(settings.state_directory, directory=True)
        for name in _PATH_FIELDS[1:]:
            _plain(getattr(settings, name))
        _assert_repository_isolation(settings, descriptor)
        return settings, hashlib.sha256(raw).hexdigest()
    except RunError as error:
        if str(error) in {
            "PLANNING_ADMISSION_BOOTSTRAP_INVALID",
            "PLANNING_ADMISSION_BOOTSTRAP_CHANGED",
        }:
            raise
        raise _invalid() from None
    except (OSError, TypeError, ValueError, sqlite3.Error):
        raise _invalid() from None


def assert_planning_bootstrap_current(
    control_directory: Path, expected_sha256: str
) -> PlanningBootstrapSettings:
    """Revalidate the descriptor and reject a changed current digest."""
    if not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
        raise RunError("PLANNING_ADMISSION_BOOTSTRAP_CHANGED")
    try:
        int(expected_sha256, 16)
    except ValueError:
        raise RunError("PLANNING_ADMISSION_BOOTSTRAP_CHANGED") from None
    settings, actual = read_planning_bootstrap(control_directory)
    if actual != expected_sha256.lower():
        raise RunError("PLANNING_ADMISSION_BOOTSTRAP_CHANGED")
    return settings


def migrate_commander_conversations(control_directory: Path) -> None:
    """Explicitly upgrade the trusted Run ledger's Commander projection.

    This is a normal bootstrap action, not part of any existing-only factory.
    It creates only the local Conversation projection tables introduced after
    older planning deployments and deterministically binds their historical
    Runs.  It neither reads qualification material nor creates a planning,
    admission, capacity, or provider effect.
    """
    from karajan.conversations import ConversationStore
    from karajan.projects import ProjectRegistry
    from karajan.runs import RunPlanner

    settings, bootstrap_sha256 = read_planning_bootstrap(control_directory)
    # These are deliberately normal constructors: RunPlanner owns the schema
    # upgrade and ConversationStore owns the one-time legacy binding.  The
    # trusted runtime factory remains existing-only and cannot provision this
    # state as a side effect of a read.
    projects = ProjectRegistry(settings.projects_database, settings.allowed_roots)
    planner = RunPlanner(settings.state_directory / "runs.sqlite", projects)
    ConversationStore(projects, planner)
    # Do not retain a migration result if the controller descriptor was
    # replaced during the bootstrap window.
    assert_planning_bootstrap_current(control_directory, bootstrap_sha256)


def provision_planning_bootstrap(
    control_directory: Path, state_directory: Path, allowed_roots: tuple[Path, ...]
) -> PlanningBootstrapSettings:
    """Explicitly provision empty controller stores and one protected descriptor.

    This setup action creates no Project, Run, Commander qualification, admission,
    execution, admission decision, output artifact, or Plan.  It does create
    the otherwise empty output ledger so a runtime factory never provisions
    one while assembling production authority. Runtime factories subsequently
    reopen only these fixed stores through ``read_planning_bootstrap``.
    """
    from karajan.capacity import CapacityStore
    from karajan.orchestration.planning_admission import PlanningAdmissionAuthority
    from karajan.orchestration.planning_execution import PlanningExecution
    from karajan.projects import ProjectRegistry
    from karajan.projects.demand import AttemptEstimateStore
    from karajan.runs import RunPlanner

    control, state = control_directory.absolute(), state_directory.absolute()
    roots = tuple(path.absolute() for path in allowed_roots)
    if not roots or control.exists() or state.exists() or control == state:
        raise _invalid()
    try:
        control.mkdir(mode=0o700)
        state.mkdir(mode=0o700)
        _private(control, directory=True)
        _private(state, directory=True)
        projects = ProjectRegistry(state / "projects.sqlite", roots)
        planner = RunPlanner(state / "runs.sqlite", projects)
        # Normal application composition always constructs ApprovedRunRouting,
        # whose estimate ledger is in the protected project store.  Provision
        # its empty schema here; no estimate is registered by this action.
        AttemptEstimateStore(planner)
        capacity = CapacityStore(state / "capacity.sqlite")
        # ``create_app`` also opens this normal routing assessment ledger with
        # all planning stores in existing-only mode.  Create its schema while
        # provisioning, without assessing a task or reserving capacity.
        from karajan.orchestration.routing import ApprovedRunRouting
        from karajan.projects.qualification import ProfileQualificationStore

        ApprovedRunRouting(planner, ProfileQualificationStore(projects), capacity)
        execution = PlanningExecution(state / "planning-execution.sqlite", planner)
        from karajan.orchestration.planning_transport import PlanningOutputStore

        output_ledger = state / "planning-output.sqlite"
        PlanningOutputStore(output_ledger, authority_kind="production")
        output_ledger.chmod(0o600)
        _private(output_ledger)

        class NoCommanderFacts:
            def read_commander(
                self, binding: dict[str, Any], *, scope: str, reader_version: str
            ) -> None:
                del binding, scope, reader_version
                return None

        PlanningAdmissionAuthority(
            state / "planning-admission.sqlite",
            execution.database,
            planner,
            capacity,
            NoCommanderFacts(),
            authority_kind="production",
        )
        settings = PlanningBootstrapSettings(
            control,
            state,
            execution.database,
            state / "planning-admission.sqlite",
            state / "capacity.sqlite",
            state / "projects.sqlite",
            roots,
        )
        descriptor = control / PLANNING_ADMISSION_BOOTSTRAP
        raw = (
            json.dumps(settings.document(), sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        fd = os.open(descriptor, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        _private(descriptor)
        read_planning_bootstrap(control)
        from karajan.orchestration.planning_snapshot import (
            provision_planning_repository_snapshots,
        )

        provision_planning_repository_snapshots(control)
        return settings
    except (OSError, RunError, ValueError):
        raise _invalid() from None
