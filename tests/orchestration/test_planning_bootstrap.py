"""C-level checks for the protected planning bootstrap boundary."""

import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from karajan.orchestration.planning_bootstrap import (
    PLANNING_ADMISSION_BOOTSTRAP,
    assert_planning_bootstrap_current,
    migrate_commander_conversations,
    provision_planning_bootstrap,
    read_planning_bootstrap,
)
from karajan.projects import ProjectRegistry
from karajan.runs import RunError

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="Linux mode evidence is platform-specific"
)


@pytest.fixture
def deployment(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    allowed = tmp_path / "repositories"
    repository = allowed / "project"
    repository.mkdir(parents=True)
    (repository / ".git").mkdir()
    control = tmp_path / "control"
    state = tmp_path / "state"
    control.mkdir(mode=0o700)
    state.mkdir(mode=0o700)
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(repository)],
        check=True,
        capture_output=True,
    )
    (repository / "README").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "README"], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
        capture_output=True,
    )
    projects = ProjectRegistry(state / "projects.sqlite", [allowed])
    projects.create(
        {
            "name": "bootstrap fixture",
            "repository_path": str(repository),
            "base_ref": "main",
            "target_branch": "main",
            "allowed_target_branches": ["main"],
        },
        command_key="create-project",
        principal="owner",
    )
    # These stores are only existence probes here. Their owners open them later.
    # ``runs.sqlite`` is likewise mandatory: the planning bootstrap now rejects
    # a missing or aliased Run ledger before its factory can reopen it.
    for name in (
        "runs.sqlite",
        "planning-execution.sqlite",
        "planning-admission.sqlite",
        "capacity.sqlite",
    ):
        sqlite3.connect(state / name).close()
    for path in state.glob("*.sqlite"):
        path.chmod(0o600)
    paths = {
        "state_directory": str(state),
        "planning_execution_database": str(state / "planning-execution.sqlite"),
        "planning_admission_database": str(state / "planning-admission.sqlite"),
        "capacity_database": str(state / "capacity.sqlite"),
        "projects_database": str(state / "projects.sqlite"),
    }
    return control, {**paths, "allowed_root": str(allowed)}


def _write(control: Path, fields: dict[str, object], *, raw: bytes | None = None) -> bytes:
    path = control / PLANNING_ADMISSION_BOOTSTRAP
    data = raw if raw is not None else json.dumps(fields, separators=(",", ":")).encode()
    path.write_bytes(data)
    path.chmod(0o600)
    return data


def _document(case: tuple[Path, dict[str, str]]) -> dict[str, object]:
    control, values = case
    return {
        "schema_version": "karajan.planning-admission-bootstrap.v1",
        "state_directory": values["state_directory"],
        "planning_execution_database": values["planning_execution_database"],
        "planning_admission_database": values["planning_admission_database"],
        "capacity_database": values["capacity_database"],
        "projects_database": values["projects_database"],
        "allowed_roots": [values["allowed_root"]],
    }


def test_reads_existing_private_deployment_and_rechecks_digest(deployment) -> None:
    control, _ = deployment
    raw = _write(control, _document(deployment))
    settings, digest = read_planning_bootstrap(control)

    assert settings.control_directory == control
    assert settings.projects_database.exists()
    assert digest == hashlib.sha256(raw).hexdigest()
    assert assert_planning_bootstrap_current(control, digest) == settings


def test_provisioning_creates_empty_normal_routing_dependencies(tmp_path: Path) -> None:
    """A protected normal app may reopen its routing readers without evidence."""
    from karajan.capacity import CapacityStore
    from karajan.orchestration.planning_snapshot import (
        PlanningRepositorySnapshotStore,
        snapshot_database,
    )
    from karajan.orchestration.routing import ApprovedRunRouting
    from karajan.projects.qualification import ProfileQualificationStore
    from karajan.runs import RunPlanner

    roots = tmp_path / "repositories"
    roots.mkdir()
    settings = provision_planning_bootstrap(
        tmp_path / "control", tmp_path / "state", (roots,)
    )
    projects = ProjectRegistry(
        settings.projects_database, settings.allowed_roots, existing_only=True
    )
    planner = RunPlanner(settings.state_directory / "runs.sqlite", projects, existing_only=True)
    capacity = CapacityStore(settings.capacity_database, existing_only=True)

    routing = ApprovedRunRouting(planner, ProfileQualificationStore(projects), capacity)
    snapshots = PlanningRepositorySnapshotStore(
        snapshot_database(settings.control_directory),
        existing_only=True,
        private_root=settings.state_directory,
    )

    assert routing.estimates.planner is planner
    assert snapshots.database == snapshot_database(settings.control_directory)


def test_explicit_conversation_migration_upgrades_historical_runs_without_reader_writes(
    deployment: tuple[Path, dict[str, str]],
) -> None:
    """Only normal bootstrap may add the #159 Run conversation projection."""
    from karajan.conversations import ConversationStore
    from karajan.runs import RunPlanner
    from karajan.storage import ExistingStoreError

    control, _ = deployment
    _write(control, _document(deployment))
    settings, _ = read_planning_bootstrap(control)
    projects = ProjectRegistry(
        settings.projects_database, settings.allowed_roots, existing_only=True
    )
    historic = {
        "id": "legacy_run",
        "owner": "owner",
        "project_id": projects.list()[0]["id"],
    }
    # Simulate the exact pre-#159 ledger: initialize the ordinary Run schema,
    # then remove only the new conversation projection.
    RunPlanner(
        settings.state_directory / "runs.sqlite",
        ProjectRegistry(settings.projects_database, settings.allowed_roots),
    )
    with sqlite3.connect(settings.state_directory / "runs.sqlite") as database:
        database.execute("PRAGMA foreign_keys=OFF")
        for table in (
            "conversation_events",
            "conversation_commands",
            "conversation_task_drafts",
            "conversation_drafts",
            "conversation_messages",
            "conversation_run_bindings",
            "conversation_migration_blockers",
            "commander_conversations",
        ):
            database.execute(f"DROP TABLE {table}")
        database.execute("INSERT INTO runs VALUES (?, ?)", (historic["id"], json.dumps(historic)))

    projects = ProjectRegistry(
        settings.projects_database, settings.allowed_roots, existing_only=True
    )
    planner = RunPlanner(settings.state_directory / "runs.sqlite", projects, existing_only=True)
    with pytest.raises(ExistingStoreError, match="^EXISTING_STORE_SCHEMA_UNSUPPORTED$"):
        ConversationStore(projects, planner)
    with sqlite3.connect(settings.state_directory / "runs.sqlite") as database:
        assert database.execute(
            "SELECT snapshot FROM runs WHERE id=?", (historic["id"],)
        ).fetchone()[0] == json.dumps(historic)

    migrate_commander_conversations(settings.control_directory)

    readonly_projects = ProjectRegistry(
        settings.projects_database, settings.allowed_roots, existing_only=True
    )
    readonly_planner = RunPlanner(
        settings.state_directory / "runs.sqlite", readonly_projects, existing_only=True
    )
    conversations = ConversationStore(readonly_projects, readonly_planner)
    conversation_id = conversations.bound_conversation(historic["id"], historic["project_id"])
    message = conversations.message(
        conversation_id,
        {"client_message_id": "migration_check", "content": "recover this conversation"},
        principal="owner",
        key="migration_message",
    )
    draft = conversations.draft(
        conversation_id,
        {"content": "saved after strict reopen"},
        principal="owner",
        key="migration_draft",
        revision=1,
    )
    assert message["content"] == "recover this conversation"
    assert draft["content"] == "saved after strict reopen"


def test_explicit_invalid_historical_conversation_is_a_recovery_blocker(
    deployment: tuple[Path, dict[str, str]],
) -> None:
    """Migration must not turn an explicit bad reference into a legacy identity."""
    from karajan.conversations import ConversationStore
    from karajan.runs import RunPlanner

    control, _ = deployment
    _write(control, _document(deployment))
    settings, _ = read_planning_bootstrap(control)
    projects = ProjectRegistry(settings.projects_database, settings.allowed_roots)
    project_id = projects.list()[0]["id"]
    planner = RunPlanner(settings.state_directory / "runs.sqlite", projects)
    historic = {
        "id": "invalid_explicit",
        "owner": "owner",
        "project_id": project_id,
        "conversation_id": "does-not-exist",
    }
    with planner._transaction() as db:
        db.execute("INSERT INTO runs VALUES (?, ?)", (historic["id"], json.dumps(historic)))
    conversations = ConversationStore(projects, planner)

    assert conversations.run_binding(historic["id"], project_id) == {
        "conversation_id": None,
        "recovery_blocker": "CONVERSATION_NOT_FOUND",
    }
    with sqlite3.connect(settings.state_directory / "runs.sqlite") as database:
        assert database.execute(
            "SELECT COUNT(*) FROM commander_conversations WHERE id='does-not-exist'"
        ).fetchone()[0] == 0
        assert database.execute(
            "SELECT COUNT(*) FROM conversation_run_bindings WHERE run_id=?", (historic["id"],)
        ).fetchone()[0] == 0


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: {**value, "unexpected": True},
        lambda value: {**value, "state_directory": "relative-state"},
        lambda value: {**value, "allowed_roots": []},
    ],
)
def test_descriptor_shape_and_paths_are_strict(deployment, mutation) -> None:
    control, _ = deployment
    _write(control, mutation(_document(deployment)))
    with pytest.raises(RunError, match="^PLANNING_ADMISSION_BOOTSTRAP_INVALID$"):
        read_planning_bootstrap(control)


def test_duplicate_keys_and_oversized_descriptor_are_rejected(deployment) -> None:
    control, _ = deployment
    value = _document(deployment)
    duplicate = json.dumps(value, separators=(",", ":")).replace(
        '"schema_version":', '"schema_version":"duplicate","schema_version":', 1
    )
    _write(control, value, raw=duplicate.encode())
    with pytest.raises(RunError, match="^PLANNING_ADMISSION_BOOTSTRAP_INVALID$"):
        read_planning_bootstrap(control)
    _write(control, value, raw=b"{" + b"x" * (32 * 1024) + b"}")
    with pytest.raises(RunError, match="^PLANNING_ADMISSION_BOOTSTRAP_INVALID$"):
        read_planning_bootstrap(control)


def test_repository_containing_control_plane_is_rejected_without_writes(deployment) -> None:
    control, values = deployment
    repository = Path(values["allowed_root"]) / "project"
    inside = repository / "control"
    inside.mkdir()
    inside.chmod(0o700)
    state = Path(values["state_directory"])
    document = _document(deployment)
    document["state_directory"] = str(state)
    _write(inside, document)
    before = (repository / ".git").stat()
    with pytest.raises(RunError, match="^PLANNING_ADMISSION_BOOTSTRAP_INVALID$"):
        read_planning_bootstrap(inside)
    after = (repository / ".git").stat()
    assert (before.st_mode, before.st_size) == (after.st_mode, after.st_size)
    assert control.exists()


def test_permission_link_missing_store_and_current_digest_fail_closed(deployment) -> None:
    control, values = deployment
    _write(control, _document(deployment))
    digest = read_planning_bootstrap(control)[1]
    control.chmod(0o755)
    with pytest.raises(RunError, match="^PLANNING_ADMISSION_BOOTSTRAP_INVALID$"):
        read_planning_bootstrap(control)
    control.chmod(0o700)
    replacement = control / PLANNING_ADMISSION_BOOTSTRAP
    saved = control.parent / "saved-bootstrap.json"
    replacement.replace(saved)
    replacement.symlink_to(saved)
    with pytest.raises(RunError, match="^PLANNING_ADMISSION_BOOTSTRAP_INVALID$"):
        read_planning_bootstrap(control)
    replacement.unlink()
    saved.replace(replacement)
    replacement.write_bytes(replacement.read_bytes() + b" ")
    replacement.chmod(0o600)
    with pytest.raises(RunError, match="^PLANNING_ADMISSION_BOOTSTRAP_CHANGED$"):
        assert_planning_bootstrap_current(control, digest)
    document = _document(deployment)
    document["capacity_database"] = str(Path(values["state_directory"]) / "missing.sqlite")
    _write(control, document)
    with pytest.raises(RunError, match="^PLANNING_ADMISSION_BOOTSTRAP_INVALID$"):
        read_planning_bootstrap(control)
    assert not Path(document["capacity_database"]).exists()
    Path(values["state_directory"]).joinpath("runs.sqlite").unlink()
    _write(control, _document(deployment))
    with pytest.raises(RunError, match="^PLANNING_ADMISSION_BOOTSTRAP_INVALID$"):
        read_planning_bootstrap(control)
