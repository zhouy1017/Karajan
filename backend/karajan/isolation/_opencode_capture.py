"""Pinned, bounded file reads for a controller-owned stopped native projection."""

import hashlib
import json
import os
import stat
import sys
import weakref
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_CAPTURE_BYTES = 64 * 1024 * 1024
MAX_CAPTURE_ENTRIES = 4096


@dataclass(frozen=True, slots=True)
class ProjectionEntry:
    path: str
    sha256: str
    writable: bool


@dataclass(frozen=True, slots=True)
class StoppedProjection:
    """Immutable output bytes, not proof of Task/Run/Attempt authorization."""

    runtime_sha256: str
    projection: tuple[ProjectionEntry, ...]
    files: tuple[tuple[str, bytes], ...] = field(repr=False)
    _stop_json: str = field(repr=False)

    @property
    def stop_evidence(self) -> dict[str, Any]:
        """A detached view: a consumer cannot mutate the retained stop receipt."""
        result: dict[str, Any] = json.loads(self._stop_json)
        return result


def _identity(info: os.stat_result) -> tuple[int, int, int]:
    return info.st_dev, info.st_ino, info.st_mode


def _version(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (*_identity(info), info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _release(fds: list[int]) -> None:
    while fds:
        try:
            os.close(fds.pop())
        except OSError:
            pass


class ProjectionPins:
    """Hold original inodes until capture or object disposal, preventing inode reuse.

    Start permits unprojected host files for existing isolation diagnostics. Only
    capture requires the exact projected tree, after the owned runtime has stopped.
    The caller must serialize lifecycle and capture operations.
    """

    root: Path
    projection: tuple[ProjectionEntry, ...]
    _finalize: Callable[[], None]
    _initial_versions: dict[str, tuple[int, int, int, int, int, int]]

    def __init__(self, root: Path, projection: list[dict[str, Any]]) -> None:
        if sys.platform != "linux":
            raise ValueError("LINUX_NAMESPACES_REQUIRED")
        self.root = root
        self.projection = tuple(ProjectionEntry(**row) for row in projection)
        self._fds: list[int] = []
        self._finalize = weakref.finalize(self, _release, self._fds)
        self._directories: dict[str, tuple[int, tuple[int, int, int]]] = {}
        self._files: dict[str, tuple[int, tuple[int, int, int]]] = {}
        self._initial_versions = {}
        self._children: dict[str, set[str]] = {"": set()}
        try:
            directories: set[str] = {""}
            for row in self.projection:
                path = PurePosixPath(row.path)
                for ancestor in path.parents:
                    directories.add("" if ancestor == PurePosixPath(".") else str(ancestor))
            if len(directories) + len(projection) > MAX_CAPTURE_ENTRIES:
                raise ValueError("PROJECTION_CAPTURE_LIMIT_EXCEEDED")
            for relative in sorted(directories, key=lambda p: (p.count("/"), len(p), p)):
                if not relative:
                    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
                else:
                    parent, name = self._parent(relative)
                    fd = os.open(
                        name,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                        dir_fd=self._directories[parent][0],
                    )
                    self._children[parent].add(name)
                self._fds.append(fd)
                self._directories[relative] = (fd, _identity(os.fstat(fd)))
                self._children.setdefault(relative, set())
            total = 0
            for row in self.projection:
                parent, name = self._parent(row.path)
                fd = os.open(
                    name,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                    dir_fd=self._directories[parent][0],
                )
                self._fds.append(fd)
                identity = os.fstat(fd)
                self._files[row.path] = (fd, _identity(identity))
                self._children[parent].add(name)
                content = self._read_file(row.path)
                self._initial_versions[row.path] = _version(os.fstat(fd))
                total += len(content)
                if total > MAX_CAPTURE_BYTES:
                    raise ValueError("PROJECTION_CAPTURE_LIMIT_EXCEEDED")
                if hashlib.sha256(content).hexdigest() != row.sha256:
                    raise ValueError("WORKSPACE_PROJECTION_CONTENT_CHANGED")
            self._check_paths()
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _parent(relative: str) -> tuple[str, str]:
        parent, _, name = relative.rpartition("/")
        return parent, name

    def _check_paths(self) -> None:
        if _identity(self.root.lstat()) != self._directories[""][1]:
            raise ValueError("PROJECTION_CAPTURE_IDENTITY_CHANGED")
        for relative, (fd, expected) in {**self._directories, **self._files}.items():
            if _identity(os.fstat(fd)) != expected:
                raise ValueError("PROJECTION_CAPTURE_IDENTITY_CHANGED")
            if relative:
                parent, name = self._parent(relative)
                current = os.stat(name, dir_fd=self._directories[parent][0], follow_symlinks=False)
                if _identity(current) != expected:
                    raise ValueError("PROJECTION_CAPTURE_IDENTITY_CHANGED")

    def _check_tree(self) -> None:
        self._check_paths()
        for relative, (fd, _) in self._directories.items():
            expected = self._children[relative]
            found: set[str] = set()
            with os.scandir(fd) as entries:
                for entry in entries:
                    if entry.name not in expected:
                        raise ValueError("PROJECTION_CAPTURE_TREE_MISMATCH")
                    found.add(entry.name)
            if found != expected:
                raise ValueError("PROJECTION_CAPTURE_TREE_MISMATCH")

    def _read_file(self, relative: str) -> bytes:
        if sys.platform != "linux":
            raise ValueError("LINUX_NAMESPACES_REQUIRED")
        fd, expected = self._files[relative]
        before = os.fstat(fd)
        if (
            _identity(before) != expected
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
        ):
            raise ValueError("PROJECTION_CAPTURE_FILE_INVALID")
        if before.st_size > MAX_FILE_BYTES:
            raise ValueError("PROJECTION_CAPTURE_LIMIT_EXCEEDED")
        blocks = bytearray()
        while len(blocks) <= MAX_FILE_BYTES:
            block = os.pread(fd, min(65536, MAX_FILE_BYTES + 1 - len(blocks)), len(blocks))
            if not block:
                break
            blocks.extend(block)
        after = os.fstat(fd)
        if len(blocks) > MAX_FILE_BYTES:
            raise ValueError("PROJECTION_CAPTURE_LIMIT_EXCEEDED")
        if _version(before) != _version(after) or len(blocks) != after.st_size:
            raise ValueError("PROJECTION_CAPTURE_CONTENT_UNSTABLE")
        if after.st_nlink != 1:
            raise ValueError("PROJECTION_CAPTURE_FILE_INVALID")
        return bytes(blocks)

    def capture(self, runtime_sha256: str, stopped: dict[str, Any]) -> StoppedProjection:
        self._check_tree()
        versions = {path: _version(os.fstat(fd)) for path, (fd, _) in self._files.items()}
        files: list[tuple[str, bytes]] = []
        total = 0
        for row in self.projection:
            content = self._read_file(row.path)
            total += len(content)
            if total > MAX_CAPTURE_BYTES:
                raise ValueError("PROJECTION_CAPTURE_LIMIT_EXCEEDED")
            if not row.writable:
                if (
                    hashlib.sha256(content).hexdigest() != row.sha256
                    or versions[row.path] != self._initial_versions[row.path]
                ):
                    raise ValueError("PROJECTION_CAPTURE_READONLY_CHANGED")
            files.append((row.path, content))
        self._check_tree()
        if any(_version(os.fstat(fd)) != versions[path] for path, (fd, _) in self._files.items()):
            raise ValueError("PROJECTION_CAPTURE_CONTENT_UNSTABLE")
        return StoppedProjection(
            runtime_sha256,
            self.projection,
            tuple(files),
            json.dumps(stopped, sort_keys=True, separators=(",", ":"), allow_nan=False),
        )

    def close(self) -> None:
        self._finalize()
