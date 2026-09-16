"""Durable routing assessments built from approved Run and controller-owned facts.

Assessment records a planned identity and a decision; it does not reserve quota
or enable execution. A later consumer must recheck authority and acquire admission.
"""

import json
import uuid
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from karajan.capacity import (
    CapacityBoundaryFacts,
    CapacityError,
    CapacityStore,
    derive_capacity_boundary_facts,
)
from karajan.projects.qualification import ProfileQualificationStore, QualificationError
from karajan.routing import evaluate_reserved_profile, evaluate_route, select_rule
from karajan.routing.compiler import RoutingError, digest, parse, reference
from karajan.routing.models import AccountState, PoolState, TaskClassification
from karajan.routing.quotas import QuotaTemporalFence, capture_quota_temporal_fence
from karajan.runs import RunError, RunPlanner
from karajan.runs.planning import encoded, identifier
from karajan.storage import require_schema

from .go_reviewer_scope import resolve_go_reviewer_execution
from .go_scope import resolve_go_execution


@dataclass
class ReviewerTemporalFence:
    """Retained Reviewer source windows for an O(1) final boundary check."""

    floor: float
    qualification_observed_at: float
    qualification_valid_until: float
    estimate_created_at: float
    estimate_valid_until: float

    def assert_current(self, *, as_of: float) -> None:
        if type(as_of) not in (int, float) or as_of < self.floor:
            raise RunError("REVIEWER_BOUNDARY_CLOCK_INVALID")
        if not self.qualification_observed_at <= as_of < self.qualification_valid_until:
            raise RunError("REVIEWER_QUALIFICATION_EXPIRED")
        if not self.estimate_created_at <= as_of < self.estimate_valid_until:
            raise RunError("REVIEWER_ESTIMATE_EXPIRED")
        self.floor = max(self.floor, as_of)


class _ReviewerRevalidation(dict[str, Any]):
    """Ephemeral current Reviewer receipt with a held-Project source recheck."""

    def __init__(self, value: dict[str, Any], source_recheck: Callable[[], None] | None) -> None:
        super().__init__(value)
        self._source_recheck = source_recheck

    def recheck_source(self) -> None:
        if self._source_recheck is None:
            raise RunError("REVIEWER_QUALIFICATION_SOURCE_CHANGED")
        self._source_recheck()


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
        if planner.existing_only and not (
            planner.projects.existing_only
            and qualifications.projects.existing_only
            and capacity.existing_only
        ):
            raise RunError("EXISTING_STORE_PARENT_MODE_REQUIRED")
        self.planner = planner
        self.qualifications = qualifications
        self.capacity = capacity
        self.estimates = estimates or AttemptEstimateStore(planner)
        if planner.existing_only and not (
            self.estimates.planner.existing_only and self.estimates.projects.existing_only
        ):
            raise RunError("EXISTING_STORE_PARENT_MODE_REQUIRED")
        if (
            self.estimates.planner.database.resolve() != planner.database.resolve()
            or self.estimates.projects.database.resolve() != planner.projects.database.resolve()
        ):
            raise RunError("ROUTING_ESTIMATE_SOURCE_MISMATCH")
        if planner.existing_only:
            require_schema(
                planner.database,
                {
                    "approved_routing_assessments": [
                        "id",
                        "run_id",
                        "principal",
                        "command_key",
                        "payload",
                        "result",
                    ]
                },
            )
            return
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

    def assess_reviewer(
        self,
        run_id: str,
        task_id: str,
        *,
        principal: str,
        command_key: str,
        worker_operation: dict[str, Any],
        candidates: Any,
        reviewer_validator: Any,
    ) -> dict[str, Any]:
        """Assess a Reviewer from its recorded Worker lineage.

        This is intentionally an internal controller port: public callers only
        reach it through ``ApprovedTaskAdmission.enqueue`` with Run/task IDs.
        The Worker operation and Candidate store are controller-owned facts,
        never part of an RPC payload.
        """
        for value in (run_id, task_id, principal, command_key):
            identifier(value)
        payload = encoded([run_id, task_id])
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
                "scope": "approved_reviewer_assessment",
                "state": "blocked",
                "activation_allowed": False,
                "dispatch_enabled": False,
                "reason_codes": [],
                "route": None,
                "sources": {},
                "admission_expectations": [],
            }
            with ExitStack() as holds:
                self._build(
                    receipt,
                    run,
                    task_id,
                    principal,
                    holds,
                    worker_operation=worker_operation,
                    candidates=candidates,
                    reviewer_validator=reviewer_validator,
                )
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
    def reviewer_admission_guard(
        self,
        run_id: str,
        task_id: str,
        *,
        principal: str,
        attempt_id: str,
        context_id: str,
        worker_operation: dict[str, Any],
        candidates: Any,
        reviewer_validator: Any,
        _held_run: dict[str, Any] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Fresh Reviewer guard using the original Worker operation only."""
        for value in (run_id, task_id, principal, attempt_id, context_id):
            identifier(value)
        if _held_run is None:
            with self.planner.activation_guard(run_id) as run:
                self.planner._owner(run, principal)
                with self.reviewer_admission_guard(
                    run_id,
                    task_id,
                    principal=principal,
                    attempt_id=attempt_id,
                    context_id=context_id,
                    worker_operation=worker_operation,
                    candidates=candidates,
                    reviewer_validator=reviewer_validator,
                    _held_run=run,
                ) as current:
                    yield current
            return
        with ExitStack() as holds:
            run = _held_run
            self.planner._owner(run, principal)
            receipt: dict[str, Any] = {
                "schema_version": "karajan.approved-routing-assessment.v1",
                "id": str(uuid.uuid4()),
                "run_id": run_id,
                "task_id": task_id,
                "planned_attempt_id": attempt_id,
                "planned_context_id": context_id,
                "scope": "reviewer_admission_revalidation",
                "state": "blocked",
                "activation_allowed": False,
                "dispatch_enabled": False,
                "reason_codes": [],
                "route": None,
                "sources": {},
                "admission_expectations": [],
            }
            source_recheck = self._build(
                receipt,
                run,
                task_id,
                principal,
                holds,
                worker_operation=worker_operation,
                candidates=candidates,
                reviewer_validator=reviewer_validator,
            )
            receipt["digest"] = digest(receipt)
            yield _ReviewerRevalidation(receipt, source_recheck)

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
                    before: dict[str, Any] = next(
                        (row for row in old_source[collection] if row["profile"] == selected),
                        {},
                    )
                    current: dict[str, Any] = next(
                        (row for row in current_source[collection] if row["profile"] == selected),
                        {},
                    )
                    changed |= not before or current != before
                if changed:
                    receipt["state"] = "blocked"
                    receipt["reason_codes"] = ["RESERVED_EXECUTION_INPUT_CHANGED"]
                    receipt["route"]["selected_profile"] = None
                    receipt["route"]["reason_codes"] = ["RESERVED_EXECUTION_INPUT_CHANGED"]
            receipt["digest"] = digest(receipt)
            yield receipt

    @contextmanager
    def _reviewer_reserved_execution_guard(
        self,
        run_id: str,
        reviewer_operation: dict[str, Any],
        worker_operation: dict[str, Any],
        *,
        principal: str,
        candidates: Any,
        reviewer_validator: Any,
        _held_run: dict[str, Any] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Recheck one persisted Reviewer operation without admitting new demand.

        ``reviewer_operation`` is deliberately a controller record, rather than
        an assessment ID supplied by an execution caller.  Its original request
        stays outside this guard: the caller must enter Capacity's matching
        ``pre_effect_guard`` after this yields.  The held order is therefore
        operation -> Run -> Project -> Capacity.
        """
        for value in (run_id, reviewer_operation["id"], worker_operation["id"], principal):
            identifier(value)
        original = reviewer_operation.get("assessment")
        if (
            not isinstance(original, dict)
            or original.get("state") != "selected"
            or not isinstance(original.get("route"), dict)
            or not original["route"].get("selected_profile")
        ):
            raise RunError("RESERVED_REVIEWER_ROUTE_REQUIRED")
        if (
            reviewer_operation.get("run_id") != run_id
            or reviewer_operation.get("depends_on_operation_id") != worker_operation["id"]
            or original.get("reviewer_lineage", {}).get("worker_operation_id")
            != worker_operation["id"]
        ):
            raise RunError("REVIEW_WORKER_LINEAGE_REQUIRED")
        selected = original["route"]["selected_profile"]
        if _held_run is None:
            with self.planner.activation_guard(run_id) as run:
                self.planner._owner(run, principal)
                with self._reviewer_reserved_execution_guard(
                    run_id,
                    reviewer_operation,
                    worker_operation,
                    principal=principal,
                    candidates=candidates,
                    reviewer_validator=reviewer_validator,
                    _held_run=run,
                ) as current:
                    yield current
            return
        with ExitStack() as holds:
            run = _held_run
            self.planner._owner(run, principal)
            receipt: dict[str, Any] = {
                "schema_version": "karajan.approved-routing-assessment.v1",
                "id": str(uuid.uuid4()),
                "run_id": run_id,
                "task_id": reviewer_operation["task_id"],
                "planned_attempt_id": reviewer_operation["planned_attempt_id"],
                "planned_context_id": reviewer_operation["planned_context_id"],
                "scope": "reserved_reviewer_execution_revalidation",
                "reviewer_operation_id": reviewer_operation["id"],
                "original_assessment_digest": original.get("digest"),
                "state": "blocked",
                "activation_allowed": False,
                "dispatch_enabled": False,
                "reason_codes": [],
                "route": None,
                "sources": {},
                "admission_expectations": [],
            }
            source_recheck = self._build(
                receipt,
                run,
                reviewer_operation["task_id"],
                principal,
                holds,
                reserved_profile=selected,
                worker_operation=worker_operation,
                candidates=candidates,
                reviewer_validator=reviewer_validator,
            )
            if receipt["state"] == "selected":
                current_source, old_source = receipt["sources"], original["sources"]
                changed = any(
                    current_source[key] != old_source[key]
                    for key in ("approval", "execution_policy_digest", "routing_digest")
                )
                changed |= receipt.get("reviewer_lineage") != original.get("reviewer_lineage")
                changed |= (
                    receipt["route"]["snapshots"]["task"]
                    != original["route"]["snapshots"]["task"]
                )
                for collection in ("profiles", "estimates"):
                    before: dict[str, Any] | None = next(
                        (row for row in old_source[collection] if row["profile"] == selected),
                        None,
                    )
                    current_profile: dict[str, Any] | None = next(
                        (row for row in current_source[collection] if row["profile"] == selected),
                        None,
                    )
                    changed |= before is None or current_profile != before
                if changed:
                    receipt["state"] = "blocked"
                    receipt["reason_codes"] = ["RESERVED_REVIEWER_INPUT_CHANGED"]
                    receipt["route"]["selected_profile"] = None
                    receipt["route"]["reason_codes"] = ["RESERVED_REVIEWER_INPUT_CHANGED"]
            receipt["digest"] = digest(receipt)
            yield _ReviewerRevalidation(receipt, source_recheck)

    def reviewer_boundary_guard(
        self,
        assessment: dict[str, Any],
        *,
        worker_operation: dict[str, Any],
        candidates: Any,
        clock: Callable[[], float],
        source_recheck: Callable[[], None],
    ) -> ReviewerTemporalFence:
        """Recheck elapsed Reviewer facts while the caller still holds Project and Capacity.

        The caller owns operation -> Run -> Project and invokes this only after
        Capacity has acquired its boundary transaction.  Project writes cannot
        replace the locked source; elapsed qualification/estimate facts and
        Candidate artifacts still need a fresh read at this moment.
        """
        # Reopen the original Reader's source observations and then the final
        # Check artifacts while Project remains held. Both can block, so finish
        # them before the temporal sample used for every source below.
        source_recheck()
        _current_reviewer_check_artifacts(worker_operation, candidates)
        return self.reviewer_elapsed_boundary_guard(assessment, clock=clock)

    def reviewer_elapsed_boundary_guard(
        self, assessment: dict[str, Any], *, clock: Callable[[], float]
    ) -> ReviewerTemporalFence:
        """Capture stored Reviewer windows after all source reads complete."""
        if assessment.get("state") != "selected":
            raise RunError("RESERVED_REVIEWER_ROUTE_NOT_CURRENT")
        route = assessment.get("route")
        sources = assessment.get("sources")
        if not isinstance(route, dict) or not isinstance(sources, dict):
            raise RunError("RESERVED_REVIEWER_ROUTE_NOT_CURRENT")
        selected = route.get("selected_profile")
        if not isinstance(selected, dict):
            raise RunError("RESERVED_REVIEWER_ROUTE_NOT_CURRENT")
        now = clock()
        if type(now) not in (int, float):
            raise RunError("REVIEWER_BOUNDARY_CLOCK_INVALID")
        profile = next(
            (
                row.get("qualification")
                for row in sources.get("profiles", [])
                if row.get("profile") == selected
            ),
            None,
        )
        facts = profile.get("facts") if isinstance(profile, dict) else None
        observed_at = facts.get("observed_at") if isinstance(facts, dict) else None
        valid_until = facts.get("valid_until") if isinstance(facts, dict) else None
        if (
            not isinstance(observed_at, (int, float))
            or isinstance(observed_at, bool)
            or not isinstance(valid_until, (int, float))
            or isinstance(valid_until, bool)
            or not observed_at <= now < valid_until
        ):
            raise RunError("REVIEWER_QUALIFICATION_EXPIRED")
        estimate = next(
            (
                row.get("source_binding")
                for row in sources.get("estimates", [])
                if row.get("profile") == selected
            ),
            None,
        )
        created_at = estimate.get("created_at") if isinstance(estimate, dict) else None
        estimate_valid_until = estimate.get("valid_until") if isinstance(estimate, dict) else None
        if (
            not isinstance(created_at, (int, float))
            or isinstance(created_at, bool)
            or not isinstance(estimate_valid_until, (int, float))
            or isinstance(estimate_valid_until, bool)
            or not created_at <= now < estimate_valid_until
        ):
            raise RunError("REVIEWER_ESTIMATE_EXPIRED")
        return ReviewerTemporalFence(
            floor=float(now),
            qualification_observed_at=float(observed_at),
            qualification_valid_until=float(valid_until),
            estimate_created_at=float(created_at),
            estimate_valid_until=float(estimate_valid_until),
        )

    def reviewer_capacity_boundary_guard(
        self,
        assessment: dict[str, Any],
        *,
        request: dict[str, Any],
        boundary: CapacityBoundaryFacts,
        as_of: float | None = None,
    ) -> QuotaTemporalFence:
        """Reapply the frozen Reviewer route's quota policy to fresh Capacity facts.

        Capacity owns the complete source fragment and, for an effect boundary,
        the only admissible existing claim.  The pure routing evaluator then
        retains its single conservative-quota algorithm instead of re-encoding
        unknown-estimate or observation policy in admission.
        """
        route = assessment.get("route")
        if not isinstance(route, dict):
            raise RunError("RESERVED_REVIEWER_ROUTE_NOT_CURRENT")
        task = route.get("snapshots", {}).get("task")
        policy = route.get("snapshots", {}).get("policy")
        original_capacity = route.get("snapshots", {}).get("capacity")
        selected = route.get("selected_profile")
        if any(
            not isinstance(value, dict) for value in (task, policy, original_capacity, selected)
        ):
            raise RunError("RESERVED_REVIEWER_ROUTE_NOT_CURRENT")
        assert isinstance(task, dict)
        assert isinstance(policy, dict)
        assert isinstance(original_capacity, dict)
        assert isinstance(selected, dict)
        try:
            resources = deepcopy(policy["resources"])
            derived = derive_capacity_boundary_facts(boundary, expected_request=request)
            if as_of is not None:
                captured_at = derived.get("captured_at")
                if (
                    type(as_of) not in (int, float)
                    or not isinstance(captured_at, (int, float))
                    or isinstance(captured_at, bool)
                ):
                    raise ValueError
                final_as_of = float(as_of)
                captured_time = float(captured_at)
                if final_as_of < captured_time:
                    raise ValueError
                derived["captured_at"] = final_as_of
                derived["derived_capacity_boundary"] = {
                    **derived.get("derived_capacity_boundary", {}),
                    "as_of": final_as_of,
                }
            capacity, _ = _capacity_snapshot(derived, resources)
            capacity.update(
                id="capacity-boundary:" + digest(
                    [boundary.facts.sha256, boundary.owned_admission_id]
                ),
                estimates=deepcopy(original_capacity["estimates"]),
                budget_remaining=deepcopy(original_capacity["budget_remaining"]),
                fx=deepcopy(original_capacity["fx"]),
            )
            current_policy = deepcopy(policy)
            current_policy["resources"] = resources
            report = evaluate_reserved_profile(
                task,
                current_policy,
                capacity,
                selected,
                revalidate_quota=True,
            )
        except (CapacityError, KeyError, TypeError, ValueError):
            raise RunError("REVIEWER_CAPACITY_BOUNDARY_INVALID") from None
        if report["selected_profile"] != selected:
            raise RunError("REVIEWER_CAPACITY_REVALIDATION_FAILED")
        try:
            return capture_quota_temporal_fence(report)
        except (RoutingError, KeyError, TypeError, ValueError):
            raise RunError("REVIEWER_CAPACITY_BOUNDARY_INVALID") from None

    def _build(
        self,
        receipt: dict[str, Any],
        run: dict[str, Any],
        task_id: str,
        principal: str,
        holds: ExitStack,
        *,
        reserved_profile: dict[str, Any] | None = None,
        worker_operation: dict[str, Any] | None = None,
        candidates: Any | None = None,
        reviewer_validator: Any | None = None,
    ) -> Callable[[], None] | None:
        if run["schema_version"] != "karajan.run-planning.v2":
            receipt["reason_codes"] = ["APPROVED_ROUTING_V2_REQUIRED"]
            return None
        plan = next(
            (p for p in run["plans"] if p["plan_revision"] == run["active_plan_revision"]), None
        )
        if plan is None or run["state"] != "executing":
            receipt["reason_codes"] = ["APPROVED_PLAN_REQUIRED"]
            return None
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
            return None
        reviewer = task["role"] == "reviewer"
        if reviewer:
            if worker_operation is None or candidates is None or reviewer_validator is None:
                receipt["reason_codes"] = ["EXECUTION_LINEAGE_REQUIRED"]
                return None
            reviewer_operation = worker_operation
            try:
                transition = reviewer_operation.get("validation", {}).get("review_binding")
                if not isinstance(transition, dict):
                    raise RunError("REVIEWER_BINDING_REQUIRED")
                lineage = _reviewer_lineage(run, task, reviewer_operation, candidates)
            except RunError as error:
                receipt["reason_codes"] = [error.code]
                return None
        elif task["role"] != "worker" or task["depends_on"]:
            receipt["reason_codes"] = [
                "EXECUTION_LINEAGE_REQUIRED" if task["depends_on"] else "ROLE_NOT_IMPLEMENTED"
            ]
            return None
        fixed = run["configuration_snapshot"]["configuration"]
        execution = run["execution_policy_snapshot"]
        classification = {
            key: (
                lineage["worker_task"][key]
                if reviewer and key in {"complexity", "risk", "paths"}
                else task[key]
            )
            for key in TaskClassification.model_fields
            if key != "authors"
        }
        classification["authors"] = lineage["authors"] if reviewer else []
        selection = select_rule(classification, fixed["rulebook"], execution["risk_policy"])
        auth = plan["plan"]["authorization"]
        requirements = plan["routing_binding"]["task_requirements"].get(task_id)
        expected_requirements = {
            key: task[key]
            for key in (
                "revision",
                "role",
                "purpose",
                "readiness",
                "complexity",
                "risk",
                "paths",
                "domains",
                "required_capabilities",
                "tools",
                "context_tokens",
                "duration_seconds",
            )
        }
        for key in ("profile_ref", "source_ref", "checks"):
            if task.get(key) is not None:
                expected_requirements[key] = task[key]
        if requirements != expected_requirements:
            raise RunError("APPROVED_REQUIREMENTS_MISMATCH")
        explicit_profile, explicit_source = task.get("profile_ref"), task.get("source_ref")
        if (explicit_profile is None) != (explicit_source is None):
            raise RunError("TASK_SOURCE_BINDING_MISMATCH")
        if explicit_profile is not None:
            registration = next(
                (
                    row
                    for row in fixed["resources"]["profiles"]
                    if {"id": row["id"], "revision": row["revision"]} == explicit_profile
                ),
                None,
            )
            profile = registration.get("profile") if isinstance(registration, dict) else None
            if (
                not isinstance(profile, dict)
                or profile["binding"]["channel_id"] != explicit_source
                or explicit_profile not in auth["profile_refs"]
                or explicit_source not in auth["channel_ids"]
                or reserved_profile is not None
                and reserved_profile != explicit_profile
            ):
                raise RunError("TASK_SOURCE_BINDING_MISMATCH")
            # The evaluator therefore cannot choose an eligible alternative:
            # the owner selection is a route input, not proposal display data.
            reserved_profile = explicit_profile
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
        if reviewer:
            receipt["reviewer_lineage"] = {
                "worker_operation_id": reviewer_operation["id"],
                "worker_task_id": lineage["worker_task"]["id"],
                "source_candidate": lineage["source_candidate"],
                "subject_digest": lineage["subject_digest"],
                "checks_digest": lineage["checks_digest"],
                "binding_digest": lineage["binding_digest"],
            }
        view = holds.enter_context(
            self.qualifications.routing_facts_guard(
                run["project_id"],
                fixed["resources"]["profiles"],
                principal=principal,
                scope="runtime_tools",
            )
        )
        if reviewer:
            try:
                assert reviewer_validator is not None
                reviewer_validator.current_locked(
                    view["project_db"],
                    run,
                    reviewer_operation,
                    transition,
                    principal=principal,
                )
            except RunError as error:
                receipt["reason_codes"] = [error.code]
                return None
        resources = deepcopy(fixed["resources"])
        current = view["catalog"]
        profile_facts = []
        for registration, qualified in zip(resources["profiles"], view["profiles"], strict=True):
            # Raw configured 'passed' evidence is a declaration, not a
            # controller-produced qualification observation.
            observation = qualified["qualification"]
            if reviewer:
                execution_context, scope_issues = resolve_go_reviewer_execution(
                    registration,
                    observation,
                    task_snapshot,
                    execution,
                    selection["effective_class"],
                )
                ref = {"id": registration["id"], "revision": registration["revision"]}
                if reference(ref) not in lineage["reviewer_profiles"]:
                    registration["enabled"] = False
                    qualified["reason_codes"].append("APPROVED_REVIEWER_PROFILE_REQUIRED")
                expected_source = lineage["reviewer_sources"].get(reference(ref))
                try:
                    authentication = observation["observation"]["binding"]["execution_start"][
                        "authentication_source"
                    ]
                    current_source = {
                        "reviewer": {
                            "profile_id": ref["id"],
                            "profile_revision": ref["revision"],
                            "model_family": registration["model_family"],
                            "qualification_ref": observation["facts"]["evidence_ref"],
                        },
                        "qualification_source_digest": digest(
                            observation["observation"]["binding"]
                        ),
                        "authentication_source_digest": digest(authentication),
                    }
                except (KeyError, TypeError):
                    current_source = None
                if expected_source != current_source:
                    registration["enabled"] = False
                    qualified["reason_codes"].append("REVIEWER_QUALIFICATION_SOURCE_CHANGED")
            else:
                execution_context, scope_issues = resolve_go_execution(
                    registration,
                    observation,
                    task_snapshot,
                    execution,
                    selection["effective_class"],
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
        if not reviewer:
            return None
        return lambda: self._recheck_reviewer_source_locked(view["project_db"], run, receipt)

    def _recheck_reviewer_source_locked(
        self, project_db: Any, run: dict[str, Any], receipt: dict[str, Any]
    ) -> None:
        """Reobserve the selected Reviewer's sealed runtime and credential material.

        ``routing_facts_guard`` owns ``project_db`` through Capacity's callback.
        This repeats the same private ``_facts`` reader that built the route, so
        current suite/controller/runtime/credential material cannot be replaced
        by a retained SQLite generation or a fixture declaration.
        """
        route = receipt.get("route")
        sources = receipt.get("sources")
        if not isinstance(route, dict) or not isinstance(sources, dict):
            raise RunError("REVIEWER_QUALIFICATION_SOURCE_CHANGED")
        selected = route.get("selected_profile")
        configuration = run.get("configuration_snapshot", {}).get("configuration")
        resources = configuration.get("resources") if isinstance(configuration, dict) else None
        profiles = resources.get("profiles") if isinstance(resources, dict) else None
        if not isinstance(selected, dict) or not isinstance(profiles, list):
            raise RunError("REVIEWER_QUALIFICATION_SOURCE_CHANGED")
        frozen = next(
            (
                row
                for row in profiles
                if isinstance(row, dict)
                and {"id": row.get("id"), "revision": row.get("revision")}
                == selected
            ),
            None,
        )
        expected = next(
            (
                row.get("qualification")
                for row in sources.get("profiles", [])
                if isinstance(row, dict) and row.get("profile") == selected
            ),
            None,
        )
        if not isinstance(frozen, dict) or not isinstance(expected, dict):
            raise RunError("REVIEWER_QUALIFICATION_SOURCE_CHANGED")
        try:
            observed = self.qualifications._facts(
                project_db, run["project_id"], frozen, "runtime_tools", None
            )
        except QualificationError:
            raise RunError("REVIEWER_QUALIFICATION_SOURCE_CHANGED") from None
        if observed != expected:
            raise RunError("REVIEWER_QUALIFICATION_SOURCE_CHANGED")


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


def _current_reviewer_check_artifacts(worker_operation: dict[str, Any], candidates: Any) -> None:
    """Require every persisted final Check log through CandidateStore's current gate."""
    from karajan.candidates import CandidateError

    checks = worker_operation.get("validation", {}).get("checks", {})
    rows = checks.get("runs") if isinstance(checks, dict) else None
    if not isinstance(rows, list) or not rows:
        raise RunError("REVIEW_SUBJECT_CHECKS_REQUIRED")
    try:
        for row in rows:
            evidence = row["evidence"]
            candidate = candidates.get(row["evidence_request"]["candidate_id"])
            current = {
                key: candidate[key]
                for key in ("repository_identity", "base_sha", "input_sha256", "policy_sha256")
            }
            gate = candidates.gate(candidate["id"], current=current)
            stored = next(
                (
                    item
                    for item in gate["evidence"]
                    if item.get("kind") == "check" and item.get("id") == evidence.get("id")
                ),
                None,
            )
            if (
                evidence.get("status") != "passed"
                or stored is None
                or stored.get("effective_status") != "passed"
                or "ARTIFACT_UNAVAILABLE" in gate["reasons"]
                or any(
                    reason.startswith("CHECK_EVIDENCE_MISSING:")
                    or reason.startswith("CHECK_NOT_PASSED:")
                    for reason in gate["reasons"]
                )
            ):
                raise RunError("REVIEW_SUBJECT_CHECKS_REQUIRED")
    except (CandidateError, KeyError, TypeError):
        raise RunError("REVIEW_SUBJECT_CHECKS_REQUIRED") from None



def _reviewer_lineage(
    run: dict[str, Any], reviewer: dict[str, Any], worker_operation: dict[str, Any], candidates: Any
) -> dict[str, Any]:
    """Return only controller-captured Reviewer inputs, or reject before Capacity.

    The Reviewer task cannot nominate a Worker, Candidate, author, check, or
    membership identity.  A bound current validation subject supplies those
    facts, while the frozen plan supplies the Worker classification so a
    Reviewer task cannot lower complexity, risk, or path scope.
    """
    from .candidate_subjects import current_subject

    if worker_operation.get("run_id") != run["id"]:
        raise RunError("REVIEW_WORKER_OPERATION_RUN_MISMATCH")
    dependencies = reviewer.get("depends_on")
    if not isinstance(dependencies, list) or len(dependencies) != 1:
        raise RunError("UNIQUE_APPROVED_REVIEWER_DEPENDENCY_REQUIRED")
    worker_task = next(
        (row for plan in run["plans"] if plan["plan_revision"] == run["active_plan_revision"]
        for row in plan["plan"]["tasks"]
        if row["id"] == dependencies[0]),
        None,
    )
    if (
        worker_task is None
        or worker_task["role"] != "worker"
        or worker_task["depends_on"]
        or worker_operation.get("task_id") != worker_task["id"]
        or worker_operation.get("cancel_requested")
    ):
        raise RunError("REVIEW_WORKER_LINEAGE_REQUIRED")
    validation = worker_operation.get("validation")
    execution = worker_operation.get("execution", {})
    if validation is None or execution.get("collection") is None:
        raise RunError("REVIEW_SUBJECT_REQUIRED")
    checks = validation.get("checks")
    if (
        not isinstance(checks, dict)
        or checks.get("phase") != "checks_passed"
        or not checks.get("runs")
        or any(
            row.get("phase") != "recorded"
            or row.get("evidence", {}).get("status") != "passed"
            for row in checks["runs"]
        )
    ):
        raise RunError("REVIEW_SUBJECT_CHECKS_REQUIRED")
    installed = validation.get("review_binding")
    if not isinstance(installed, dict) or installed.get("phase") != "installed":
        raise RunError("REVIEWER_BINDING_REQUIRED")
    binding = installed.get("binding")
    if (
        not isinstance(binding, dict)
        or binding.get("run_id") != run["id"]
        or binding.get("operation_id") != worker_operation["id"]
        or binding.get("reviewer_task_id") != reviewer["id"]
        or binding.get("reviewer_task_digest") != digest(reviewer)
    ):
        raise RunError("REVIEW_SUBJECT_BINDING_MISMATCH")
    subject = current_subject(worker_operation, candidates)
    authors = subject["capture_candidate"]["request"].get("authors")
    if not isinstance(authors, list) or not authors:
        raise RunError("REVIEW_AUTHOR_LINEAGE_REQUIRED")
    compiled_authors = []
    for author in authors:
        try:
            compiled_authors.append(
                {
                    "profile": {
                        "id": author["profile_id"],
                        "revision": author["profile_revision"],
                    },
                    "model_family": author["model_family"],
                    "attempt_id": author["attempt_id"],
                    "context_id": author["context_id"],
                    "complexity": worker_task["complexity"],
                    "risk": worker_task["risk"],
                    "paths": worker_task["paths"],
                }
            )
        except (KeyError, TypeError):
            raise RunError("REVIEW_AUTHOR_LINEAGE_REQUIRED") from None
    sources = binding.get("reviewer_sources")
    if not isinstance(sources, list) or not sources:
        raise RunError("REVIEWER_BINDING_REQUIRED")
    try:
        profiles = {
            (row["reviewer"]["profile_id"], row["reviewer"]["profile_revision"])
            for row in sources
        }
    except (KeyError, TypeError):
        raise RunError("REVIEWER_BINDING_REQUIRED") from None
    return {
        "worker_task": worker_task,
        "authors": compiled_authors,
        "reviewer_profiles": profiles,
        "reviewer_sources": {
            (row["reviewer"]["profile_id"], row["reviewer"]["profile_revision"]): row
            for row in sources
        },
        "source_candidate": subject["subject"]["candidate"],
        "subject_digest": digest(subject["subject"]),
        "checks_digest": digest(checks),
        "binding_digest": digest(binding),
    }


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
