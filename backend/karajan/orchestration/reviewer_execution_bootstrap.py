"""Private bootstrap for reopening Reviewer execution state without provisioning it."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from karajan.runs import RunError

_NAME = "reviewer-execution-bootstrap.json"
_SCHEMA = "karajan.reviewer-execution-bootstrap.v1"


@dataclass(frozen=True)
class ReviewerExecutionSettings:
    control_directory: Path
    execution_database: Path
    state_directory: Path
    candidate_directory: Path
    host_directory: Path

    def document(self) -> dict[str, str]:
        return {
            "schema_version": _SCHEMA,
            "control_directory": str(self.control_directory),
            "execution_database": str(self.execution_database),
            "state_directory": str(self.state_directory),
            "candidate_directory": str(self.candidate_directory),
            "host_directory": str(self.host_directory),
        }

    @classmethod
    def from_document(cls, value: object) -> ReviewerExecutionSettings:
        keys = {
            "schema_version",
            "control_directory",
            "execution_database",
            "state_directory",
            "candidate_directory",
            "host_directory",
        }
        try:
            if (
                not isinstance(value, dict)
                or set(value) != keys
                or value["schema_version"] != _SCHEMA
            ):
                raise ValueError
            paths = {key: Path(value[key]) for key in keys - {"schema_version"}}
            if any(not path.is_absolute() or ".." in path.parts for path in paths.values()):
                raise ValueError
            return cls(**paths)
        except (KeyError, TypeError, ValueError):
            raise RunError("REVIEWER_EXECUTION_BOOTSTRAP_INVALID") from None


def _private(path: Path, *, directory: bool = False) -> None:
    try:
        info = path.stat()
        if (directory and not stat.S_ISDIR(info.st_mode)) or (
            not directory and not stat.S_ISREG(info.st_mode)
        ):
            raise ValueError
        if path.is_symlink() or (not directory and info.st_nlink != 1):
            raise ValueError
    except (OSError, ValueError):
        raise RunError("REVIEWER_EXECUTION_BOOTSTRAP_INVALID") from None


def provision_reviewer_execution_bootstrap(settings: ReviewerExecutionSettings) -> Path:
    settings = ReviewerExecutionSettings.from_document(settings.document())
    _private(settings.control_directory, directory=True)
    path = settings.control_directory / _NAME
    raw = (json.dumps(settings.document(), sort_keys=True, separators=(",", ":")) + "\n").encode()
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        raise RunError("REVIEWER_EXECUTION_BOOTSTRAP_PROVISION_FAILED") from None
    return path


def open_existing_reviewer_execution_bootstrap(
    control_directory: Path,
) -> tuple[ReviewerExecutionSettings, str]:
    _private(control_directory, directory=True)
    path = control_directory / _NAME
    _private(path)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            raw = stream.read(32769)
        if len(raw) > 32768:
            raise ValueError
        settings = ReviewerExecutionSettings.from_document(json.loads(raw))
        if (
            settings.control_directory != control_directory
            or not settings.execution_database.is_file()
        ):
            raise ValueError
        return settings, hashlib.sha256(raw).hexdigest()
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        raise RunError("REVIEWER_EXECUTION_BOOTSTRAP_INVALID") from None
