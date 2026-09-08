"""Private, immutable repository input snapshots for planning.

This is deliberately a separate ledger keyed by the existing planning binding
digest.  It does not alter the v1 execution binding consumed by Admission.
"""

import hashlib
import json
import os
import sqlite3
import stat
import subprocess
from pathlib import Path
from typing import Any

from karajan.runs import RunError
from karajan.runs.planning import digest, encoded
from karajan.storage import ExistingStoreError, open_database, require_schema

_NAME = "planning-repository-snapshots.sqlite"
_MAX_FILES = 2_000
_MAX_BYTES = 8_000_000


def snapshot_database(control_directory: Path) -> Path:
    """Fixed private location; callers cannot select a snapshot store."""
    from .planning_bootstrap import read_planning_bootstrap

    settings, _ = read_planning_bootstrap(control_directory)
    return settings.state_directory / _NAME


def provision_planning_repository_snapshots(control_directory: Path) -> Path:
    """Controller provisioning entry.  It creates only the fixed private ledger."""
    path = snapshot_database(control_directory)
    PlanningRepositorySnapshotStore(path)
    try:
        path.chmod(0o600)
    except OSError as error:
        raise RunError("PLANNING_SNAPSHOT_PROVISION_FAILED") from error
    return path


class PlanningRepositorySnapshotStore:
    def __init__(self, database: Path, *, existing_only: bool = False) -> None:
        self.database = database.resolve()
        self.existing_only = existing_only
        if existing_only:
            require_schema(
                self.database,
                {
                    "snapshots": ["binding_sha256", "data"],
                    "blobs": ["binding_sha256", "path", "content"],
                },
            )
        else:
            self.database.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as db:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS snapshots "
                    "(binding_sha256 TEXT PRIMARY KEY, data TEXT NOT NULL)"
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS blobs (binding_sha256 TEXT NOT NULL, "
                    "path TEXT NOT NULL, content BLOB NOT NULL, "
                    "PRIMARY KEY(binding_sha256,path))"
                )
                db.commit()

    def _connect(self) -> sqlite3.Connection:
        return open_database(self.database, existing_only=self.existing_only, isolation_level=None)

    @staticmethod
    def _allowed(path: str, paths: list[str]) -> bool:
        return any(path == item or path.startswith(item + "/") for item in paths)

    @staticmethod
    def _paths(value: object) -> list[str]:
        if not isinstance(value, list) or not value:
            raise RunError("PLANNING_SNAPSHOT_PATHS_INVALID")
        result: list[str] = []
        for item in value:
            if (
                not isinstance(item, str)
                or not item
                or item.startswith("/")
                or "\\" in item
                or any(part in {"", ".", ".."} for part in item.split("/"))
            ):
                raise RunError("PLANNING_SNAPSHOT_PATHS_INVALID")
            result.append(item)
        return sorted(set(result))

    @staticmethod
    def _sha256(value: object) -> bool:
        if not isinstance(value, str) or len(value) != 64:
            return False
        try:
            int(value, 16)
        except ValueError:
            return False
        return True

    @staticmethod
    def _git(root: Path, *args: str) -> bytes:
        env = {
            key: os.environ[key]
            for key in ("SystemRoot", "WINDIR", "PATH", "TEMP", "TMP")
            if key in os.environ
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
            result = subprocess.run(
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
        if result.returncode:
            raise RunError("PLANNING_SNAPSHOT_BASE_UNAVAILABLE")
        return result.stdout

    def freeze(
        self, binding: dict[str, Any], run: dict[str, Any], project: dict[str, Any]
    ) -> dict[str, Any]:
        binding_sha = digest(binding)
        if project.get("id") != run.get("project_id") or project.get("revision") != run.get(
            "configuration_snapshot", {}
        ).get("project_revision"):
            raise RunError("PLANNING_REPOSITORY_SOURCE_CHANGED")
        repo = project.get("repository")
        if not isinstance(repo, dict) or not all(
            isinstance(repo.get(key), str) for key in ("root", "identity_sha256", "base_sha")
        ):
            raise RunError("PLANNING_REPOSITORY_SOURCE_INVALID")
        root = Path(repo["root"])
        try:
            resolved = root.resolve(strict=True)
            if resolved != root or not stat.S_ISDIR(root.stat().st_mode):
                raise ValueError
        except (OSError, ValueError):
            raise RunError("PLANNING_REPOSITORY_SOURCE_INVALID") from None
        paths = self._paths(run["authorization_ceiling"]["read_paths"])
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT data FROM snapshots WHERE binding_sha256=?", (binding_sha,)
            ).fetchone()
            if old:
                db.commit()
                return {key: value for key, value in self.read(binding).items() if key != "content"}
            raw = self._git(root, "ls-tree", "-r", "-z", "--full-tree", repo["base_sha"])
            rows: list[tuple[str, str, bytes]] = []
            matched = {item: False for item in paths}
            total = 0
            for entry in raw.split(b"\0"):
                if not entry:
                    continue
                head, path_raw = entry.split(b"\t", 1)
                (
                    mode,
                    kind,
                    _,
                ) = head.decode("ascii").split(" ")
                path = path_raw.decode("utf-8", "strict")
                if self._allowed(path, paths):
                    if kind != "blob" or mode not in {"100644", "100755"}:
                        raise RunError("PLANNING_SNAPSHOT_ENTRY_UNSUPPORTED")
                    for item in matched:
                        matched[item] = matched[item] or path == item or path.startswith(item + "/")
                    size = int(
                        self._git(root, "cat-file", "-s", repo["base_sha"] + ":" + path).decode(
                            "ascii"
                        )
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
            files = [
                {"path": p, "mode": m, "size": len(c), "sha256": hashlib.sha256(c).hexdigest()}
                for p, m, c in rows
            ]
            requirement_sha = binding.get("requirement_sha256")
            authorization_sha = binding.get("authorization_ceiling_sha256")
            if not self._sha256(requirement_sha) or not self._sha256(authorization_sha):
                raise RunError("PLANNING_REPOSITORY_SOURCE_INVALID")
            result = {
                "schema_version": "karajan.planning-repository-snapshot.v1",
                "binding_sha256": binding_sha,
                "execution_id": binding["execution_id"],
                "run_id": binding["run_id"],
                "intent_id": binding["intent_id"],
                "repository_identity_sha256": repo["identity_sha256"],
                "base_sha": repo["base_sha"],
                "read_paths_sha256": digest(paths),
                "requirement_sha256": requirement_sha,
                "authorization_ceiling_sha256": authorization_sha,
                "files": files,
                "total_bytes": sum(len(c) for _, _, c in rows),
            }
            result["snapshot_sha256"] = digest(result)
            db.execute("INSERT INTO snapshots VALUES (?,?)", (binding_sha, encoded(result)))
            db.executemany(
                "INSERT INTO blobs VALUES (?,?,?)", [(binding_sha, p, c) for p, _, c in rows]
            )
            db.commit()
            return result

    def read(self, binding: dict[str, Any]) -> dict[str, Any]:
        binding_sha = digest(binding)
        try:
            with self._connect() as db:
                row = db.execute(
                    "SELECT data FROM snapshots WHERE binding_sha256=?", (binding_sha,)
                ).fetchone()
                if row is None:
                    raise RunError("PLANNING_REPOSITORY_SNAPSHOT_NOT_FOUND")
                result = json.loads(row[0])
                blobs = db.execute(
                    "SELECT path,content FROM blobs WHERE binding_sha256=? ORDER BY path",
                    (binding_sha,),
                ).fetchall()
        except ExistingStoreError:
            raise RunError("PLANNING_REPOSITORY_SNAPSHOT_UNAVAILABLE") from None
        except sqlite3.Error:
            raise RunError("PLANNING_REPOSITORY_SNAPSHOT_CHANGED") from None
        try:
            if not isinstance(result, dict) or set(result) != {
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
            }:
                raise ValueError
            if (
                result["schema_version"] != "karajan.planning-repository-snapshot.v1"
                or result["binding_sha256"] != binding_sha
                or any(
                    result[key] != binding.get(key)
                    for key in ("execution_id", "run_id", "intent_id")
                )
                or result["requirement_sha256"] != binding.get("requirement_sha256")
                or result["authorization_ceiling_sha256"]
                != binding.get("authorization_ceiling_sha256")
                or not all(
                    self._sha256(result[key])
                    for key in (
                        "binding_sha256",
                        "repository_identity_sha256",
                        "read_paths_sha256",
                        "requirement_sha256",
                        "authorization_ceiling_sha256",
                        "snapshot_sha256",
                    )
                )
                or not isinstance(result["base_sha"], str)
                or not isinstance(result["files"], list)
                or not isinstance(result["total_bytes"], int)
                or isinstance(result["total_bytes"], bool)
                or result["total_bytes"] < 0
                or result["snapshot_sha256"]
                != digest({key: value for key, value in result.items() if key != "snapshot_sha256"})
            ):
                raise RunError("PLANNING_REPOSITORY_SNAPSHOT_CHANGED")
            expected: dict[str, dict[str, Any]] = {}
            for item in result["files"]:
                if (
                    not isinstance(item, dict)
                    or set(item) != {"path", "mode", "size", "sha256"}
                    or not isinstance(item["path"], str)
                    or item["path"] in expected
                    or self._paths([item["path"]]) != [item["path"]]
                    or item["mode"] not in {"100644", "100755"}
                    or not isinstance(item["size"], int)
                    or isinstance(item["size"], bool)
                    or item["size"] < 0
                    or not self._sha256(item["sha256"])
                ):
                    raise ValueError
                expected[item["path"]] = item
            if (
                not expected
                or sum(item["size"] for item in expected.values()) != result["total_bytes"]
                or len(blobs) != len(expected)
                or any(
                    not isinstance(item[0], str)
                    or not isinstance(item[1], bytes)
                    or item[0] not in expected
                    or hashlib.sha256(item[1]).hexdigest() != expected[item[0]]["sha256"]
                    or len(item[1]) != expected[item[0]]["size"]
                    for item in blobs
                )
            ):
                raise ValueError
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise RunError("PLANNING_REPOSITORY_SNAPSHOT_CHANGED") from None
        return {**result, "content": {path: bytes(content) for path, content in blobs}}
