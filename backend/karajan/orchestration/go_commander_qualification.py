"""Private Commander producer composition and protected v2 descriptor."""

import hashlib
import json
import os
import stat
from collections.abc import Callable
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
_SCHEMA = "karajan.commander-qualification-settings.v3"
_LEGACY_SCHEMA = "karajan.commander-qualification-settings.v2"
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
    journal_path: Path | None = None
    work_root: Path | None = None

    def document(self) -> dict[str, Any]:
        value = {
            "schema_version": _SCHEMA
            if self.journal_path is not None and self.work_root is not None
            else _LEGACY_SCHEMA,
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
        if value["schema_version"] == _SCHEMA:
            value.update(journal_path=str(self.journal_path), work_root=str(self.work_root))
        return value

    @classmethod
    def from_document(cls, value: object) -> "CommanderQualificationSettings":
        try:
            legacy_keys = {"schema_version", *_PATHS, "credential_sources"}
            current_keys = {*legacy_keys, "journal_path", "work_root"}
            if (
                type(value) is not dict
                or value.get("schema_version") == _LEGACY_SCHEMA
                and set(value) != legacy_keys
                or value.get("schema_version") == _SCHEMA
                and set(value) != current_keys
                or value.get("schema_version") not in {_SCHEMA, _LEGACY_SCHEMA}
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
            has_native = value["schema_version"] == _SCHEMA
            return cls(
                **{name: path(value[name]) for name in _PATHS},
                journal_path=path(value["journal_path"]) if has_native else None,
                work_root=path(value["work_root"]) if has_native else None,
                credential_sources=tuple(sources),
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


def _plain_hierarchy(path: Path, *, directory: bool = False) -> Path:
    """Return an alias-free absolute controller path without normalising it.

    ``Path.resolve`` is deliberately not used until every component has been
    inspected: resolving first turns a symlink or Windows reparse point into a
    seemingly trustworthy path.
    """
    absolute = path.absolute()
    try:
        for candidate in (absolute, *absolute.parents):
            info = candidate.lstat()
            if (
                stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                or candidate == absolute
                and (
                    not stat.S_ISDIR(info.st_mode)
                    if directory
                    else not stat.S_ISREG(info.st_mode)
                )
                or candidate == absolute
                and not directory
                and info.st_nlink != 1
            ):
                raise OSError
        return absolute
    except OSError:
        raise RunError("COMMANDER_QUALIFICATION_SOURCE_UNAVAILABLE") from None


def read_commander_qualification_settings(
    control_directory: Path,
) -> tuple[CommanderQualificationSettings, str]:
    path = control_directory / DESCRIPTOR_NAME
    try:
        _plain_hierarchy(control_directory, directory=True)
        _private(control_directory, directory=True)
        _plain_hierarchy(path)
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
    _plain_hierarchy(control_directory, directory=True)
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
        # POSIX directory fsync durably publishes the newly linked descriptor.
        # Windows does not expose an equivalent directory handle through this
        # API, so the file flush remains the truthful durability guarantee.
        if os.name == "posix":
            directory = os.open(control_directory, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except OSError:
        raise RunError("COMMANDER_QUALIFICATION_BOOTSTRAP_INVALID") from None
    return path


def validate_commander_qualification_settings(
    projects: ProjectRegistry,
    settings: CommanderQualificationSettings,
    *,
    repositories: tuple[Path, ...] | None = None,
) -> None:
    """Validate the complete current producer material without opening a store.

    This is intentionally usable while a Project transaction is already held by
    the current-reader. It performs no schema creation and no credential read.
    """
    settings = CommanderQualificationSettings.from_document(settings.document())
    if settings.journal_path is None or settings.work_root is None:
        raise RunError("COMMANDER_QUALIFICATION_SOURCE_UNAVAILABLE")
    if repositories is None:
        repositories = tuple(Path(row["repository"]["root"]).absolute() for row in projects.list())
    paths = (
        settings.runtime,
        settings.tokenizer_directory,
        settings.credential_private_directory,
        settings.journal_path,
        settings.work_root,
        *(row.path for row in settings.credential_sources),
    )
    if any(path.absolute().is_relative_to(root) for path in paths for root in repositories):
        raise RunError("QUALIFICATION_CONTROL_STATE_IN_REPOSITORY")
    _plain_hierarchy(settings.runtime)
    _plain_hierarchy(settings.tokenizer_directory, directory=True)
    _plain_hierarchy(settings.journal_path)
    _plain_hierarchy(settings.work_root, directory=True)
    _plain_hierarchy(settings.credential_private_directory, directory=True)
    for source in settings.credential_sources:
        _plain_hierarchy(source.path)
    _private(settings.credential_private_directory, directory=True)
    _private(settings.journal_path)
    _private(settings.work_root, directory=True)


def open_go_commander_qualification_store(
    projects: ProjectRegistry,
    settings: CommanderQualificationSettings | None = None,
    *,
    descriptor_sha256: str | None = None,
    existing_only: bool = False,
    control_directory: Path | None = None,
    _fixture_client_factory: Callable[[], Any] | None = None,
) -> ProfileQualificationStore:
    if control_directory is not None:
        if settings is not None or descriptor_sha256 is not None:
            raise RunError("COMMANDER_QUALIFICATION_BOOTSTRAP_INVALID")
        settings, descriptor_sha256 = read_commander_qualification_settings(control_directory)
    if settings is None or descriptor_sha256 is None:
        raise RunError("COMMANDER_QUALIFICATION_BOOTSTRAP_INVALID")
    settings = CommanderQualificationSettings.from_document(settings.document())
    if existing_only and not projects.existing_only:
        raise RunError("EXISTING_QUALIFICATION_STORE_REQUIRED")
    if settings.journal_path is None or settings.work_root is None:
        # Legacy v2 descriptors can expose the existing qualification history,
        # but cannot assemble a current producer or credential authority.
        return ProfileQualificationStore(projects, commander_reader_only=True)
    validate_commander_qualification_settings(projects, settings)
    journal = None
    if settings.journal_path is not None or settings.work_root is not None:
        if settings.journal_path is None or settings.work_root is None:
            raise RunError("COMMANDER_QUALIFICATION_SOURCE_UNAVAILABLE")
        from karajan.adapters.opencode.go_journal import GoCallJournal

        journal = GoCallJournal(settings.journal_path, existing_only=existing_only)
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
            settings.runtime,
            settings.tokenizer_directory,
            descriptor_sha256,
            journal=journal,
            work_root=settings.work_root,
            descriptor_path=(control_directory / DESCRIPTOR_NAME)
            if control_directory is not None
            else None,
            project_database=projects.database,
            client_factory=_fixture_client_factory,
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
