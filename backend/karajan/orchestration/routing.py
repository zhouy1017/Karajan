"""Durable routing assessments built from approved Run and controller-owned facts.

Assessment records a planned identity and a decision; it does not reserve quota
or enable execution. A later consumer must recheck authority and acquire admission.
"""

import json
import uuid
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from karajan.capacity import CapacityStore
from karajan.projects.qualification import ProfileQualificationStore
from karajan.routing import evaluate_route, select_rule
from karajan.routing.compiler import digest, parse, reference
from karajan.routing.models import AccountState, PoolState, TaskClassification
from karajan.runs import RunError, RunPlanner
from karajan.runs.planning import encoded, identifier

from .go_scope import resolve_go_execution

if TYPE_CHECKING:
    from karajan.projects.demand import AttemptEstimateStore


class ApprovedRunRouting:
    """Only Run/task identities enter from a client; no raw snapshots are accepted."""

    def __init__(
        self,
        planner: RunPlanner,
        qualifications: ProfileQualificationStore,
        capacity: CapacityStore,
        *,
        estimates: "AttemptEstimateStore | None" = None,
    ) -> None:
        from karajan.projects.demand import AttemptEstimateStore

        if qualifications.projects.database.resolve() != planner.projects.database.resolve():
            raise RunError("ROUTING_PROJECT_SOURCE_MISMATCH")
        self.planner = planner
        self.qualifications = qualifications
        self.capacity = capacity
        self.estimates = estimates or AttemptEstimateStore(planner)
        if (
            self.estimates.planner.database.resolve() != planner.database.resolve()
            or self.estimates.projects.database.resolve() != planner.projects.database.resolve()
        ):
            raise RunError("ROUTING_ESTIMATE_SOURCE_MISMATCH")
        with planner._transaction() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS approved_routing_assessments ("
                "id TEXT PRIMARY KEY, run_id TEXT NOT NULL, principal TEXT NOT NULL, "
                "command_key TEXT NOT NULL, payload TEXT NOT NULL, result TEXT NOT NULL, "
                "UNIQUE(principal,command_key))"
            )

    def assess(
        self, run_id: str, task_id: str, *, principal: str, command_key: str
    ) -> dict[str, Any]:
        for value in (run_id, task_id, principal, command_key):
            identifier(value)
        payload = encoded([run_id, task_id])
        # Same Run transaction owns the frozen input, idempotency and receipt.
        # Project/qualification/estimate writes are fenced next; capacity is a
        # separate read snapshot, not an atomic admission across these stores.
        with self.planner._transaction() as db:
            run = self.planner._get(db, run_id)
            self.planner._owner(run, principal)
            prior = db.execute(
                "SELECT payload,result FROM approved_routing_assessments "
                "WHERE principal=? AND command_key=?",
                (principal, command_key),
            ).fetchone()
            if prior:
                if prior["payload"] != payload:
                    raise RunError("IDEMPOTENCY_CONFLICT")
                return dict(json.loads(prior["result"]))
            receipt: dict[str, Any] = {
                "schema_version": "karajan.approved-routing-assessment.v1",
                "id": str(uuid.uuid4()),
                "run_id": run_id,
                "task_id": task_id,
                "planned_attempt_id": str(uuid.uuid4()),
                "planned_context_id": str(uuid.uuid4()),
                "scope": "approved_run_assessment",
                "state": "blocked",
                "activation_allowed": False,
                "dispatch_enabled": False,
                "reason_codes": [],
                "route": None,
                "sources": {},
                "admission_expectations": [],
            }
            with ExitStack() as holds:
                self._build(receipt, run, task_id, principal, holds)
            receipt["digest"] = digest(receipt)
            db.execute(
                "INSERT INTO approved_routing_assessments VALUES (?,?,?,?,?,?)",
                (receipt["id"], run_id, principal, command_key, payload, encoded(receipt)),
            )
            return receipt

    def get(self, run_id: str, assessment_id: str, *, principal: str) -> dict[str, Any]:
        for value in (run_id, assessment_id, principal):
            identifier(value)
        with self.planner._transaction() as db:
            self.planner._owner(self.planner._get(db, run_id), principal)
            row = db.execute(
                "SELECT result FROM approved_routing_assessments WHERE id=? AND run_id=?",
                (assessment_id, run_id),
            ).fetchone()
            if row is None:
                raise RunError("ROUTING_ASSESSMENT_NOT_FOUND")
            return dict(json.loads(row["result"]))

    @contextmanager
    def admission_guard(
        self, run_id: str, task_id: str, *, principal: str, attempt_id: str, context_id: str
    ) -> Iterator[dict[str, Any]]:
        """Fresh controller facts fenced through a consumer's Capacity transaction.

        Order: coordinator (caller), Run, project, Capacity (consumer). The
        yielded decision is transient and never grants permission to execute.
        Consumers must already have durably recorded these planned identities.
        """
        for value in (run_id, task_id, principal, attempt_id, context_id):
            identifier(value)
        with self.planner.activation_guard(run_id) as run, ExitStack() as holds:
            self.planner._owner(run, principal)
            receipt: dict[str, Any] = {
                "schema_version": "karajan.approved-routing-assessment.v1",
                "id": str(uuid.uuid4()),
                "run_id": run_id,
                "task_id": task_id,
                "planned_attempt_id": attempt_id,
                "planned_context_id": context_id,
                "scope": "admission_revalidation",
                "state": "blocked",
                "activation_allowed": False,
                "dispatch_enabled": False,
                "reason_codes": [],
                "route": None,
                "sources": {},
                "admission_expectations": [],
            }
            self._build(receipt, run, task_id, principal, holds)
            receipt["digest"] = digest(receipt)
            yield receipt

    @contextmanager
    def reserved_execution_guard(
        self, run_id: str, assessment_id: str, *, principal: str
    ) -> Iterator[dict[str, Any]]:
        """Revalidate a stored selected route without treating its hold as new demand.

        This is an internal consumer port, not proof of a reservation or a start
        permission. The consumer must also hold its operation/Workspace state,
        activate the matching original Capacity request, and enter the fresh
        Capacity pre-effect guard at the real execution boundary.
        """
        original = self.get(run_id, assessment_id, principal=principal)
        if original["state"] != "selected" or not original["route"]["selected_profile"]:
            raise RunError("RESERVED_ROUTE_REQUIRED")
        selected = original["route"]["selected_profile"]
        # The immutable assessment is read before taking the long-lived guard.
        # No nested public Run read occurs while activation_guard holds its DB.
        with self.planner.activation_guard(run_id) as run, ExitStack() as holds:
            self.planner._owner(run, principal)
            receipt: dict[str, Any] = {
                "schema_version": "karajan.approved-routing-assessment.v1",
                "id": str(uuid.uuid4()),
                "run_id": run_id,
                "task_id": original["task_id"],
                "planned_attempt_id": original["planned_attempt_id"],
                "planned_context_id": original["planned_context_id"],
                "scope": "reserved_execution_revalidation",
                "original_assessment_digest": original["digest"],
                "state": "blocked",
                "activation_allowed": False,
                "dispatch_enabled": False,
                "reason_codes": [],
                "route": None,
                "sources": {},
                "admission_expectations": [],
            }
            self._build(
                receipt, run, original["task_id"], principal, holds, reserved_profile=selected
            )
            if receipt["state"] == "selected":
                # Preserve the complete approved task, identity, estimate and
                # qualified source. A replacement source requires a new Attempt.
                current_source, old_source = receipt["sources"], original["sources"]
                changed = any(
                    current_source[key] != old_source[key]
                    for key in ("approval", "execution_policy_digest", "routing_digest")
                )
                changed |= (
                    receipt["route"]["snapshots"]["task"] != original["route"]["snapshots"]["task"]
                )
                for collection in ("profiles", "estimates"):
                    before = next(
                        (row for row in old_source[collection] if row["profile"] == selected),
                        None,
                    )
                    current = next(
                        (row for row in current_source[collection] if row["profile"] == selected),
                        None,
                    )
                    changed |= before is None or current != before
                if changed:
                    receipt["state"] = "blocked"
                    receipt["reason_codes"] = ["RESERVED_EXECUTION_INPUT_CHANGED"]
                    receipt["route"]["selected_profile"] = None
                    receipt["route"]["reason_codes"] = ["RESERVED_EXECUTION_INPUT_CHANGED"]
            receipt["digest"] = digest(receipt)
            yield receipt

    def _build(
        self,
        receipt: dict[str, Any],
        run: dict[str, Any],
        task_id: str,
        principal: str,
        holds: ExitStack,
        *,
        reserved_profile: dict[str, Any] | None = None,
    ) -> None:
        if run["schema_version"] != "karajan.run-planning.v2":
            receipt["reason_codes"] = ["APPROVED_ROUTING_V2_REQUIRED"]
            return
        plan = next(
            (p for p in run["plans"] if p["plan_revision"] == run["active_plan_revision"]), None
        )
        if plan is None or run["state"] != "executing":
            receipt["reason_codes"] = ["APPROVED_PLAN_REQUIRED"]
            return
        approval = next(
            (a for a in run["approvals"] if a["plan_revision"] == plan["plan_revision"]), None
        )
        if approval is None or any(
            approval[k] != plan[k]
            for k in (
                "term",
                "plan_digest",
                "authorization_digest",
                "configuration_digest",
                "routing_digest",
            )
        ):
            raise RunError("APPROVAL_BINDING_MISMATCH")
        task = next((t for t in plan["plan"]["tasks"] if t["id"] == task_id), None)
        if task is None:
            receipt["reason_codes"] = ["TASK_SCOPE_NOT_APPROVED"]
            return
        # Until recorded execution lineage is consumed, no client can manufacture
        # authors, dependency evidence, failure history or a quality stage here.
        if task["role"] != "worker" or task["depends_on"]:
            receipt["reason_codes"] = [
                "EXECUTION_LINEAGE_REQUIRED" if task["depends_on"] else "ROLE_NOT_IMPLEMENTED"
            ]
            return
        fixed = run["configuration_snapshot"]["configuration"]
        execution = run["execution_policy_snapshot"]
        classification = {
            key: task[key] for key in TaskClassification.model_fields if key != "authors"
        }
        classification["authors"] = []
        selection = select_rule(classification, fixed["rulebook"], execution["risk_policy"])
        auth = plan["plan"]["authorization"]
        grant = plan["routing_binding"]["stage_grants"].get(
            selection["rule_id"], {"normal": {}, "quality": []}
        )
        groups = deepcopy(grant["normal"])
        for row in grant["quality"]:
            groups[row["group"]] = row["profiles"]
        allowed_stages = (["normal"] if grant["normal"] else []) + (
            ["quality"] if grant["quality"] else []
        )
        task_snapshot = {
            **classification,
            "schema_version": "karajan.routing.task.v1",
            "task_id": task_id,
            "task_revision": task["revision"],
            "root_task_id": digest([run["id"], task_id]),
            "plan_revision": plan["plan_revision"],
            "authorization_digest": plan["authorization_digest"],
            **{
                key: task[key]
                for key in ("required_capabilities", "tools", "context_tokens", "duration_seconds")
            },
            "reserved_output_tokens": execution["context_policy"]["reserved_output_tokens"],
            "stage": "normal",
            "quality_stage_index": 0,
            "failure_reason": None,
            "previous_profile": None,
            "quality_repair_rounds_used": 0,
            "planned_attempt_id": receipt["planned_attempt_id"],
            "planned_context_id": receipt["planned_context_id"],
            "authorization": {
                **{
                    key: auth[key]
                    for key in (
                        "profile_refs",
                        "channel_ids",
                        "tools",
                        "data_destinations",
                        "required_capabilities",
                        "min_isolation",
                        "budget_ref",
                        "currency_limits",
                        "max_attempt_duration_seconds",
                        "max_quality_repair_rounds",
                    )
                },
                "ceiling_profile_refs": run["authorization_ceiling"]["profile_refs"],
                "allowed_stages": allowed_stages,
                "approved_groups": groups,
                "approved_quality_stage_indices": [r["index"] for r in grant["quality"]],
            },
        }
        view = holds.enter_context(
            self.qualifications.routing_facts_guard(
                run["project_id"],
                fixed["resources"]["profiles"],
                principal=principal,
                scope="runtime_tools",
            )
        )
        resources = deepcopy(fixed["resources"])
        current = view["catalog"]
        profile_facts = []
        for registration, qualified in zip(resources["profiles"], view["profiles"], strict=True):
            # Raw configured 'passed' evidence is a declaration, not a
            # controller-produced qualification observation.
            observation = qualified["qualification"]
            execution_context, scope_issues = resolve_go_execution(
                registration, observation, task_snapshot, execution, selection["effective_class"]
            )
            if execution_context is not None:
                qualified["execution_context"] = execution_context
            if scope_issues:
                registration["enabled"] = False
                qualified["reason_codes"].extend(scope_issues)
            registration["capability_evidence"] = (
                observation["capability_evidence"] if observation else []
            )
            if observation:
                profile_facts.append(observation["facts"])
                profile = registration["profile"]
                if profile is None or observation["facts"]["data_destination"] != execution[
                    "channel_destinations"
                ].get(profile["binding"]["channel_id"]):
                    registration["enabled"] = False
                    qualified["reason_codes"].append("PROFILE_DESTINATION_BINDING_MISMATCH")
            if not _current_binding(fixed["resources"], current, registration):
                registration["enabled"] = False
                qualified["reason_codes"].append("CURRENT_PROFILE_RESTRICTED")
        captured = self.capacity.routing_facts()
        facts = captured.as_dict()
        snapshot, capacity_sources = _capacity_snapshot(facts, resources)
        estimate_sources = []
        for registration in resources["profiles"]:
            ref = {"id": registration["id"], "revision": registration["revision"]}
            windows = [
                {
                    "pool_id": p["id"],
                    **{k: p[k] for k in ("account_id", "kind", "unit", "window_kind", "window_id")},
                }
                for p in snapshot["pools"]
                if p["id"] in registration["quota_pool_refs"]
            ]
            resolution = (
                self.estimates.estimate_locked(
                    run,
                    task_id,
                    ref,
                    current_catalog=current,
                    pool_windows=windows,
                    as_of=facts["captured_at"],
                )
                if self.estimates
                else {
                    "estimate": None,
                    "source_binding": None,
                    "reason_codes": ["RESOURCE_ESTIMATE_MISSING"],
                }
            )
            estimate_sources.append({"profile": ref, **resolution})
            if resolution["estimate"] is not None:
                snapshot["estimates"].append(resolution["estimate"])
        snapshot["id"] = "capacity:" + digest([captured.sha256, estimate_sources])
        policy = {
            "schema_version": "karajan.routing.policy.v1",
            "rulebook": fixed["rulebook"],
            "resources": resources,
            "approved_profile_refs": [
                p for p in fixed["approved_profile_refs"] if p in current["approved_profile_refs"]
            ],
            "profile_facts": profile_facts,
            "risk_policy": execution["risk_policy"],
            "constraints": execution["constraints"],
        }
        if reserved_profile is None:
            route = evaluate_route(task_snapshot, policy, snapshot)
        else:
            from karajan.routing import evaluate_reserved_profile

            route = evaluate_reserved_profile(task_snapshot, policy, snapshot, reserved_profile)
        receipt["route"] = route
        receipt["reason_codes"] = route["reason_codes"]
        receipt["state"] = "selected" if route["selected_profile"] else "blocked"
        receipt["sources"] = {
            "approval": approval,
            "execution_policy_digest": execution["digest"],
            "routing_digest": plan["routing_digest"],
            "catalog_digest": current["digest"],
            "catalog_revision": current["revision"],
            "profiles": view["profiles"],
            "capacity_facts_sha256": captured.sha256,
            "capacity_sources": capacity_sources,
            "estimates": estimate_sources,
        }
        for estimate in snapshot["estimates"]:
            registration = next(
                r for r in resources["profiles"] if reference(r) == reference(estimate["profile"])
            )
            if registration["profile"] is None:
                continue
            account_id = registration["profile"]["binding"]["account_id"]
            account = next((a for a in snapshot["accounts"] if a["id"] == account_id), None)
            if account is not None and selection["rule"] is not None:
                receipt["admission_expectations"].append(
                    {
                        "profile": estimate["profile"],
                        "estimate_sha256": digest(estimate),
                        "expected_capacity": {
                            "policy_revision": account["policy_revision"],
                            "pool_windows": {
                                d["pool_id"]: d["window_id"] for d in estimate["demand"]
                            },
                            "lead_reserve_access": task["role"] == "commander"
                            and task["purpose"] == "lead"
                            and selection["rule"]["lead_reserve_access"] is not False,
                        },
                    }
                )


def _current_binding(frozen: dict[str, Any], catalog: dict[str, Any], row: dict[str, Any]) -> bool:
    current = catalog["resources"]
    ref = {"id": row["id"], "revision": row["revision"]}
    if current is None or ref not in catalog["approved_profile_refs"] or row["profile"] is None:
        return False
    original = next(p for p in frozen["profiles"] if reference(p) == reference(row))
    registered = next((p for p in current["profiles"] if reference(p) == reference(row)), None)
    if registered is None or registered != original:
        return False
    binding = row["profile"]["binding"]
    for kind, identities in (
        ("accounts", [binding["account_id"]]),
        ("channels", [binding["channel_id"]]),
        ("quota_pools", row["quota_pool_refs"]),
    ):
        for identity in identities:
            before = next((p for p in frozen[kind] if p["id"] == identity), None)
            after = next((p for p in current[kind] if p["id"] == identity), None)
            if before is None or before != after:
                return False
    return registered["enabled"] is True


def _capacity_snapshot(
    facts: dict[str, Any], resources: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    from karajan.routing import RoutingError

    snapshot: dict[str, Any] = {
        "schema_version": "karajan.routing.capacity.v1",
        "id": "pending",
        "revision": 1,
        "as_of": facts["captured_at"],
        "accounts": [],
        "pools": [],
        "estimates": [],
        "budget_remaining": {},
        "fx": None,
    }
    diagnostics = []
    accounts = {
        p["profile"]["binding"]["account_id"] for p in resources["profiles"] if p["profile"]
    }
    for source in facts["accounts"]:
        if source["id"] not in accounts:
            continue
        reasons = []
        if source["policy"] is not None:
            try:
                snapshot["accounts"].append(
                    parse(
                        AccountState,
                        {
                            "id": source["id"],
                            "policy_revision": source["policy_revision"],
                            "current_policy_revision": source["policy_revision"],
                            "policy": source["policy"],
                            "active_attempts": source["held_attempts"],
                            "cash_remaining": {},
                            "cooldown_until": source["cooldown_until"],
                            "exhaustion_observation_required": source[
                                "exhaustion_requires_new_observation"
                            ],
                        },
                        "CAPACITY_ACCOUNT_FACTS_INVALID",
                    )
                )
            except RoutingError as error:
                reasons.append(error.code)
        else:
            reasons.append("CAPACITY_POLICY_REQUIRED")
        for pool in source["pools"]:
            reasons.extend(f"{r}:{pool['id']}" for r in pool["diagnostics"])
            if pool["observation"] is None:
                continue
            observed = pool["observation"]["observation"]
            try:
                snapshot["pools"].append(
                    parse(
                        PoolState,
                        {
                            **{
                                k: pool[k]
                                for k in (
                                    "id",
                                    "account_id",
                                    "kind",
                                    "unit",
                                    "window_kind",
                                    "reported_remaining",
                                    "local_uncovered",
                                    "future_reserved",
                                )
                            },
                            **{
                                k: observed[k]
                                for k in (
                                    "window_id",
                                    "observed_at",
                                    "reset_at",
                                    "source",
                                    "coverage_ref",
                                )
                            },
                            "reported_limit": observed["limit"],
                            "confidence": "unknown",
                            "evidence_ref": observed["source_ref"],
                        },
                        "CAPACITY_POOL_FACTS_INVALID",
                    )
                )
            except RoutingError as error:
                reasons.append(f"{error.code}:{pool['id']}")
        for registration in resources["profiles"]:
            profile = registration["profile"]
            if profile is None or profile["binding"]["account_id"] != source["id"]:
                continue
            registered = next(
                (p for p in source["profiles"] if reference(p) == reference(registration)), None
            )
            if registered is None or set(registered["pool_ids"]) != set(
                registration["quota_pool_refs"]
            ):
                registration["enabled"] = False
                reasons.append(f"CAPACITY_PROFILE_BINDING_MISMATCH:{registration['id']}")
        diagnostics.append({"account_id": source["id"], "reason_codes": sorted(set(reasons))})
    return snapshot, diagnostics
