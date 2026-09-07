"""Controller-owned sources for current Go Worker and readonly Reviewer facts."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from karajan.projects import ProjectRegistry
from karajan.projects.qualification import ProfileQualificationStore
from karajan.runs import RunError
from karajan.runs.planning import identifier

from .go_task_runtime import GoTaskCredentialSource

_SCHEMA = "karajan.go-qualification-sources.v1"
_PATHS = (
    "runtime",
    "tokenizer_directory",
    "journal_path",
    "worker_work_root",
    "reviewer_work_root",
    "credential_private_directory",
)


@dataclass(frozen=True, repr=False)
class GoQualificationSettings:
    runtime: Path
    tokenizer_directory: Path
    journal_path: Path
    worker_work_root: Path
    reviewer_work_root: Path
    credential_private_directory: Path
    credential_sources: tuple[GoTaskCredentialSource, ...] = field(repr=False)

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
    def from_document(cls, value: object) -> "GoQualificationSettings":
        try:
            if (
                type(value) is not dict
                or set(value) != {"schema_version", *_PATHS, "credential_sources"}
                or value["schema_version"] != _SCHEMA
                or type(value["credential_sources"]) is not list
            ):
                raise ValueError

            def path(raw: object) -> Path:
                if type(raw) is not str or not raw or "\x00" in raw:
                    raise ValueError
                item = Path(raw)
                if not item.is_absolute() or ".." in item.parts or str(item) != raw:
                    raise ValueError
                return item

            sources = []
            for row in value["credential_sources"]:
                if type(row) is not dict or set(row) != {
                    "project_id",
                    "auth_ref",
                    "source_id",
                    "path",
                }:
                    raise ValueError
                for key in ("project_id", "auth_ref", "source_id"):
                    identifier(row[key])
                sources.append(
                    GoTaskCredentialSource(
                        row["project_id"],
                        row["auth_ref"],
                        row["source_id"],
                        path(row["path"]),
                    )
                )
            if len({(row.project_id, row.auth_ref) for row in sources}) != len(sources):
                raise ValueError
            return cls(
                **{name: path(value[name]) for name in _PATHS}, credential_sources=tuple(sources)
            )
        except (KeyError, TypeError, ValueError, OSError):
            raise RunError("QUALIFICATION_BOOTSTRAP_INVALID") from None

    def paths(self) -> tuple[Path, ...]:
        """All trusted paths that must remain outside model repositories and Check roots."""
        return (
            *(getattr(self, name) for name in _PATHS),
            *(row.path for row in self.credential_sources),
        )


def open_go_qualification_store(
    projects: ProjectRegistry,
    settings: GoQualificationSettings | None,
    *,
    for_current: bool = False,
) -> ProfileQualificationStore:
    """History never opens current keys, tokenizer, runtime, Journal or suite sources.

    Current composition reopens already provisioned ledgers and credential seals.
    It offers no HTTP fixture, endpoint, registration or provider execution switch.
    """
    if not for_current or settings is None:
        return ProfileQualificationStore(projects)
    if not projects.existing_only:
        raise RunError("EXISTING_QUALIFICATION_STORE_REQUIRED")
    settings = GoQualificationSettings.from_document(settings.document())

    from karajan.adapters.opencode.go_context import GoRequestAccounting
    from karajan.adapters.opencode.go_journal import GoCallJournal
    from karajan.projects.credential_sources import CredentialSourceStore, LocalKeyFile, _private
    from karajan.projects.go_reviewer_suite import FixedGoReviewerSuite
    from karajan.projects.go_suite import V2_SUITE_REF, FixedGoSuite

    from .go_task_runtime import _plain

    repositories = [Path(row["repository"]["root"]).resolve() for row in projects.list()]
    if any(path.is_relative_to(root) for path in settings.paths() for root in repositories):
        raise RunError("QUALIFICATION_CONTROL_STATE_IN_REPOSITORY")
    for path in (settings.runtime, settings.journal_path):
        _plain(path)
    for path in (
        settings.tokenizer_directory,
        settings.worker_work_root,
        settings.reviewer_work_root,
        settings.credential_private_directory,
    ):
        _plain(path, directory=True)
    for path in (
        settings.worker_work_root,
        settings.reviewer_work_root,
        settings.credential_private_directory,
    ):
        _private(path, directory=True)
    _private(settings.journal_path)
    accounting = GoRequestAccounting(settings.tokenizer_directory)
    journal = GoCallJournal(settings.journal_path, existing_only=True)
    credentials = CredentialSourceStore(
        projects,
        sources={
            (row.project_id, row.auth_ref): LocalKeyFile(row.source_id, row.path)
            for row in settings.credential_sources
        },
        private_directory=settings.credential_private_directory,
        existing_only=True,
    )
    return ProfileQualificationStore(
        projects,
        credentials=credentials,
        go_suite=FixedGoSuite(
            settings.runtime,
            settings.worker_work_root,
            journal,
            suite_ref=V2_SUITE_REF,
            accounting=accounting,
        ),
        reviewer_suite=FixedGoReviewerSuite(
            settings.runtime, settings.reviewer_work_root, journal, accounting=accounting
        ),
    )
