import json
import os
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import zlib
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


def _windows_open_process(pid: int) -> int:
    """Hold an exact child identity without sending it any control event."""
    import ctypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel.OpenProcess
    open_process.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    open_process.restype = ctypes.c_void_p
    handle = int(open_process(0x00100000 | 0x1000, False, pid) or 0)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    return handle


def _windows_process_exited(handle: int) -> bool:
    """Read-only zero-time wait on a held process handle."""
    import ctypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    wait = kernel.WaitForSingleObject
    wait.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    wait.restype = ctypes.c_uint32
    result = wait(ctypes.c_void_p(handle), 0)
    if result == 0:  # WAIT_OBJECT_0
        return True
    if result == 258:  # WAIT_TIMEOUT
        return False
    raise ctypes.WinError(ctypes.get_last_error())


def _windows_close_handle(handle: int) -> None:
    import ctypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    assert kernel.CloseHandle(ctypes.c_void_p(handle))


def test_git_metadata_read_rejects_oversized_regular_file_before_subprocess(tmp_path: Path) -> None:
    """The source metadata receiver has a fixed byte bound before decoding."""
    root = tmp_path / "repo"
    root.mkdir()
    pointer = root / ".git"
    # Deliberately only one byte above the boundary: this is an actual regular
    # metadata file, not a synthetic giant allocation.
    pointer.write_bytes(b"x" * (planning_snapshot._MAX_GIT_METADATA_BYTES + 1))
    with pytest.raises(RunError, match="^PLANNING_SNAPSHOT_BASE_UNAVAILABLE$"):
        PlanningRepositorySnapshotStore._git_objects(root)


@pytest.mark.skipif(os.name != "nt", reason="Windows executable search is platform-specific")
def test_git_reader_ignores_repository_local_git_exe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real repository-local executable cannot replace the pinned reader."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init")
    (root / "input.txt").write_text("base", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "-c", "user.name=x", "-c", "user.email=x@y.z", "commit", "-m", "base")
    base = _git(root, "rev-parse", "HEAD")
    marker = tmp_path / "repository-git-executed"
    source = tmp_path / "shadow.cs"
    source.write_text(
        "using System; using System.IO; public class Shadow { public static void Main() { "
        "File.WriteAllText(Environment.GetEnvironmentVariable("
        '"KARAJAN_GIT_SHADOW_MARKER"), "ran"); } }',
        encoding="utf-8",
    )
    shadow = root / "git.exe"
    compiler = Path(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe")
    if not compiler.exists():
        compiler = Path(r"C:\Windows\Microsoft.NET\Framework\v4.0.30319\csc.exe")
    subprocess.run([str(compiler), "/nologo", "/out:" + str(shadow), str(source)], check=True)
    monkeypatch.chdir(root)
    monkeypatch.setenv("PATH", str(root) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("KARAJAN_GIT_SHADOW_MARKER", str(marker))
    result = PlanningRepositorySnapshotStore._git(
        root, "sha1", "cat-file", "commit", base, limit=100_000
    )
    assert result.startswith(b"tree ")
    assert not marker.exists()


def _replace_loose_object_same_length(root: Path, oid: str, kind: str) -> tuple[bytes, int]:
    """Corrupt the actual loose object named by ``oid`` without a replace ref."""
    original = subprocess.run(
        ["git", "-C", str(root), "cat-file", kind, oid], check=True, capture_output=True
    ).stdout
    assert original
    changed = original[:-1] + (b"X" if original[-1:] != b"X" else b"Y")
    assert len(changed) == len(original)
    pathname = root / ".git" / "objects" / oid[:2] / oid[2:]
    assert pathname.is_file()
    original_mode = stat.S_IMODE(pathname.stat().st_mode)
    pathname.chmod(original_mode | stat.S_IWRITE)
    pathname.write_bytes(
        zlib.compress(kind.encode() + b" " + str(len(changed)).encode() + b"\0" + changed)
    )
    return original, original_mode


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


@pytest.mark.parametrize(
    ("kind", "object_expression"),
    [("blob", "HEAD:src/a.txt"), ("tree", "HEAD^{tree}"), ("commit", "HEAD")],
)
@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
def test_freeze_rejects_same_length_replaced_loose_git_object(
    tmp_path: Path, kind: str, object_expression: str, object_format: str
) -> None:
    """The registered base is object identity, not cat-file's unverified body."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "--object-format=" + object_format)
    (root / "src").mkdir()
    source = root / "src" / "a.txt"
    source.write_bytes(b"base\n")
    original_bytes, original_mode = source.read_bytes(), stat.S_IMODE(source.stat().st_mode)
    _git(root, "add", ".")
    _git(root, "-c", "user.name=x", "-c", "user.email=x@y.z", "commit", "-m", "base")
    base = _git(root, "rev-parse", "HEAD")
    oid = _git(root, "rev-parse", object_expression)
    original, object_mode = _replace_loose_object_same_length(root, oid, kind)
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
            "base_sha": base,
        },
    }
    store = PlanningRepositorySnapshotStore(tmp_path / "snapshots.sqlite")
    with pytest.raises(RunError, match="^PLANNING_SNAPSHOT_BASE_UNAVAILABLE$"):
        store.freeze(binding, run, project)
    assert source.read_bytes() == original_bytes
    assert stat.S_IMODE(source.stat().st_mode) == original_mode
    with sqlite3.connect(store.database) as db:
        assert db.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM files").fetchone()[0] == 0
    assert list(store.artifacts.iterdir()) == []

    pathname = root / ".git" / "objects" / oid[:2] / oid[2:]
    pathname.write_bytes(
        zlib.compress(kind.encode() + b" " + str(len(original)).encode() + b"\0" + original)
    )
    pathname.chmod(object_mode)
    assert store.freeze(binding, run, project)["base_sha"] == base
    assert store.read(binding)["content"] == {"src/a.txt": original_bytes}


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


@pytest.mark.skipif(os.name == "nt", reason="the owned process-group fixture is POSIX-only")
def test_bounded_git_reader_times_out_and_reaps_its_owned_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A no-output Git child cannot hold the receiving boundary past its deadline."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init")
    (root / "input.txt").write_text("base")
    _git(root, "add", ".")
    _git(root, "-c", "user.name=x", "-c", "user.email=x@y.z", "commit", "-m", "base")
    base = _git(root, "rev-parse", "HEAD")
    marker = tmp_path / "owned-child.pid"
    tools = tmp_path / "tools"
    tools.mkdir()
    fake_git = tools / "git"
    fake_git.write_text(
        "#!/usr/bin/env python3\n"
        "import subprocess\n"
        "import sys\n"
        "import time\n"
        "from pathlib import Path\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"Path({str(marker)!r}).write_text(str(child.pid))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    fake_git.chmod(0o755)
    monkeypatch.setattr(planning_snapshot, "_trusted_git_executable", lambda _root: fake_git)
    monkeypatch.setattr(planning_snapshot, "_GIT_TIMEOUT_SECONDS", 0.2)

    started = time.monotonic()
    with pytest.raises(RunError, match="^PLANNING_SNAPSHOT_GIT_UNAVAILABLE$"):
        PlanningRepositorySnapshotStore._git(root, "sha1", "cat-file", "commit", base, limit=100)
    assert time.monotonic() - started < 2
    child_pid = int(marker.read_text(encoding="ascii"))
    for _ in range(100):
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.01)
    else:
        pytest.fail("owned Git child remained alive after timeout cleanup")


def test_bounded_git_reader_reaps_child_after_its_leader_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Leader exit cannot prevent cleanup of its stdout-owning owned child."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init")
    (root / "input.txt").write_text("base")
    _git(root, "add", ".")
    _git(root, "-c", "user.name=x", "-c", "user.email=x@y.z", "commit", "-m", "base")
    base = _git(root, "rev-parse", "HEAD")
    marker = tmp_path / "owned-child.pid"
    tools = tmp_path / "tools"
    tools.mkdir()
    script = tools / "git.py"
    marker_value = (
        "f'{leader}:{child.pid}:{os.getpgid(child.pid)}'"
        if os.name == "posix"
        else "str(child.pid)"
    )
    script.write_text(
        (
            "import os\n"
            "import subprocess\n"
            "import sys\n"
            "import time\n"
            "from pathlib import Path\n"
            + "leader = os.getpid()\n"
            + "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(2)'])\n"
            + f"Path({str(marker)!r}).write_text({marker_value})\n"
        ),
        encoding="utf-8",
    )
    if os.name == "nt":
        fake_git = tools / "git.cmd"
        fake_git.write_text(
            '@echo off\r\n"' + sys.executable + '" "%~dp0git.py" %*\r\n',
            encoding="ascii",
        )
    else:
        fake_git = tools / "git"
        fake_git.write_text(
            "#!/usr/bin/env python3\n" + script.read_text(encoding="utf-8"), encoding="utf-8"
        )
        fake_git.chmod(0o755)
    # The fixture is deliberately injected at the controller-owned executable
    # resolver, not through PATH.  Production never discovers Git from PATH.
    monkeypatch.setattr(planning_snapshot, "_trusted_git_executable", lambda _root: fake_git)
    if os.name == "nt":
        # Keep the actual receiving-boundary Popen and its pinned absolute
        # fixture executable.  The wrapper observes rather than rewrites argv.
        original_popen = planning_snapshot.subprocess.Popen

        def fixture_popen(
            command: list[str], *args: object, **kwargs: object
        ) -> subprocess.Popen[bytes]:
            return original_popen(command, *args, **kwargs)

        monkeypatch.setattr(planning_snapshot.subprocess, "Popen", fixture_popen)
    monkeypatch.setattr(planning_snapshot, "_GIT_TIMEOUT_SECONDS", 0.2)
    reader_threads: list[threading.Thread] = []
    original_thread = planning_snapshot.threading.Thread

    def observe_thread(*args: object, **kwargs: object) -> threading.Thread:
        thread = original_thread(*args, **kwargs)
        if getattr(kwargs.get("target"), "__name__", None) == "read_output":
            reader_threads.append(thread)
        return thread

    monkeypatch.setattr(planning_snapshot.threading, "Thread", observe_thread)

    child_handle = 0
    live_control_handle = 0
    if os.name == "nt":
        # A deliberately live control proves the observer itself is not a
        # console-control operation which can mask failed cleanup.
        control = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(.5)"])
        try:
            live_control_handle = _windows_open_process(control.pid)
            assert not _windows_process_exited(live_control_handle)
        finally:
            if live_control_handle:
                _windows_close_handle(live_control_handle)
            control.wait(timeout=2)

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=1) as workers:
        future = workers.submit(
            PlanningRepositorySnapshotStore._git,
            root,
            "sha1",
            "cat-file",
            "commit",
            base,
            limit=100,
        )
        for _ in range(100):
            if marker.exists():
                break
            time.sleep(0.002)
        else:
            pytest.fail("Git fixture did not publish its owned child identity")
        marker_parts = marker.read_text(encoding="ascii").split(":")
        if os.name == "posix":
            leader_pid, child_pid, child_group = (int(part) for part in marker_parts)
            assert child_group == leader_pid
        else:
            child_pid = int(marker_parts[0])
            child_handle = _windows_open_process(child_pid)
        with pytest.raises(RunError, match="^PLANNING_SNAPSHOT_GIT_UNAVAILABLE$"):
            future.result(timeout=2)
    assert time.monotonic() - started < 1
    try:
        for _ in range(100):
            if os.name == "nt":
                if _windows_process_exited(child_handle):
                    break
            else:
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    break
            time.sleep(0.01)
        else:
            pytest.fail("owned child survived after its Git leader exited")
    finally:
        if child_handle:
            _windows_close_handle(child_handle)
    assert len(reader_threads) == 1
    assert not reader_threads[0].is_alive()


@pytest.mark.skipif(os.name == "nt", reason="the owned process-group fixture is POSIX-only")
def test_bounded_git_reader_rejects_over_limit_output_and_reaps_its_owned_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bounded receiving boundary rejects byte excess without waiting for EOF."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init")
    (root / "input.txt").write_text("base")
    _git(root, "add", ".")
    _git(root, "-c", "user.name=x", "-c", "user.email=x@y.z", "commit", "-m", "base")
    base = _git(root, "rev-parse", "HEAD")
    marker = tmp_path / "owned-child.pid"
    tools = tmp_path / "tools"
    tools.mkdir()
    fake_git = tools / "git"
    fake_git.write_text(
        "#!/usr/bin/env python3\n"
        "import subprocess\n"
        "import sys\n"
        "import time\n"
        "from pathlib import Path\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"Path({str(marker)!r}).write_text(str(child.pid))\n"
        "sys.stdout.buffer.write(b'x' * 101)\n"
        "sys.stdout.flush()\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    fake_git.chmod(0o755)
    monkeypatch.setattr(planning_snapshot, "_trusted_git_executable", lambda _root: fake_git)

    started = time.monotonic()
    with pytest.raises(RunError, match="^PLANNING_SNAPSHOT_BASE_UNAVAILABLE$"):
        PlanningRepositorySnapshotStore._git(root, "sha1", "cat-file", "commit", base, limit=100)
    assert time.monotonic() - started < 2
    child_pid = int(marker.read_text(encoding="ascii"))
    for _ in range(100):
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.01)
    else:
        pytest.fail("owned Git child remained alive after output-limit cleanup")


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
    legacy_env = {key: os.environ[key] for key in ("PATH", "TEMP", "TMP") if key in os.environ}
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


def test_concurrent_publish_uses_atomic_no_replace_without_live_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Concurrent publishers race only at the no-replace atomic commit."""
    if os.name == "nt":
        pytest.skip("Windows uses a no-replace write-through rename, not link overlap")
    content = b"original bytes"
    sha = __import__("hashlib").sha256(content).hexdigest()
    database = tmp_path / "snapshots.sqlite"
    first = PlanningRepositorySnapshotStore(database)
    second = PlanningRepositorySnapshotStore(database)
    entered = Event()
    release = Event()
    original_move = planning_snapshot._move_file_no_replace_posix
    calls = 0

    def pause_before_atomic_move(source: str, target: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            assert release.wait(timeout=5)
        original_move(source, target)

    monkeypatch.setattr(planning_snapshot, "_move_file_no_replace_posix", pause_before_atomic_move)
    with ThreadPoolExecutor(max_workers=2) as workers:
        published = workers.submit(first._publish, sha, content)
        assert entered.wait(timeout=5)
        recovered = workers.submit(second._publish, sha, content)
        # Neither producer has made a target alias before its atomic rename.
        assert not (database.parent / "planning-repository-snapshot-blobs" / sha).exists()
        release.set()
        published.result(timeout=5)
        recovered.result(timeout=5)
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
