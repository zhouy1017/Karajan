"""Private Commander producer composition and protected v2 descriptor."""

import hashlib
import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from karajan.projects import ProjectRegistry
from karajan.projects.credential_sources import CredentialSourceStore, LocalKeyFile, _private
from karajan.projects.go_commander_suite import FixedGoCommanderSuite
from karajan.projects.qualification import ProfileQualificationStore
from karajan.runs import RunError
from karajan.runs.planning import identifier

DESCRIPTOR_NAME = "commander-qualification-source.v2.json"
_SCHEMA = "karajan.commander-qualification-settings.v2"
_PATHS = ("runtime", "tokenizer_directory", "credential_private_directory")


@dataclass(frozen=True)
class CommanderCredentialSource:
    project_id: str
    auth_ref: str
    source_id: str
    path: Path = field(repr=False)


@dataclass(frozen=True, repr=False)
class CommanderQualificationSettings:
    runtime: Path
    tokenizer_directory: Path
    credential_private_directory: Path
    credential_sources: tuple[CommanderCredentialSource, ...] = field(repr=False)

    def document(self) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA,
            **{name: str(getattr(self, name)) for name in _PATHS},
            "credential_sources": [
                {
                    "project_id": row.project_id,
                    "auth_ref": row.auth_ref,
                    "source_id": row.source_id,
                    "path": str(row.path),
                }
                for row in self.credential_sources
            ],
        }

    @classmethod
    def from_document(cls, value: object) -> "CommanderQualificationSettings":
        try:
            if (
                type(value) is not dict
                or set(value) != {"schema_version", *_PATHS, "credential_sources"}
                or value["schema_version"] != _SCHEMA
                or type(value["credential_sources"]) is not list
            ):
                raise ValueError

            def path(raw: object) -> Path:
                if type(raw) is not str or not raw or "\0" in raw:
                    raise ValueError
                result = Path(raw)
                if not result.is_absolute() or ".." in result.parts or str(result) != raw:
                    raise ValueError
                return result

            sources = []
            for row in value["credential_sources"]:
                if type(row) is not dict or set(row) != {
                    "project_id",
                    "auth_ref",
                    "source_id",
                    "path",
                }:
                    raise ValueError
                for name in ("project_id", "auth_ref", "source_id"):
                    identifier(row[name])
                sources.append(
                    CommanderCredentialSource(
                        row["project_id"], row["auth_ref"], row["source_id"], path(row["path"])
                    )
                )
            if len({(row.project_id, row.auth_ref) for row in sources}) != len(sources):
                raise ValueError
            return cls(
                **{name: path(value[name]) for name in _PATHS}, credential_sources=tuple(sources)
            )
        except (KeyError, TypeError, ValueError, OSError):
            raise RunError("COMMANDER_QUALIFICATION_BOOTSTRAP_INVALID") from None


def _plain(path: Path, *, directory: bool = False) -> None:
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or (
            not stat.S_ISDIR(info.st_mode) if directory else not stat.S_ISREG(info.st_mode)
        ):
            raise OSError
    except OSError:
        raise RunError("COMMANDER_QUALIFICATION_SOURCE_UNAVAILABLE") from None


def read_commander_qualification_settings(
    control_directory: Path,
) -> tuple[CommanderQualificationSettings, str]:
    path = control_directory / DESCRIPTOR_NAME
    try:
        _plain(control_directory, directory=True)
        _private(control_directory, directory=True)
        _plain(path)
        _private(path)
        raw = path.read_bytes()
        if len(raw) > 32768:
            raise ValueError
        return CommanderQualificationSettings.from_document(json.loads(raw)), hashlib.sha256(
            raw
        ).hexdigest()
    except (OSError, ValueError, RunError):
        raise RunError("COMMANDER_QUALIFICATION_BOOTSTRAP_INVALID") from None


def write_commander_qualification_settings(
    control_directory: Path, settings: CommanderQualificationSettings
) -> Path:
    settings = CommanderQualificationSettings.from_document(settings.document())
    if control_directory != control_directory.absolute():
        raise RunError("COMMANDER_QUALIFICATION_BOOTSTRAP_INVALID")
    _plain(control_directory, directory=True)
    _private(control_directory, directory=True)
    path = control_directory / DESCRIPTOR_NAME
    raw = (json.dumps(settings.document(), sort_keys=True, separators=(",", ":")) + "\n").encode()
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        _private(path)
    except OSError:
        raise RunError("COMMANDER_QUALIFICATION_BOOTSTRAP_INVALID") from None
    return path


def open_go_commander_qualification_store(
    projects: ProjectRegistry,
    settings: CommanderQualificationSettings,
    *,
    descriptor_sha256: str,
    existing_only: bool = False,
) -> ProfileQualificationStore:
    settings = CommanderQualificationSettings.from_document(settings.document())
    if existing_only and not projects.existing_only:
        raise RunError("EXISTING_QUALIFICATION_STORE_REQUIRED")
    repositories = [Path(row["repository"]["root"]).resolve() for row in projects.list()]
    paths = (
        settings.runtime,
        settings.tokenizer_directory,
        settings.credential_private_directory,
        *(row.path for row in settings.credential_sources),
    )
    if any(path.resolve().is_relative_to(root) for path in paths for root in repositories):
        raise RunError("QUALIFICATION_CONTROL_STATE_IN_REPOSITORY")
    _plain(settings.runtime)
    _plain(settings.tokenizer_directory, directory=True)
    _private(settings.credential_private_directory, directory=True)
    credentials = CredentialSourceStore(
        projects,
        sources={
            (row.project_id, row.auth_ref): LocalKeyFile(row.source_id, row.path)
            for row in settings.credential_sources
        },
        private_directory=settings.credential_private_directory,
        existing_only=existing_only,
    )
    return ProfileQualificationStore(
        projects,
        credentials=credentials,
        commander_suite=FixedGoCommanderSuite(
            settings.runtime, settings.tokenizer_directory, descriptor_sha256
        ),
    )


def qualify_commander_planning(
    store: ProfileQualificationStore,
    project_id: str,
    profile_ref: dict[str, Any],
    *,
    principal: str,
    command_key: str,
    validity_seconds: int,
) -> dict[str, Any]:
    return store.qualify_commander_planning(
        project_id,
        profile_ref,
        principal=principal,
        command_key=command_key,
        validity_seconds=validity_seconds,
    )
