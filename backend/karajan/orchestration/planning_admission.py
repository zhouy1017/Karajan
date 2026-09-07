"""Durable, ID-only admission for planning before a Plan exists.

This is deliberately separate from ``ApprovedTaskAdmission``: planning has no
approved task to borrow an estimate or a run-execution budget from.  Estimates
and the finite grant ledger are therefore scoped to the original Run and its
frozen planning budget, never to a Plan, an intent, or a transport grant.
"""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol

from karajan.capacity import CapacityError, CapacityStore
from karajan.routing import select_rule
from karajan.runs import RunError, RunPlanner
from karajan.runs.planning import digest, encoded, identifier
from karajan.storage import open_database, require_schema

COMMANDER_QUALIFICATION_SCOPE = "commander_planning.v1"
COMMANDER_QUALIFICATION_READER_VERSION = "karajan.commander-qualification-reader.v1"


class CommanderQualificationReader(Protocol):
    """The future #113 reader; qualification itself must not use an execution grant."""

    def read_commander(
        self, binding: dict[str, Any], *, scope: str, reader_version: str
    ) -> dict[str, Any] | None: ...


class PlanningAdmissionAuthority:
    """Trusted controller port with durable cross-store recovery phases.

    Public effect inputs are IDs and command keys.  Provisioning may register a
    conservative estimate, but runtime callers cannot supply receipts, profile
    facts, source labels, or cash figures.
    """

    authority_kind: str

    def __init__(
        self,
        database: Path,
        execution_database: Path,
        planner: RunPlanner,
        capacity: CapacityStore,
        qualifications: CommanderQualificationReader,
        *,
        authority_kind: str = "fixture",
        existing_only: bool = False,
    ) -> None:
        if authority_kind not in {"fixture", "production"}:
            raise RunError("PLANNING_AUTHORITY_KIND_INVALID")
        if existing_only and not (planner.existing_only and capacity.existing_only):
            raise RunError("EXISTING_STORE_PARENT_MODE_REQUIRED")
        self.database, self.execution_database = database.resolve(), execution_database.resolve()
        self.planner, self.capacity = planner, capacity
        self.qualifications = qualifications
        self.authority_kind, self.existing_only = authority_kind, existing_only
        if self.database in {
            self.execution_database,
            planner.database.resolve(),
            planner.projects.database.resolve(),
            capacity.path.resolve(),
        }:
            raise RunError("PLANNING_ADMISSION_DATABASE_MUST_BE_SEPARATE")
        if not existing_only:
            self.database.parent.mkdir(parents=True, exist_ok=True)
        if existing_only:
            require_schema(
                self.database,
                {
                    "planning_admissions": ["execution_id", "run_id", "intent_id", "phase", "data"],
                    "planning_estimates": [
                        "run_id",
                        "budget_ref",
                        "profile_id",
                        "profile_revision",
                        "data",
                    ],
                    "planning_budget_usage": ["run_id", "budget_identity", "data"],
                    "commands": ["principal", "key", "payload", "result"],
                },
            )
            return
        with self._transaction() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS planning_admissions ("
                "execution_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, "
                "intent_id TEXT NOT NULL, phase TEXT NOT NULL, data TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS planning_estimates ("
                "run_id TEXT NOT NULL, budget_ref TEXT NOT NULL, profile_id TEXT NOT NULL, "
                "profile_revision INTEGER NOT NULL, data TEXT NOT NULL, "
                "PRIMARY KEY(run_id,budget_ref,profile_id,profile_revision))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS planning_budget_usage ("
                "run_id TEXT NOT NULL, budget_identity TEXT NOT NULL, data TEXT NOT NULL, "
                "PRIMARY KEY(run_id,budget_identity))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS commands (principal TEXT NOT NULL, key TEXT NOT NULL, "
                "payload TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(principal,key))"
            )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        db = open_database(self.database, existing_only=self.existing_only, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _load(db: sqlite3.Connection, execution_id: str) -> dict[str, Any] | None:
        row = db.execute(
            "SELECT data FROM planning_admissions WHERE execution_id=?", (execution_id,)
        ).fetchone()
        return None if row is None else dict(json.loads(row["data"]))

    @staticmethod
    def _save(db: sqlite3.Connection, record: dict[str, Any]) -> None:
        db.execute(
            "INSERT INTO planning_admissions VALUES (?,?,?,?,?) ON CONFLICT(execution_id) "
            "DO UPDATE SET phase=excluded.phase,data=excluded.data",
            (
                record["execution_id"],
                record["run_id"],
                record["intent_id"],
                record["phase"],
                encoded(record),
            ),
        )

    def register_estimate(
        self,
        run_id: str,
        budget_ref: str,
        profile: dict[str, Any],
        *,
        demand: dict[str, str],
        expected_capacity: dict[str, Any],
        duration_seconds: int,
        max_requests: int,
        max_duration_seconds: int,
        principal: str,
        command_key: str,
    ) -> dict[str, Any]:
        """Provision a finite local planning estimate for an original Run.

        It is an explicit controller record, not an approved-task estimate and
        not a caller-supplied execution permission.
        """
        profile_id = profile.get("id")
        if not isinstance(profile_id, str):
            raise RunError("PLANNING_ESTIMATE_INVALID")
        for value in (run_id, budget_ref, principal, command_key, profile_id):
            identifier(value)
        if (
            type(profile.get("revision")) is not int
            or not demand
            or not isinstance(expected_capacity, dict)
            or any(type(v) is not str for v in demand.values())
            or min(duration_seconds, max_requests, max_duration_seconds) <= 0
        ):
            raise RunError("PLANNING_ESTIMATE_INVALID")
        run = self.planner.get(run_id, principal=principal)
        if (
            budget_ref
            != run["configuration_snapshot"]["configuration"]["rulebook"]["resource_policy"][
                "planning_budget_ref"
            ]
        ):
            raise RunError("PLANNING_BUDGET_SCOPE_MISMATCH")
        record = {
            "schema_version": "karajan.planning-estimate.v1",
            "run_id": run_id,
            "budget_ref": budget_ref,
            "profile": {"id": profile["id"], "revision": profile["revision"]},
            "demand": dict(demand),
            "expected_capacity": expected_capacity,
            "duration_seconds": duration_seconds,
            "max_requests": max_requests,
            "max_duration_seconds": max_duration_seconds,
            "configuration_sha256": run["configuration_snapshot"]["digest"],
        }
        record["digest"] = digest(record)
        payload = encoded(record)
        with self._transaction() as db:
            prior = db.execute(
                "SELECT payload,result FROM commands WHERE principal=? AND key=?",
                (principal, command_key),
            ).fetchone()
            if prior is not None:
                if prior["payload"] != payload:
                    raise RunError("IDEMPOTENCY_CONFLICT")
                return dict(json.loads(prior["result"]))
            db.execute(
                "INSERT INTO planning_estimates VALUES (?,?,?,?,?) ON CONFLICT("
                "run_id,budget_ref,profile_id,profile_revision) DO UPDATE SET data=excluded.data",
                (run_id, budget_ref, profile["id"], profile["revision"], encoded(record)),
            )
            db.execute(
                "INSERT INTO commands VALUES (?,?,?,?)",
                (principal, command_key, payload, encoded(record)),
            )
            return record

    def _run_intent(
        self, binding: dict[str, Any], principal: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        run = self.planner.get(binding["run_id"], principal=principal)
        participant = run["commander"]
        intent = next(
            (row for row in run["planning_intents"] if row["id"] == binding["intent_id"]), None
        )
        configuration = run["configuration_snapshot"]
        if intent is None or (
            intent["state"] != "awaiting_receipt"
            or principal != run["owner"]
            or intent["principal"] != participant["principal"]
            or intent["term"] != participant["term"]
            or intent["profile"] != participant["profile"]
            or binding["term"] != participant["term"]
            or binding["profile"] != participant["profile"]
            or binding["principal"] != participant["principal"]
            or binding["configuration_sha256"] != configuration["digest"]
            or binding["rulebook_sha256"] != digest(configuration["configuration"]["rulebook"])
            or binding["authorization_ceiling_sha256"] != digest(run["authorization_ceiling"])
        ):
            raise RunError("PLANNING_ADMISSION_BINDING_STALE")
        execution = run.get("execution_policy_snapshot")
        if execution is not None:
            selection = select_rule(
                {
                    "role": "commander",
                    "purpose": "lead",
                    "readiness": "ready",
                    "complexity": "T1",
                    "risk": "standard",
                    "paths": [],
                    "domains": [],
                    "authors": [],
                },
                configuration["configuration"]["rulebook"],
                execution["risk_policy"],
            )
            if selection["reason_codes"]:
                raise RunError(selection["reason_codes"][0])
        return run, intent

    def _execution_binding(self, execution_id: str, principal: str) -> dict[str, Any]:
        """Read the controller's original ID-only binding without opening a claim."""
        db = sqlite3.connect(self.execution_database.as_uri() + "?mode=ro", uri=True, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            row = db.execute("SELECT data FROM executions WHERE id=?", (execution_id,)).fetchone()
        finally:
            db.close()
        if row is None:
            raise RunError("PLANNING_EXECUTION_NOT_FOUND")
        execution = dict(json.loads(row["data"]))
        binding = execution.get("binding")
        if not isinstance(binding, dict) or binding.get("execution_id") != execution_id:
            raise RunError("PLANNING_EXECUTION_BINDING_INVALID")
        self.planner.get(binding["run_id"], principal=principal)
        return binding

    @staticmethod
    def _budget(run: dict[str, Any], budget_ref: str) -> tuple[dict[str, Any], str]:
        configuration = run["configuration_snapshot"]
        resources = configuration["configuration"]["resources"]
        budget = next((row for row in resources["budgets"] if row["id"] == budget_ref), None)
        if budget is None or budget["scope"] != "planning":
            raise RunError("PLANNING_BUDGET_SCOPE_MISMATCH")
        identity = digest(
            {
                "run_id": run["id"],
                "budget_ref": budget_ref,
                "configuration": configuration["digest"],
                "budget": budget,
            }
        )
        return budget, identity

    def _prepare(
        self, execution_id: str, binding: dict[str, Any], principal: str
    ) -> dict[str, Any]:
        run, intent = self._run_intent(binding, principal)
        budget, budget_identity = self._budget(run, binding["budget_ref"])
        with self._transaction() as db:
            existing = self._load(db, execution_id)
            if existing is not None:
                if existing["binding_sha256"] != digest(binding):
                    raise RunError("PLANNING_ADMISSION_BINDING_CHANGED")
                return existing
            row = db.execute(
                "SELECT data FROM planning_estimates WHERE run_id=? AND budget_ref=? "
                "AND profile_id=? AND profile_revision=?",
                (
                    run["id"],
                    binding["budget_ref"],
                    binding["profile"]["id"],
                    binding["profile"]["revision"],
                ),
            ).fetchone()
            estimate = None if row is None else dict(json.loads(row["data"]))
            record = {
                "schema_version": "karajan.planning-admission.v1",
                "execution_id": execution_id,
                "run_id": run["id"],
                "intent_id": intent["id"],
                "binding_sha256": digest(binding),
                "binding": binding,
                "phase": "prepared",
                "reason_codes": [],
                "estimate": estimate,
                "budget": budget,
                "budget_identity": budget_identity,
                "capacity_request": None,
                "capacity_command_key": "planning-admit:" + execution_id,
                "capacity_receipt": None,
                "capacity_activation_request": None,
                "capacity_activation_command_key": "planning-activate:" + execution_id,
                "capacity_activation_receipt": None,
                "qualification": None,
                "activation_allowed": False,
                "dispatch_enabled": False,
            }
            self._save(db, record)
            return record

    def _deny(self, record: dict[str, Any], reason: str) -> dict[str, Any]:
        with self._transaction() as db:
            current = self._load(db, record["execution_id"]) or record
            if current["phase"] not in {"admitted", "unknown"}:
                current["phase"], current["reason_codes"] = "denied", [reason]
                self._save(db, current)
            return current

    def _claim_budget(self, record: dict[str, Any]) -> dict[str, Any] | None:
        estimate, budget = record["estimate"], record["budget"]
        if estimate is None:
            return self._deny(record, "PLANNING_ESTIMATE_MISSING")
        if budget.get("max_total_attempts") is None or budget.get("max_duration_seconds") is None:
            return self._deny(record, "PLANNING_FINITE_BUDGET_REQUIRED")
        if (
            estimate["max_requests"] > budget["max_total_attempts"]
            or estimate["max_duration_seconds"] > budget["max_duration_seconds"]
        ):
            return self._deny(record, "PLANNING_ESTIMATE_EXCEEDS_BUDGET")
        with self._transaction() as db:
            current = self._load(db, record["execution_id"])
            assert current is not None
            if current["phase"] != "prepared":
                return current
            usage_row = db.execute(
                "SELECT data FROM planning_budget_usage WHERE run_id=? AND budget_identity=?",
                (current["run_id"], current["budget_identity"]),
            ).fetchone()
            usage = (
                {
                    "run_id": current["run_id"],
                    "budget_identity": current["budget_identity"],
                    "first_claim_execution_id": current["execution_id"],
                    "attempts": 0,
                    "duration_seconds": 0,
                    "claims": [],
                }
                if usage_row is None
                else dict(json.loads(usage_row["data"]))
            )
            estimate = current["estimate"]
            if (
                usage["attempts"] + estimate["max_requests"]
                > current["budget"]["max_total_attempts"]
                or usage["duration_seconds"] + estimate["max_duration_seconds"]
                > current["budget"]["max_duration_seconds"]
            ):
                current["phase"], current["reason_codes"] = "denied", ["PLANNING_BUDGET_EXHAUSTED"]
                self._save(db, current)
                return current
            usage["attempts"] += estimate["max_requests"]
            usage["duration_seconds"] += estimate["max_duration_seconds"]
            usage["claims"].append(
                {
                    "execution_id": current["execution_id"],
                    "intent_id": current["intent_id"],
                    "attempts": estimate["max_requests"],
                    "duration_seconds": estimate["max_duration_seconds"],
                }
            )
            db.execute(
                "INSERT INTO planning_budget_usage VALUES (?,?,?) ON CONFLICT("
                "run_id,budget_identity) DO UPDATE SET data=excluded.data",
                (current["run_id"], current["budget_identity"], encoded(usage)),
            )
            current["budget_usage"] = usage
            current["phase"] = "budget_claimed"
            self._save(db, current)
            return current

    def advance(self, execution_id: str, principal: str, command_key: str) -> dict[str, Any]:
        """Recover exactly one original request; no receipt can be caller-supplied."""
        for value in (execution_id, principal, command_key):
            identifier(value)
        binding = self._execution_binding(execution_id, principal)
        run = self.planner.get(binding["run_id"], principal=principal)
        payload = encoded([execution_id, digest(binding)])
        with self._transaction() as db:
            prior = db.execute(
                "SELECT payload,result FROM commands WHERE principal=? AND key=?",
                (principal, command_key),
            ).fetchone()
            if prior is not None:
                if prior["payload"] != payload:
                    raise RunError("IDEMPOTENCY_CONFLICT")
                return dict(json.loads(prior["result"]))
        record = self._prepare(execution_id, binding, principal)
        if record["phase"] in {"admitted", "denied"}:
            return record
        if record["phase"] == "unknown":
            request = record.get("capacity_activation_request")
            if not isinstance(request, dict):
                return record
            receipt = self.capacity.command_receipt(
                "activate", request, command_key=record["capacity_activation_command_key"]
            )
            if receipt is None:
                return record
            with self._transaction() as db:
                current = self._load(db, execution_id) or record
                current["capacity_activation_receipt"] = receipt
                current["phase"] = (
                    "admitted" if receipt.get("decision") == "capacity_revalidated" else "denied"
                )
                current["reason_codes"] = receipt.get("reason_codes", [])
                self._save(db, current)
                return current
        profile = binding["profile"]
        registration = next(
            (
                row
                for row in run["configuration_snapshot"]["configuration"]["resources"]["profiles"]
                if {"id": row["id"], "revision": row["revision"]} == profile
            ),
            None,
        )
        if (
            not isinstance(registration, dict)
            or registration.get("profile") is None
            or registration["profile"]["binding"]["billing_path"] != "subscription_only"
        ):
            return self._deny(record, "PLANNING_CASH_UPPER_BOUND_REQUIRED")
        qualification = self.qualifications.read_commander(
            binding,
            scope=COMMANDER_QUALIFICATION_SCOPE,
            reader_version=COMMANDER_QUALIFICATION_READER_VERSION,
        )
        if (
            not isinstance(qualification, dict)
            or qualification.get("scope") != COMMANDER_QUALIFICATION_SCOPE
            or qualification.get("source_generation_sha256") is None
            or self.authority_kind == "production"
            and qualification.get("provenance") != "official"
        ):
            record = self._deny(record, "COMMANDER_QUALIFICATION_REQUIRED")
        else:
            with self._transaction() as db:
                stored = self._load(db, execution_id)
                if stored is not None:
                    record = stored
                if record["phase"] == "prepared":
                    record["qualification"] = qualification
                    self._save(db, record)
            claimed = self._claim_budget(record)
            if claimed is None:
                return self._deny(record, "PLANNING_ADMISSION_UNKNOWN")
            record = claimed
        if record is None or record["phase"] in {"denied", "unknown"}:
            return record or self._deny(
                self._prepare(execution_id, binding, principal), "PLANNING_ADMISSION_UNKNOWN"
            )
        estimate = record["estimate"]
        request = {
            "attempt_id": binding["attempt_id"],
            "run_id": binding["run_id"],
            "profile_id": binding["profile"]["id"],
            "profile_revision": binding["profile"]["revision"],
            "role": "commander",
            "purpose": "lead",
            "authorization_ref": record["budget_identity"],
            "rulebook_revision": binding["rulebook_sha256"],
            "duration_seconds": estimate["duration_seconds"],
            "demand": estimate["demand"],
            "expected_capacity": estimate.get("expected_capacity"),
        }
        if request["expected_capacity"] is None:
            return self._deny(record, "PLANNING_CAPACITY_BINDING_REQUIRED")
        with self._transaction() as db:
            record = self._load(db, execution_id) or record
            if record["phase"] == "budget_claimed":
                record["capacity_request"], record["phase"] = request, "capacity_admit_unknown"
                self._save(db, record)
        try:
            receipt = self.capacity.command_receipt(
                "admit", request, command_key=record["capacity_command_key"]
            ) or self.capacity.admit(request, command_key=record["capacity_command_key"])
        except CapacityError as error:
            return self._deny(record, str(error))
        with self._transaction() as db:
            record = self._load(db, execution_id) or record
            record["capacity_receipt"] = receipt
            if receipt.get("decision") != "admitted":
                record["phase"], record["reason_codes"] = (
                    "denied",
                    receipt.get("reason_codes", ["PLANNING_CAPACITY_DENIED"]),
                )
                self._save(db, record)
                return record
            record["capacity_activation_request"] = {"admission_id": receipt["admission_id"]}
            record["phase"] = "capacity_activate_unknown"
            self._save(db, record)
        try:
            activation_request = record["capacity_activation_request"]
            activation = self.capacity.command_receipt(
                "activate",
                activation_request,
                command_key=record["capacity_activation_command_key"],
            ) or self.capacity.activate(
                receipt["admission_id"], command_key=record["capacity_activation_command_key"]
            )
        except CapacityError:
            with self._transaction() as db:
                record = self._load(db, execution_id) or record
                record["phase"], record["reason_codes"] = (
                    "unknown",
                    ["PLANNING_CAPACITY_ACTIVATION_UNKNOWN"],
                )
                self._save(db, record)
                return record
        with self._transaction() as db:
            record = self._load(db, execution_id) or record
            record["capacity_activation_receipt"] = activation
            record["phase"] = (
                "admitted" if activation.get("decision") == "capacity_revalidated" else "denied"
            )
            record["reason_codes"] = activation.get("reason_codes", [])
            self._save(db, record)
            db.execute(
                "INSERT INTO commands VALUES (?,?,?,?)",
                (principal, command_key, payload, encoded(record)),
            )
            return record

    def read_admission(self, binding: dict[str, Any]) -> object:
        """Read the original durable record only; it never calls Capacity."""
        with self._transaction() as db:
            record = self._load(db, binding["execution_id"])
        if record is None or record["binding_sha256"] != digest(binding):
            raise ValueError("PLANNING_ADMISSION_NOT_FOUND")
        return {
            "schema_version": "karajan.planning-admission-evidence.v1",
            "binding_sha256": record["binding_sha256"],
            "authority_kind": self.authority_kind,
            "source_sha256": digest(record["capacity_request"])
            if record["capacity_request"]
            else "0" * 64,
            "budget_ref": binding["budget_ref"],
            "capacity_request": record["capacity_request"] or {},
            "capacity_command_key": record["capacity_command_key"],
            "capacity_receipt": record["capacity_receipt"],
            "capacity_activation_request": record["capacity_activation_request"] or {},
            "capacity_activation_command_key": record["capacity_activation_command_key"],
            "capacity_activation_receipt": record["capacity_activation_receipt"],
            "state": "admitted"
            if record["phase"] == "admitted"
            else "unknown"
            if record["phase"] == "unknown"
            else "denied",
        }

    @contextmanager
    def effect_guard(
        self, execution_id: str, principal: str, effect_id: str
    ) -> Iterator[dict[str, Any]]:
        """Fresh, non-reusable authorization around one owned transport effect."""
        for value in (execution_id, principal, effect_id):
            identifier(value)
        binding = self._execution_binding(execution_id, principal)
        record = self.advance(execution_id, principal, "planning-guard:" + effect_id)
        if record["phase"] != "admitted" or record["binding"] != binding:
            raise RunError("PLANNING_EFFECT_NOT_ADMITTED")
        self._run_intent(binding, principal)
        qualification = self.qualifications.read_commander(
            binding,
            scope=COMMANDER_QUALIFICATION_SCOPE,
            reader_version=COMMANDER_QUALIFICATION_READER_VERSION,
        )
        if not isinstance(qualification, dict) or qualification != record["qualification"]:
            raise RunError("COMMANDER_QUALIFICATION_CHANGED")
        with self.capacity.pre_effect_guard(
            record["capacity_receipt"]["admission_id"], expected_request=record["capacity_request"]
        ) as capacity:
            yield {
                "execution_id": execution_id,
                "binding_sha256": record["binding_sha256"],
                "attempt_id": binding["attempt_id"],
                "fence": binding["fence"],
                "source_generation_sha256": qualification["source_generation_sha256"],
                "budget_identity": record["budget_identity"],
                "capacity": capacity,
            }
