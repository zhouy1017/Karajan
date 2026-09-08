"""Private immutable content-addressed repository snapshots for planning."""

import errno
import hashlib
import json
import os
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import nullcontext
from pathlib import Path
from typing import Any, cast

from karajan.projects.credential_sources import _plain, _private
from karajan.runs import RunError
from karajan.runs.planning import digest, encoded
from karajan.storage import ExistingStoreError, open_database, require_schema

_NAME = "planning-repository-snapshots.sqlite"
_ARTIFACTS = "planning-repository-snapshot-blobs"
_MAX_FILES = 2_000
_MAX_BYTES = 8_000_000
_MAX_GIT_OBJECT_BYTES = 8_000_000
_GIT_TIMEOUT_SECONDS = 10.0
_GIT_CLEANUP_SECONDS = 1.0
_MAX_GIT_METADATA_BYTES = 8 * 1024
_MOVEFILE_WRITE_THROUGH = 0x8
_ERROR_FILE_EXISTS = 80
_ERROR_ALREADY_EXISTS = 183
_AT_FDCWD = -100
_RENAME_NOREPLACE = 1
_CREATE_SUSPENDED = 0x00000004


def _git_alternate_pathname(path: Path) -> str:
    """Encode one exact alternate pathname for Git's C-style list parser.

    ``GIT_ALTERNATE_OBJECT_DIRECTORIES`` is a platform path list.  Git accepts
    a C-quoted member in that list, so quote the whole controller-selected
    pathname rather than allowing a separator in its spelling to add another
    object source.  Do not use JSON escapes: Git documents C-style quoting and
    accepts backslash, quote, and three-digit octal escapes there.
    """
    text = os.fsdecode(os.fsencode(path))
    if "\0" in text:
        raise OSError("NUL alternate object pathname")
    escaped: list[str] = []
    for character in text:
        if character == "\\":
            escaped.append("\\\\")
        elif character == '"':
            escaped.append('\\"')
        elif ord(character) < 32 or ord(character) == 127:
            escaped.append("\\" + format(ord(character), "03o"))
        else:
            escaped.append(character)
    return '"' + "".join(escaped) + '"'


def _trusted_git_executable(root: Path) -> Path:
    """Return a controller-installed Git, never a repository/PATH lookup.

    The receiving boundary deliberately does not use ``which``.  On Windows a
    bare executable name also searches the controller's current directory, so
    a repository-local ``git.exe`` could run before Git's object restrictions
    take effect.  These are the ordinary system installation locations for Git
    on the platforms we support; deployments with Git elsewhere must expose it
    through one of those controller-managed locations rather than an
    untrusted repository or mutable PATH entry.
    """
    candidates: tuple[Path, ...]
    if os.name == "nt":
        program_files = Path(os.environ.get("ProgramW6432", r"C:\Program Files"))
        candidates = (
            program_files / "Git" / "cmd" / "git.exe",
            program_files / "Git" / "bin" / "git.exe",
            Path(r"C:\Program Files\Git\cmd\git.exe"),
        )
    else:
        candidates = (Path("/usr/bin/git"), Path("/usr/local/bin/git"))
    try:
        repository = root.resolve(strict=True)
        controller_cwd = Path.cwd().resolve(strict=True)
    except OSError:
        raise OSError("repository working directory unavailable") from None
    for candidate in candidates:
        try:
            executable = candidate.resolve(strict=True)
            info = executable.lstat()
            if (
                not executable.is_absolute()
                or not stat.S_ISREG(info.st_mode)
                or stat.S_ISLNK(info.st_mode)
                or executable.is_relative_to(repository)
                or executable.is_relative_to(controller_cwd)
            ):
                continue
            return executable
        except OSError:
            continue
    raise OSError("trusted controller Git unavailable")


def _read_git_metadata(path: Path) -> str:
    """Read one small, regular Git metadata file without an unbounded decode."""
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_GIT_METADATA_BYTES:
            raise ValueError()
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(_MAX_GIT_METADATA_BYTES + 1)
        if len(raw) > _MAX_GIT_METADATA_BYTES:
            raise ValueError()
        return raw.decode("utf-8")
    finally:
        os.close(descriptor)


if sys.platform == "win32":
    from ctypes import (
        Structure,
        WinDLL,
        WinError,
        addressof,
        c_int,
        c_long,
        c_uint32,
        c_void_p,
        c_wchar_p,
        get_last_error,
        sizeof,
    )

    class _WindowsThreadEntry(Structure):
        _fields_ = [
            ("dwSize", c_uint32),
            ("cntUsage", c_uint32),
            ("th32ThreadID", c_uint32),
            ("th32OwnerProcessID", c_uint32),
            ("tpBasePri", c_long),
            ("tpDeltaPri", c_long),
            ("dwFlags", c_uint32),
        ]

    def _windows_job() -> int:
        """Create an unnamed job that outlives one receiving-boundary leader."""
        kernel = WinDLL("kernel32", use_last_error=True)
        create = kernel.CreateJobObjectW
        create.argtypes = [c_void_p, c_wchar_p]
        create.restype = c_void_p
        handle = int(create(None, None) or 0)
        if not handle:
            raise WinError(get_last_error())
        return handle

    def _windows_assign_job(job: int, process_handle: int) -> None:
        kernel = WinDLL("kernel32", use_last_error=True)
        assign = kernel.AssignProcessToJobObject
        assign.argtypes = [c_void_p, c_void_p]
        assign.restype = c_int
        if not assign(c_void_p(job), c_void_p(process_handle)):
            raise WinError(get_last_error())

    def _windows_terminate_job(job: int) -> None:
        kernel = WinDLL("kernel32", use_last_error=True)
        terminate = kernel.TerminateJobObject
        terminate.argtypes = [c_void_p, c_uint32]
        terminate.restype = c_int
        if not terminate(c_void_p(job), 125):
            raise WinError(get_last_error())

    def _windows_close_handle(handle: int) -> None:
        kernel = WinDLL("kernel32", use_last_error=True)
        close = kernel.CloseHandle
        close.argtypes = [c_void_p]
        close.restype = c_int
        if not close(c_void_p(handle)):
            raise WinError(get_last_error())

    def _windows_cancel_reader(thread_id: int) -> None:
        """Cancel the reader's pending synchronous pipe read, if it has one."""
        kernel = WinDLL("kernel32", use_last_error=True)
        open_thread = kernel.OpenThread
        open_thread.argtypes = [c_uint32, c_int, c_uint32]
        open_thread.restype = c_void_p
        thread = int(open_thread(0x0001, False, thread_id) or 0)  # THREAD_TERMINATE
        if not thread:
            raise WinError(get_last_error())
        try:
            cancel = kernel.CancelSynchronousIo
            cancel.argtypes = [c_void_p]
            cancel.restype = c_int
            if not cancel(c_void_p(thread)):
                raise WinError(get_last_error())
        finally:
            _windows_close_handle(thread)

    def _windows_resume_process(process_id: int) -> None:
        """Resume the one primary thread created suspended for Job assignment."""
        kernel = WinDLL("kernel32", use_last_error=True)
        snapshot = kernel.CreateToolhelp32Snapshot
        snapshot.argtypes = [c_uint32, c_uint32]
        snapshot.restype = c_void_p
        threads = int(snapshot(0x00000004, 0) or 0)  # TH32CS_SNAPTHREAD
        if threads == c_void_p(-1).value or not threads:
            raise WinError(get_last_error())
        try:
            first = kernel.Thread32First
            first.argtypes = [c_void_p, c_void_p]
            first.restype = c_int
            next_thread = kernel.Thread32Next
            next_thread.argtypes = [c_void_p, c_void_p]
            next_thread.restype = c_int
            entry = _WindowsThreadEntry()
            entry.dwSize = sizeof(entry)
            present = bool(first(c_void_p(threads), c_void_p(addressof(entry))))
            while present and entry.th32OwnerProcessID != process_id:
                entry.dwSize = sizeof(entry)
                present = bool(next_thread(c_void_p(threads), c_void_p(addressof(entry))))
            if not present:
                raise OSError("suspended Git primary thread disappeared")
            open_thread = kernel.OpenThread
            open_thread.argtypes = [c_uint32, c_int, c_uint32]
            open_thread.restype = c_void_p
            thread = int(open_thread(0x0002, False, entry.th32ThreadID) or 0)  # SUSPEND_RESUME
            if not thread:
                raise WinError(get_last_error())
            try:
                resume = kernel.ResumeThread
                resume.argtypes = [c_void_p]
                resume.restype = c_uint32
                if resume(c_void_p(thread)) == 0xFFFFFFFF:
                    raise WinError(get_last_error())
            finally:
                _windows_close_handle(thread)
        finally:
            _windows_close_handle(threads)

    def _move_file_write_through_windows(source: str, target: Path) -> None:
        """Atomically publish a new Windows artifact without replacing one."""
        move_file = WinDLL("kernel32", use_last_error=True).MoveFileExW
        move_file.argtypes = [c_wchar_p, c_wchar_p, c_uint32]
        move_file.restype = c_int
        if move_file(source, str(target), _MOVEFILE_WRITE_THROUGH):
            return
        error = get_last_error()
        if error in {_ERROR_FILE_EXISTS, _ERROR_ALREADY_EXISTS}:
            raise FileExistsError(error, "artifact already exists", str(target))
        raise WinError(error)

    def _move_file_no_replace_posix(source: str, target: Path) -> None:
        raise OSError("POSIX artifact publication required")


else:

    def _windows_job() -> int:
        raise OSError("Windows Job Objects are unavailable")

    def _windows_assign_job(job: int, process_handle: int) -> None:
        raise OSError("Windows Job Objects are unavailable")

    def _windows_terminate_job(job: int) -> None:
        raise OSError("Windows Job Objects are unavailable")

    def _windows_close_handle(handle: int) -> None:
        raise OSError("Windows Job Objects are unavailable")

    def _windows_cancel_reader(thread_id: int) -> None:
        raise OSError("Windows pipe cancellation is unavailable")

    def _windows_resume_process(process_id: int) -> None:
        raise OSError("Windows process resumption is unavailable")

    def _move_file_write_through_windows(source: str, target: Path) -> None:
        raise OSError("Windows artifact publication required")

    def _move_file_no_replace_posix(source: str, target: Path) -> None:
        """Atomically install ``source`` without ever replacing ``target``.

        Linux ``renameat2(RENAME_NOREPLACE)`` turns a prepared, flushed
        temporary file into its CAS name in one directory-entry operation.
        Unlike link-then-unlink, a killed publisher cannot leave a second
        hardlink which makes an otherwise valid digest unrecoverable.  Other
        POSIX kernels have no equivalent primitive in this boundary, so fail
        closed rather than reopening the crash window with a link fallback.
        """
        if not sys.platform.startswith("linux"):
            raise OSError("atomic no-replace publication unavailable")
        try:
            from ctypes import CDLL, c_char_p, c_int, get_errno

            renameat2 = CDLL(None, use_errno=True).renameat2
            renameat2.argtypes = [c_int, c_char_p, c_int, c_char_p, c_int]
            renameat2.restype = c_int
        except (AttributeError, OSError):
            raise OSError("atomic no-replace publication unavailable") from None
        if (
            renameat2(
                _AT_FDCWD, os.fsencode(source), _AT_FDCWD, os.fsencode(target), _RENAME_NOREPLACE
            )
            == 0
        ):
            return
        error = get_errno()
        if error == errno.EEXIST:
            raise FileExistsError(error, "artifact already exists", str(target))
        raise OSError(error, "renameat2", str(target))


def snapshot_database(control_directory: Path) -> Path:
    from .planning_bootstrap import read_planning_bootstrap

    return read_planning_bootstrap(control_directory)[0].state_directory / _NAME


def _production_paths(control: Path) -> tuple[Path, Path]:
    from .planning_bootstrap import read_planning_bootstrap

    s, _ = read_planning_bootstrap(control)
    return s.state_directory / _NAME, s.state_directory


def provision_planning_repository_snapshots(control_directory: Path) -> Path:
    path, root = _production_paths(control_directory)
    _plain(root, directory=True)
    _private(root, directory=True)
    PlanningRepositorySnapshotStore(path, private_root=root)
    try:
        path.chmod(0o600)
    except OSError as e:
        raise RunError("PLANNING_SNAPSHOT_PROVISION_FAILED") from e
    return path


class PlanningRepositorySnapshotStore:
    def __init__(
        self, database: Path, *, existing_only: bool = False, private_root: Path | None = None
    ) -> None:
        self.database = database
        self.existing_only = existing_only
        self.private_root = private_root
        self.artifacts = database.parent / _ARTIFACTS
        if private_root is not None:
            self._validate_private(existing_only)
        if existing_only:
            require_schema(
                database,
                {
                    "snapshots": ["binding_sha256", "data", "source_sha256", "manifest_sha256"],
                    "files": ["binding_sha256", "path", "sha256"],
                },
            )
            return
        database.parent.mkdir(parents=True, exist_ok=True)
        self.artifacts.mkdir(mode=0o700, exist_ok=True)
        if private_root is not None:
            self._validate_private(False)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA foreign_keys=ON")
            db.execute(
                "CREATE TABLE IF NOT EXISTS snapshots "
                "(binding_sha256 TEXT PRIMARY KEY, data TEXT NOT NULL, "
                "source_sha256 TEXT, manifest_sha256 TEXT)"
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(snapshots)")}
            if "source_sha256" not in columns:
                db.execute("ALTER TABLE snapshots ADD COLUMN source_sha256 TEXT")
            if "manifest_sha256" not in columns:
                db.execute("ALTER TABLE snapshots ADD COLUMN manifest_sha256 TEXT")
            db.execute(
                "CREATE TABLE IF NOT EXISTS files (binding_sha256 TEXT NOT NULL "
                "REFERENCES snapshots(binding_sha256) ON DELETE CASCADE, "
                "path TEXT NOT NULL, sha256 TEXT NOT NULL, "
                "PRIMARY KEY(binding_sha256,path))"
            )
            db.commit()

    def _validate_private(self, existing: bool) -> None:
        try:
            assert self.private_root is not None
            _plain(self.private_root, directory=True)
            _private(self.private_root, directory=True)
            if (
                self.database.parent != self.private_root
                or self.artifacts.parent != self.private_root
            ):
                raise ValueError()
            if existing:
                _plain(self.database)
                _private(self.database)
                _plain(self.artifacts, directory=True)
                _private(self.artifacts, directory=True)
            elif self.database.exists():
                _plain(self.database)
                _private(self.database)
            elif self.artifacts.exists():
                _plain(self.artifacts, directory=True)
                _private(self.artifacts, directory=True)
        except Exception:
            raise RunError("PLANNING_REPOSITORY_SNAPSHOT_UNAVAILABLE") from None

    def _connect(self) -> sqlite3.Connection:
        # ``open_database`` resolves its input spelling before SQLite opens it.
        # Re-establish the controller-owned path boundary immediately before
        # every connection, rather than trusting a check made by a factory that
        # may have remained alive while an attacker replaced a path component.
        if self.private_root is not None:
            self._validate_private(self.database.exists())
        db = open_database(self.database, existing_only=self.existing_only, isolation_level=None)
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @staticmethod
    def _allowed(path: str, paths: list[str]) -> bool:
        return any(PlanningRepositorySnapshotStore._within(path, x) for x in paths)

    @staticmethod
    def _within(path: str, parent: str) -> bool:
        return path == parent or path.startswith(parent + "/")

    @staticmethod
    def _paths(value: object) -> list[str]:
        if not isinstance(value, list) or not value:
            raise RunError("PLANNING_SNAPSHOT_PATHS_INVALID")
        out = []
        for x in value:
            if (
                not isinstance(x, str)
                or not x
                or x.startswith("/")
                or "\\" in x
                or any(p in {"", ".", ".."} for p in x.split("/"))
            ):
                raise RunError("PLANNING_SNAPSHOT_PATHS_INVALID")
            out.append(x)
        return sorted(set(out))

    @staticmethod
    def _sha256(x: object) -> bool:
        try:
            return isinstance(x, str) and len(x) == 64 and int(x, 16) is not None
        except ValueError:
            return False

    @staticmethod
    def _git_oid(x: object) -> bool:
        try:
            return isinstance(x, str) and len(x) in {40, 64} and int(x, 16) is not None
        except ValueError:
            return False

    @staticmethod
    def _git_objects(root: Path) -> Path:
        """Find source objects without asking Git to load source configuration."""
        dot_git = root / ".git"
        try:
            info = dot_git.lstat()
            if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                git_dir = dot_git
            elif stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                text = _read_git_metadata(dot_git)
                if not text.startswith("gitdir: ") or "\n" not in text:
                    raise ValueError()
                location = text.removeprefix("gitdir: ").splitlines()[0]
                if not location or "\0" in location:
                    raise ValueError()
                git_dir = (root / location).resolve(strict=True)
                if not stat.S_ISDIR(git_dir.lstat().st_mode):
                    raise ValueError()
            else:
                raise ValueError()
            common = git_dir
            common_file = git_dir / "commondir"
            if common_file.exists():
                common_info = common_file.lstat()
                if not stat.S_ISREG(common_info.st_mode) or stat.S_ISLNK(common_info.st_mode):
                    raise ValueError()
                relative = _read_git_metadata(common_file).strip()
                if not relative or "\0" in relative:
                    raise ValueError()
                common = (git_dir / relative).resolve(strict=True)
                if not stat.S_ISDIR(common.lstat().st_mode):
                    raise ValueError()
            objects = common / "objects"
            object_info = objects.lstat()
            if not stat.S_ISDIR(object_info.st_mode) or stat.S_ISLNK(object_info.st_mode):
                raise ValueError()
            return objects
        except (OSError, UnicodeError, ValueError):
            raise RunError("PLANNING_SNAPSHOT_BASE_UNAVAILABLE") from None

    @classmethod
    def _git(
        cls,
        root: Path,
        object_format: str,
        *args: str,
        limit: int,
        input: bytes | None = None,
    ) -> bytes:
        if object_format not in {"sha1", "sha256"}:
            raise RunError("PLANNING_SNAPSHOT_BASE_UNAVAILABLE")
        source_objects = cls._git_objects(root)
        git = _trusted_git_executable(root)
        env = {k: os.environ[k] for k in ("SystemRoot", "WINDIR", "TEMP", "TMP") if k in os.environ}
        if os.name == "nt":
            system_root = Path(env.get("SystemRoot", r"C:\Windows"))
            env["PATH"] = os.pathsep.join(
                str(path)
                for path in (git.parent, git.parent.parent / "bin", system_root / "System32")
            )
        else:
            # The executable is absolute, and Git receives no inherited
            # executable search path. Keep only standard controller locations
            # for normal Git helper startup and test interpreter shebangs.
            env["PATH"] = "/usr/bin:/bin"
        env.update(
            {
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_NO_REPLACE_OBJECTS": "1",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_TERMINAL_PROMPT": "0",
            }
        )
        # Do not execute Git with ``-C root``: that reads the untrusted local
        # config.  A fresh bare object reader contains only controller-written
        # config and sees the registered repository solely as an object
        # alternate.  In particular it has no promisor remote, so a missing
        # object fails locally instead of invoking a configured transport.
        with tempfile.TemporaryDirectory(prefix="karajan-planning-reader-") as directory:
            reader = Path(directory) / "reader.git"
            (reader / "objects").mkdir(parents=True)
            (reader / "refs" / "heads").mkdir(parents=True)
            (reader / "refs" / "tags").mkdir(parents=True)
            (reader / "HEAD").write_text("ref: refs/heads/empty\n", encoding="ascii")
            (reader / "config").write_text(
                "[core]\nrepositoryformatversion = 1\nbare = true\n"
                "[extensions]\nobjectformat = " + object_format + "\n",
                encoding="ascii",
            )
            env.update(
                {
                    "GIT_DIR": str(reader),
                    "GIT_OBJECT_DIRECTORY": str(reader / "objects"),
                    "GIT_ALTERNATE_OBJECT_DIRECTORIES": _git_alternate_pathname(source_objects),
                }
            )
            process: subprocess.Popen[bytes] | None = None
            reader_thread: threading.Thread | None = None
            job = 0
            contained = os.name != "nt"
            cleanup_unconfirmed = False
            returncode: int | None = None
            try:
                command = [
                    str(git),
                    "--no-replace-objects",
                    "--git-dir=" + str(reader),
                    "-c",
                    "core.hooksPath=" + os.devnull,
                    "-c",
                    "core.fsmonitor=false",
                    "-c",
                    "credential.helper=",
                    "-c",
                    "protocol.allow=never",
                    *args,
                ]
                options: dict[str, Any] = {
                    "stdin": subprocess.PIPE if input is not None else subprocess.DEVNULL,
                    "stdout": subprocess.PIPE,
                    "stderr": subprocess.DEVNULL,
                    "env": env,
                    # The temporary bare reader is controller-owned.  Pinning
                    # cwd as well as argv prevents Windows executable search
                    # from consulting a repository controller cwd.
                    "cwd": str(reader),
                }
                if os.name == "posix":
                    # This reader owns a fresh group, so its timeout cleanup
                    # cannot touch a controller or unrelated Git process.
                    options["start_new_session"] = True
                elif os.name == "nt":
                    job = _windows_job()
                    options["creationflags"] = _CREATE_SUSPENDED
                process = subprocess.Popen(command, **options)
                if job:
                    # A Job keeps ownership of descendants after the Git leader
                    # exits.  Its ordinary children inherit the same Job.
                    _windows_assign_job(job, int(cast(Any, process)._handle))
                    contained = True
                    _windows_resume_process(process.pid)
                stream = process.stdout
                assert stream is not None
                if input is not None:
                    assert process.stdin is not None
                    process.stdin.write(input)
                    process.stdin.close()

                output: list[bytes] = []
                reader_errors: list[BaseException] = []
                output_ready = threading.Event()

                def read_output() -> None:
                    try:
                        output.append(stream.read(limit + 1))
                    except BaseException as error:  # Pipe close is also a failed Git read.
                        reader_errors.append(error)
                    finally:
                        output_ready.set()

                # Normal cleanup proves this thread reached EOF before return.
                # If the platform cannot cancel a stuck pipe read, it is daemon
                # only so an unavailable reader cannot hold the controller open.
                reader_thread = threading.Thread(target=read_output, daemon=True)
                reader_thread.start()
                deadline = time.monotonic() + _GIT_TIMEOUT_SECONDS
                while not output_ready.wait(max(0.0, min(0.05, deadline - time.monotonic()))):
                    if time.monotonic() >= deadline:
                        raise subprocess.TimeoutExpired(command, _GIT_TIMEOUT_SECONDS)
                if reader_errors:
                    raise OSError("bounded Git reader failed") from reader_errors[0]
                result = output[0]
                if len(result) > limit:
                    raise RunError("PLANNING_SNAPSHOT_BASE_UNAVAILABLE")
                returncode = process.wait(timeout=max(0.0, deadline - time.monotonic()))
                reader_thread.join(timeout=_GIT_CLEANUP_SECONDS)
                if reader_thread.is_alive():
                    raise OSError("bounded Git reader did not finish")
                output_bytes = result
            except (OSError, subprocess.TimeoutExpired):
                raise RunError("PLANNING_SNAPSHOT_GIT_UNAVAILABLE") from None
            finally:
                if process is not None:
                    try:
                        if os.name == "posix":
                            kill_group = getattr(os, "kill" + "pg")
                            kill_signal = getattr(signal, "SIG" + "KILL")
                            kill_group(process.pid, kill_signal)
                        elif os.name == "nt":
                            if job and contained:
                                _windows_terminate_job(job)
                            else:
                                process.kill()
                                cleanup_unconfirmed = True
                        else:
                            process.kill()
                    except ProcessLookupError:
                        # On POSIX this proves the dedicated group is already empty.
                        pass
                    except OSError:
                        cleanup_unconfirmed = True
                    try:
                        process.wait(timeout=_GIT_CLEANUP_SECONDS)
                    except (OSError, subprocess.TimeoutExpired):
                        cleanup_unconfirmed = True
                if reader_thread is not None:
                    if reader_thread.is_alive() and os.name == "nt":
                        thread_id = reader_thread.native_id
                        if thread_id is None:
                            cleanup_unconfirmed = True
                        else:
                            try:
                                _windows_cancel_reader(thread_id)
                            except OSError:
                                cleanup_unconfirmed = True
                    reader_thread.join(timeout=_GIT_CLEANUP_SECONDS)
                    if reader_thread.is_alive():
                        cleanup_unconfirmed = True
                # BufferedReader.close() waits on a concurrent read.  It is safe
                # only after the reader has actually exited; otherwise report the
                # boundary unavailable without turning cleanup into an unbounded
                # controller wait.
                if (
                    reader_thread is not None
                    and not reader_thread.is_alive()
                    and process is not None
                    and process.stdout is not None
                ):
                    process.stdout.close()
                if job:
                    try:
                        _windows_close_handle(job)
                    except OSError:
                        cleanup_unconfirmed = True
            if cleanup_unconfirmed:
                raise RunError("PLANNING_SNAPSHOT_GIT_UNAVAILABLE")
        if returncode:
            raise RunError("PLANNING_SNAPSHOT_BASE_UNAVAILABLE")
        return output_bytes

    @staticmethod
    def _object_hash(oid: str, kind: str, content: bytes) -> str:
        """Return the Git object ID for a bounded canonical object body."""
        algorithm = "sha1" if len(oid) == 40 else "sha256"
        hashed = hashlib.new(algorithm)
        hashed.update(kind.encode("ascii") + b" " + str(len(content)).encode("ascii") + b"\0")
        hashed.update(content)
        return hashed.hexdigest()

    @classmethod
    def _git_object(cls, root: Path, object_format: str, oid: str, kind: str) -> bytes:
        """Read one bounded object and bind its type, bytes, and name together."""
        if not cls._git_oid(oid) or {"sha1": 40, "sha256": 64}.get(object_format) != len(oid):
            raise RunError("PLANNING_SNAPSHOT_BASE_UNAVAILABLE")
        content = cls._git(root, object_format, "cat-file", kind, oid, limit=_MAX_GIT_OBJECT_BYTES)
        if cls._object_hash(oid, kind, content) != oid:
            raise RunError("PLANNING_SNAPSHOT_BASE_UNAVAILABLE")
        return content

    @classmethod
    def _git_tree_pair(cls, root: Path, object_format: str, oids: list[str]) -> dict[str, bytes]:
        """Read at most two child trees in one bounded, config-isolated Git call."""
        if not (
            1 <= len(oids) <= 2
            and all(
                cls._git_oid(oid) and {"sha1": 40, "sha256": 64}.get(object_format) == len(oid)
                for oid in oids
            )
        ):
            raise RunError("PLANNING_SNAPSHOT_BASE_UNAVAILABLE")
        output = cls._git(
            root,
            object_format,
            "cat-file",
            "--batch",
            input="".join(oid + "\n" for oid in oids).encode("ascii"),
            limit=len(oids) * (_MAX_GIT_OBJECT_BYTES + 200),
        )
        result: dict[str, bytes] = {}
        offset = 0
        try:
            for oid in oids:
                end = output.index(b"\n", offset)
                received, kind, raw_size = output[offset:end].decode("ascii").split(" ")
                size = int(raw_size)
                start = end + 1
                finish = start + size
                if (
                    received != oid
                    or kind != "tree"
                    or size < 0
                    or size > _MAX_GIT_OBJECT_BYTES
                    or finish >= len(output)
                    or output[finish : finish + 1] != b"\n"
                ):
                    raise ValueError()
                content = output[start:finish]
                if cls._object_hash(oid, "tree", content) != oid:
                    raise ValueError()
                result[oid] = content
                offset = finish + 1
            if offset != len(output):
                raise ValueError()
        except (UnicodeError, ValueError):
            raise RunError("PLANNING_SNAPSHOT_BASE_UNAVAILABLE") from None
        return result

    @classmethod
    def _tree_entries(cls, tree: bytes, oid_length: int) -> list[tuple[str, str, str]]:
        """Parse a verified Git tree without making a pathspec authoritative."""
        raw_oid_length = oid_length // 2
        entries: list[tuple[str, str, str]] = []
        offset = 0
        while offset < len(tree):
            space = tree.find(b" ", offset)
            nul = tree.find(b"\0", space + 1)
            if space < 1 or nul < space + 2 or nul + 1 + raw_oid_length > len(tree):
                raise RunError("PLANNING_SNAPSHOT_BASE_UNAVAILABLE")
            try:
                mode = tree[offset:space].decode("ascii")
                name = tree[space + 1 : nul].decode("utf-8", "strict")
            except UnicodeError:
                raise RunError("PLANNING_SNAPSHOT_BASE_UNAVAILABLE") from None
            if not name or "/" in name or name in {".", ".."}:
                raise RunError("PLANNING_SNAPSHOT_BASE_UNAVAILABLE")
            raw_oid = tree[nul + 1 : nul + 1 + raw_oid_length]
            entries.append((mode, name, raw_oid.hex()))
            offset = nul + 1 + raw_oid_length
        return entries

    @classmethod
    def _base_tree(cls, root: Path, object_format: str, base: str) -> tuple[str, bytes]:
        commit = cls._git_object(root, object_format, base, "commit")
        first, separator, _ = commit.partition(b"\n")
        if not separator or not first.startswith(b"tree "):
            raise RunError("PLANNING_SNAPSHOT_BASE_UNAVAILABLE")
        try:
            tree = first.removeprefix(b"tree ").decode("ascii")
        except UnicodeError:
            raise RunError("PLANNING_SNAPSHOT_BASE_UNAVAILABLE") from None
        if len(tree) != len(base) or not cls._git_oid(tree):
            raise RunError("PLANNING_SNAPSHOT_BASE_UNAVAILABLE")
        return tree, cls._git_object(root, object_format, tree, "tree")

    def _sync_artifacts(self, target: Path | None = None) -> None:
        """Durably record a blob directory entry before a SQLite reference.

        POSIX requires a directory fsync after a name change.  Windows uses a
        same-directory ``MoveFileExW(..., MOVEFILE_WRITE_THROUGH)`` commit;
        there is no directory fsync substitute in this boundary.  The blob has
        already been flushed before that rename, and is flushed once more here
        before its SQLite reference is committed.
        """
        if self.private_root is not None:
            self._validate_private(True)
        if os.name == "nt":
            if target is None:
                raise OSError("Windows artifact target required")
            descriptor = os.open(target, os.O_RDWR)
        elif os.name == "posix":
            descriptor = os.open(self.artifacts, os.O_RDONLY)
        else:
            raise OSError("artifact durability unsupported")
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _move_file_write_through(source: str, target: Path) -> None:
        """Atomically publish a new Windows artifact without replacing one."""
        _move_file_write_through_windows(source, target)

    def _published_content(self, target: Path, content: bytes) -> bool:
        """Accept a single-name CAS artifact only, never a foreign alias."""
        if self.private_root is not None:
            self._validate_private(True)
        info = target.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_nlink != 1
            or info.st_size != len(content)
        ):
            raise ValueError()
        with target.open("rb") as stream:
            return stream.read(len(content) + 1) == content

    def _publish(self, sha: str, content: bytes) -> None:
        if self.private_root is not None:
            self._validate_private(True)
        target = self.artifacts / sha
        if target.exists():
            try:
                if not self._published_content(target, content):
                    raise ValueError()
                self._sync_artifacts(target)
                return
            except (OSError, ValueError):
                raise RunError("PLANNING_REPOSITORY_SNAPSHOT_CHANGED") from None
        try:
            fd, name = tempfile.mkstemp(prefix=".snapshot-", dir=self.artifacts)
            with os.fdopen(fd, "wb") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(name, 0o600)
            if os.name == "nt":
                try:
                    self._move_file_write_through(name, target)
                except FileExistsError:
                    pass
                finally:
                    try:
                        os.unlink(name)
                    except FileNotFoundError:
                        pass
            else:
                try:
                    _move_file_no_replace_posix(name, target)
                except FileExistsError:
                    pass
                finally:
                    try:
                        os.unlink(name)
                    except FileNotFoundError:
                        pass
            if not self._published_content(target, content):
                raise ValueError()
            self._sync_artifacts(target)
        except (OSError, ValueError):
            raise RunError("PLANNING_REPOSITORY_SNAPSHOT_CHANGED") from None

    def freeze(
        self,
        binding: dict[str, Any],
        run: dict[str, Any],
        project: dict[str, Any],
        *,
        guard: object = None,
    ) -> dict[str, Any]:
        if self.private_root is not None:
            self._validate_private(True)
        key = digest(binding)
        if project.get("id") != run.get("project_id") or project.get("revision") != run.get(
            "configuration_snapshot", {}
        ).get("project_revision"):
            raise RunError("PLANNING_REPOSITORY_SOURCE_CHANGED")
        repo = project.get("repository")
        if (
            not isinstance(repo, dict)
            or not all(
                isinstance(repo.get(k), str) for k in ("root", "identity_sha256", "base_sha")
            )
            or not (
                self._sha256(repo.get("identity_sha256")) and self._git_oid(repo.get("base_sha"))
            )
        ):
            raise RunError("PLANNING_REPOSITORY_SOURCE_INVALID")
        root = Path(repo["root"])
        try:
            if root.resolve(strict=True) != root or not stat.S_ISDIR(root.stat().st_mode):
                raise ValueError()
        except (OSError, ValueError):
            raise RunError("PLANNING_REPOSITORY_SOURCE_INVALID") from None
        paths = self._paths(run["authorization_ceiling"]["read_paths"])
        rows: list[tuple[str, str, bytes]] = []
        matched = {p: False for p in paths}
        total = 0
        # Git and complete enumeration are deliberately outside both SQLite writers.
        # Do not ask a tree-ish resolver for ``base:path``: the registered commit,
        # every selected tree, and every selected blob are each read by their
        # original object name and independently rehashed before their bytes can
        # enter the new SHA-256 CAS seal.
        object_format = "sha1" if len(repo["base_sha"]) == 40 else "sha256"
        base_tree, raw_tree = self._base_tree(root, object_format, repo["base_sha"])
        oid_length = len(repo["base_sha"])

        def visit(tree_oid: str, tree: bytes, parent: str) -> None:
            nonlocal total
            entries = self._tree_entries(tree, oid_length)
            selected = []
            for mode, name, oid in entries:
                path = name if not parent else parent + "/" + name
                relevant = any(
                    self._within(path, approved) or self._within(approved, path)
                    for approved in paths
                )
                if not relevant:
                    continue
                selected.append((mode, path, oid))
            child_tree_oids = [oid for mode, _, oid in selected if mode in {"40000", "040000"}]
            child_trees: dict[str, bytes] = {}
            for index in range(0, len(child_tree_oids), 2):
                pair = child_tree_oids[index : index + 2]
                child_trees.update(self._git_tree_pair(root, object_format, pair))
            for mode, path, oid in selected:
                # Git permits names that the snapshot protocol deliberately
                # cannot represent. Validate all selected path components before
                # a child object read or any CAS publication.
                self._paths([path])
                if mode in {"40000", "040000"}:
                    visit(oid, child_trees[oid], path)
                    continue
                if not self._allowed(path, paths):
                    continue
                if mode not in {"100644", "100755"}:
                    raise RunError("PLANNING_SNAPSHOT_ENTRY_UNSUPPORTED")
                content = self._git_object(root, object_format, oid, "blob")
                total += len(content)
                if len(rows) >= _MAX_FILES or total > _MAX_BYTES:
                    raise RunError("PLANNING_SNAPSHOT_LIMIT_EXCEEDED")
                for approved in matched:
                    matched[approved] = matched[approved] or self._within(path, approved)
                rows.append((path, mode, content))

        visit(base_tree, raw_tree, "")
        if not rows or not all(matched.values()):
            raise RunError("PLANNING_SNAPSHOT_PATH_EMPTY")
        if not self._sha256(binding.get("requirement_sha256")) or not self._sha256(
            binding.get("authorization_ceiling_sha256")
        ):
            raise RunError("PLANNING_REPOSITORY_SOURCE_INVALID")
        files = [
            {"path": p, "mode": m, "size": len(c), "sha256": hashlib.sha256(c).hexdigest()}
            for p, m, c in rows
        ]
        result = {
            "schema_version": "karajan.planning-repository-snapshot.v1",
            "binding_sha256": key,
            "execution_id": binding["execution_id"],
            "run_id": binding["run_id"],
            "intent_id": binding["intent_id"],
            "repository_identity_sha256": repo["identity_sha256"],
            "base_sha": repo["base_sha"],
            "read_paths_sha256": digest(paths),
            "requirement_sha256": binding["requirement_sha256"],
            "authorization_ceiling_sha256": binding["authorization_ceiling_sha256"],
            "files": files,
            "total_bytes": total,
        }
        source_sha256 = digest(
            {k: result[k] for k in ("repository_identity_sha256", "base_sha", "read_paths_sha256")}
        )
        result["snapshot_sha256"] = digest(result)
        # This database value is a separate seal over every manifest field;
        # ``snapshot_sha256`` is retained as an internal consistency check,
        # not treated as independent authority.
        manifest_sha256 = digest(result)
        for _, _, c in rows:
            self._publish(hashlib.sha256(c).hexdigest(), c)
        # A controller guard is deliberately after slow preparation and held
        # through the brief manifest/reference commit. Orphan CAS bytes are inert.
        if guard is not None:
            if not callable(guard):
                raise RunError("PLANNING_REPOSITORY_SNAPSHOT_UNAVAILABLE")
        held = nullcontext() if guard is None else guard()
        if not hasattr(held, "__enter__") or not hasattr(held, "__exit__"):
            raise RunError("PLANNING_REPOSITORY_SNAPSHOT_UNAVAILABLE")
        previous = False
        with held:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                old = db.execute(
                    "SELECT 1 FROM snapshots WHERE binding_sha256=?", (key,)
                ).fetchone()
                if old:
                    db.commit()
                    previous = True
                else:
                    db.execute(
                        "INSERT INTO snapshots VALUES (?,?,?,?)",
                        (key, encoded(result), source_sha256, manifest_sha256),
                    )
                    db.executemany(
                        "INSERT INTO files VALUES (?,?,?)",
                        [(key, p, hashlib.sha256(c).hexdigest()) for p, _, c in rows],
                    )
                    db.commit()
        if previous:
            return {k: v for k, v in self.read(binding).items() if k != "content"}
        return result

    def read(self, binding: dict[str, Any]) -> dict[str, Any]:
        if self.private_root is not None:
            self._validate_private(True)
        key = digest(binding)
        try:
            with self._connect() as db:
                row = db.execute(
                    "SELECT data,source_sha256,manifest_sha256 FROM snapshots "
                    "WHERE binding_sha256=?",
                    (key,),
                ).fetchone()
                refs = db.execute(
                    "SELECT path,sha256 FROM files WHERE binding_sha256=? ORDER BY path", (key,)
                ).fetchall()
            if row is None:
                raise RunError("PLANNING_REPOSITORY_SNAPSHOT_NOT_FOUND")
            result = json.loads(row[0])
        except ExistingStoreError:
            raise RunError("PLANNING_REPOSITORY_SNAPSHOT_UNAVAILABLE") from None
        except (sqlite3.Error, json.JSONDecodeError, TypeError):
            raise RunError("PLANNING_REPOSITORY_SNAPSHOT_CHANGED") from None
        try:
            required = {
                "schema_version",
                "binding_sha256",
                "execution_id",
                "run_id",
                "intent_id",
                "repository_identity_sha256",
                "base_sha",
                "read_paths_sha256",
                "requirement_sha256",
                "authorization_ceiling_sha256",
                "files",
                "total_bytes",
                "snapshot_sha256",
            }
            if (
                not isinstance(result, dict)
                or set(result) != required
                or result["schema_version"] != "karajan.planning-repository-snapshot.v1"
                or result["binding_sha256"] != key
                or not self._sha256(result["repository_identity_sha256"])
                or not self._git_oid(result["base_sha"])
                or not self._sha256(result["read_paths_sha256"])
                or row[1]
                != digest(
                    {
                        k: result[k]
                        for k in ("repository_identity_sha256", "base_sha", "read_paths_sha256")
                    }
                )
                or row[2] != digest(result)
                or any(
                    result[k] != binding.get(k)
                    for k in (
                        "execution_id",
                        "run_id",
                        "intent_id",
                        "requirement_sha256",
                        "authorization_ceiling_sha256",
                    )
                )
                or result["snapshot_sha256"]
                != digest({k: v for k, v in result.items() if k != "snapshot_sha256"})
            ):
                raise ValueError()
            expected = {}
            for x in result["files"]:
                if (
                    not isinstance(x, dict)
                    or set(x) != {"path", "mode", "size", "sha256"}
                    or x["path"] in expected
                    or self._paths([x["path"]]) != [x["path"]]
                    or x["mode"] not in {"100644", "100755"}
                    or not isinstance(x["size"], int)
                    or isinstance(x["size"], bool)
                    or x["size"] < 0
                    or not self._sha256(x["sha256"])
                ):
                    raise ValueError()
                expected[x["path"]] = x
            if (
                not expected
                or len(expected) > _MAX_FILES
                or sum(x["size"] for x in expected.values()) != result["total_bytes"]
                or result["total_bytes"] > _MAX_BYTES
                or [(p, s) for p, s in refs]
                != [(p, expected[p]["sha256"]) for p in sorted(expected)]
            ):
                raise ValueError()
            content = {}
            for p, x in expected.items():
                if self.private_root is not None:
                    self._validate_private(True)
                target = self.artifacts / x["sha256"]
                info = target.lstat()
                if (
                    not stat.S_ISREG(info.st_mode)
                    or stat.S_ISLNK(info.st_mode)
                    or info.st_nlink != 1
                ):
                    raise ValueError()
                if info.st_size != x["size"]:
                    raise ValueError()
                with target.open("rb") as stream:
                    value = stream.read(x["size"] + 1)
                if len(value) != x["size"] or hashlib.sha256(value).hexdigest() != x["sha256"]:
                    raise ValueError()
                content[p] = value
        except (OSError, KeyError, TypeError, ValueError):
            raise RunError("PLANNING_REPOSITORY_SNAPSHOT_CHANGED") from None
        return {**result, "content": content}
