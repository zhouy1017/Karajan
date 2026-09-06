"""Persist project identity and controller-owned configuration decisions."""

import hashlib
import json
import os
import sqlite3
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter, ValidationError

from karajan.storage import open_database, require_schema

from .configuration import VALIDATOR_REVISION, validate_configuration, validator_identity
from .models import Identifier, ProjectCreate, ProjectUpdate, TaskPreview
from .publication import (
    PublicationError,
    apply_catalog,
    bind_version,
    compile_document,
    compiler_identity,
    effective_catalog,
    guard_identity,
    initialize,
)


class ProjectError(ValueError):
    def __init__(self, code: str, *, current_revision: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.current_revision = current_revision


def encoded(value: object) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise ProjectError("INPUT_NOT_JSON") from None


def identifier(value: object, code: str = "IDENTIFIER_INVALID") -> str:
    try:
        result = TypeAdapter(Identifier).validate_python(value, strict=True)
        result.encode("utf-8")
        if not result.isprintable():
            raise ValueError("nonprintable")
        return result
    except (ValidationError, UnicodeError, ValueError):
        raise ProjectError(code) from None


class ProjectRegistry:
    def __init__(
        self,
        database: Path,
        allowed_roots: Sequence[Path],
        *,
        clock: Callable[[], float] = time.time,
        preview_ttl_seconds: int = 300,
        existing_only: bool = False,
    ) -> None:
        if type(preview_ttl_seconds) is not int or not 1 <= preview_ttl_seconds <= 3600:
            raise ProjectError("PREVIEW_TTL_INVALID")
        self.database = database
        self.clock = clock
        self.preview_ttl_seconds = preview_ttl_seconds
        self.allowed_roots = tuple(path.resolve() for path in allowed_roots)
        self.existing_only = existing_only
        if existing_only:
            require_schema(
                self.database,
                {
                    "projects": ["id", "snapshot"],
                    "commands": ["principal", "key", "digest", "result"],
                    "previews": ["id", "project_id", "configuration", "result"],
                    "execution_policies": ["project_id", "id", "revision", "record"],
                    "project_owners": ["project_id", "principal"],
                    "rulebook_versions": ["project_id", "id", "revision", "digest", "result"],
                    "rulebook_publications": ["sequence", "project_id", "result"],
                    "effective_catalogs": ["project_id", "result"],
                    "rulebook_conflicts": ["project_id", "id", "revision", "digests"],
                    "publication_migrations": ["version"],
                },
            )
            return
        with self._transaction() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, snapshot TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS commands (principal TEXT NOT NULL, key TEXT NOT NULL, "
                "digest TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(principal, key))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS previews (id TEXT PRIMARY KEY, "
                "project_id TEXT NOT NULL "
                "REFERENCES projects(id), configuration TEXT, result TEXT NOT NULL)"
            )
            initialize(db)
            db.execute(
                "CREATE TABLE IF NOT EXISTS execution_policies (project_id TEXT NOT NULL "
                "REFERENCES projects(id), id TEXT NOT NULL, revision INTEGER NOT NULL, "
                "record TEXT NOT NULL, PRIMARY KEY(project_id,id,revision))"
            )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        db = open_database(self.database, existing_only=self.existing_only, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except PublicationError as error:
            db.rollback()
            raise ProjectError(str(error)) from None
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def create(
        self, request: dict[str, Any], *, command_key: str, principal: str
    ) -> dict[str, Any]:
        try:
            request = ProjectCreate.model_validate(request).model_dump()
        except ValidationError:
            raise ProjectError("PROJECT_INPUT_INVALID") from None
        digest = hashlib.sha256(encoded(["create", request]).encode()).hexdigest()
        with self._transaction() as db:
            previous = self._replay(db, principal, command_key, digest)
            if previous is not None:
                return previous
        repository = self._repository(request)
        snapshot = {
            "schema_version": "karajan.project.v1",
            "id": str(uuid.uuid4()),
            "revision": 1,
            "name": request["name"],
            "repository": repository,
            "target_branch": request["target_branch"],
            "allowed_target_branches": request["allowed_target_branches"],
            "configuration": {
                "revision": 0,
                "status": "unconfigured",
                "digest": None,
                "preview_id": None,
                "dispatch_eligible": False,
            },
            "live_qualified": False,
        }
        with self._transaction() as db:
            previous = self._replay(db, principal, command_key, digest)
            if previous is not None:
                return previous
            db.execute("INSERT INTO projects VALUES (?, ?)", (snapshot["id"], encoded(snapshot)))
            db.execute("INSERT INTO project_owners VALUES (?,?)", (snapshot["id"], principal))
            db.execute(
                "INSERT INTO commands VALUES (?, ?, ?, ?)",
                (principal, command_key, digest, encoded(snapshot)),
            )
        return snapshot

    def _repository(self, request: dict[str, Any]) -> dict[str, str]:
        root = Path(request["repository_path"]).resolve()
        if not any(root.is_relative_to(allowed) for allowed in self.allowed_roots):
            raise ProjectError("REPOSITORY_OUTSIDE_ROOTS")
        if self.database.resolve().parent.is_relative_to(root):
            raise ProjectError("REPOSITORY_CONTAINS_CONTROL_STATE")
        observed_root = Path(
            self._git(root, "REPOSITORY_INVALID", "rev-parse", "--show-toplevel")
        ).resolve()
        if root != observed_root:
            raise ProjectError("REPOSITORY_ROOT_REQUIRED")
        if request["target_branch"] not in request["allowed_target_branches"]:
            raise ProjectError("TARGET_BRANCH_NOT_ALLOWED")
        for branch in request["allowed_target_branches"]:
            self._git(root, "BRANCH_INVALID", "check-ref-format", "refs/heads/" + branch)
        base = self._git(
            root,
            "BASE_UNRESOLVED",
            "rev-parse",
            "--verify",
            "--end-of-options",
            request["base_ref"] + "^{commit}",
        )
        return {
            "root": str(root),
            "identity_sha256": hashlib.sha256(str(root).encode()).hexdigest(),
            "base_ref": request["base_ref"],
            "base_sha": base,
        }

    def update(
        self,
        project_id: str,
        request: dict[str, Any],
        *,
        expected_revision: int,
        command_key: str,
        principal: str,
    ) -> dict[str, Any]:
        project_id = identifier(project_id)
        try:
            request = ProjectUpdate.model_validate(request).model_dump()
        except ValidationError:
            raise ProjectError("PROJECT_INPUT_INVALID") from None
        digest = hashlib.sha256(
            encoded(["update", project_id, expected_revision, request]).encode()
        ).hexdigest()
        with self._transaction() as db:
            previous = self._replay(db, principal, command_key, digest)
            if previous is not None:
                return previous
        old = self.get(project_id)
        repository = self._repository({**request, "repository_path": old["repository"]["root"]})
        with self._transaction() as db:
            previous = self._replay(db, principal, command_key, digest)
            if previous is not None:
                return previous
            snapshot = self._current(db, project_id, expected_revision)
            snapshot.update(request)
            snapshot["repository"] = repository
            snapshot.pop("base_ref", None)
            snapshot["revision"] += 1
            db.execute("UPDATE projects SET snapshot=? WHERE id=?", (encoded(snapshot), project_id))
            db.execute(
                "INSERT INTO commands VALUES (?, ?, ?, ?)",
                (principal, command_key, digest, encoded(snapshot)),
            )
        return snapshot

    def _current(self, db: sqlite3.Connection, project_id: str, revision: int) -> dict[str, Any]:
        row = db.execute("SELECT snapshot FROM projects WHERE id=?", (project_id,)).fetchone()
        if row is None:
            raise ProjectError("PROJECT_NOT_FOUND")
        snapshot: dict[str, Any] = json.loads(row["snapshot"])
        if type(revision) is not int or revision != snapshot["revision"]:
            raise ProjectError("REVISION_CONFLICT", current_revision=snapshot["revision"])
        return snapshot

    def preview_configuration(
        self, project_id: str, configuration: dict[str, Any], *, command_key: str, principal: str
    ) -> dict[str, Any]:
        project_id = identifier(project_id)
        digest = hashlib.sha256(
            encoded(["preview", project_id, configuration]).encode()
        ).hexdigest()
        with self._transaction() as db:
            previous = self._replay(db, principal, command_key, digest)
            if previous is not None:
                return previous
        snapshot = self.get(project_id)
        with self._transaction() as db:
            self._require_owner(db, project_id, principal)
        issues = validate_configuration(configuration)
        can_apply = not any(
            issue["code"] in {"CREDENTIAL_VALUE_FORBIDDEN", "CONFIGURATION_SCHEMA_INVALID"}
            for issue in issues
        )
        rulebook = configuration.get("rulebook") if isinstance(configuration, dict) else None
        rulebook = rulebook if isinstance(rulebook, dict) else {}
        compiled, compile_issues = compile_document(rulebook)
        preview = {
            "schema_version": "karajan.configuration-preview.v1",
            "preview_id": str(uuid.uuid4()),
            "project_id": project_id,
            "project_revision": snapshot["revision"],
            "configuration_digest": hashlib.sha256(encoded(configuration).encode()).hexdigest(),
            "status": "draft" if issues else "offline_valid",
            "issues": issues,
            "can_apply": can_apply,
            "can_save_draft": can_apply,
            "can_publish": can_apply and compiled is not None and not compile_issues,
            "principal": principal,
            "expires_at": self.clock() + self.preview_ttl_seconds,
            "compiler_identity": compiler_identity(),
            "dispatch_eligible": False,
            "qualification_scope": "offline_configuration",
            "live_qualified": False,
            "validation": {
                "validator_revision": VALIDATOR_REVISION,
                "fixed_rulebook_sha256": validator_identity(),
                "rulebook_id": rulebook.get("id")
                if can_apply and isinstance(rulebook.get("id"), str)
                else None,
                "rulebook_revision": rulebook.get("revision")
                if type(rulebook.get("revision")) is int
                else None,
            },
        }
        with self._transaction() as db:
            previous = self._replay(db, principal, command_key, digest)
            if previous is not None:
                return previous
            self._current(db, project_id, snapshot["revision"])
            preview["catalog_binding"] = {
                key: effective_catalog(db, project_id)[key] for key in ("revision", "digest")
            }
            self._preview_identity(db, project_id, preview, rulebook, compiled)
            safe_configuration = encoded(configuration) if preview["can_save_draft"] else None
            db.execute(
                "INSERT INTO previews VALUES (?, ?, ?, ?)",
                (preview["preview_id"], project_id, safe_configuration, encoded(preview)),
            )
            db.execute(
                "INSERT INTO commands VALUES (?, ?, ?, ?)",
                (principal, command_key, digest, encoded(preview)),
            )
        return preview

    def apply_configuration(
        self,
        project_id: str,
        preview_id: str,
        *,
        expected_revision: int,
        command_key: str,
        principal: str,
    ) -> dict[str, Any]:
        project_id = identifier(project_id)
        preview_id = identifier(preview_id)
        digest = hashlib.sha256(
            encoded(["apply", project_id, preview_id, expected_revision]).encode()
        ).hexdigest()
        with self._transaction() as db:
            previous = self._replay(db, principal, command_key, digest)
            if previous is not None:
                return previous
            snapshot = self._current(db, project_id, expected_revision)
            self._require_owner(db, project_id, principal)
            row = db.execute(
                "SELECT result, configuration FROM previews WHERE id=? AND project_id=?",
                (preview_id, project_id),
            ).fetchone()
            if row is None:
                raise ProjectError("PREVIEW_NOT_FOUND")
            preview = json.loads(row["result"])
            self._check_preview(db, project_id, preview, principal)
            if not preview.get("can_save_draft") or row["configuration"] is None:
                raise ProjectError("CONFIGURATION_NOT_STORABLE")
            rulebook_draft = preview.get("schema_version") == "karajan.rulebook-preview.v1"
            if not rulebook_draft and (
                preview["validation"]["validator_revision"] != VALIDATOR_REVISION
                or preview["validation"]["fixed_rulebook_sha256"] != validator_identity()
            ):
                raise ProjectError("PREVIEW_POLICY_CHANGED")
            if preview["project_revision"] != snapshot["revision"]:
                raise ProjectError("PREVIEW_STALE", current_revision=snapshot["revision"])
            configuration = json.loads(row["configuration"])
            if (
                hashlib.sha256(encoded(configuration).encode()).hexdigest()
                != preview["configuration_digest"]
            ):
                raise ProjectError("PREVIEW_CONTENT_CHANGED")
            document = configuration.get("rulebook")
            compiled, compile_issues = compile_document(document)
            guard_identity(db, project_id, document, compiled)
            status = "draft" if rulebook_draft else preview["status"]
            if status == "offline_valid" and compiled is not None and not compile_issues:
                bind_version(db, project_id, document, compiled)
            apply_catalog(db, project_id, configuration)
            snapshot["revision"] += 1
            snapshot["configuration"] = {
                "revision": snapshot["configuration"]["revision"] + 1,
                "status": status,
                "digest": preview["configuration_digest"],
                "preview_id": preview_id,
                "dispatch_eligible": False,
            }
            db.execute("UPDATE projects SET snapshot=? WHERE id=?", (encoded(snapshot), project_id))
            db.execute(
                "INSERT INTO commands VALUES (?, ?, ?, ?)",
                (principal, command_key, digest, encoded(snapshot)),
            )
        return snapshot

    def _require_owner(self, db: sqlite3.Connection, project_id: str, principal: str) -> None:
        row = db.execute(
            "SELECT principal FROM project_owners WHERE project_id=?", (project_id,)
        ).fetchone()
        if row is None:
            raise ProjectError("PROJECT_OWNER_UNRESOLVED")
        if row["principal"] != principal:
            raise ProjectError("USER_DECISION_REQUIRED")

    def _preview_identity(
        self,
        db: sqlite3.Connection,
        project_id: str,
        preview: dict[str, Any],
        document: Any,
        compiled: dict[str, Any] | None,
    ) -> None:
        if not preview["can_save_draft"]:
            return
        try:
            guard_identity(db, project_id, document, compiled)
        except PublicationError as error:
            preview["identity_issue"] = str(error)
            preview["issues"].append({"code": str(error), "path": "rulebook.revision"})
            preview["can_save_draft"] = False
            preview["can_publish"] = False
            if "can_apply" in preview:
                preview["can_apply"] = False
                preview["status"] = "draft"

    def _check_preview(
        self, db: sqlite3.Connection, project_id: str, preview: dict[str, Any], principal: str
    ) -> None:
        if any(
            key not in preview
            for key in ("principal", "expires_at", "compiler_identity", "catalog_binding")
        ):
            raise ProjectError("PREVIEW_REVIEW_REQUIRED")
        if preview["principal"] != principal:
            raise ProjectError("PREVIEW_OWNER_MISMATCH")
        if self.clock() >= preview["expires_at"]:
            raise ProjectError("PREVIEW_EXPIRED")
        if preview["compiler_identity"] != compiler_identity():
            raise ProjectError("PREVIEW_COMPILER_CHANGED")
        current = effective_catalog(db, project_id)
        if preview["catalog_binding"] != {key: current[key] for key in ("revision", "digest")}:
            raise ProjectError("PREVIEW_CATALOG_CHANGED")
        if preview.get("identity_issue"):
            raise ProjectError(preview["identity_issue"])

    def preview_rulebook(
        self,
        project_id: str,
        document: dict[str, Any],
        *,
        expected_revision: int,
        command_key: str,
        principal: str,
    ) -> dict[str, Any]:
        project_id = identifier(project_id)
        identity = hashlib.sha256(
            encoded(["rulebook_preview", project_id, document, expected_revision]).encode()
        ).hexdigest()
        with self._transaction() as db:
            previous = self._replay(db, principal, command_key, identity)
            if previous is not None:
                return previous
            snapshot = self._current(db, project_id, expected_revision)
            self._require_owner(db, project_id, principal)
            catalog = effective_catalog(db, project_id)
        configuration = {
            "schema_version": "karajan.project-config.v1",
            "rulebook": document,
            "resources": catalog["resources"],
            "approved_profile_refs": catalog["approved_profile_refs"],
        }
        issues = validate_configuration(configuration)
        compiled, compile_issues = compile_document(document)
        storable = not any(
            issue["code"] in {"CREDENTIAL_VALUE_FORBIDDEN", "CONFIGURATION_SCHEMA_INVALID"}
            for issue in issues
        )
        preview = {
            "schema_version": "karajan.rulebook-preview.v1",
            "preview_id": str(uuid.uuid4()),
            "project_id": project_id,
            "project_revision": snapshot["revision"],
            "principal": principal,
            "expires_at": self.clock() + self.preview_ttl_seconds,
            "compiler_identity": compiler_identity(),
            "catalog_binding": {key: catalog[key] for key in ("revision", "digest")},
            "configuration_digest": hashlib.sha256(encoded(configuration).encode()).hexdigest(),
            "can_save_draft": storable,
            "can_publish": storable and compiled is not None and not compile_issues,
            "issues": issues,
            "compile_issues": compile_issues,
            "warnings": compiled["warnings"] if compiled else [],
            "rulebook_sha256": compiled["rulebook_sha256"] if compiled else None,
            "compiled_document": compiled["document"] if compiled and storable else None,
            "waiting_reasons": ["LIVE_QUALIFICATION_NOT_RUN"],
            "activation_allowed": False,
        }
        with self._transaction() as db:
            previous = self._replay(db, principal, command_key, identity)
            if previous is not None:
                return previous
            self._current(db, project_id, expected_revision)
            self._check_preview(db, project_id, preview, principal)
            self._preview_identity(db, project_id, preview, document, compiled)
            db.execute(
                "INSERT INTO previews VALUES (?,?,?,?)",
                (
                    preview["preview_id"],
                    project_id,
                    encoded(configuration) if preview["can_save_draft"] else None,
                    encoded(preview),
                ),
            )
            db.execute(
                "INSERT INTO commands VALUES (?,?,?,?)",
                (principal, command_key, identity, encoded(preview)),
            )
        return preview

    def publish_rulebook(
        self,
        project_id: str,
        preview_id: str,
        *,
        expected_revision: int,
        command_key: str,
        principal: str,
    ) -> dict[str, Any]:
        project_id, preview_id = identifier(project_id), identifier(preview_id)
        identity = hashlib.sha256(
            encoded(["rulebook_publish", project_id, preview_id, expected_revision]).encode()
        ).hexdigest()
        with self._transaction() as db:
            previous = self._replay(db, principal, command_key, identity)
            if previous is not None:
                return previous
            snapshot = self._current(db, project_id, expected_revision)
            self._require_owner(db, project_id, principal)
            row = db.execute(
                "SELECT result,configuration FROM previews WHERE id=? AND project_id=?",
                (preview_id, project_id),
            ).fetchone()
            if row is None:
                raise ProjectError("PREVIEW_NOT_FOUND")
            preview = json.loads(row["result"])
            self._check_preview(db, project_id, preview, principal)
            if (
                preview.get("schema_version") != "karajan.rulebook-preview.v1"
                or not preview.get("can_publish")
                or row["configuration"] is None
            ):
                raise ProjectError("RULEBOOK_NOT_PUBLISHABLE")
            if preview["project_revision"] != snapshot["revision"]:
                raise ProjectError("PREVIEW_STALE", current_revision=snapshot["revision"])
            configuration = json.loads(row["configuration"])
            if (
                hashlib.sha256(encoded(configuration).encode()).hexdigest()
                != preview["configuration_digest"]
            ):
                raise ProjectError("PREVIEW_CONTENT_CHANGED")
            compiled, compile_issues = compile_document(configuration["rulebook"])
            if (
                compiled is None
                or compile_issues
                or compiled["rulebook_sha256"] != preview["rulebook_sha256"]
            ):
                raise ProjectError("PREVIEW_COMPILER_CHANGED")
            version = bind_version(db, project_id, configuration["rulebook"], compiled)
            snapshot["revision"] += 1
            snapshot["configuration"] = {
                "revision": snapshot["configuration"]["revision"] + 1,
                "status": "draft" if preview["issues"] else "offline_valid",
                "digest": preview["configuration_digest"],
                "preview_id": preview_id,
                "dispatch_eligible": False,
            }
            apply_catalog(db, project_id, configuration)
            result = {
                "schema_version": "karajan.rulebook-publication.v1",
                "publication_id": str(uuid.uuid4()),
                "project_id": project_id,
                "project_revision": snapshot["revision"],
                "configuration_revision": snapshot["configuration"]["revision"],
                "rulebook": {key: version[key] for key in ("id", "revision", "rulebook_sha256")},
                "preview_id": preview_id,
                "state": "waiting_qualification",
                "principal": principal,
                "at": self.clock(),
                "activation_allowed": False,
                "live_qualification": "not_run",
            }
            snapshot["published_rulebook"] = result
            db.execute(
                "INSERT INTO rulebook_publications(project_id,result) VALUES (?,?)",
                (project_id, encoded(result)),
            )
            db.execute("UPDATE projects SET snapshot=? WHERE id=?", (encoded(snapshot), project_id))
            db.execute(
                "INSERT INTO commands VALUES (?,?,?,?)",
                (principal, command_key, identity, encoded(result)),
            )
        return result

    def get_effective_resources(self, project_id: str) -> dict[str, Any]:
        with self.effective_resources_guard(project_id) as result:
            return result

    @contextmanager
    def effective_resources_guard(self, project_id: str) -> Iterator[dict[str, Any]]:
        project_id = identifier(project_id)
        with self._transaction() as db:
            if db.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone() is None:
                raise ProjectError("PROJECT_NOT_FOUND")
            yield effective_catalog(db, project_id)

    def get_rulebook(self, project_id: str, rulebook_id: str, revision: int) -> dict[str, Any]:
        project_id, rulebook_id = identifier(project_id), identifier(rulebook_id)
        if type(revision) is not int or revision <= 0:
            raise ProjectError("RULEBOOK_REVISION_INVALID")
        with self._transaction() as db:
            if db.execute(
                "SELECT 1 FROM rulebook_conflicts WHERE project_id=? AND id=? AND revision=?",
                (project_id, rulebook_id, revision),
            ).fetchone():
                raise ProjectError("LEGACY_RULEBOOK_IDENTITY_CONFLICT")
            row = db.execute(
                "SELECT result FROM rulebook_versions WHERE project_id=? AND id=? AND revision=?",
                (project_id, rulebook_id, revision),
            ).fetchone()
        if row is None:
            raise ProjectError("RULEBOOK_NOT_FOUND")
        return dict(json.loads(row["result"]))

    def list_rulebook_versions(self, project_id: str) -> list[dict[str, Any]]:
        project_id = identifier(project_id)
        with self._transaction() as db:
            versions = [
                json.loads(row["result"])
                for row in db.execute(
                    "SELECT result FROM rulebook_versions WHERE project_id=? ORDER BY id,revision",
                    (project_id,),
                )
            ]
            versions.extend(
                {
                    "project_id": project_id,
                    "id": row["id"],
                    "revision": row["revision"],
                    "status": "legacy_conflict",
                    "digest_candidates": json.loads(row["digests"]),
                    "activation_allowed": False,
                }
                for row in db.execute(
                    "SELECT id,revision,digests FROM rulebook_conflicts WHERE project_id=?",
                    (project_id,),
                )
            )
            return sorted(versions, key=lambda row: (row["id"], row["revision"]))

    def list_rulebook_publications(self, project_id: str) -> list[dict[str, Any]]:
        project_id = identifier(project_id)
        with self._transaction() as db:
            return [
                json.loads(row["result"])
                for row in db.execute(
                    "SELECT result FROM rulebook_publications WHERE project_id=? ORDER BY sequence",
                    (project_id,),
                )
            ]

    def evaluate_task(self, project_id: str, task: dict[str, Any]) -> dict[str, Any]:
        """Legacy configuration preview; custom rules require complete routing snapshots."""
        from .legacy_evaluation import evaluate_fixed_task

        try:
            request = TaskPreview.model_validate(task)
        except ValidationError:
            raise ProjectError("TASK_INPUT_INVALID") from None
        exported = self.get_configuration(project_id)
        snapshot = self.get(project_id)
        if snapshot["revision"] != exported["project_revision"]:
            raise ProjectError("REVISION_CONFLICT", current_revision=snapshot["revision"])
        configuration = exported["configuration"]
        return evaluate_fixed_task(
            configuration or {},
            request,
            project_revision=exported["project_revision"],
            configuration_digest=snapshot["configuration"]["digest"],
        )

    def get_configuration(self, project_id: str) -> dict[str, Any]:
        project_id = identifier(project_id)
        with self._transaction() as db:
            row = db.execute("SELECT snapshot FROM projects WHERE id=?", (project_id,)).fetchone()
            if row is None:
                raise ProjectError("PROJECT_NOT_FOUND")
            snapshot = json.loads(row["snapshot"])
            configuration = None
            if snapshot["configuration"]["preview_id"] is not None:
                stored = db.execute(
                    "SELECT configuration FROM previews WHERE id=? AND project_id=?",
                    (snapshot["configuration"]["preview_id"], project_id),
                ).fetchone()
                if stored is not None and stored["configuration"] is not None:
                    configuration = json.loads(stored["configuration"])
            return {
                "project_id": project_id,
                "project_revision": snapshot["revision"],
                "configuration_revision": snapshot["configuration"]["revision"],
                "configuration": configuration,
            }

    def register_execution_policy(
        self, project_id: str, request: dict[str, Any], *, command_key: str, principal: str
    ) -> dict[str, Any]:
        """Fix explicit routing constraints under the project owner's authority."""
        from karajan.routing.compiler import RoutingError

        from .execution_policy import policy_components, validate_policy

        project_id = identifier(project_id)
        identity = hashlib.sha256(
            encoded(["execution_policy", project_id, request]).encode()
        ).hexdigest()
        with self._transaction() as db:
            prior = self._replay(db, principal, command_key, identity)
            if prior is not None:
                return prior
            self._require_owner(db, project_id, principal)
            row = db.execute("SELECT snapshot FROM projects WHERE id=?", (project_id,)).fetchone()
            snapshot = json.loads(row["snapshot"])
            if snapshot["configuration"]["status"] != "offline_valid":
                raise ProjectError("CONFIGURATION_NOT_READY")
            stored = db.execute(
                "SELECT configuration FROM previews WHERE id=? AND project_id=?",
                (snapshot["configuration"]["preview_id"], project_id),
            ).fetchone()
            try:
                document = validate_policy(request, json.loads(stored["configuration"]))
            except RoutingError as rejected:
                raise ProjectError(rejected.code) from None
            latest = db.execute(
                "SELECT MAX(revision) FROM execution_policies WHERE project_id=? AND id=?",
                (project_id, document["id"]),
            ).fetchone()[0]
            if document["revision"] != (latest or 0) + 1:
                raise ProjectError("EXECUTION_POLICY_REVISION_CONFLICT")
            components = policy_components(document)
            for old in db.execute(
                "SELECT record FROM execution_policies WHERE project_id=?", (project_id,)
            ):
                previous = policy_components(json.loads(old["record"]))
                if any(
                    previous[key] != components[key] for key in previous.keys() & components.keys()
                ):
                    raise ProjectError("EXECUTION_POLICY_COMPONENT_REVISION_CONFLICT")
            result = {
                **document,
                "project_id": project_id,
                "digest": hashlib.sha256(encoded(document).encode()).hexdigest(),
                "registered_by": principal,
                "registered_at": self.clock(),
                "activation_allowed": False,
            }
            db.execute(
                "INSERT INTO execution_policies VALUES (?,?,?,?)",
                (
                    project_id,
                    document["id"],
                    document["revision"],
                    encoded(result),
                ),
            )
            db.execute(
                "INSERT INTO commands VALUES (?,?,?,?)",
                (
                    principal,
                    command_key,
                    identity,
                    encoded(result),
                ),
            )
            return result

    def get_execution_policy(
        self, project_id: str, policy_id: str, revision: int, *, principal: str
    ) -> dict[str, Any]:
        from karajan.routing.models import Positive

        project_id, policy_id = identifier(project_id), identifier(policy_id)
        try:
            TypeAdapter(Positive).validate_python(revision, strict=True)
        except ValidationError:
            raise ProjectError("EXECUTION_POLICY_REFERENCE_INVALID") from None
        with self._transaction() as db:
            self._require_owner(db, project_id, principal)
            row = db.execute(
                "SELECT record FROM execution_policies WHERE project_id=? AND id=? AND revision=?",
                (project_id, policy_id, revision),
            ).fetchone()
            if row is None:
                raise ProjectError("EXECUTION_POLICY_NOT_FOUND")
            return dict(json.loads(row["record"]))

    def _replay(
        self, db: sqlite3.Connection, principal: str, key: str, digest: str
    ) -> dict[str, Any] | None:
        principal = identifier(principal, "COMMAND_IDENTITY_INVALID")
        key = identifier(key, "COMMAND_IDENTITY_INVALID")
        old = db.execute(
            "SELECT digest, result FROM commands WHERE principal=? AND key=?", (principal, key)
        ).fetchone()
        if old is None:
            return None
        if old["digest"] != digest:
            raise ProjectError("IDEMPOTENCY_CONFLICT")
        result: dict[str, Any] = json.loads(old["result"])
        return result

    def _git(self, root: Path, reason: str, *arguments: str) -> str:
        environment = {
            key: os.environ[key]
            for key in ("SystemRoot", "WINDIR", "PATH", "TEMP", "TMP")
            if key in os.environ
        }
        environment.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
        try:
            result = subprocess.run(
                ["git", "-C", str(root), *arguments],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=5,
                env=environment,
                check=False,
            )
        except (OSError, ValueError, subprocess.TimeoutExpired):
            raise ProjectError(reason) from None
        if result.returncode != 0:
            raise ProjectError(reason)
        return result.stdout.strip()

    def get(self, project_id: str) -> dict[str, Any]:
        project_id = identifier(project_id)
        with self._transaction() as db:
            row = db.execute("SELECT snapshot FROM projects WHERE id=?", (project_id,)).fetchone()
        if row is None:
            raise ProjectError("PROJECT_NOT_FOUND")
        result: dict[str, Any] = json.loads(row["snapshot"])
        return result

    def list(self) -> list[dict[str, Any]]:
        with self._transaction() as db:
            return [
                json.loads(row["snapshot"])
                for row in db.execute("SELECT snapshot FROM projects ORDER BY rowid")
            ]
