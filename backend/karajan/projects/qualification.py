"""Controller-produced, persistent qualification observations with explicit scope.

The only producer currently implemented runs the fixed local fixture. Imported
reports, including offline-contract and Go diagnostic reports, cannot enable a
runtime. This module never reads credentials or executes caller-provided code.
"""

import hashlib
import json
import math
import os
import platform
import sqlite3
import stat
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .models import ProfileRef, RegisteredProfile
from .publication import digest, effective_catalog
from .registry import ProjectRegistry, encoded, identifier


class QualificationError(ValueError):
    @property
    def code(self) -> str:
        return str(self)


def _safe_root(path: Path) -> Path:
    path = path.absolute()
    for candidate in (path, *path.parents):
        try:
            info = candidate.lstat()
        except OSError:
            raise QualificationError("FIXTURE_ROOT_INVALID") from None
        if stat.S_ISLNK(info.st_mode) or (
            getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise QualificationError("FIXTURE_ROOT_INVALID")
    if not path.is_dir():
        raise QualificationError("FIXTURE_ROOT_INVALID")
    return path.resolve()


def _runtime(root: Path) -> dict[str, Any]:
    script = Path(__file__).parents[1] / "orchestration" / "_fixture_process.py"
    return {
        "producer": "fixed-local-fixture-qualification.v1",
        "execution_path": "python-isolated-fixed-script",
        "fixture_root": str(root),
        "os": platform.system(),
        "os_release": platform.release(),
        "machine": platform.machine(),
        "python": sys.version,
        "executable": sys.executable,
        "executable_sha256": hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest(),
        "script": str(script.resolve()),
        "script_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
        "producer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }


class ProfileQualificationStore:
    """Use the project DB and its owner/catalog transaction boundary.

    A local controller calls this service; no report-upload endpoint exists.
    Records are append-only. Revocation is a separate retained fact. A persisted
    start without a completed result is unknown and is never silently rerun.
    """

    def __init__(
        self, projects: ProjectRegistry, *, clock: Callable[[], float] = time.time
    ) -> None:
        self.projects = projects
        self.clock = clock
        with projects._transaction() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS profile_qualification_starts ("
                "id TEXT PRIMARY KEY, project_id TEXT NOT NULL, principal TEXT NOT NULL, "
                "command_key TEXT NOT NULL, request_digest TEXT NOT NULL, binding TEXT NOT NULL, "
                "UNIQUE(principal,command_key))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS profile_qualification_records ("
                "id TEXT PRIMARY KEY REFERENCES profile_qualification_starts(id), "
                "record TEXT NOT NULL, digest TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS profile_qualification_revocations ("
                "id TEXT PRIMARY KEY REFERENCES profile_qualification_starts(id), "
                "record TEXT NOT NULL)"
            )

    def _now(self) -> float:
        value = self.clock()
        if type(value) not in (int, float) or not math.isfinite(value):
            raise QualificationError("QUALIFICATION_CLOCK_INVALID")
        return float(value)

    @contextmanager
    def _owned(self, project_id: str, principal: str) -> Iterator[sqlite3.Connection]:
        identifier(project_id)
        identifier(principal)
        with self.projects._transaction() as db:
            self.projects._require_owner(db, project_id, principal)
            yield db

    def _binding(
        self, db: sqlite3.Connection, project_id: str, ref: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            ref = ProfileRef.model_validate(ref).model_dump()
        except ValidationError:
            raise QualificationError("PROFILE_REFERENCE_INVALID") from None
        catalog = effective_catalog(db, project_id)
        resources = catalog.get("resources")
        if not resources or ref not in catalog["approved_profile_refs"]:
            raise QualificationError("PROFILE_NOT_APPROVED")
        matches = [
            p for p in resources["profiles"] if {"id": p["id"], "revision": p["revision"]} == ref
        ]
        if len(matches) != 1 or matches[0]["profile"] is None:
            raise QualificationError("PROFILE_IDENTITY_MISSING")
        registration = RegisteredProfile.model_validate(matches[0]).model_dump()
        profile = registration["profile"]
        binding = profile["binding"]
        if {"id": profile["id"], "revision": profile["revision"]} != ref:
            raise QualificationError("PROFILE_IDENTITY_MISMATCH")
        accounts = [a for a in resources["accounts"] if a["id"] == binding["account_id"]]
        channels = [c for c in resources["channels"] if c["id"] == binding["channel_id"]]
        if (
            len(accounts) != 1
            or len(channels) != 1
            or accounts[0]["secret_ref"] != profile["auth_ref"]
            or channels[0]["account_id"] != binding["account_id"]
            or channels[0]["billing_path"] != binding["billing_path"]
            or channels[0]["approved_data_destination"] is not True
        ):
            raise QualificationError("PROFILE_CHANNEL_ACCOUNT_MISMATCH")
        project = json.loads(
            db.execute("SELECT snapshot FROM projects WHERE id=?", (project_id,)).fetchone()[
                "snapshot"
            ]
        )
        return {
            "registration": registration,
            "account": accounts[0],
            "channel": channels[0],
            "repository": project["repository"],
        }

    def qualify_local_fixture(
        self,
        project_id: str,
        profile_ref: dict[str, Any],
        *,
        principal: str,
        command_key: str,
        fixture_root: Path,
        validity_seconds: int,
    ) -> dict[str, Any]:
        identifier(command_key)
        if type(validity_seconds) is not int or not 1 <= validity_seconds <= 86400:
            raise QualificationError("QUALIFICATION_VALIDITY_INVALID")
        root = _safe_root(fixture_root)
        if not any(root.is_relative_to(allowed) for allowed in self.projects.allowed_roots):
            raise QualificationError("FIXTURE_ROOT_OUTSIDE_PROJECT_ROOTS")
        runtime = _runtime(root)
        request_digest = digest([project_id, profile_ref, str(root), validity_seconds])
        with self._owned(project_id, principal) as db:
            previous = db.execute(
                "SELECT * FROM profile_qualification_starts WHERE principal=? AND command_key=?",
                (principal, command_key),
            ).fetchone()
            if previous is not None:
                if previous["request_digest"] != request_digest:
                    raise QualificationError("IDEMPOTENCY_CONFLICT")
                return self._record(db, previous["id"])
            bound = self._binding(db, project_id, profile_ref)
            profile = bound["registration"]["profile"]
            binding = profile["binding"]
            if (
                binding["runtime_kind"] != "fixture-runtime"
                or binding["runtime_version"] != "1"
                or binding["model_id"] != "fixture-model"
                or binding["auth_mode"] != "none"
                or binding["billing_path"] != "subscription_only"
                or binding["native_settings"] != {}
                or set(profile["required_permissions"]) != {"fixture-tools"}
                or not Path(bound["repository"]["root"]).resolve().is_relative_to(root)
                or Path(bound["repository"]["root"]).resolve() == root
            ):
                raise QualificationError("QUALIFICATION_SOURCE_UNSUPPORTED")
            observation_id = str(uuid.uuid4())
            db.execute(
                "INSERT INTO profile_qualification_starts VALUES (?,?,?,?,?,?)",
                (
                    observation_id,
                    project_id,
                    principal,
                    command_key,
                    request_digest,
                    encoded({"profile_binding": bound, "runtime": runtime}),
                ),
            )
        observation = self._observe(root, observation_id, runtime)
        now = self._now()
        record: dict[str, Any] = {
            "schema_version": "karajan.profile-qualification.v1",
            "id": observation_id,
            "project_id": project_id,
            "principal": principal,
            "status": "passed" if all(observation["checks"].values()) else "failed",
            "qualification_scope": "local_fixture",
            "provenance": "fixture",
            "runtime_tools_status": "not_run",
            "live_qualified": False,
            "dispatch_eligible": False,
            "binding": {"profile_binding": bound, "runtime": runtime},
            "fact_sources": {
                "registration": "owner_configuration_not_qualification",
                "roles": "fixed_synthetic_write_and_review_processes",
                "tools": "fixed_synthetic_script_operations",
                "data_destination": "fixed_local_process_no_network_operations",
                "context_tokens": "not_observed",
                "budget_enforcement": "not_observed",
                "model_family": "owner_declaration_only",
            },
            "observed_at": now,
            "valid_until": now + validity_seconds,
            "observation": observation,
            "limitations": [
                "Fixed synthetic write/check/review only; no model or network was used.",
                "No runtime tool sandbox, language-model context, or cash bound was qualified.",
                "Owner-declared class/family/isolation is identity, not observed capability.",
            ],
        }
        with self._owned(project_id, principal) as db:
            current = self._binding(db, project_id, profile_ref)
            if current != bound:
                record["status"] = "failed"
                record["limitations"].append("Profile identity changed during observation.")
            db.execute(
                "INSERT INTO profile_qualification_records VALUES (?,?,?)",
                (observation_id, encoded(record), digest(record)),
            )
        return record

    def _observe(self, root: Path, observation_id: str, runtime: dict[str, Any]) -> dict[str, Any]:
        directory = root / ("qualification-" + observation_id)
        directory.mkdir()
        target = directory / "fixture.py"
        expected = b"print('fixture candidate')\n"
        processes: list[dict[str, Any]] = []
        checks = {"write": False, "check": False, "review": False, "source_unchanged": False}
        environment = {
            key: os.environ[key] for key in ("SystemRoot", "WINDIR") if key in os.environ
        }
        environment.update({"HOME": str(directory), "TEMP": str(directory), "TMP": str(directory)})
        for operation in ("write", "check", "review"):
            log = directory / (operation + ".json")
            argv = [sys.executable, "-I", runtime["script"], operation, '["fixture.py"]']
            if operation != "write":
                argv.extend([str(log), "pass" if operation == "check" else "passed"])
            try:
                result = subprocess.run(
                    argv,
                    cwd=directory,
                    env=environment,
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
                processes.append(
                    {
                        "operation": operation,
                        "exit_code": result.returncode,
                        "stdout_sha256": hashlib.sha256(result.stdout).hexdigest(),
                        "stderr_sha256": hashlib.sha256(result.stderr).hexdigest(),
                    }
                )
                passed = result.returncode == 0 and target.read_bytes() == expected
                if operation != "write":
                    passed = passed and json.loads(log.read_bytes()) == {
                        "operation": operation,
                        "verdict": "passed",
                        "synthetic": True,
                        "files": ["fixture.py"],
                        "author_reasoning_included": False,
                    }
                checks[operation] = passed
            except (OSError, ValueError, subprocess.TimeoutExpired):
                processes.append({"operation": operation, "exit_code": None})
                break
        checks["source_unchanged"] = _runtime(root) == runtime
        return {
            "checks": checks,
            "processes": processes,
            "workspace": str(directory),
            "candidate_sha256": hashlib.sha256(target.read_bytes()).hexdigest()
            if target.is_file()
            else None,
        }

    def _record(self, db: sqlite3.Connection, observation_id: str) -> dict[str, Any]:
        row = db.execute(
            "SELECT * FROM profile_qualification_records WHERE id=?", (observation_id,)
        ).fetchone()
        if row is None:
            raise QualificationError("QUALIFICATION_IN_PROGRESS_OR_UNKNOWN")
        result: dict[str, Any] = json.loads(row["record"])
        if digest(result) != row["digest"]:
            raise QualificationError("QUALIFICATION_RECORD_CHANGED")
        return result

    def get(self, project_id: str, observation_id: str, *, principal: str) -> dict[str, Any]:
        identifier(observation_id)
        with self._owned(project_id, principal) as db:
            record = self._record(db, observation_id)
            if record["project_id"] != project_id:
                raise QualificationError("QUALIFICATION_PROJECT_MISMATCH")
            row = db.execute(
                "SELECT record FROM profile_qualification_revocations WHERE id=?", (observation_id,)
            ).fetchone()
            return {"record": record, "revocation": json.loads(row["record"]) if row else None}

    def revoke(
        self, project_id: str, observation_id: str, *, principal: str, reason: str
    ) -> dict[str, Any]:
        identifier(reason)
        identifier(observation_id)
        with self._owned(project_id, principal) as db:
            record = self._record(db, observation_id)
            if record["project_id"] != project_id:
                raise QualificationError("QUALIFICATION_PROJECT_MISMATCH")
            revocation = {
                "id": observation_id,
                "principal": principal,
                "reason": reason,
                "revoked_at": self._now(),
            }
            db.execute(
                "INSERT OR IGNORE INTO profile_qualification_revocations VALUES (?,?)",
                (observation_id, encoded(revocation)),
            )
            return dict(
                json.loads(
                    db.execute(
                        "SELECT record FROM profile_qualification_revocations WHERE id=?",
                        (observation_id,),
                    ).fetchone()["record"]
                )
            )

    def facts_for_profile(
        self,
        project_id: str,
        frozen_registration: dict[str, Any],
        *,
        principal: str,
        scope: str,
        fixture_root: Path | None = None,
    ) -> dict[str, Any]:
        with self._owned(project_id, principal) as db:
            return self._facts(db, project_id, frozen_registration, scope, fixture_root)

    @contextmanager
    def routing_facts_guard(
        self,
        project_id: str,
        frozen_registrations: list[dict[str, Any]],
        *,
        principal: str,
        scope: str = "runtime_tools",
        fixture_root: Path | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Hold current project/qualification state for the caller's admission check.

        Only this project DB is locked. Capacity, Run, and Host state still need
        their own protocol; this is not a cross-database atomic admission claim.
        """
        with self._owned(project_id, principal) as db:
            catalog = effective_catalog(db, project_id)
            rows = []
            seen = set()
            for registration in frozen_registrations:
                try:
                    frozen = RegisteredProfile.model_validate(registration).model_dump()
                except ValidationError:
                    raise QualificationError("PROFILE_IDENTITY_MISMATCH") from None
                key = (frozen["id"], frozen["revision"])
                if key in seen:
                    raise QualificationError("PROFILE_REFERENCE_DUPLICATE")
                seen.add(key)
                ref = {"id": key[0], "revision": key[1]}
                try:
                    qualified = self._facts(db, project_id, frozen, scope, fixture_root)
                except QualificationError as error:
                    rows.append(
                        {"profile": ref, "qualification": None, "reason_codes": [error.code]}
                    )
                else:
                    rows.append({"profile": ref, "qualification": qualified, "reason_codes": []})
            yield {
                "catalog": catalog,
                "profiles": rows,
                "qualification_scope": scope,
                "activation_allowed": False,
            }

    def _facts(
        self,
        db: sqlite3.Connection,
        project_id: str,
        frozen_registration: dict[str, Any],
        scope: str,
        fixture_root: Path | None,
    ) -> dict[str, Any]:
        from karajan.routing.models import ProfileFacts

        if scope != "local_fixture":
            raise QualificationError("RUNTIME_TOOLS_NOT_QUALIFIED")
        if fixture_root is None:
            raise QualificationError("FIXTURE_ROOT_REQUIRED")
        try:
            frozen = RegisteredProfile.model_validate(frozen_registration).model_dump()
        except ValidationError:
            raise QualificationError("PROFILE_IDENTITY_MISMATCH") from None
        ref = {"id": frozen["id"], "revision": frozen["revision"]}
        current = self._binding(db, project_id, ref)
        if current["registration"] != frozen:
            raise QualificationError("PROFILE_IDENTITY_MISMATCH")
        if not frozen["enabled"]:
            raise QualificationError("PROFILE_DISABLED")
        starts = db.execute(
            "SELECT id,binding FROM profile_qualification_starts WHERE project_id=? "
            "ORDER BY rowid DESC",
            (project_id,),
        ).fetchall()
        latest = next(
            (
                row
                for row in starts
                if (
                    json.loads(row["binding"])["profile_binding"]["registration"]["id"] == ref["id"]
                    and json.loads(row["binding"])["profile_binding"]["registration"]["revision"]
                    == ref["revision"]
                )
            ),
            None,
        )
        if latest is None:
            raise QualificationError("PROFILE_FACTS_MISSING")
        record = self._record(db, latest["id"])
        if db.execute(
            "SELECT 1 FROM profile_qualification_revocations WHERE id=?", (record["id"],)
        ).fetchone():
            raise QualificationError("QUALIFICATION_REVOKED")
        if record["binding"]["profile_binding"] != current:
            raise QualificationError("PROFILE_IDENTITY_MISMATCH")
        if record["status"] != "passed":
            raise QualificationError("QUALIFICATION_NOT_PASSED")
        if not record["observed_at"] <= self._now() < record["valid_until"]:
            raise QualificationError("QUALIFICATION_EXPIRED")
        if record["binding"]["runtime"] != _runtime(_safe_root(fixture_root)):
            raise QualificationError("QUALIFICATION_RUNTIME_MISMATCH")
        profile = frozen["profile"]
        evidence_ref = "local-qualification:" + record["id"]
        facts = ProfileFacts.model_validate(
            {
                "profile": ref,
                "profile_digest": digest(profile),
                "runtime_version": profile["binding"]["runtime_version"],
                "roles": ["worker", "reviewer"],
                "tools": ["fixture-tools"],
                "context_tokens": None,
                "data_destination": "local_fixture",
                "budget_enforcement": "unknown",
                "provenance": "fixture",
                "evidence_ref": evidence_ref,
                "observed_at": record["observed_at"],
                "valid_until": record["valid_until"],
            }
        ).model_dump()
        return {
            "facts": facts,
            "qualification_scope": "local_fixture",
            "runtime_tools_status": "not_run",
            "dispatch_eligible": False,
            "observation": record,
            "capability_evidence": [
                {
                    "capability": "fixed_fixture_" + operation,
                    "status": "passed",
                    "profile_digest": digest(profile),
                    "runtime_version": profile["binding"]["runtime_version"],
                    "evidence_ref": evidence_ref,
                    "provenance": "fixture",
                }
                for operation in ("write", "check", "review")
            ],
        }
