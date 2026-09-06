"""Controller-produced, persistent qualification observations with explicit scope.

Fixed local and native Go suites produce observations here. Go uses controller
registered credential generations and never accepts an uploaded report. The
versioned projected suite can qualify a bounded Worker executor; concrete Task
paths, current authorization and effect admission are still separate checks.
"""

import copy
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
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from .models import ProfileRef, RegisteredProfile
from .publication import digest, effective_catalog
from .registry import ProjectRegistry, encoded, identifier

if TYPE_CHECKING:
    from .credential_sources import CredentialSourceStore
    from .go_suite import FixedGoSuite

LOCAL_SUITE = {"id": "fixed-local-fixture-qualification", "revision": 1}
PROJECTED_GO_SUITE = {"id": "opencode-go-native-read-edit-linux", "revision": 2}


def _source_scope(binding: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if "qualification_scope" in binding and "suite_ref" in binding:
        return binding["qualification_scope"], binding["suite_ref"]
    # Existing fixed-fixture starts predate explicit scopes. Recognize only that
    # exact producer; an unknown legacy source is never guessed to be a fixture.
    if binding.get("runtime", {}).get("producer") == "fixed-local-fixture-qualification.v1":
        return "local_fixture", LOCAL_SUITE.copy()
    return "unknown", {}


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
        self,
        projects: ProjectRegistry,
        *,
        clock: Callable[[], float] = time.time,
        credentials: "CredentialSourceStore | None" = None,
        go_suite: "FixedGoSuite | None" = None,
    ) -> None:
        self.projects = projects
        self.clock = clock
        if credentials is not None and credentials.projects is not projects:
            raise QualificationError("CREDENTIAL_PROJECT_STORE_MISMATCH")
        self.credentials = credentials
        self.go_suite = go_suite
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
            db.execute(
                "CREATE TABLE IF NOT EXISTS profile_qualification_start_seals ("
                "id TEXT PRIMARY KEY REFERENCES profile_qualification_starts(id), "
                "digest TEXT NOT NULL)"
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

    def qualify_runtime_tools(
        self,
        project_id: str,
        profile_ref: dict[str, Any],
        *,
        principal: str,
        command_key: str,
        suite_ref: dict[str, Any],
        validity_seconds: int,
    ) -> dict[str, Any]:
        """Run the controller's fixed native suite; never import a passed report.

        IDs and source identity commit before credential resolution or execution.
        An interrupted command stays unknown and requires a new explicit command
        to run a new qualification. Replaying it cannot replenish call grants.
        """
        identifier(command_key)
        if type(validity_seconds) is not int or not 1 <= validity_seconds <= 86400:
            raise QualificationError("QUALIFICATION_VALIDITY_INVALID")
        try:
            profile_ref = ProfileRef.model_validate(profile_ref).model_dump()
            suite_ref = ProfileRef.model_validate(suite_ref).model_dump()
        except ValidationError:
            raise QualificationError("QUALIFICATION_REFERENCE_INVALID") from None
        request_digest = digest(
            ["qualify_runtime_tools", project_id, profile_ref, suite_ref, validity_seconds]
        )
        with self._owned(project_id, principal) as db:
            previous = db.execute(
                "SELECT * FROM profile_qualification_starts WHERE principal=? AND command_key=?",
                (principal, command_key),
            ).fetchone()
            if previous is not None:
                if previous["request_digest"] != request_digest:
                    raise QualificationError("IDEMPOTENCY_CONFLICT")
                self._checked_start(db, previous)
                return self._record(db, previous["id"])
            if self.go_suite is None or self.credentials is None:
                raise QualificationError("RUNTIME_QUALIFICATION_SOURCE_UNCONFIGURED")
            source = copy.deepcopy(self.go_suite.source())
            if source["suite_ref"] != suite_ref:
                raise QualificationError("QUALIFICATION_SUITE_UNSUPPORTED")
            scope = self._go_scope(source)
            bound = self._binding(db, project_id, profile_ref)
            self.go_suite.validate_profile(bound)
            authentication = self.credentials.current_locked(
                db, project_id, bound["registration"]["profile"]["auth_ref"], principal=principal
            )
            now = self._now()
            observation_id = str(uuid.uuid4())
            profile = bound["registration"]["profile"]
            start: dict[str, Any] = {
                "qualification_id": observation_id,
                "project_id": project_id,
                "suite_ref": suite_ref,
                "profile_binding": bound,
                "profile_digest": digest(profile),
                "auth_generation": authentication["generation"],
                "credential_source_id": authentication["source"]["id"],
                "authentication_source": authentication,
                "source": source,
                "started_at": now,
                "expires_at": now + 420,
                "scenarios": [],
            }
            for scenario in ("edit", "denied_read"):
                attempt_id, grant_id = str(uuid.uuid4()), str(uuid.uuid4())
                start["scenarios"].append(
                    {
                        "scenario": scenario,
                        "attempt_id": attempt_id,
                        "fence": 1,
                        "grant_id": grant_id,
                        "grant_binding": {
                            "qualification_id": observation_id,
                            "attempt_id": attempt_id,
                            "fence": 1,
                            "profile_digest": digest(profile),
                            "runtime_digest": source["runtime_digest"],
                            "channel": profile["binding"]["channel_id"],
                            "model": profile["binding"]["model_id"],
                            "auth_generation": authentication["generation"],
                            "expires_at": start["expires_at"],
                            "max_requests": 6,
                        },
                    }
                )
                if suite_ref == PROJECTED_GO_SUITE:
                    start["scenarios"][-1]["grant_binding"].update(
                        schema_version="karajan.go-qualification-grant.v2",
                        probe_spec_digest=digest(source["probe_spec"]),
                        scenario=scenario,
                        context=copy.deepcopy(source["probe_spec"]["context"]),
                    )
            binding = {
                "qualification_scope": scope,
                "suite_ref": suite_ref,
                "profile_binding": bound,
                "execution_start": start,
                "controller_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            }
            db.execute(
                "INSERT INTO profile_qualification_starts VALUES (?,?,?,?,?,?)",
                (
                    observation_id,
                    project_id,
                    principal,
                    command_key,
                    request_digest,
                    encoded(binding),
                ),
            )
            db.execute(
                "INSERT INTO profile_qualification_start_seals VALUES (?,?)",
                (observation_id, digest(binding)),
            )
        try:
            credential = self.credentials.resolve_exact(
                project_id, profile["auth_ref"], authentication["generation"], principal=principal
            )
            observation = self.go_suite.observe(start, credential)
        except Exception:
            # Exception strings can carry file paths, provider text, or secrets.
            observation = {
                "status": "failed",
                "reason_codes": ["QUALIFICATION_EXECUTION_INCOMPLETE"],
                "scenarios": [],
            }
        now = self._now()
        record: dict[str, Any] = {
            "schema_version": "karajan.profile-qualification.v1",
            "id": observation_id,
            "project_id": project_id,
            "principal": principal,
            "status": observation.get("status", "failed"),
            "qualification_scope": scope,
            "suite_ref": suite_ref,
            "provenance": "fixture" if scope.endswith("_fixture") else "imported_observation",
            "runtime_tools_status": "not_run",
            "live_qualified": False,
            "dispatch_eligible": False,
            "binding": binding,
            "observed_at": now,
            "valid_until": now + validity_seconds,
            "observation": observation,
            "reason_codes": list(observation.get("reason_codes", [])),
            "limitations": [
                "Only the fixed file is covered; arbitrary Task paths are unqualified.",
                "No Commander, Reviewer, context, candidate capture, or cash bound qualified.",
                "Provider remote stop remains unknown; local grant cleanup is not a billing cap.",
            ],
        }
        with self._owned(project_id, principal) as db:
            try:
                if self._binding(db, project_id, profile_ref) != bound:
                    raise QualificationError("PROFILE_IDENTITY_MISMATCH")
                if self.go_suite.source() != source:
                    raise QualificationError("QUALIFICATION_RUNTIME_MISMATCH")
                if (
                    binding["controller_sha256"]
                    != hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
                ):
                    raise QualificationError("QUALIFICATION_RUNTIME_MISMATCH")
                if (
                    self.credentials.current_locked(
                        db, project_id, profile["auth_ref"], principal=principal
                    )
                    != authentication
                ):
                    raise QualificationError("AUTHENTICATION_SOURCE_MISMATCH")
                if now < start["started_at"]:
                    raise QualificationError("QUALIFICATION_CLOCK_INVALID")
            except Exception:
                record["status"] = "failed"
                record["reason_codes"].append("QUALIFICATION_SOURCE_CHANGED")
            if suite_ref == PROJECTED_GO_SUITE:
                record["limitations"] = [
                    "Only controlled existing-file projection for T1 Workers is covered.",
                    "New files, Commander, Reviewer, T2/T3 and cash bounds are unqualified.",
                    "The observed small input is not a measured maximum model context window.",
                    "Current Task paths, authorization, quota and effect gates remain required.",
                    "Provider remote stop remains unknown.",
                ]
                if not self._projected_observation_passed(start, observation):
                    record["status"] = "failed"
                    record["reason_codes"].append("PROJECTED_EXECUTOR_EVIDENCE_INCOMPLETE")
                if record["status"] == "passed" and scope == "projected_native_tools":
                    record["runtime_tools_status"] = "passed"
                    record["live_qualified"] = True
            db.execute(
                "INSERT INTO profile_qualification_records VALUES (?,?,?)",
                (observation_id, encoded(record), digest(record)),
            )
        return record

    @staticmethod
    def _go_scope(source: dict[str, Any]) -> str:
        projected = source.get("suite_ref") == PROJECTED_GO_SUITE
        if not projected and source.get("suite_ref") != {
            "id": "opencode-go-native-read-edit-linux",
            "revision": 1,
        }:
            raise QualificationError("QUALIFICATION_SOURCE_UNSUPPORTED")
        if projected:
            from karajan.adapters.opencode.go_journal import GoQualificationLimits

            try:
                if source["schema_version"] != "karajan.fixed-go-suite-source.v2":
                    raise ValueError
                GoQualificationLimits.model_validate(source["probe_spec"]["context"])
            except (KeyError, TypeError, ValueError):
                raise QualificationError("QUALIFICATION_SOURCE_UNSUPPORTED") from None
        prefix = "projected_native_tools" if projected else "fixed_native_tools"
        origin = source.get("observation_origin")
        if origin not in {"official_go", "http_fixture"}:
            raise QualificationError("QUALIFICATION_SOURCE_UNSUPPORTED")
        scope = prefix + ("_fixture" if origin == "http_fixture" else "")
        if projected and source.get("qualification_scope") != scope:
            raise QualificationError("QUALIFICATION_SOURCE_UNSUPPORTED")
        return scope

    @staticmethod
    def _projected_observation_passed(start: dict[str, Any], observation: dict[str, Any]) -> bool:
        """Check the configured Suite's validated envelope, not caller reports.

        The Suite owns the native/Collector/Journal correlation. These checks
        bind its complete result to this persisted execution and source.
        """
        try:
            validation = observation["validation"]
            return bool(
                observation["schema_version"] == "karajan.fixed-go-suite-observation.v2"
                and observation["qualification_id"] == start["qualification_id"]
                and observation["suite_ref"] == start["suite_ref"] == PROJECTED_GO_SUITE
                and observation["source"] == start["source"]
                and observation["observation_origin"] == start["source"]["observation_origin"]
                and observation["qualification_scope"] == start["source"]["qualification_scope"]
                and observation["status"] == "passed"
                and observation["reason_codes"] == []
                and all(
                    validation[key] == "passed"
                    for key in ("projected_native_tools", "candidate_capture", "context_accounting")
                )
                and validation["runtime_tools"] == "not_run"
                and validation["budget"] == "unknown"
                and validation["dispatch"] is False
                and len(observation["scenarios"]) == 2
                and all(
                    result["status"] == "passed"
                    and result["reason_codes"] == []
                    and all(
                        result[key] == expected[key]
                        for key in ("scenario", "attempt_id", "fence", "grant_id")
                    )
                    for result, expected in zip(
                        observation["scenarios"], start["scenarios"], strict=True
                    )
                )
            )
        except (KeyError, TypeError, ValueError):
            return False

    def _checked_start(self, db: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        binding: dict[str, Any] = json.loads(row["binding"])
        if "execution_start" in binding:
            seal = db.execute(
                "SELECT digest FROM profile_qualification_start_seals WHERE id=?", (row["id"],)
            ).fetchone()
            if seal is None or seal["digest"] != digest(binding):
                raise QualificationError("QUALIFICATION_START_CHANGED")
        return binding

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
                    encoded(
                        {
                            "profile_binding": bound,
                            "runtime": runtime,
                            "qualification_scope": "local_fixture",
                            "suite_ref": LOCAL_SUITE,
                        }
                    ),
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

    def get_start(self, project_id: str, observation_id: str, *, principal: str) -> dict[str, Any]:
        """Read the durable intent even when execution has no completed receipt."""
        identifier(observation_id)
        with self._owned(project_id, principal) as db:
            row = db.execute(
                "SELECT * FROM profile_qualification_starts WHERE id=?", (observation_id,)
            ).fetchone()
            if row is None:
                raise QualificationError("QUALIFICATION_START_NOT_FOUND")
            if row["project_id"] != project_id:
                raise QualificationError("QUALIFICATION_PROJECT_MISMATCH")
            return self._start_view(db, row)

    def get_command_start(
        self, project_id: str, command_key: str, *, principal: str
    ) -> dict[str, Any]:
        """Recover a lost reply using the caller's original command identity."""
        identifier(command_key)
        with self._owned(project_id, principal) as db:
            row = db.execute(
                "SELECT * FROM profile_qualification_starts WHERE project_id=? "
                "AND principal=? AND command_key=?",
                (project_id, principal, command_key),
            ).fetchone()
            if row is None:
                raise QualificationError("QUALIFICATION_START_NOT_FOUND")
            return self._start_view(db, row)

    def _start_view(self, db: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        binding = self._checked_start(db, row)
        scope, suite = _source_scope(binding)
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "qualification_scope": scope,
            "suite_ref": suite,
            "binding": binding,
            "completed": db.execute(
                "SELECT 1 FROM profile_qualification_records WHERE id=?", (row["id"],)
            ).fetchone()
            is not None,
        }

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

        runtime_read = scope != "local_fixture"
        if runtime_read:
            if (
                scope
                not in {
                    "runtime_tools",
                    "fixed_native_tools",
                    "fixed_native_tools_fixture",
                    "projected_native_tools",
                    "projected_native_tools_fixture",
                }
                or self.go_suite is None
                or self.credentials is None
            ):
                raise QualificationError("RUNTIME_TOOLS_NOT_QUALIFIED")
            try:
                current_source = self.go_suite.source()
            except Exception:
                raise QualificationError("QUALIFICATION_RUNTIME_MISMATCH") from None
            source_scope = self._go_scope(current_source)
            wanted_scope = source_scope if scope == "runtime_tools" else scope
            if (
                source_scope != wanted_scope
                or scope == "runtime_tools"
                and source_scope.endswith("_fixture")
            ):
                raise QualificationError("RUNTIME_TOOLS_NOT_QUALIFIED")
            wanted_suite = current_source["suite_ref"]
        else:
            wanted_scope, wanted_suite = "local_fixture", LOCAL_SUITE
        if not runtime_read and fixture_root is None:
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
            "SELECT * FROM profile_qualification_starts WHERE project_id=? ORDER BY rowid DESC",
            (project_id,),
        ).fetchall()
        latest = next(
            (
                row
                for row in starts
                if (
                    _source_scope(json.loads(row["binding"])) == (wanted_scope, wanted_suite)
                    and json.loads(row["binding"])["profile_binding"]["registration"]["id"]
                    == ref["id"]
                    and json.loads(row["binding"])["profile_binding"]["registration"]["revision"]
                    == ref["revision"]
                )
            ),
            None,
        )
        if latest is None:
            raise QualificationError("PROFILE_FACTS_MISSING")
        start_binding = self._checked_start(db, latest)
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
        if runtime_read:
            return self._go_facts(
                db, project_id, frozen, record, start_binding, current_source, scope
            )
        assert fixture_root is not None
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

    def _go_facts(
        self,
        db: sqlite3.Connection,
        project_id: str,
        frozen: dict[str, Any],
        record: dict[str, Any],
        start_binding: dict[str, Any],
        current_source: dict[str, Any],
        requested_scope: str,
    ) -> dict[str, Any]:
        from karajan.routing.models import ProfileFacts

        if (
            record["binding"] != start_binding
            or start_binding["controller_sha256"]
            != hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
            or start_binding["execution_start"]["source"] != current_source
        ):
            raise QualificationError("QUALIFICATION_RUNTIME_MISMATCH")
        start = start_binding["execution_start"]
        assert self.credentials is not None
        try:
            authentication = self.credentials.current_locked(
                db, project_id, frozen["profile"]["auth_ref"], principal=record["principal"]
            )
            if authentication != start["authentication_source"]:
                raise QualificationError("AUTHENTICATION_SOURCE_MISMATCH")
        except Exception:
            raise QualificationError("AUTHENTICATION_SOURCE_MISMATCH") from None
        if start["suite_ref"] == PROJECTED_GO_SUITE:
            return self._projected_facts(frozen, record, start)
        # ProfileFacts cannot express path constraints yet. A fixed fixture
        # observation must never satisfy a general Run's read/edit requirements.
        if requested_scope == "runtime_tools":
            raise QualificationError("TASK_PERMISSION_SCOPE_NOT_QUALIFIED")
        profile = frozen["profile"]
        fixture = record["qualification_scope"] == "fixed_native_tools_fixture"
        evidence_ref = "fixed-go-qualification:" + record["id"]
        facts = ProfileFacts.model_validate(
            {
                "profile": {"id": frozen["id"], "revision": frozen["revision"]},
                "profile_digest": digest(profile),
                "runtime_version": profile["binding"]["runtime_version"],
                "roles": [] if fixture else ["worker"],
                "tools": ["fixed_go_fixture_read", "fixed_go_fixture_edit"],
                "context_tokens": None,
                "data_destination": "http_fixture" if fixture else "opencode-go",
                "budget_enforcement": "unknown",
                "provenance": record["provenance"],
                "evidence_ref": evidence_ref,
                "observed_at": record["observed_at"],
                "valid_until": record["valid_until"],
            }
        ).model_dump()
        return {
            "facts": facts,
            "qualification_scope": record["qualification_scope"],
            "runtime_tools_status": "not_run",
            "dispatch_eligible": False,
            "observation": record,
            "capability_evidence": [
                {
                    "capability": "fixed_go_fixture_" + operation,
                    "status": "passed",
                    "profile_digest": digest(profile),
                    "runtime_version": profile["binding"]["runtime_version"],
                    "evidence_ref": evidence_ref,
                    "provenance": record["provenance"],
                }
                for operation in ("read", "edit", "denied_read")
            ],
        }

    def _projected_facts(
        self, frozen: dict[str, Any], record: dict[str, Any], start: dict[str, Any]
    ) -> dict[str, Any]:
        from karajan.routing.models import ProfileFacts

        if not self._projected_observation_passed(start, record["observation"]):
            raise QualificationError("PROJECTED_EXECUTOR_EVIDENCE_INCOMPLETE")
        fixture = record["qualification_scope"].endswith("_fixture")
        profile = frozen["profile"]
        context = copy.deepcopy(start["source"]["probe_spec"]["context"])
        evidence_ref = "projected-go-qualification:" + record["id"]
        capabilities = (
            ["projected_fixture_read", "projected_fixture_edit", "projected_fixture_capture"]
            if fixture
            else ["bounded_code_edit", "controlled_tools", "candidate_capture"]
        )
        result = {
            "facts": ProfileFacts.model_validate(
                {
                    "profile": {"id": frozen["id"], "revision": frozen["revision"]},
                    "profile_digest": digest(profile),
                    "runtime_version": profile["binding"]["runtime_version"],
                    "roles": [] if fixture else ["worker"],
                    "tools": ["projected_fixture_read", "projected_fixture_edit"]
                    if fixture
                    else ["read", "edit"],
                    "context_tokens": None if fixture else context["operating_context_tokens"],
                    "data_destination": "http_fixture" if fixture else "opencode-go",
                    "budget_enforcement": "unknown",
                    "provenance": record["provenance"],
                    "evidence_ref": evidence_ref,
                    "observed_at": record["observed_at"],
                    "valid_until": record["valid_until"],
                }
            ).model_dump(),
            "qualification_scope": record["qualification_scope"],
            "runtime_tools_status": "not_run" if fixture else "passed",
            "dispatch_eligible": False,
            "observation": record,
            "capability_evidence": [
                {
                    "capability": capability,
                    "status": "passed",
                    "profile_digest": digest(profile),
                    "runtime_version": profile["binding"]["runtime_version"],
                    "evidence_ref": evidence_ref,
                    "provenance": record["provenance"],
                }
                for capability in capabilities
            ],
        }
        if not fixture:
            result["executor_scope"] = {
                "schema_version": "karajan.go-projected-executor-scope.v1",
                "suite_ref": PROJECTED_GO_SUITE.copy(),
                "projection": "existing_regular_files",
                "new_files_supported": False,
                "tools": ["read", "edit"],
                "supported_roles": ["worker"],
                "task_classes": ["T1"],
                "context": context,
                "max_requests": 6,
                "candidate_capture": True,
            }
            result["context_evidence"] = {
                "provider_declared": {"context_tokens": 1000000, "max_output_tokens": 131072},
                "adapter_limits": {
                    "operating_context_tokens": context["operating_context_tokens"],
                    "reserved_output_tokens": context["reserved_output_tokens"],
                    "output_policy": "fixed_native_limit",
                },
                "observed": "bounded_small_input_accepted",
                "maximum_context_observed": False,
            }
        return result
