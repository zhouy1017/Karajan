"""Private immutable content-addressed repository snapshots for planning."""

import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import tempfile
import time
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from karajan.projects.credential_sources import _plain, _private
from karajan.runs import RunError
from karajan.runs.planning import digest, encoded
from karajan.storage import ExistingStoreError, open_database, require_schema

_NAME = "planning-repository-snapshots.sqlite"
_ARTIFACTS = "planning-repository-snapshot-blobs"
_MAX_FILES = 2_000
_MAX_BYTES = 8_000_000
_MOVEFILE_WRITE_THROUGH = 0x8
_ERROR_FILE_EXISTS = 80
_ERROR_ALREADY_EXISTS = 183


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
                    "snapshots": ["binding_sha256", "data", "source_sha256"],
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
                "source_sha256 TEXT)"
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(snapshots)")}
            if "source_sha256" not in columns:
                db.execute("ALTER TABLE snapshots ADD COLUMN source_sha256 TEXT")
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
    def _git(root: Path, *args: str) -> bytes:
        env = {
            k: os.environ[k]
            for k in ("SystemRoot", "WINDIR", "PATH", "TEMP", "TMP")
            if k in os.environ
        }
        env.update(
            {
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_NO_REPLACE_OBJECTS": "1",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_TERMINAL_PROMPT": "0",
            }
        )
        try:
            r = subprocess.run(
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
                    *args,
                ],
                capture_output=True,
                timeout=10,
                env=env,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise RunError("PLANNING_SNAPSHOT_GIT_UNAVAILABLE") from None
        if r.returncode:
            raise RunError("PLANNING_SNAPSHOT_BASE_UNAVAILABLE")
        return r.stdout

    def _sync_artifacts(self, target: Path | None = None) -> None:
        """Durably record a blob directory entry before a SQLite reference.

        POSIX requires a directory fsync after a name change.  Windows uses a
        same-directory ``MoveFileExW(..., MOVEFILE_WRITE_THROUGH)`` commit;
        there is no directory fsync substitute in this boundary.  The blob has
        already been flushed before that rename, and is flushed once more here
        before its SQLite reference is committed.
        """
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
        import ctypes

        move_file = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
        move_file.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
        move_file.restype = ctypes.c_int
        if move_file(source, str(target), _MOVEFILE_WRITE_THROUGH):
            return
        error = ctypes.get_last_error()
        if error in {_ERROR_FILE_EXISTS, _ERROR_ALREADY_EXISTS}:
            raise FileExistsError(error, "artifact already exists", str(target))
        raise ctypes.WinError(error)

    def _published_content(self, target: Path, content: bytes) -> bool:
        """Accept only our short-lived link(2) overlap, never a durable alias."""
        for _ in range(100):
            info = target.lstat()
            if stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                if info.st_nlink == 1:
                    with target.open("rb") as stream:
                        return stream.read(len(content) + 1) == content
                # A competing publisher owns a temporary link until it unlinks
                # it.  Do not mistake that exact protocol interval for an
                # attacker-created alias, but never accept it as final state.
                if info.st_nlink == 2:
                    time.sleep(0.005)
                    continue
            raise ValueError()
        raise ValueError()

    def _publish(self, sha: str, content: bytes) -> None:
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
                    os.link(name, target)
                except FileExistsError:
                    pass
                finally:
                    os.unlink(name)
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
        key = digest(binding)
        if project.get("id") != run.get("project_id") or project.get("revision") != run.get(
            "configuration_snapshot", {}
        ).get("project_revision"):
            raise RunError("PLANNING_REPOSITORY_SOURCE_CHANGED")
        repo = project.get("repository")
        if not isinstance(repo, dict) or not all(
            isinstance(repo.get(k), str) for k in ("root", "identity_sha256", "base_sha")
        ) or not (
            self._sha256(repo.get("identity_sha256")) and self._git_oid(repo.get("base_sha"))
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
        for entry in self._git(root, "ls-tree", "-r", "-z", "--full-tree", repo["base_sha"]).split(
            b"\0"
        ):
            if not entry:
                continue
            head, raw = entry.split(b"\t", 1)
            mode, kind, _ = head.decode("ascii").split(" ")
            path = raw.decode("utf-8", "strict")
            if not self._allowed(path, paths):
                continue
            if kind != "blob" or mode not in {"100644", "100755"}:
                raise RunError("PLANNING_SNAPSHOT_ENTRY_UNSUPPORTED")
            for x in matched:
                matched[x] = matched[x] or self._within(path, x)
            size = int(
                self._git(root, "cat-file", "-s", repo["base_sha"] + ":" + path).decode("ascii")
            )
            total += size
            if len(rows) >= _MAX_FILES or total > _MAX_BYTES:
                raise RunError("PLANNING_SNAPSHOT_LIMIT_EXCEEDED")
            content = self._git(root, "cat-file", "blob", repo["base_sha"] + ":" + path)
            if len(content) != size:
                raise RunError("PLANNING_SNAPSHOT_BASE_UNAVAILABLE")
            rows.append((path, mode, content))
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
            {
                k: result[k]
                for k in ("repository_identity_sha256", "base_sha", "read_paths_sha256")
            }
        )
        result["snapshot_sha256"] = digest(result)
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
                        "INSERT INTO snapshots VALUES (?,?,?)",
                        (key, encoded(result), source_sha256),
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
        key = digest(binding)
        try:
            with self._connect() as db:
                row = db.execute(
                    "SELECT data,source_sha256 FROM snapshots WHERE binding_sha256=?", (key,)
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
