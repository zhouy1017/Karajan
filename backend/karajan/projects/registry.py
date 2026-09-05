"""Persist project identity and controller-owned configuration decisions."""

import hashlib
import json
import os
import sqlite3
import subprocess
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter, ValidationError

from .configuration import VALIDATOR_REVISION, validate_configuration, validator_identity
from .models import Identifier, ProjectCreate, ProjectUpdate, TaskPreview


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
    def __init__(self, database: Path, allowed_roots: Sequence[Path]) -> None:
        self.database = database
        self.allowed_roots = tuple(path.resolve() for path in allowed_roots)
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

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.database, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
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
        issues = validate_configuration(configuration)
        can_apply = not any(
            issue["code"] in {"CREDENTIAL_VALUE_FORBIDDEN", "CONFIGURATION_SCHEMA_INVALID"}
            for issue in issues
        )
        rulebook = configuration.get("rulebook") if isinstance(configuration, dict) else None
        rulebook = rulebook if isinstance(rulebook, dict) else {}
        preview = {
            "schema_version": "karajan.configuration-preview.v1",
            "preview_id": str(uuid.uuid4()),
            "project_id": project_id,
            "project_revision": snapshot["revision"],
            "configuration_digest": hashlib.sha256(encoded(configuration).encode()).hexdigest(),
            "status": "draft" if issues else "offline_valid",
            "issues": issues,
            "can_apply": can_apply,
            "dispatch_eligible": False,
            "qualification_scope": "offline_configuration",
            "live_qualified": False,
            "validation": {
                "validator_revision": VALIDATOR_REVISION,
                "fixed_rulebook_sha256": validator_identity(),
                "rulebook_id": rulebook.get("id") if isinstance(rulebook.get("id"), str) else None,
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
            safe_configuration = encoded(configuration) if can_apply else None
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
            row = db.execute(
                "SELECT result, configuration FROM previews WHERE id=? AND project_id=?",
                (preview_id, project_id),
            ).fetchone()
            if row is None:
                raise ProjectError("PREVIEW_NOT_FOUND")
            preview = json.loads(row["result"])
            if not preview["can_apply"] or row["configuration"] is None:
                raise ProjectError("CONFIGURATION_NOT_STORABLE")
            if (
                preview["validation"]["validator_revision"] != VALIDATOR_REVISION
                or preview["validation"]["fixed_rulebook_sha256"] != validator_identity()
            ):
                raise ProjectError("PREVIEW_POLICY_CHANGED")
            if preview["project_revision"] != snapshot["revision"]:
                raise ProjectError("PREVIEW_STALE", current_revision=snapshot["revision"])
            snapshot["revision"] += 1
            snapshot["configuration"] = {
                "revision": snapshot["configuration"]["revision"] + 1,
                "status": preview["status"],
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

    def evaluate_task(self, project_id: str, task: dict[str, Any]) -> dict[str, Any]:
        """Trusted coordinator supplies risk; this read-only preview grants no execution."""
        try:
            request = TaskPreview.model_validate(task)
        except ValidationError:
            raise ProjectError("TASK_INPUT_INVALID") from None
        snapshot = self.get(project_id)
        effective = max(request.complexity, "T3" if request.risk == "critical" else "T1")
        result: dict[str, Any] = {
            "schema_version": "karajan.task-preview.v1",
            "project_revision": snapshot["revision"],
            "configuration_digest": snapshot["configuration"]["digest"],
            "effective_class": effective,
            "rule_id": None,
            "qualified_candidates": [],
            "reason_codes": [],
            "dispatch_eligible": False,
            "qualification_scope": "offline_configuration",
        }
        if request.role == "worker" and request.readiness != "ready":
            result["reason_codes"] = ["TASK_NOT_READY"]
            return result
        if snapshot["configuration"]["status"] != "offline_valid":
            result["reason_codes"] = ["CONFIGURATION_NOT_READY"]
            return result
        with self._transaction() as db:
            row = db.execute(
                "SELECT configuration FROM previews WHERE id=?",
                (snapshot["configuration"]["preview_id"],),
            ).fetchone()
        configuration = json.loads(row["configuration"])
        rules = []
        for rule in configuration["rulebook"]["rules"]:
            when = rule["when"]
            if when["role"] != request.role or (
                "purpose" in when and when["purpose"] != request.purpose
            ):
                continue
            if when.get("effective_class", effective) != effective or effective not in when.get(
                "effective_class_in", [effective]
            ):
                continue
            rules.append(rule)
        if len(rules) != 1:
            result["reason_codes"] = ["RULE_NOT_UNIQUE"]
            return result
        rule = rules[0]
        result["rule_id"] = rule["id"]
        result["required_independence"] = rule.get("independence", [])
        approved = {(item.id, item.revision) for item in request.approved_profile_refs}
        candidates = {
            (ref["id"], ref["revision"])
            for group in rule["eligible_groups"]
            for ref in configuration["rulebook"]["profile_groups"][group]
            if (ref["id"], ref["revision"]) in approved
        }
        profiles = {
            (item["id"], item["revision"]): item for item in configuration["resources"]["profiles"]
        }
        authors = {(item.id, item.revision) for item in request.author_profile_refs}
        author_families = set(request.author_model_families) | {
            profiles[author]["model_family"] for author in authors if author in profiles
        }
        for candidate in tuple(candidates):
            registration = profiles[candidate]
            profile_digest = hashlib.sha256(encoded(registration["profile"]).encode()).hexdigest()
            evidence = registration["capability_evidence"]
            capabilities = {
                item["capability"]
                for item in evidence
                if item["status"] == "passed"
                and item["evidence_ref"]
                and item["profile_digest"] == profile_digest
                and item["runtime_version"] == registration["profile"]["binding"]["runtime_version"]
                and item.get("provenance") in {"fixture", "imported_observation"}
                and sum(other["capability"] == item["capability"] for other in evidence) == 1
            }
            if not set(request.required_capabilities) <= capabilities:
                candidates.remove(candidate)
            elif (
                request.role == "reviewer"
                and effective == "T3"
                and registration["model_family"] in author_families
            ):
                candidates.remove(candidate)
        result["qualified_candidates"] = [
            {"id": identity, "revision": revision} for identity, revision in sorted(candidates)
        ]
        if not candidates:
            result["reason_codes"] = ["NO_APPROVED_CANDIDATE"]
        return result

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
