"""Prepare Reviewer membership from approved lineage, without model admission.

Qualification reads can verify current credential material through the existing
Store seal. Historical get/reconcile do not read current qualification or assets.
Preparation identities are not an actual Reviewer Attempt or native context.
"""

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from typing import Any

from karajan.candidates import CandidateError, CandidateStore
from karajan.projects.publication import effective_catalog
from karajan.projects.qualification import ProfileQualificationStore, QualificationError
from karajan.routing import evaluate_profile_membership, select_rule
from karajan.routing.compiler import digest, reference
from karajan.routing.models import TaskClassification
from karajan.runs import RunError
from karajan.runs.routing_authorization import resolve_binding

from .admission import ApprovedTaskAdmission
from .candidate_subjects import (
    assert_cycle_quiescent,
    candidate_identity,
    current_subject,
    mark_ready,
    parse_transition,
    replace_prepared_transition,
    stage_transition,
)
from .go_execution_intent import GoExecutionIntents, _connection
from .go_reviewer_scope import resolve_go_reviewer_execution
from .routing import _current_binding
from .workspace import _approved_task


class ApprovedReviewerBindings:
    """Only IDs enter this controller; no caller-supplied binding or Profile."""

    def __init__(
        self,
        admissions: ApprovedTaskAdmission,
        candidates: CandidateStore,
        qualifications: ProfileQualificationStore,
    ) -> None:
        if qualifications.projects is not admissions.routing.planner.projects:
            raise RunError("REVIEW_PROJECT_STORE_MISMATCH")
        self.admissions, self.candidates, self.qualifications = (
            admissions,
            candidates,
            qualifications,
        )

    @staticmethod
    def _view(operation: dict[str, Any]) -> dict[str, Any] | None:
        status = operation.get("validation", {}).get("review_binding_status")
        if status is None:
            return None
        result: dict[str, Any] = deepcopy(status)
        result["transition"] = parse_transition(operation)
        if (
            result["transition"] is not None
            and result["transition"]["phase"] == "installed"
            and result["state"] not in {"blocked", "reconciliation_required"}
        ):
            result["state"] = "installed"
        return result

    @staticmethod
    def _status(
        operation: dict[str, Any],
        state: str,
        reasons: list[str],
        assessment: dict[str, Any] | None = None,
    ) -> None:
        if "validation" not in operation:
            raise RunError("REVIEW_SUBJECT_REQUIRED")
        old = operation["validation"].get("review_binding_status", {})
        operation["validation"]["review_binding_status"] = {
            "schema_version": "karajan.approved-reviewer-binding.v1",
            "state": state,
            "reason_codes": sorted(set(reasons)),
            "assessment": deepcopy(assessment if assessment is not None else old.get("assessment")),
            "activation_allowed": False,
            "dispatch_enabled": False,
        }

    def get(
        self, run_id: str, worker_operation_id: str, *, principal: str
    ) -> dict[str, Any] | None:
        operation = GoExecutionIntents.read_operation(
            self.admissions, run_id, worker_operation_id, principal=principal
        )
        return self._view(operation)

    def reconcile(
        self, run_id: str, worker_operation_id: str, *, principal: str
    ) -> dict[str, Any] | None:
        GoExecutionIntents._check_owner(self.admissions, run_id, worker_operation_id, principal)
        with _connection(self.admissions.database, readonly=False) as db:
            operation = self.admissions._load(db, run_id, worker_operation_id)
            transition = parse_transition(operation)
            if transition is None or transition["phase"] not in {"rebind_claimed", "ready"}:
                return self._view(operation)
            try:
                candidate = self.candidates.lookup_review_rebind(
                    transition["binding"], command_key=transition["command_key"]
                )
                if candidate is None:
                    self._status(
                        operation,
                        "reconciliation_required",
                        [*transition["reason_codes"], "REVIEW_REBIND_RECEIPT_MISSING"],
                    )
                else:
                    mark_ready(operation, candidate)
                    self._status(operation, "ready", [])
            except CandidateError as error:
                self._status(operation, "reconciliation_required", [error.code])
            self.admissions._save(db, operation)
            return self._view(operation)

    @contextmanager
    def _current(
        self, operation: dict[str, Any], principal: str
    ) -> Iterator[tuple[sqlite3.Connection, dict[str, Any]]]:
        planner = self.admissions.routing.planner
        with _connection(planner.database, readonly=False) as run_db:
            run_db.execute("PRAGMA query_only=ON")
            run = planner._get(run_db, operation["run_id"])
            with _connection(planner.projects.database, readonly=False) as project_db:
                project_db.execute("PRAGMA query_only=ON")
                yield project_db, run

    def advance(self, run_id: str, worker_operation_id: str, *, principal: str) -> dict[str, Any]:
        GoExecutionIntents._check_owner(self.admissions, run_id, worker_operation_id, principal)
        fresh_claim = None
        with _connection(self.admissions.database, readonly=False) as db:
            operation = self.admissions._load(db, run_id, worker_operation_id)
            transition = parse_transition(operation)
            if transition is not None and transition["phase"] in {"rebind_claimed", "ready"}:
                recover = True
            else:
                recover = False
                try:
                    with self._current(operation, principal) as (project_db, run):
                        compiled = self._compile(project_db, run, operation, principal)
                        if compiled["binding"] is None:
                            self._status(
                                operation,
                                "blocked",
                                compiled["reason_codes"],
                                compiled["assessment"],
                            )
                        elif transition is not None and transition["phase"] == "prepared":
                            assert_cycle_quiescent(operation)
                            if transition["semantic_digest"] != compiled["semantic_digest"]:
                                token = str(uuid.uuid4())
                                replace_prepared_transition(
                                    operation,
                                    compiled["binding"],
                                    transition_id="review-binding:" + token,
                                    command_key="review-rebind:" + token,
                                    semantic_digest=compiled["semantic_digest"],
                                )
                                self._status(operation, "prepared", [], compiled["assessment"])
                            else:
                                self.current_locked(
                                    project_db, run, operation, transition, principal=principal
                                )
                                transition["phase"] = "rebind_claimed"
                                operation["validation"]["subject_transition"] = transition
                                self._status(
                                    operation, "rebind_claimed", [], compiled["assessment"]
                                )
                                fresh_claim = {
                                    key: transition[key]
                                    for key in ("id", "command_key", "binding_sha256")
                                }
                        elif (
                            transition is not None
                            and transition["semantic_digest"] == compiled["semantic_digest"]
                        ):
                            self._status(operation, "installed", [], compiled["assessment"])
                        else:
                            assert_cycle_quiescent(operation)
                            token = str(uuid.uuid4())
                            stage_transition(
                                operation,
                                compiled["binding"],
                                transition_id="review-binding:" + token,
                                command_key="review-rebind:" + token,
                                semantic_digest=compiled["semantic_digest"],
                            )
                            self._status(operation, "prepared", [], compiled["assessment"])
                except RunError as error:
                    self._status(operation, "blocked", [error.code])
                self.admissions._save(db, operation)
        # A claim is usable only by this invocation after its commit returned.
        # Reopened/parallel invocations observe it and can only look up history.
        if recover:
            return self.reconcile(run_id, worker_operation_id, principal=principal) or {}
        if fresh_claim is not None:
            try:
                with _connection(self.admissions.database, readonly=False) as db:
                    operation = self.admissions._load(db, run_id, worker_operation_id)
                    transition = parse_transition(operation)
                    if (
                        transition is None
                        or any(transition[key] != value for key, value in fresh_claim.items())
                        or transition["phase"] != "rebind_claimed"
                    ):
                        raise RunError("REVIEW_REBIND_CLAIM_CHANGED")
                    with self._current(operation, principal) as (project_db, run):
                        self.current_locked(
                            project_db, run, operation, transition, principal=principal
                        )
                        assert_cycle_quiescent(operation)
                        self.candidates.rebind_reviewers(
                            transition["binding"], command_key=transition["command_key"]
                        )
            except Exception as error:
                # A response/guard failure consumes the original claim. Never
                # infer 'not sent' or retry the CAS effect from this state.
                code = (
                    error.code
                    if isinstance(error, (RunError, CandidateError))
                    else "REVIEW_REBIND_EFFECT_UNCERTAIN"
                )
                with _connection(self.admissions.database, readonly=False) as db:
                    operation = self.admissions._load(db, run_id, worker_operation_id)
                    transition = parse_transition(operation)
                    if transition is not None and all(
                        transition[key] == value for key, value in fresh_claim.items()
                    ):
                        transition["reason_codes"] = sorted(
                            set([*transition["reason_codes"], code])
                        )
                        operation["validation"]["subject_transition"] = transition
                        self._status(
                            operation, "reconciliation_required", transition["reason_codes"]
                        )
                        self.admissions._save(db, operation)
            return self.reconcile(run_id, worker_operation_id, principal=principal) or {}
        return self.get(run_id, worker_operation_id, principal=principal) or {}

    def current_locked(
        self,
        project_db: sqlite3.Connection,
        run: dict[str, Any],
        operation: dict[str, Any],
        transition: dict[str, Any],
        *,
        principal: str,
    ) -> None:
        """Consumer callback inside its operation→Run→Project guards; no nested DB lock."""
        compiled = self._compile(project_db, run, operation, principal)
        if compiled["binding"] is None:
            raise RunError("REVIEWER_QUALIFICATION_REQUIRED")
        expected = deepcopy(compiled["binding"])
        held = (
            operation["validation"].get("review_binding")
            if transition["phase"] == "installed"
            else parse_transition(operation)
        )
        if held != transition or (
            transition["phase"] != "installed"
            and expected["source_candidate"] != transition["binding"]["source_candidate"]
        ):
            raise RunError("REVIEW_SUBJECT_BINDING_MISMATCH")
        expected.update(
            revision=transition["revision"],
            source_candidate=transition["binding"]["source_candidate"],
        )
        if (
            expected != transition["binding"]
            or digest(expected) != transition["binding_sha256"]
            or compiled["semantic_digest"] != transition["semantic_digest"]
        ):
            raise RunError("REVIEWER_BINDING_CHANGED")
        if transition["phase"] != "installed" and transition["expected_subject_digest"] != digest(
            operation["validation"]["subject"]
        ):
            raise RunError("REVIEW_SUBJECT_CHANGED")

    def _compile(
        self,
        project_db: sqlite3.Connection,
        run: dict[str, Any],
        operation: dict[str, Any],
        principal: str,
    ) -> dict[str, Any]:
        try:
            return self._compiled(project_db, run, operation, principal)
        except RunError:
            raise
        except CandidateError as error:
            raise RunError(error.code) from None
        except Exception:
            raise RunError("REVIEW_BINDING_SOURCE_UNAVAILABLE") from None

    def _compiled(
        self, db: sqlite3.Connection, run: dict[str, Any], operation: dict[str, Any], principal: str
    ) -> dict[str, Any]:
        if operation["cancel_requested"] or operation.get("execution", {}).get("cancel_requested"):
            raise RunError("REVIEW_BINDING_CANCELLED")
        plan, worker = _approved_task(run, operation, principal)
        projects = self.admissions.routing.planner.projects
        projects._require_owner(db, run["project_id"], principal)
        source = operation["workspace"]["source_binding"]
        execution = run["execution_policy_snapshot"]
        fixed = run["configuration_snapshot"]["configuration"]
        if (
            source["plan"] != plan
            or source["execution_policy"] != execution
            or source["configuration_digest"] != run["configuration_snapshot"]["digest"]
            or resolve_binding(run, plan["plan"]) != plan["routing_binding"]
        ):
            raise RunError("REVIEW_APPROVAL_CHANGED")
        project_row = db.execute(
            "SELECT snapshot FROM projects WHERE id=?", (run["project_id"],)
        ).fetchone()
        project = json.loads(project_row[0])
        current_config = db.execute(
            "SELECT configuration FROM previews WHERE id=? AND project_id=?",
            (project["configuration"]["preview_id"], run["project_id"]),
        ).fetchone()
        policy_row = db.execute(
            "SELECT record FROM execution_policies WHERE project_id=? AND id=? AND revision=?",
            (run["project_id"], execution["id"], execution["revision"]),
        ).fetchone()
        if (
            project["repository"] != run["configuration_snapshot"]["repository"]
            or current_config is None
            or json.loads(current_config[0])["rulebook"] != fixed["rulebook"]
            or policy_row is None
            or json.loads(policy_row[0]) != execution
        ):
            raise RunError("REVIEW_PROJECT_SOURCE_CHANGED")
        workspace = operation["workspace"]
        document = {key: value for key, value in workspace.items() if key != "digest"}
        if workspace["digest"] != digest(document) or workspace["input_sha256"] != digest(
            {key: value for key, value in document.items() if key != "input_sha256"}
        ):
            raise RunError("TASK_INPUT_WORKSPACE_DIGEST_MISMATCH")
        subject = current_subject(operation, self.candidates)
        reviewers = [
            task
            for task in plan["plan"]["tasks"]
            if task["role"] == "reviewer" and worker["id"] in task["depends_on"]
        ]
        if len(reviewers) != 1 or reviewers[0]["depends_on"] != [worker["id"]]:
            raise RunError("UNIQUE_APPROVED_REVIEWER_DEPENDENCY_REQUIRED")
        reviewer = reviewers[0]
        classification = {
            key: reviewer[key] for key in TaskClassification.model_fields if key != "authors"
        }
        classification["authors"] = [
            {
                "profile": {"id": author["profile_id"], "revision": author["profile_revision"]},
                "model_family": author["model_family"],
                "attempt_id": author["attempt_id"],
                "context_id": author["context_id"],
                **{key: worker[key] for key in ("complexity", "risk", "paths")},
            }
            for author in subject["capture_candidate"]["request"]["authors"]
        ]
        selection = select_rule(classification, fixed["rulebook"], execution["risk_policy"])
        normal = (
            plan["routing_binding"]["stage_grants"].get(selection["rule_id"], {}).get("normal", {})
        )
        auth = plan["plan"]["authorization"]
        preparation = digest([run["id"], operation["id"], reviewer["id"], "membership-only"])
        task_snapshot = {
            **classification,
            "schema_version": "karajan.routing.task.v1",
            "task_id": reviewer["id"],
            "task_revision": reviewer["revision"],
            "root_task_id": operation["assessment"]["route"]["snapshots"]["task"]["root_task_id"],
            "plan_revision": plan["plan_revision"],
            "authorization_digest": plan["authorization_digest"],
            **{
                key: reviewer[key]
                for key in ("required_capabilities", "tools", "context_tokens", "duration_seconds")
            },
            "reserved_output_tokens": execution["context_policy"]["reserved_output_tokens"],
            "stage": "normal",
            "quality_stage_index": 0,
            "failure_reason": None,
            "previous_profile": None,
            "quality_repair_rounds_used": 0,
            "planned_attempt_id": "membership-attempt:" + preparation,
            "planned_context_id": "membership-context:" + preparation,
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
                "allowed_stages": ["normal"] if normal else [],
                "approved_groups": deepcopy(normal),
                "approved_quality_stage_indices": [],
            },
        }
        catalog = effective_catalog(db, run["project_id"])
        resources = deepcopy(fixed["resources"])
        facts, observations, issues, profile_limits = [], {}, [], []
        for registration in resources["profiles"]:
            ref = {"id": registration["id"], "revision": registration["revision"]}
            registration["capability_evidence"] = []
            original = next(
                row for row in fixed["resources"]["profiles"] if reference(row) == reference(ref)
            )
            try:
                observed = self.qualifications._facts(
                    db, run["project_id"], original, "runtime_tools", None
                )
                if (
                    observed["runtime_tools_status"] != "passed"
                    or observed["qualification_scope"].endswith("_fixture")
                    or observed["facts"]["provenance"] != "imported_observation"
                ):
                    raise QualificationError("REVIEWER_QUALIFICATION_REQUIRED")
                limits, scope_reasons = resolve_go_reviewer_execution(
                    original, observed, task_snapshot, execution, selection["effective_class"]
                )
                if limits is None:
                    raise QualificationError(scope_reasons[0])
                authentication = observed["observation"]["binding"]["execution_start"][
                    "authentication_source"
                ]
                if (
                    not isinstance(authentication, dict)
                    or authentication.get("project_id") != run["project_id"]
                    or authentication.get("auth_ref") != original["profile"]["auth_ref"]
                    or not isinstance(authentication.get("generation"), str)
                    or not authentication["generation"]
                ):
                    raise QualificationError("REVIEW_QUALIFICATION_AUTHENTICATION_MISMATCH")
                observations[reference(ref)] = {
                    "reviewer": {
                        "profile_id": ref["id"],
                        "profile_revision": ref["revision"],
                        "model_family": registration["model_family"],
                        "qualification_ref": observed["facts"]["evidence_ref"],
                    },
                    "qualification_source_digest": digest(observed["observation"]["binding"]),
                    "authentication_source_digest": digest(authentication),
                }
                profile_limits.append({"profile": ref, "limits": limits})
                facts.append(observed["facts"])
                registration["capability_evidence"] = observed["capability_evidence"]
                if observed["facts"]["data_destination"] != execution["channel_destinations"].get(
                    registration["profile"]["binding"]["channel_id"]
                ):
                    registration["enabled"] = False
                    issues.append(
                        {"profile": ref, "reason_code": "PROFILE_DESTINATION_BINDING_MISMATCH"}
                    )
            except QualificationError as error:
                issues.append({"profile": ref, "reason_code": error.code})
            except Exception:
                issues.append(
                    {"profile": ref, "reason_code": "REVIEW_QUALIFICATION_SOURCE_UNAVAILABLE"}
                )
            if not _current_binding(fixed["resources"], catalog, registration):
                registration["enabled"] = False
                issues.append({"profile": ref, "reason_code": "CURRENT_PROFILE_RESTRICTED"})
        policy = {
            "schema_version": "karajan.routing.policy.v1",
            "rulebook": fixed["rulebook"],
            "resources": resources,
            "approved_profile_refs": [
                ref
                for ref in fixed["approved_profile_refs"]
                if ref in catalog["approved_profile_refs"]
            ],
            "profile_facts": facts,
            "risk_policy": execution["risk_policy"],
            "constraints": execution["constraints"],
        }
        membership = evaluate_profile_membership(
            task_snapshot, policy, as_of=self.admissions.routing.planner.clock()
        )
        assessment = {
            "membership": membership,
            "qualification_issues": issues,
            "preparation_identity_only": True,
            "actual_reviewer_attempt": None,
            "profile_limits": profile_limits,
        }
        if not membership["eligible_profiles"]:
            return {
                "binding": None,
                "semantic_digest": None,
                "assessment": assessment,
                "reason_codes": ["REVIEWER_QUALIFICATION_REQUIRED", *membership["reason_codes"]],
            }
        rows = [observations[reference(ref)] for ref in membership["eligible_profiles"]]
        binding = {
            "schema_version": "karajan.reviewer-binding.v1",
            "revision": operation["validation"].get("review_binding", {}).get("revision", 0) + 1,
            "source_candidate": candidate_identity(subject["candidate"]),
            "run_id": run["id"],
            "operation_id": operation["id"],
            "reviewer_task_id": reviewer["id"],
            "capture_digest": operation["execution"]["collection"]["capture_digest"],
            "approval_digest": digest(source["approval"]),
            "plan_digest": plan["plan_digest"],
            "execution_policy_digest": execution["digest"],
            "reviewer_task_digest": digest(reviewer),
            "rulebook_digest": digest(fixed["rulebook"]),
            "reviewer_sources": rows,
        }
        semantic = {
            key: value
            for key, value in binding.items()
            if key not in {"revision", "source_candidate"}
        }
        semantic["capture_candidate"] = candidate_identity(subject["capture_candidate"])
        return {
            "binding": binding,
            "semantic_digest": digest(semantic),
            "assessment": assessment,
            "reason_codes": [],
        }
