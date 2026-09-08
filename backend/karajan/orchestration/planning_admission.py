"""Durable, ID-only admission for planning before a Plan exists.

This is deliberately separate from ``ApprovedTaskAdmission``: planning has no
approved task to borrow an estimate or a run-execution budget from.  Estimates
and the finite grant ledger are therefore scoped to the original Run and its
frozen planning budget, never to a Plan, an intent, or a transport grant.
"""

import json
import math
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from karajan.capacity import (
    CapacityBoundaryFacts,
    CapacityError,
    CapacityStore,
    derive_capacity_boundary_facts,
)
from karajan.projects import ProjectRegistry
from karajan.projects.credential_sources import (
    CredentialSourceError,
    CredentialSourceStore,
    LocalKeyFile,
)
from karajan.projects.qualification import ProfileQualificationStore
from karajan.resources.broker import units
from karajan.routing import RoutingError, evaluate_reserved_profile, evaluate_route
from karajan.routing.quotas import QuotaTemporalFence, capture_quota_temporal_fence
from karajan.runs import RunError, RunPlanner
from karajan.runs.planning import digest, encoded, identifier
from karajan.storage import open_database, require_schema

from .planning_bootstrap import (
    assert_planning_bootstrap_current,
    read_planning_bootstrap,
)
from .routing import _capacity_snapshot

COMMANDER_QUALIFICATION_SCOPE = "commander_planning.v1"
COMMANDER_QUALIFICATION_READER_VERSION = "karajan.commander-qualification-reader.v1"


@dataclass(frozen=True)
class _FinalBoundary:
    """Only constant-time values needed at Capacity's final controller edge."""

    qualification_valid_until: float
    profile_facts_valid_until: float
    budget_started_at: float
    budget_duration_seconds: int


class _CurrentCommanderQualification(dict[str, Any]):
    """Private effect lease that can re-observe external source material."""

    def __init__(self, value: dict[str, Any], recheck: Callable[[], dict[str, Any] | None]) -> None:
        super().__init__(value)
        self._recheck = recheck

    def recheck(self) -> dict[str, Any] | None:
        return self._recheck()


class CommanderQualificationReader(Protocol):
    """The future #113 reader; qualification itself must not use an execution grant."""

    def read_commander(
        self, binding: dict[str, Any], *, scope: str, reader_version: str
    ) -> dict[str, Any] | None: ...


class PersistentCommanderQualificationReader:
    """Current production reader: Worker/Reviewer facts never become Commander facts."""

    def __init__(
        self,
        planner: RunPlanner,
        qualifications: ProfileQualificationStore,
        *,
        control_directory: Path,
    ) -> None:
        self.planner = planner
        self.qualifications = qualifications
        self.control_directory = control_directory
        self._source_settings: Any | None = None
        self._credentials: CredentialSourceStore | None = None
        # CredentialSourceStore performs its own Project transactions, so build
        # the existing-only handle before commander_facts_guard owns one. The
        # descriptor is still re-read below before every facts read/effect;
        # cached material is never a substitute for that current check.
        try:
            from karajan.orchestration.go_commander_qualification import (
                read_commander_qualification_settings,
            )

            settings, _ = read_commander_qualification_settings(self.control_directory)
            self._credentials = CredentialSourceStore(
                self.planner.projects,
                sources={
                    (row.project_id, row.auth_ref): LocalKeyFile(row.source_id, row.path)
                    for row in settings.credential_sources
                },
                private_directory=settings.credential_private_directory,
                existing_only=True,
            )
            self._source_settings = settings
        except (CredentialSourceError, OSError, RunError, ValueError):
            self._credentials = None

    def _current_source(
        self, db: sqlite3.Connection, project_id: str, current: dict[str, Any], principal: str
    ) -> dict[str, Any]:
        """Re-observe protected deployment/runtime and current credential material.

        The Go bootstrap is controller-owned and private. Its source identifiers
        only locate credential files; ``current_locked`` verifies their material
        seal before yielding a generation. A missing future Commander suite or
        unsupported platform remains unavailable instead of becoming a pass.
        """
        from karajan.orchestration.go_commander_qualification import (
            read_commander_qualification_settings,
        )
        from karajan.projects.go_commander_suite import FixedGoCommanderSuite

        settings, descriptor_sha256 = read_commander_qualification_settings(self.control_directory)
        cached = self._source_settings
        credentials = self._credentials
        if cached is None or credentials is None:
            raise RunError("COMMANDER_SOURCE_UNAVAILABLE")
        if settings.document() != cached.document():
            raise RunError("COMMANDER_SOURCE_CHANGED")
        from karajan.orchestration.go_commander_qualification import (
            validate_commander_qualification_settings,
        )
        legacy_history_only = settings.journal_path is None or settings.work_root is None
        if not legacy_history_only:
            validate_commander_qualification_settings(
                self.planner.projects,
                settings,
                repositories=(Path(current["repository"]["root"]).absolute(),),
            )
        profile = current["registration"]["profile"]
        generation = credentials.current_locked(
            db, project_id, profile["auth_ref"], principal=principal
        )
        if legacy_history_only:
            # This keeps old v2 material seals observable for record/history
            # recovery, but deliberately makes its source unequal to every
            # production start: it has no Journal/work-root authority.
            source = FixedGoCommanderSuite(
                settings.runtime,
                settings.tokenizer_directory,
                descriptor_sha256,
                descriptor_path=self.control_directory / "commander-qualification-source.v2.json",
                project_database=self.planner.projects.database,
            ).source(current, generation)
            source["legacy_history_only"] = True
            return source
        assert settings.journal_path is not None
        assert settings.work_root is not None
        from karajan.adapters.opencode.go_journal import GoCallJournal

        return FixedGoCommanderSuite(
            settings.runtime,
            settings.tokenizer_directory,
            descriptor_sha256,
            journal=GoCallJournal(settings.journal_path, existing_only=True),
            work_root=settings.work_root,
            descriptor_path=self.control_directory / "commander-qualification-source.v2.json",
            project_database=self.planner.projects.database,
        ).source(current, generation)

    def current_output_source(self, binding: dict[str, Any]) -> dict[str, Any]:
        """Read current protected producer material without requiring a pass.

        Output arming precedes admission.  A missing or failed Commander record
        must therefore remain an ordinary durable admission denial, rather than
        becoming a transport-only source exception.  This retains only the
        Project reader transaction needed for the existing sealed-current
        credential observation and never enters Capacity or an effect guard.
        """
        run = self.planner.get(binding["run_id"], principal=binding["owner"])
        registration = self._registration(run, binding)
        if registration is None:
            raise RunError("COMMANDER_SOURCE_UNAVAILABLE")
        with self.qualifications._owned(run["project_id"], binding["owner"]) as db:
            current = self.qualifications._binding(
                db,
                run["project_id"],
                {"id": registration["id"], "revision": registration["revision"]},
            )
            return self._current_source(db, run["project_id"], current, binding["owner"])

    @staticmethod
    def _registration(run: dict[str, Any], binding: dict[str, Any]) -> dict[str, Any] | None:
        """Resolve the frozen Commander profile without inventing a Plan."""
        return next(
            (
                row
                for row in run["configuration_snapshot"]["configuration"]["resources"]["profiles"]
                if {"id": row["id"], "revision": row["revision"]} == binding["profile"]
            ),
            None,
        )

    @contextmanager
    def current_guard_locked(
        self,
        binding: dict[str, Any],
        run: dict[str, Any],
        *,
        scope: str,
        reader_version: str,
    ) -> Iterator[dict[str, Any] | None]:
        """Hold current Project qualification facts through one actual effect.

        ``run`` comes only from the caller's already-held activation guard.  It
        is intentionally a private locked seam: public callers continue to
        provide IDs only and can never substitute an authority snapshot.
        """
        registration = self._registration(run, binding)
        if registration is None:
            yield None
            return
        with self.qualifications.commander_facts_guard(
            run["project_id"],
            registration,
            principal=binding["owner"],
            scope=scope,
            reader_version=reader_version,
        ) as current:
            if current is None:
                yield None
                return

            def envelope(value: dict[str, Any]) -> dict[str, Any]:
                return {
                    "schema_version": "karajan.commander-qualification.v1",
                    "scope": scope,
                    "reader_version": reader_version,
                    "binding_sha256": digest(binding),
                    **value,
                }

            refreshed = getattr(current, "recheck", None)
            if callable(refreshed):
                yield _CurrentCommanderQualification(
                    envelope(current),
                    lambda: None if (next_value := refreshed()) is None else envelope(next_value),
                )
            else:
                yield envelope(current)

    def read_commander(
        self, binding: dict[str, Any], *, scope: str, reader_version: str
    ) -> dict[str, Any] | None:
        # The reader opens the existing Project qualification ledger. A missing
        # #113 source/record is an explicit no-fact result, never a conversion
        # of frozen declaration or Worker/Reviewer observations.
        run = self.planner.get(binding["run_id"], principal=binding["owner"])
        with self.current_guard_locked(
            binding,
            run,
            scope=scope,
            reader_version=reader_version,
        ) as current:
            return current


def open_persistent_planning_admission(control_directory: Path) -> "PlanningAdmissionAuthority":
    """Rebuild only fixed existing stores from a private deployment descriptor."""
    settings, bootstrap_sha = read_planning_bootstrap(control_directory)
    projects = ProjectRegistry(
        settings.projects_database, settings.allowed_roots, existing_only=True
    )
    planner = RunPlanner(settings.state_directory / "runs.sqlite", projects, existing_only=True)
    capacity = CapacityStore(settings.capacity_database, existing_only=True)
    require_schema(
        settings.planning_execution_database,
        {
            "executions": ["id", "run_id", "intent_id", "state", "data"],
            "commands": ["principal", "key", "payload", "result"],
        },
    )
    qualifications = ProfileQualificationStore(projects, commander_reader_only=True)
    reader = PersistentCommanderQualificationReader(
        planner, qualifications, control_directory=settings.control_directory
    )
    qualifications.commander_source = reader._current_source
    return PlanningAdmissionAuthority(
        settings.planning_admission_database,
        settings.planning_execution_database,
        planner,
        capacity,
        reader,
        authority_kind="production",
        existing_only=True,
        bootstrap_sha256=bootstrap_sha,
        bootstrap_control_directory=settings.control_directory,
    )


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
        bootstrap_sha256: str | None = None,
        bootstrap_control_directory: Path | None = None,
    ) -> None:
        if authority_kind not in {"fixture", "production"}:
            raise RunError("PLANNING_AUTHORITY_KIND_INVALID")
        if existing_only and not (planner.existing_only and capacity.existing_only):
            raise RunError("EXISTING_STORE_PARENT_MODE_REQUIRED")
        self.database, self.execution_database = database.resolve(), execution_database.resolve()
        self.planner, self.capacity = planner, capacity
        self.qualifications = qualifications
        self.authority_kind, self.existing_only = authority_kind, existing_only
        self.bootstrap_sha256 = bootstrap_sha256
        self.bootstrap_control_directory = bootstrap_control_directory
        if (bootstrap_sha256 is None) != (bootstrap_control_directory is None):
            raise RunError("PLANNING_ADMISSION_BOOTSTRAP_INVALID")
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

    def _assert_bootstrap_current(self) -> None:
        if self.bootstrap_sha256 is not None and self.bootstrap_control_directory is not None:
            assert_planning_bootstrap_current(
                self.bootstrap_control_directory, self.bootstrap_sha256
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
        return self._run_intent_from_run(run, binding, principal)

    @staticmethod
    def _run_intent_from_run(
        run: dict[str, Any], binding: dict[str, Any], principal: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Validate a Run snapshot supplied by an already-held Run guard."""
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
        return run, intent

    @staticmethod
    def _planning_authorization(
        run: dict[str, Any], binding: dict[str, Any], record: dict[str, Any]
    ) -> dict[str, Any]:
        """Derive a narrow planning-only authorization from frozen v2 Run facts.

        A planning intent has no approved Plan or task grant.  This document is
        therefore deliberately a new routing identity, bounded only by the
        owner-frozen execution policy, Run ceiling, planning budget, and the
        rulebook that were all frozen on the original Run.
        """
        execution = run["execution_policy_snapshot"]
        ceiling = run["authorization_ceiling"]
        constraints = execution["constraints"]
        permitted = {(row["id"], row["revision"]) for row in constraints["profile_refs"]} & {
            (row["id"], row["revision"]) for row in ceiling["profile_refs"]
        }
        profiles = [
            row for row in constraints["profile_refs"] if (row["id"], row["revision"]) in permitted
        ]
        rulebook = run["configuration_snapshot"]["configuration"]["rulebook"]
        approved_groups = {
            group: [row for row in members if (row["id"], row["revision"]) in permitted]
            for group, members in rulebook["profile_groups"].items()
        }
        return {
            "profile_refs": profiles,
            "ceiling_profile_refs": profiles,
            "channel_ids": sorted(set(constraints["channel_ids"]) & set(ceiling["channel_ids"])),
            "tools": sorted(set(constraints["tools"]) & set(ceiling["tools"])),
            "data_destinations": sorted(
                set(constraints["data_destinations"]) & set(ceiling["data_destinations"])
            ),
            "required_capabilities": sorted(
                set(constraints["required_capabilities"]) | set(ceiling["required_capabilities"])
            ),
            "min_isolation": "tool_sandboxed",
            "allowed_stages": ["normal"],
            "approved_groups": approved_groups,
            "approved_quality_stage_indices": [],
            "budget_ref": binding["budget_ref"],
            "currency_limits": record["budget"]["currency_limits"],
            "max_attempt_duration_seconds": min(
                record["estimate"]["duration_seconds"],
                record["budget"]["max_duration_seconds"],
                ceiling["max_attempt_duration_seconds"],
            ),
            "max_quality_repair_rounds": 0,
        }

    def _evaluate_planning_route(
        self,
        run: dict[str, Any],
        binding: dict[str, Any],
        record: dict[str, Any],
        qualification: dict[str, Any],
    ) -> dict[str, Any]:
        """Run the ordinary routing evaluator over frozen policy and live capacity.

        This intentionally feeds the evaluator a distinct planning schema
        instead of inventing approved Task/Plan identifiers before approval.
        """
        facts = self.capacity.routing_facts()
        fixed = run["configuration_snapshot"]["configuration"]
        with self.planner.projects.effective_resources_guard(run["project_id"]) as current_catalog:
            if (
                current_catalog.get("resources") != fixed["resources"]
                or current_catalog.get("approved_profile_refs") != fixed["approved_profile_refs"]
            ):
                raise RunError("PLANNING_CURRENT_CATALOG_CHANGED")
        resources = deepcopy(fixed["resources"])
        capacity, diagnostics = _capacity_snapshot(facts.as_dict(), resources)
        estimate = record["estimate"]
        registration = next(
            row
            for row in resources["profiles"]
            if {"id": row["id"], "revision": row["revision"]} == binding["profile"]
        )
        verified_capabilities = qualification.get("capability_evidence")
        if not isinstance(verified_capabilities, list):
            raise RunError("COMMANDER_CAPABILITY_EVIDENCE_REQUIRED")
        registration["capability_evidence"] = verified_capabilities
        windows = {
            row["id"]: row
            for row in capacity["pools"]
            if row["id"] in registration["quota_pool_refs"]
        }
        capacity["estimates"] = [
            {
                "profile": binding["profile"],
                "demand": [
                    {
                        "pool_id": pool_id,
                        "unit": windows[pool_id]["unit"],
                        "window_id": windows[pool_id]["window_id"],
                        "amount": amount,
                    }
                    for pool_id, amount in sorted(estimate["demand"].items())
                    if pool_id in windows
                ],
                "confidence": "unknown",
                "completion_seconds": float(estimate["duration_seconds"]),
                "price": None,
                "evidence_ref": "planning-estimate:" + estimate["digest"],
            }
        ]
        capacity["id"] = "planning-capacity:" + digest(
            [facts.sha256, estimate["digest"], capacity["estimates"]]
        )
        task = {
            "schema_version": "karajan.routing.planning.v1",
            "run_id": binding["run_id"],
            "intent_id": binding["intent_id"],
            "execution_id": binding["execution_id"],
            "planning_binding_sha256": digest(binding),
            "role": "commander",
            "purpose": "lead",
            "readiness": "ready",
            "complexity": "T1",
            "risk": "standard",
            "domains": [],
            "paths": [],
            "authors": [],
            "required_capabilities": ["design_reasoning", "structured_plan_output"],
            # Planning has no approved Task, but it must still account for the
            # complete owner-frozen context envelope. A later transport can
            # narrow tools, never silently widen this bounded snapshot.
            "tools": [],
            "context_tokens": run["execution_policy_snapshot"]["max_context_tokens"]
            - run["execution_policy_snapshot"]["context_policy"]["reserved_output_tokens"],
            "reserved_output_tokens": run["execution_policy_snapshot"]["context_policy"][
                "reserved_output_tokens"
            ],
            "duration_seconds": estimate["duration_seconds"],
            "stage": "normal",
            "quality_stage_index": 0,
            "failure_reason": None,
            "previous_profile": None,
            "quality_repair_rounds_used": 0,
            "planned_attempt_id": binding["attempt_id"],
            "planned_context_id": "planning-context:" + binding["execution_id"],
            "authorization": self._planning_authorization(run, binding, record),
        }
        policy = {
            "schema_version": "karajan.routing.policy.v1",
            "rulebook": fixed["rulebook"],
            "resources": resources,
            "approved_profile_refs": fixed["approved_profile_refs"],
            "profile_facts": [qualification["profile_facts"]],
            "risk_policy": run["execution_policy_snapshot"]["risk_policy"],
            "constraints": run["execution_policy_snapshot"]["constraints"],
        }
        try:
            route = evaluate_route(task, policy, capacity)
            reserved = evaluate_reserved_profile(task, policy, capacity, binding["profile"])
        except (RoutingError, KeyError, TypeError, ValueError):
            raise RunError("PLANNING_ROUTE_INPUT_INVALID") from None
        selected_rule = next(
            (row for row in fixed["rulebook"]["rules"] if row["id"] == route["rule_id"]),
            None,
        )
        if not isinstance(selected_rule, dict):
            raise RunError("PLANNING_ROUTE_INPUT_INVALID")
        return {
            "route": route,
            "reserved": reserved,
            # The estimate witnesses the exact Capacity observation, but a
            # caller cannot use it to widen the selected frozen rule's reserve
            # permission.  Capacity receives this rule-derived bit below.
            "lead_reserve_access": selected_rule.get("lead_reserve_access") is not False,
            "capacity_facts_sha256": facts.sha256,
            "capacity_diagnostics": diagnostics,
            "task_sha256": digest(task),
            "policy_sha256": digest(policy),
            "capacity_sha256": digest(capacity),
        }

    def _revalidate_boundary_route(
        self,
        boundary: CapacityBoundaryFacts,
        record: dict[str, Any],
        binding: dict[str, Any],
        *,
        as_of: float,
    ) -> QuotaTemporalFence:
        """Run the normal quota algorithm over Capacity's final immutable facts."""
        route = record["route_sources"]["route"]
        task, policy = route["snapshots"]["task"], route["snapshots"]["policy"]
        resources = deepcopy(policy["resources"])
        derived = derive_capacity_boundary_facts(
            boundary, expected_request=record["capacity_request"]
        )
        captured_at = derived.get("captured_at")
        if type(as_of) not in (int, float) or type(captured_at) not in (int, float):
            raise RunError("RUN_EXECUTION_CLOCK_REGRESSED")
        observed_as_of, original_capture = as_of, cast(float, captured_at)
        if (
            not math.isfinite(observed_as_of)
            or not math.isfinite(original_capture)
            or observed_as_of < original_capture
        ):
            raise RunError("RUN_EXECUTION_CLOCK_REGRESSED")
        # This is a marked derived routing view. It preserves the immutable
        # Capacity facts and their hash, while evaluating conservative age at
        # the final controller boundary rather than its earlier capture time.
        routing_facts = {**derived, "captured_at": observed_as_of}
        capacity, _ = _capacity_snapshot(routing_facts, resources)
        estimate = record["estimate"]
        capacity["estimates"] = [
            {
                "profile": binding["profile"],
                "demand": [
                    {
                        "pool_id": pool_id,
                        "unit": next(p for p in capacity["pools"] if p["id"] == pool_id)["unit"],
                        "window_id": next(p for p in capacity["pools"] if p["id"] == pool_id)[
                            "window_id"
                        ],
                        "amount": amount,
                    }
                    for pool_id, amount in sorted(estimate["demand"].items())
                ],
                "confidence": "unknown",
                "completion_seconds": float(estimate["duration_seconds"]),
                "price": None,
                "evidence_ref": "planning-estimate:" + estimate["digest"],
            }
        ]
        capacity["id"] = "planning-boundary:" + boundary.facts.sha256
        try:
            result = evaluate_reserved_profile(
                task, policy, capacity, binding["profile"], revalidate_quota=True
            )
        except (RoutingError, KeyError, TypeError, ValueError):
            raise RunError("PLANNING_BOUNDARY_ROUTE_INVALID") from None
        if result["selected_profile"] != binding["profile"]:
            raise RunError("PLANNING_BOUNDARY_ROUTE_REJECTED")
        try:
            return capture_quota_temporal_fence(result)
        except RoutingError:
            raise RunError("PLANNING_BOUNDARY_ROUTE_INVALID") from None

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
        if execution.get("cancel_requested") or execution.get("state") in {
            "cancelled",
            "submission_unknown",
        }:
            raise RunError("PLANNING_EXECUTION_CANCELLED")
        binding = execution.get("binding")
        if not isinstance(binding, dict) or binding.get("execution_id") != execution_id:
            raise RunError("PLANNING_EXECUTION_BINDING_INVALID")
        self.planner.get(binding["run_id"], principal=principal)
        return binding

    @contextmanager
    def _execution_guard(self, execution_id: str, principal: str) -> Iterator[dict[str, Any]]:
        """Keep cancellation from crossing an already-entered effect boundary."""
        db = sqlite3.connect(self.execution_database, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM executions WHERE id=?", (execution_id,)).fetchone()
            if row is None:
                raise RunError("PLANNING_EXECUTION_NOT_FOUND")
            execution = dict(json.loads(row["data"]))
            if execution.get("cancel_requested") or execution.get("state") in {
                "cancelled",
                "submission_unknown",
            }:
                raise RunError("PLANNING_EXECUTION_CANCELLED")
            binding = execution.get("binding")
            if not isinstance(binding, dict) or binding.get("execution_id") != execution_id:
                raise RunError("PLANNING_EXECUTION_BINDING_INVALID")
            self.planner.get(binding["run_id"], principal=principal)
            yield binding
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _finish_command(
        self, record: dict[str, Any], principal: str, command_key: str, payload: str
    ) -> dict[str, Any]:
        """Every command key, including a rejection, is durably non-reusable."""
        with self._transaction() as db:
            prior = db.execute(
                "SELECT payload,result FROM commands WHERE principal=? AND key=?",
                (principal, command_key),
            ).fetchone()
            if prior is not None:
                if prior["payload"] != payload:
                    raise RunError("IDEMPOTENCY_CONFLICT")
                db.execute(
                    "UPDATE commands SET result=? WHERE principal=? AND key=?",
                    (encoded(record), principal, command_key),
                )
            else:
                db.execute(
                    "INSERT INTO commands VALUES (?,?,?,?)",
                    (principal, command_key, payload, encoded(record)),
                )
            return record

    def _claim_command(
        self, principal: str, command_key: str, payload: str
    ) -> dict[str, Any] | None:
        """Durably bind a caller key before any budget or Capacity mutation."""
        placeholder = {
            "schema_version": "karajan.planning-admission-command.v1",
            "phase": "claimed",
        }
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
                "INSERT INTO commands VALUES (?,?,?,?)",
                (principal, command_key, payload, encoded(placeholder)),
            )
        return None

    @staticmethod
    def _capacity_admit_transition(record: dict[str, Any], receipt: dict[str, Any]) -> None:
        """Apply the one durable admission receipt meaning in normal and recovery paths."""
        record["capacity_receipt"] = receipt
        if receipt.get("decision") != "admitted" or not isinstance(
            receipt.get("admission_id"), str
        ):
            record["phase"] = "denied"
            record["reason_codes"] = receipt.get("reason_codes", ["PLANNING_CAPACITY_DENIED"])
            return
        record["capacity_activation_request"] = {"admission_id": receipt["admission_id"]}
        record["phase"] = "capacity_activate_unknown"

    def _budget_live(self, record: dict[str, Any]) -> None:
        """Recheck the original frozen Run deadline immediately before every effect."""
        with self._transaction() as db:
            row = db.execute(
                "SELECT data FROM planning_budget_usage WHERE run_id=? AND budget_identity=?",
                (record["run_id"], record["budget_identity"]),
            ).fetchone()
        if row is None:
            raise RunError("PLANNING_BUDGET_USAGE_MISSING")
        usage = dict(json.loads(row["data"]))
        self._assert_budget_deadline(record, usage)

    def _assert_budget_deadline(
        self, record: dict[str, Any], usage: dict[str, Any], *, now: float | None = None
    ) -> None:
        """Check the durable first-claim clock without opening a nested store.

        Capacity invokes this while it already owns its write transaction.  The
        usage is the sealed claim recorded before the Capacity request; only
        its immutable first-claim clock is needed at that narrow boundary.
        """
        observed = self.planner.clock() if now is None else now
        started = usage.get("started_at")
        duration = record["budget"].get("max_duration_seconds")
        if (
            type(observed) not in (int, float)
            or type(started) not in (int, float)
            or type(duration) is not int
            or duration <= 0
        ):
            raise RunError("PLANNING_BUDGET_EXPIRED")
        observed_at, started_at = observed, cast(float, started)
        if not math.isfinite(observed_at) or not math.isfinite(started_at):
            raise RunError("PLANNING_BUDGET_EXPIRED")
        if observed_at < started_at:
            raise RunError("RUN_EXECUTION_CLOCK_REGRESSED")
        if observed_at >= started_at + duration:
            raise RunError("PLANNING_BUDGET_EXPIRED")

    def _current_qualification_guard(
        self, binding: dict[str, Any], held_run: dict[str, Any]
    ) -> Any:
        """Keep an actual current Commander fact locked through Capacity.

        Persistent readers supply a private Run-locked Project guard. Fixture
        readers retain the legacy C seam, but still return a fresh fact that is
        checked again at the Capacity boundary.
        """
        reader_guard = getattr(self.qualifications, "current_guard_locked", None)
        if callable(reader_guard):
            return reader_guard(
                binding,
                held_run,
                scope=COMMANDER_QUALIFICATION_SCOPE,
                reader_version=COMMANDER_QUALIFICATION_READER_VERSION,
            )
        return nullcontext(
            self.qualifications.read_commander(
                binding,
                scope=COMMANDER_QUALIFICATION_SCOPE,
                reader_version=COMMANDER_QUALIFICATION_READER_VERSION,
            )
        )

    def _assert_qualification_live(
        self,
        record: dict[str, Any],
        current: object,
        *,
        reobserve: bool = False,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Reject source/fact drift or expiry at the actual Capacity boundary."""
        current = self._assert_qualification_binding(record, current, reobserve=reobserve)
        observed = self.planner.clock() if now is None else now
        valid_until, facts_valid_until = self._qualification_deadlines(current)
        if type(observed) not in (int, float):
            raise RunError("COMMANDER_QUALIFICATION_EXPIRED")
        observed_at = observed
        if (
            not math.isfinite(observed_at)
            or not math.isfinite(valid_until)
            or valid_until <= observed_at
        ):
            raise RunError("COMMANDER_QUALIFICATION_EXPIRED")
        if not math.isfinite(facts_valid_until) or facts_valid_until <= observed_at:
            raise RunError("COMMANDER_PROFILE_FACTS_EXPIRED")
        return current

    @staticmethod
    def _qualification_deadlines(current: dict[str, Any]) -> tuple[float, float]:
        """Extract the two frozen Commander deadlines before a final time tail."""
        valid_until = current.get("valid_until")
        if type(valid_until) not in (int, float):
            raise RunError("COMMANDER_QUALIFICATION_EXPIRED")
        facts = current.get("profile_facts")
        facts_valid_until = facts.get("valid_until") if isinstance(facts, dict) else None
        if type(facts_valid_until) not in (int, float):
            raise RunError("COMMANDER_PROFILE_FACTS_EXPIRED")
        return cast(float, valid_until), cast(float, facts_valid_until)

    @staticmethod
    def _assert_qualification_binding(
        record: dict[str, Any], current: object, *, reobserve: bool = False
    ) -> dict[str, Any]:
        """Complete source and fact comparison, without sampling a clock."""
        if reobserve:
            refresh = getattr(current, "recheck", None)
            if callable(refresh):
                current = refresh()
        if not isinstance(current, dict) or current != record.get("qualification"):
            raise RunError("COMMANDER_QUALIFICATION_CHANGED")
        return current

    def _assert_estimate_live(self, record: dict[str, Any], held_run: dict[str, Any]) -> None:
        """Validate the sealed, finite estimate without reopening a store."""
        estimate = record.get("estimate")
        if not isinstance(estimate, dict):
            raise RunError("PLANNING_ESTIMATE_MISSING")
        sealed = {key: value for key, value in estimate.items() if key != "digest"}
        if (
            estimate.get("schema_version") != "karajan.planning-estimate.v1"
            or not isinstance(estimate.get("digest"), str)
            or estimate["digest"] != digest(sealed)
            or estimate.get("configuration_sha256") != held_run["configuration_snapshot"]["digest"]
            or not isinstance(estimate.get("demand"), dict)
            or not estimate["demand"]
            or any(type(value) is not str for value in estimate["demand"].values())
            or any(
                type(estimate.get(field)) is not int or estimate[field] <= 0
                for field in ("duration_seconds", "max_requests", "max_duration_seconds")
            )
        ):
            raise RunError("PLANNING_ESTIMATE_INVALID")

    def _capture_final_boundary(
        self, record: dict[str, Any], qualification: object, held_run: dict[str, Any]
    ) -> _FinalBoundary:
        """Finish comparison and parsing before Capacity's constant-time tail."""
        self._assert_estimate_live(record, held_run)
        current = self._assert_qualification_binding(record, qualification)
        qualification_until, facts_until = self._qualification_deadlines(current)
        usage = record.get("budget_usage")
        budget = record.get("budget")
        started = usage.get("started_at") if isinstance(usage, dict) else None
        duration = budget.get("max_duration_seconds") if isinstance(budget, dict) else None
        if (
            not isinstance(started, (int, float))
            or isinstance(started, bool)
            or type(duration) is not int
            or duration <= 0
        ):
            raise RunError("PLANNING_BUDGET_USAGE_INVALID")
        return _FinalBoundary(
            qualification_valid_until=qualification_until,
            profile_facts_valid_until=facts_until,
            budget_started_at=float(started),
            budget_duration_seconds=duration,
        )

    def _assert_final_boundary_temporal(self, boundary: _FinalBoundary) -> None:
        """Perform no parsing, hashing, comparison, or I/O after route evaluation."""
        observed = self.planner.clock()
        if type(observed) not in (int, float):
            raise RunError("COMMANDER_QUALIFICATION_EXPIRED")
        observed_at = observed
        if (
            not math.isfinite(observed_at)
            or not math.isfinite(boundary.qualification_valid_until)
            or boundary.qualification_valid_until <= observed_at
        ):
            raise RunError("COMMANDER_QUALIFICATION_EXPIRED")
        if (
            not math.isfinite(boundary.profile_facts_valid_until)
            or boundary.profile_facts_valid_until <= observed_at
        ):
            raise RunError("COMMANDER_PROFILE_FACTS_EXPIRED")
        started = boundary.budget_started_at
        if not math.isfinite(started):
            raise RunError("PLANNING_BUDGET_EXPIRED")
        if observed_at < started:
            raise RunError("RUN_EXECUTION_CLOCK_REGRESSED")
        if observed_at - started >= boundary.budget_duration_seconds:
            raise RunError("PLANNING_BUDGET_EXPIRED")

    def _assert_quota_fence_current(self, fence: QuotaTemporalFence) -> None:
        try:
            fence.assert_current(as_of=self.capacity._now())
        except RoutingError:
            raise RunError("PLANNING_BOUNDARY_ROUTE_REJECTED") from None

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
                if (
                    self.authority_kind == "production"
                    and existing.get("producer_authority_kind") != "production"
                ):
                    raise RunError("PLANNING_ADMISSION_PROVENANCE_FORBIDDEN")
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
                "producer_authority_kind": self.authority_kind,
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
                    "started_at": self.planner.clock(),
                }
                if usage_row is None
                else dict(json.loads(usage_row["data"]))
            )
            estimate = current["estimate"]
            if (
                self.planner.clock()
                >= usage["started_at"] + current["budget"]["max_duration_seconds"]
            ):
                current["phase"], current["reason_codes"] = "denied", ["PLANNING_BUDGET_EXPIRED"]
                self._save(db, current)
                return current
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
        """Fence cancellation through every planning admission mutation."""
        for value in (execution_id, principal, command_key):
            identifier(value)
        self._assert_bootstrap_current()
        with self._execution_guard(execution_id, principal) as binding:
            return self._advance_locked(execution_id, principal, command_key, binding)

    def _advance_locked(
        self, execution_id: str, principal: str, command_key: str, binding: dict[str, Any]
    ) -> dict[str, Any]:
        """Perform one admission while the private execution cancellation fence is held."""
        run = self.planner.get(binding["run_id"], principal=principal)
        payload = encoded([execution_id, digest(binding)])
        prior = self._claim_command(principal, command_key, payload)
        if prior is not None:
            return prior
        record = self._prepare(execution_id, binding, principal)
        if record["phase"] in {"admitted", "denied"}:
            return self._finish_command(record, principal, command_key, payload)
        if record["phase"] == "unknown":
            request = record.get("capacity_activation_request")
            if not isinstance(request, dict):
                return self._finish_command(record, principal, command_key, payload)
            receipt = self.capacity.command_receipt(
                "activate", request, command_key=record["capacity_activation_command_key"]
            )
            if receipt is None:
                return self._finish_command(record, principal, command_key, payload)
            with self._transaction() as db:
                current = self._load(db, execution_id) or record
                current["capacity_activation_receipt"] = receipt
                current["phase"] = (
                    "admitted" if receipt.get("decision") == "capacity_revalidated" else "denied"
                )
                current["reason_codes"] = receipt.get("reason_codes", [])
                self._save(db, current)
                record = current
            return self._finish_command(record, principal, command_key, payload)
        if record["phase"] == "capacity_admit_unknown":
            request = record["capacity_request"]
            assert isinstance(request, dict)
            receipt = self.capacity.command_receipt(
                "admit", request, command_key=record["capacity_command_key"]
            )
            if receipt is None:
                return self._finish_command(record, principal, command_key, payload)
            with self._transaction() as db:
                current = self._load(db, execution_id) or record
                self._capacity_admit_transition(current, receipt)
                self._save(db, current)
                record = current
            if record["phase"] == "denied":
                return self._finish_command(record, principal, command_key, payload)
        if record["phase"] == "capacity_activate_unknown":
            request = record["capacity_activation_request"]
            assert isinstance(request, dict)
            receipt = self.capacity.command_receipt(
                "activate", request, command_key=record["capacity_activation_command_key"]
            )
            if receipt is None:
                return self._finish_command(record, principal, command_key, payload)
            with self._transaction() as db:
                current = self._load(db, execution_id) or record
                current["capacity_activation_receipt"] = receipt
                current["phase"] = (
                    "admitted" if receipt.get("decision") == "capacity_revalidated" else "denied"
                )
                current["reason_codes"] = receipt.get("reason_codes", [])
                self._save(db, current)
                record = current
            return self._finish_command(record, principal, command_key, payload)
        # A pre-Plan execution may only draw planning authority from the full,
        # owner-frozen v2 policy.  Legacy Runs do not carry the authorization
        # fields needed by the ordinary evaluator; guessing them would expand
        # production authority.
        execution_policy = run.get("execution_policy_snapshot")
        if (
            run.get("schema_version") != "karajan.run-planning.v2"
            or not isinstance(execution_policy, dict)
            or binding.get("execution_policy_sha256") != execution_policy.get("digest")
        ):
            return self._finish_command(
                self._deny(record, "PLANNING_POLICY_REQUIRED"),
                principal,
                command_key,
                payload,
            )
        # Route construction needs the concrete demand and duration.  Deny it
        # before dereferencing the estimate so a provisioner omission is an
        # idempotent, zero-effect command outcome rather than a claimed-key
        # placeholder that cannot be replayed.
        if not isinstance(record.get("estimate"), dict):
            return self._finish_command(
                self._deny(record, "PLANNING_ESTIMATE_MISSING"),
                principal,
                command_key,
                payload,
            )
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
            return self._finish_command(
                self._deny(record, "PLANNING_CASH_UPPER_BOUND_REQUIRED"),
                principal,
                command_key,
                payload,
            )
        qualification = self.qualifications.read_commander(
            binding,
            scope=COMMANDER_QUALIFICATION_SCOPE,
            reader_version=COMMANDER_QUALIFICATION_READER_VERSION,
        )
        if (
            not isinstance(qualification, dict)
            or qualification.get("schema_version") != "karajan.commander-qualification.v1"
            or qualification.get("scope") != COMMANDER_QUALIFICATION_SCOPE
            or qualification.get("reader_version") != COMMANDER_QUALIFICATION_READER_VERSION
            or qualification.get("binding_sha256") != digest(binding)
            or qualification.get("source_generation_sha256") is None
            or not isinstance(qualification.get("profile_facts"), dict)
            or not isinstance(qualification.get("capability_evidence"), list)
            or not isinstance(qualification.get("valid_until"), (int, float))
            or qualification["valid_until"] <= self.planner.clock()
            or self.authority_kind == "production"
            and qualification.get("provenance") != "official"
        ):
            record = self._deny(record, "COMMANDER_QUALIFICATION_REQUIRED")
        else:
            route_sources = self._evaluate_planning_route(run, binding, record, qualification)
            route, reserved = route_sources["route"], route_sources["reserved"]
            with self._transaction() as db:
                stored = self._load(db, execution_id)
                if stored is not None and stored["phase"] == "prepared":
                    stored["route_sources"] = route_sources
                    self._save(db, stored)
                    record = stored
            if (
                route["selected_profile"] != binding["profile"]
                or reserved["selected_profile"] != binding["profile"]
            ):
                record = self._deny(record, "COMMANDER_ROUTE_NOT_AUTHORIZED")
                return self._finish_command(record, principal, command_key, payload)
            with self._transaction() as db:
                stored = self._load(db, execution_id)
                if stored is not None:
                    record = stored
                if record["phase"] == "prepared":
                    record["qualification"] = qualification
                    self._save(db, record)
            claimed = self._claim_budget(record)
            if claimed is None:
                return self._finish_command(
                    self._deny(record, "PLANNING_ADMISSION_UNKNOWN"),
                    principal,
                    command_key,
                    payload,
                )
            record = claimed
        if record is None or record["phase"] in {"denied", "unknown"}:
            return self._finish_command(
                record
                or self._deny(
                    self._prepare(execution_id, binding, principal), "PLANNING_ADMISSION_UNKNOWN"
                ),
                principal,
                command_key,
                payload,
            )
        estimate = record["estimate"]
        pools = {row["id"]: row for row in self.capacity.snapshot()["pools"]}
        if any(
            pool_id not in pools
            or pools[pool_id]["unit"] == "requests"
            and units(amount) < estimate["max_requests"] * units("1")
            for pool_id, amount in estimate["demand"].items()
        ):
            return self._finish_command(
                self._deny(record, "PLANNING_REQUEST_DEMAND_UNDERRESERVED"),
                principal,
                command_key,
                payload,
            )
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
            return self._finish_command(
                self._deny(record, "PLANNING_CAPACITY_BINDING_REQUIRED"),
                principal,
                command_key,
                payload,
            )
        rule_access = record.get("route_sources", {}).get("lead_reserve_access")
        if (
            type(rule_access) is not bool
            or request["expected_capacity"].get("lead_reserve_access") is not rule_access
        ):
            return self._finish_command(
                self._deny(record, "PLANNING_LEAD_RESERVE_ACCESS_MISMATCH"),
                principal,
                command_key,
                payload,
            )
        request["expected_capacity"] = {
            **request["expected_capacity"],
            "lead_reserve_access": rule_access,
        }
        dispatch_admit = False
        with self._transaction() as db:
            record = self._load(db, execution_id) or record
            if record["phase"] == "budget_claimed":
                record["capacity_request"], record["phase"] = request, "capacity_admit_unknown"
                self._save(db, record)
                dispatch_admit = True
        if not dispatch_admit:
            return self._finish_command(record, principal, command_key, payload)
        try:
            # Run stays held before Capacity.  The callback runs after Capacity
            # accepts the exact request but before it writes a reservation, so
            # a clock that crossed the original Run's first-claim deadline
            # cannot create a late reservation.
            with self.planner.activation_guard(binding["run_id"]) as held_run:
                self._run_intent_from_run(held_run, binding, principal)
                with self._current_qualification_guard(binding, held_run) as qualification:
                    self._assert_qualification_live(record, qualification)
                    self._budget_live(record)

                    def before_reserve() -> None:
                        # The planning deployment descriptor is itself an
                        # authority source. Re-read its fixed digest after a
                        # potentially long Capacity wait, before evaluating
                        # the sealed commander source and budget.
                        self._assert_bootstrap_current()
                        self._assert_qualification_live(record, qualification, reobserve=True)
                        self._assert_budget_deadline(record, record["budget_usage"])

                    boundary: list[CapacityBoundaryFacts] = []

                    def after_capacity_facts(facts: CapacityBoundaryFacts) -> None:
                        boundary.append(facts)

                    def before_reservation_write() -> Callable[[], None]:
                        if len(boundary) != 1:
                            raise RunError("PLANNING_BOUNDARY_FACTS_REQUIRED")
                        # Finish every parsing, binding comparison, and shared
                        # route calculation before Capacity serializes its write
                        # payload. The returned tail contains only the captured
                        # scalar deadlines and quota temporal fence.
                        final_boundary = self._capture_final_boundary(
                            record, qualification, held_run
                        )
                        self._assert_final_boundary_temporal(final_boundary)
                        fence = self._revalidate_boundary_route(
                            boundary[0], record, binding, as_of=self.capacity._now()
                        )
                        self._assert_quota_fence_current(fence)

                        def final_validator() -> None:
                            self._assert_quota_fence_current(fence)
                            self._assert_final_boundary_temporal(final_boundary)

                        return final_validator

                    receipt = self.capacity.command_receipt(
                        "admit", request, command_key=record["capacity_command_key"]
                    ) or self.capacity.admit(
                        request,
                        command_key=record["capacity_command_key"],
                        before_reserve=before_reserve,
                        after_capacity_facts=after_capacity_facts,
                        before_reservation_write=before_reservation_write,
                    )
        except (CapacityError, RunError) as error:
            return self._finish_command(
                self._deny(record, str(error)), principal, command_key, payload
            )
        dispatch_activate = False
        with self._transaction() as db:
            record = self._load(db, execution_id) or record
            self._capacity_admit_transition(record, receipt)
            self._save(db, record)
            dispatch_activate = record["phase"] == "capacity_activate_unknown"
        if not dispatch_activate:
            return self._finish_command(record, principal, command_key, payload)
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
                    "capacity_activate_unknown",
                    ["PLANNING_CAPACITY_ACTIVATION_UNKNOWN"],
                )
                self._save(db, record)
            return self._finish_command(record, principal, command_key, payload)
        with self._transaction() as db:
            record = self._load(db, execution_id) or record
            record["capacity_activation_receipt"] = activation
            record["phase"] = (
                "admitted" if activation.get("decision") == "capacity_revalidated" else "denied"
            )
            record["reason_codes"] = activation.get("reason_codes", [])
            self._save(db, record)
        return self._finish_command(record, principal, command_key, payload)

    def current_output_source(self, binding: dict[str, Any]) -> dict[str, Any]:
        """Expose the read-only current Commander material to OutputAuthority."""
        reader = getattr(self.qualifications, "current_output_source", None)
        if not callable(reader):
            raise RunError("COMMANDER_SOURCE_UNAVAILABLE")
        return reader(binding)

    def read_admission(self, binding: dict[str, Any]) -> object:
        """Read the original durable record only; it never calls Capacity."""
        with self._transaction() as db:
            record = self._load(db, binding["execution_id"])
        if record is None or record["binding_sha256"] != digest(binding):
            raise ValueError("PLANNING_ADMISSION_NOT_FOUND")
        return {
            "schema_version": "karajan.planning-admission-evidence.v1",
            "binding_sha256": record["binding_sha256"],
            # Evidence describes the producer that made the durable decision,
            # never the process that happened to reopen its database later.
            "authority_kind": record.get("producer_authority_kind", "fixture"),
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
            if record["phase"]
            in {
                "unknown",
                "capacity_admit_unknown",
                "capacity_activate_unknown",
            }
            else "denied",
            "reason_codes": record.get("reason_codes", []),
        }

    @contextmanager
    def effect_guard(
        self, execution_id: str, principal: str, effect_id: str
    ) -> Iterator[dict[str, Any]]:
        """Fresh, non-reusable authorization around one owned transport effect."""
        for value in (execution_id, principal, effect_id):
            identifier(value)
        self._assert_bootstrap_current()
        # Admission owns its own execution fence. Reopen it only after that
        # mutation completes, so a cancellation between admission and effect
        # still wins before any transport boundary is entered.
        record = self.advance(execution_id, principal, "planning-guard:" + effect_id)
        with self._execution_guard(execution_id, principal) as binding:
            if record["phase"] != "admitted" or record["binding"] != binding:
                raise RunError("PLANNING_EFFECT_NOT_ADMITTED")
            # Keep execution -> Run -> Project/qualification -> Capacity in
            # this order until the actual transport boundary exits.  The Run
            # guard supplies its snapshot so no public getter re-enters it.
            with self.planner.activation_guard(binding["run_id"]) as held_run:
                self._run_intent_from_run(held_run, binding, principal)
                with self._current_qualification_guard(binding, held_run) as qualification:
                    self._assert_qualification_live(record, qualification)

                    def before_effect() -> None:
                        # This can read the sealed credential material, runtime
                        # observer and protected bootstrap. Capacity performs
                        # its own fresh temporal/evaluate pass only after this
                        # controller callback returns.
                        self._assert_bootstrap_current()
                        self._assert_qualification_live(record, qualification, reobserve=True)
                        self._assert_budget_deadline(record, record["budget_usage"])

                    boundary: list[CapacityBoundaryFacts] = []

                    def after_capacity_facts(facts: CapacityBoundaryFacts) -> None:
                        boundary.append(facts)

                    def before_effect_yield() -> Callable[[], None]:
                        if len(boundary) != 1:
                            raise RunError("PLANNING_BOUNDARY_FACTS_REQUIRED")
                        # Complete all source/binding and shared routing work
                        # before Capacity starts its final yield preparation.
                        final_boundary = self._capture_final_boundary(
                            record, qualification, held_run
                        )
                        self._assert_final_boundary_temporal(final_boundary)
                        fence = self._revalidate_boundary_route(
                            boundary[0], record, binding, as_of=self.capacity._now()
                        )
                        self._assert_quota_fence_current(fence)

                        def final_validator() -> None:
                            self._assert_quota_fence_current(fence)
                            self._assert_final_boundary_temporal(final_boundary)

                        return final_validator

                    # Project qualification remains held until Capacity has
                    # revalidated the reservation and the caller's effect
                    # exits. No blocking authority read occurs after Capacity
                    # has completed its final fresh revalidation.
                    with self.capacity.pre_effect_guard(
                        record["capacity_receipt"]["admission_id"],
                        expected_request=record["capacity_request"],
                        before_effect=before_effect,
                        after_capacity_facts=after_capacity_facts,
                        before_effect_yield=before_effect_yield,
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
