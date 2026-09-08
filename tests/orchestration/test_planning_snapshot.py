import json
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import karajan.orchestration.planning_snapshot as planning_snapshot
import pytest
from karajan.orchestration.planning_snapshot import PlanningRepositorySnapshotStore
from karajan.runs import RunError
from karajan.runs.planning import digest


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def test_base_tree_snapshot_is_immutable_and_directory_paths_are_expanded(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init")
    (root / "src").mkdir()
    (root / "src" / "a.txt").write_text("base")
    _git(root, "add", ".")
    _git(root, "-c", "user.name=x", "-c", "user.email=x@y.z", "commit", "-m", "base")
    base = _git(root, "rev-parse", "HEAD")
    binding = {
        "execution_id": "execution",
        "run_id": "run",
        "intent_id": "intent",
        "requirement_sha256": "a" * 64,
        "authorization_ceiling_sha256": "c" * 64,
    }
    run = {
        "project_id": "project",
        "configuration_snapshot": {"project_revision": 1},
        "authorization_ceiling": {"read_paths": ["src"]},
    }
    project = {
        "id": "project",
        "revision": 1,
        "repository": {"root": str(root.resolve()), "identity_sha256": "b" * 64, "base_sha": base},
    }
    store = PlanningRepositorySnapshotStore(tmp_path / "snapshots.sqlite")
    first = store.freeze(binding, run, project)
    (root / "src" / "a.txt").write_text("changed")
    assert store.freeze(binding, run, project) == first
    assert store.read(binding)["content"] == {"src/a.txt": b"base"}
    with sqlite3.connect(tmp_path / "snapshots.sqlite") as db:
        manifest = json.loads(db.execute("SELECT data FROM snapshots").fetchone()[0])
        manifest["files"][0]["mode"] = "120000"
        manifest["snapshot_sha256"] = digest(
            {key: value for key, value in manifest.items() if key != "snapshot_sha256"}
        )
        db.execute("UPDATE snapshots SET data=?", (json.dumps(manifest),))
    with pytest.raises(RunError, match="^PLANNING_REPOSITORY_SNAPSHOT_CHANGED$"):
        store.read(binding)


def test_unapproved_or_symlink_base_entry_is_rejected(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init")
    (root / "safe").write_text("ok")
    try:
        (root / "link").symlink_to("safe")
    except OSError:
        pytest.skip("Windows test account cannot create a symlink")
    _git(root, "add", ".")
    _git(root, "-c", "user.name=x", "-c", "user.email=x@y.z", "commit", "-m", "base")
    binding = {
        "execution_id": "execution",
        "run_id": "run",
        "intent_id": "intent",
        "requirement_sha256": "a" * 64,
        "authorization_ceiling_sha256": "c" * 64,
    }
    run = {
        "project_id": "project",
        "configuration_snapshot": {"project_revision": 1},
        "authorization_ceiling": {"read_paths": ["link"]},
    }
    project = {
        "id": "project",
        "revision": 1,
        "repository": {
            "root": str(root.resolve()),
            "identity_sha256": "b" * 64,
            "base_sha": _git(root, "rev-parse", "HEAD"),
        },
    }
    with pytest.raises(RunError, match="PLANNING_SNAPSHOT_ENTRY_UNSUPPORTED"):
        PlanningRepositorySnapshotStore(tmp_path / "snapshots.sqlite").freeze(binding, run, project)


def test_base_tree_read_disables_local_replace_refs_and_git_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init")
    (root / "src").mkdir()
    source = root / "src" / "a.txt"
    source.write_text("registered")
    _git(root, "add", ".")
    _git(root, "-c", "user.name=x", "-c", "user.email=x@y.z", "commit", "-m", "base")
    base = _git(root, "rev-parse", "HEAD")
    source.write_text("replacement")
    _git(root, "-c", "user.name=x", "-c", "user.email=x@y.z", "commit", "-am", "replacement")
    replacement = _git(root, "rev-parse", "HEAD")
    _git(root, "replace", base, replacement)
    attacker = tmp_path / "attacker"
    attacker.mkdir()
    _git(attacker, "init")
    monkeypatch.setenv("GIT_DIR", str(attacker / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(attacker))
    monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", str(attacker / ".git" / "objects"))
    binding = {
        "execution_id": "execution",
        "run_id": "run",
        "intent_id": "intent",
        "requirement_sha256": "a" * 64,
        "authorization_ceiling_sha256": "c" * 64,
    }
    run = {
        "project_id": "project",
        "configuration_snapshot": {"project_revision": 1},
        "authorization_ceiling": {"read_paths": ["src"]},
    }
    project = {
        "id": "project",
        "revision": 1,
        "repository": {"root": str(root.resolve()), "identity_sha256": "b" * 64, "base_sha": base},
    }
    PlanningRepositorySnapshotStore(tmp_path / "snapshots.sqlite").freeze(binding, run, project)
    assert PlanningRepositorySnapshotStore(tmp_path / "snapshots.sqlite").read(binding)[
        "content"
    ] == {"src/a.txt": b"registered"}


def test_repository_root_alias_is_rejected(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init")
    (root / "input.txt").write_text("base")
    _git(root, "add", ".")
    _git(root, "-c", "user.name=x", "-c", "user.email=x@y.z", "commit", "-m", "base")
    alias = tmp_path / "repo-alias"
    try:
        alias.symlink_to(root, target_is_directory=True)
    except OSError:
        pytest.skip("test account cannot create a repository-root symlink")
    binding = {
        "execution_id": "execution",
        "run_id": "run",
        "intent_id": "intent",
        "requirement_sha256": "a" * 64,
        "authorization_ceiling_sha256": "c" * 64,
    }
    run = {
        "project_id": "project",
        "configuration_snapshot": {"project_revision": 1},
        "authorization_ceiling": {"read_paths": ["input.txt"]},
    }
    project = {
        "id": "project",
        "revision": 1,
        "repository": {
            "root": str(alias),
            "identity_sha256": "b" * 64,
            "base_sha": _git(root, "rev-parse", "HEAD"),
        },
    }
    with pytest.raises(RunError, match="^PLANNING_REPOSITORY_SOURCE_INVALID$"):
        PlanningRepositorySnapshotStore(tmp_path / "snapshots.sqlite").freeze(binding, run, project)


def test_limits_and_malformed_persisted_manifest_reject_without_partial_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init")
    (root / "input").mkdir()
    (root / "input" / "one.txt").write_bytes(b"one")
    (root / "input" / "two.txt").write_bytes(b"two")
    _git(root, "add", ".")
    _git(root, "-c", "user.name=x", "-c", "user.email=x@y.z", "commit", "-m", "base")
    binding = {
        "execution_id": "execution",
        "run_id": "run",
        "intent_id": "intent",
        "requirement_sha256": "a" * 64,
        "authorization_ceiling_sha256": "c" * 64,
    }
    run = {
        "project_id": "project",
        "configuration_snapshot": {"project_revision": 1},
        "authorization_ceiling": {"read_paths": ["input"]},
    }
    project = {
        "id": "project",
        "revision": 1,
        "repository": {
            "root": str(root.resolve()),
            "identity_sha256": "b" * 64,
            "base_sha": _git(root, "rev-parse", "HEAD"),
        },
    }
    database = tmp_path / "snapshots.sqlite"
    monkeypatch.setattr(planning_snapshot, "_MAX_FILES", 1)
    store = PlanningRepositorySnapshotStore(database)
    with pytest.raises(RunError, match="^PLANNING_SNAPSHOT_LIMIT_EXCEEDED$"):
        store.freeze(binding, run, project)
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM files").fetchone()[0] == 0

    monkeypatch.setattr(planning_snapshot, "_MAX_FILES", 2)
    monkeypatch.setattr(planning_snapshot, "_MAX_BYTES", 1)
    with pytest.raises(RunError, match="^PLANNING_SNAPSHOT_LIMIT_EXCEEDED$"):
        store.freeze(binding, run, project)
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM files").fetchone()[0] == 0

    monkeypatch.setattr(planning_snapshot, "_MAX_BYTES", 8_000_000)
    store.freeze(binding, run, project)
    with sqlite3.connect(database) as db:
        db.execute("UPDATE snapshots SET data=?", ("{not json",))
    with pytest.raises(RunError, match="^PLANNING_REPOSITORY_SNAPSHOT_CHANGED$"):
        store.read(binding)


def test_real_store_instances_concurrently_preserve_one_original_snapshot(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init")
    (root / "input.txt").write_bytes(b"original bytes")
    _git(root, "add", ".")
    _git(root, "-c", "user.name=x", "-c", "user.email=x@y.z", "commit", "-m", "base")
    binding = {
        "execution_id": "execution",
        "run_id": "run",
        "intent_id": "intent",
        "requirement_sha256": "a" * 64,
        "authorization_ceiling_sha256": "c" * 64,
    }
    run = {
        "project_id": "project",
        "configuration_snapshot": {"project_revision": 1},
        "authorization_ceiling": {"read_paths": ["input.txt"]},
    }
    project = {
        "id": "project",
        "revision": 1,
        "repository": {
            "root": str(root.resolve()),
            "identity_sha256": "b" * 64,
            "base_sha": _git(root, "rev-parse", "HEAD"),
        },
    }
    database = tmp_path / "snapshots.sqlite"
    stores = [PlanningRepositorySnapshotStore(database), PlanningRepositorySnapshotStore(database)]
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda store: store.freeze(binding, run, project), stores))
    assert results[0] == results[1]
    assert PlanningRepositorySnapshotStore(database).read(binding)["content"] == {
        "input.txt": b"original bytes"
    }
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM files").fetchone()[0] == 1
