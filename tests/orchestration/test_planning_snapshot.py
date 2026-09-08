import subprocess
from pathlib import Path

import pytest
from karajan.orchestration.planning_snapshot import PlanningRepositorySnapshotStore
from karajan.runs import RunError


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
