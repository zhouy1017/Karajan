import json
import os
import sqlite3
import stat
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

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
        original = json.loads(json.dumps(manifest))
        # The manifest hash is not the authority for its source identity: a
        # tamperer can recompute it, but cannot rewrite the independent sealed
        # source binding stored alongside the manifest.
        for field in ("repository_identity_sha256", "base_sha", "read_paths_sha256"):
            manifest = json.loads(json.dumps(original))
            manifest[field] = None
            manifest["snapshot_sha256"] = digest(
                {key: value for key, value in manifest.items() if key != "snapshot_sha256"}
            )
            db.execute("UPDATE snapshots SET data=?", (json.dumps(manifest),))
            db.commit()
            with pytest.raises(RunError, match="^PLANNING_REPOSITORY_SNAPSHOT_CHANGED$"):
                store.read(binding)
        db.execute("UPDATE snapshots SET data=?", (json.dumps(original),))
        db.commit()
        manifest = original
        manifest["files"][0]["mode"] = "120000"
        manifest["snapshot_sha256"] = digest(
            {key: value for key, value in manifest.items() if key != "snapshot_sha256"}
        )
        db.execute("UPDATE snapshots SET data=?", (json.dumps(manifest),))
        db.commit()
    with pytest.raises(RunError, match="^PLANNING_REPOSITORY_SNAPSHOT_CHANGED$"):
        store.read(binding)
    with sqlite3.connect(tmp_path / "snapshots.sqlite") as db:
        db.execute("UPDATE snapshots SET data=?", (json.dumps(original),))
        manifest = json.loads(json.dumps(original))
        manifest["files"][0]["mode"] = "100755"
        manifest["snapshot_sha256"] = digest(
            {key: value for key, value in manifest.items() if key != "snapshot_sha256"}
        )
        db.execute("UPDATE snapshots SET data=?", (json.dumps(manifest),))
        db.commit()
    # A recomputed self-hash cannot replace the independent full-manifest seal.
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


@pytest.mark.skipif(os.name == "nt", reason="Windows cannot create a backslash filename")
def test_selected_git_path_invalid_to_snapshot_protocol_rejects_before_publication(
    tmp_path: Path,
):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init")
    (root / "src").mkdir()
    source = root / "src" / "a\\b.txt"
    source.write_bytes(b"original bytes")
    original_bytes, original_mode = source.read_bytes(), stat.S_IMODE(source.stat().st_mode)
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
        "authorization_ceiling": {"read_paths": ["src"]},
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
    store = PlanningRepositorySnapshotStore(tmp_path / "snapshots.sqlite")
    with pytest.raises(RunError, match="^PLANNING_SNAPSHOT_PATHS_INVALID$"):
        store.freeze(binding, run, project)
    assert source.read_bytes() == original_bytes
    assert stat.S_IMODE(source.stat().st_mode) == original_mode
    with sqlite3.connect(store.database) as db:
        assert db.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM files").fetchone()[0] == 0
    assert list(store.artifacts.iterdir()) == []


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


@pytest.mark.skipif(os.name == "nt", reason="the hostile ext transport fixture needs POSIX touch")
def test_missing_promisor_blob_cannot_run_repository_configured_helper(
    tmp_path: Path,
) -> None:
    """A source-repository transport may never execute in the controller reader."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init")
    (root / "src").mkdir()
    source = root / "src" / "input.txt"
    source.write_bytes(b"only in the registered base\n")
    _git(root, "add", ".")
    _git(root, "-c", "user.name=x", "-c", "user.email=x@y.z", "commit", "-m", "base")
    base = _git(root, "rev-parse", "HEAD")
    blob = _git(root, "rev-parse", base + ":src/input.txt")
    marker = tmp_path / "repository-config-helper-ran"
    # This is a deliberately local-only hostile repository fixture.  If Git
    # reads this config while trying to lazily fetch the missing blob, ext::
    # starts a local helper and creates the marker; no network endpoint or credentials are
    # involved in the feedback loop.
    _git(root, "config", "protocol.ext.allow", "always")
    _git(root, "config", "remote.origin.url", "ext::touch " + str(marker))
    _git(root, "config", "remote.origin.promisor", "true")
    _git(root, "config", "remote.origin.partialclonefilter", "blob:none")
    _git(root, "config", "extensions.partialClone", "origin")
    (root / ".git" / "objects" / blob[:2] / blob[2:]).unlink()
    legacy_env = {
        key: os.environ[key]
        for key in ("PATH", "TEMP", "TMP")
        if key in os.environ
    }
    legacy_env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    # This reproduces the former receiving boundary exactly enough to prove
    # that the fixture really does cause an OS child/helper effect.  It is a
    # local ``touch`` only; the fixed store below must leave no second marker.
    subprocess.run(
        [
            "git",
            "--no-replace-objects",
            "-C",
            str(root),
            "-c",
            "core.hooksPath=" + os.devnull,
            "-c",
            "core.fsmonitor=false",
            "-c",
            "credential.helper=",
            "-c",
            "protocol.allow=never",
            "cat-file",
            "-s",
            base + ":src/input.txt",
        ],
        capture_output=True,
        timeout=10,
        env=legacy_env,
        check=False,
    )
    assert marker.is_file()
    marker.unlink()
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
    with pytest.raises(RunError, match="^PLANNING_SNAPSHOT_BASE_UNAVAILABLE$"):
        PlanningRepositorySnapshotStore(tmp_path / "snapshots.sqlite").freeze(binding, run, project)
    assert not marker.exists()


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

    # Directly exercise caller-unrepresentable path aliases, unmatched approval,
    # and a registered corrupt base.  None may leave a partial public manifest;
    # the repository itself is never touched by a base-tree snapshot attempt.
    original = root / "input" / "one.txt"
    original_bytes, original_mode = original.read_bytes(), stat.S_IMODE(original.stat().st_mode)
    for approved, expected in ((["input/../one.txt"], "PATHS_INVALID"), (["secret"], "PATH_EMPTY")):
        bad_run = {**run, "authorization_ceiling": {"read_paths": approved}}
        with pytest.raises(RunError, match=expected):
            store.freeze(binding, bad_run, project)
    corrupt = {**project, "repository": {**project["repository"], "base_sha": "f" * 40}}
    with pytest.raises(RunError, match="BASE_UNAVAILABLE"):
        store.freeze(binding, run, corrupt)
    assert original.read_bytes() == original_bytes
    assert stat.S_IMODE(original.stat().st_mode) == original_mode
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
        artifact = (
            database.parent
            / "planning-repository-snapshot-blobs"
            / db.execute("SELECT sha256 FROM files").fetchone()[0]
        )
        # A sparse replacement can advertise an enormous regular-file size.
        # The reader must reject from lstat, before allocating its body.
        with artifact.open("wb") as stream:
            stream.truncate(16_000_000)
        before = (
            db.execute("SELECT count(*) FROM snapshots").fetchone()[0],
            db.execute("SELECT count(*) FROM files").fetchone()[0],
        )
        with pytest.raises(RunError, match="^PLANNING_REPOSITORY_SNAPSHOT_CHANGED$"):
            store.read(binding)
        assert before == (
            db.execute("SELECT count(*) FROM snapshots").fetchone()[0],
            db.execute("SELECT count(*) FROM files").fetchone()[0],
        )
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


def test_concurrent_publish_waits_only_for_its_temporary_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A second publisher sees linkcount two, then recovers the first bytes."""
    if os.name == "nt":
        pytest.skip("Windows uses a no-replace write-through rename, not link overlap")
    content = b"original bytes"
    sha = __import__("hashlib").sha256(content).hexdigest()
    database = tmp_path / "snapshots.sqlite"
    first = PlanningRepositorySnapshotStore(database)
    second = PlanningRepositorySnapshotStore(database)
    linked = Event()
    saw_overlap = Event()
    release = Event()
    original_link = planning_snapshot.os.link
    original_sleep = planning_snapshot.time.sleep
    calls = 0

    def pause_after_link(source: str, target: str) -> None:
        nonlocal calls
        original_link(source, target)
        calls += 1
        if calls == 1:
            linked.set()
            assert release.wait(timeout=5)

    def release_on_overlap(seconds: float) -> None:
        saw_overlap.set()
        release.set()
        original_sleep(seconds)

    monkeypatch.setattr(planning_snapshot.os, "link", pause_after_link)
    monkeypatch.setattr(planning_snapshot.time, "sleep", release_on_overlap)
    with ThreadPoolExecutor(max_workers=2) as workers:
        published = workers.submit(first._publish, sha, content)
        assert linked.wait(timeout=5)
        recovered = workers.submit(second._publish, sha, content)
        published.result(timeout=5)
        recovered.result(timeout=5)
    assert saw_overlap.is_set()
    target = database.parent / "planning-repository-snapshot-blobs" / sha
    assert target.read_bytes() == content
    assert target.stat().st_nlink == 1


def test_publish_rejects_persistent_alias_as_a_stable_snapshot_failure(tmp_path: Path):
    content = b"original bytes"
    sha = __import__("hashlib").sha256(content).hexdigest()
    store = PlanningRepositorySnapshotStore(tmp_path / "snapshots.sqlite")
    store._publish(sha, content)
    target = store.artifacts / sha
    os.link(target, store.artifacts / "persistent-alias")
    with pytest.raises(RunError, match="^PLANNING_REPOSITORY_SNAPSHOT_CHANGED$"):
        store._publish(sha, content)


def test_freeze_sync_failure_leaves_zero_snapshot_references(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
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
    store = PlanningRepositorySnapshotStore(tmp_path / "snapshots.sqlite")
    original_fsync = planning_snapshot.os.fsync
    calls = 0

    def fail_artifact_sync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected artifact sync failure")
        original_fsync(descriptor)

    monkeypatch.setattr(planning_snapshot.os, "fsync", fail_artifact_sync)
    with pytest.raises(RunError, match="^PLANNING_REPOSITORY_SNAPSHOT_CHANGED$"):
        store.freeze(binding, run, project)
    assert calls == 2
    with sqlite3.connect(store.database) as db:
        assert db.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM files").fetchone()[0] == 0
