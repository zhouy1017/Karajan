"""Immutable, owner-authored revisions of a trusted Planning output.

This module is intentionally a deep module: HTTP callers supply only an edit
intent and a concurrency binding; it owns identity checks, re-compilation,
current-source checks, proposal history, acceptance, and the one atomic owner
approval.  It never starts PlanningExecution, submits a Commander plan, or
creates a Worker request.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from typing import Any, cast

from pydantic import ValidationError

from karajan.conversations import ConversationError, ConversationStore
from karajan.projects import ProjectError
from karajan.projects.models import ProfileRef
from karajan.projects.qualification import ProfileQualificationStore, QualificationError
from karajan.runs import RunError, RunPlanner
from karajan.runs.planning import digest
from karajan.runs.routing_authorization import resolve_binding
from karajan.runs.validation import plan_impact, validate_plan


class ProposalError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


_EDIT_FIELDS = {
    "task_id",
    "role",
    "profile_ref",
    "source_ref",
    "dependencies",
    "write_paths",
    "checks",
}
_REQUEST_FIELDS = {
    "run_id",
    "base_plan_revision",
    "term",
    "edits",
    "expected_plan_digest",
    "expected_authorization_digest",
}


class ProposalStore:
    """Owner-intent proposal module, persisted in the existing Run aggregate."""

    def __init__(
        self,
        planner: RunPlanner,
        conversations: ConversationStore,
        qualifications: ProfileQualificationStore,
    ) -> None:
        self.planner, self.conversations = planner, conversations
        self.qualifications = qualifications

    @staticmethod
    def _input(request: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(request, dict) or set(request) - _REQUEST_FIELDS:
            raise ProposalError("PROPOSAL_INPUT_INVALID")
        required = {"run_id", "base_plan_revision", "term", "edits"}
        if set(request) & required != required or not isinstance(request["run_id"], str):
            raise ProposalError("PROPOSAL_INPUT_INVALID")
        if type(request["base_plan_revision"]) is not int or request["base_plan_revision"] < 1:
            raise ProposalError("PROPOSAL_INPUT_INVALID")
        if type(request["term"]) is not int or request["term"] < 1:
            raise ProposalError("PROPOSAL_INPUT_INVALID")
        if not isinstance(request["edits"], list) or len(request["edits"]) > 100:
            raise ProposalError("PROPOSAL_INPUT_INVALID")
        for digest_field in ("expected_plan_digest", "expected_authorization_digest"):
            if digest_field in request and (
                not isinstance(request[digest_field], str) or len(request[digest_field]) != 64
            ):
                raise ProposalError("PROPOSAL_INPUT_INVALID")
        edits: list[dict[str, Any]] = []
        seen: set[str] = set()
        for edit in request["edits"]:
            if not isinstance(edit, dict) or "task_id" not in edit or set(edit) - _EDIT_FIELDS:
                raise ProposalError("PROPOSAL_INPUT_INVALID")
            task_id = edit["task_id"]
            if not isinstance(task_id, str) or task_id in seen:
                raise ProposalError("PROPOSAL_INPUT_INVALID")
            seen.add(task_id)
            if len(edit) == 1:
                raise ProposalError("PROPOSAL_INPUT_INVALID")
            if "role" in edit and edit["role"] not in {"commander", "worker", "reviewer"}:
                raise ProposalError("PROPOSAL_INPUT_INVALID")
            if "profile_ref" in edit:
                try:
                    profile = ProfileRef.model_validate(edit["profile_ref"])
                    edit["profile_ref"] = profile.model_dump()
                except ValidationError:
                    raise ProposalError("PROPOSAL_INPUT_INVALID") from None
            if "source_ref" in edit and not isinstance(edit["source_ref"], str):
                raise ProposalError("PROPOSAL_INPUT_INVALID")
            for name in ("dependencies", "write_paths", "checks"):
                if name in edit and (
                    not isinstance(edit[name], list)
                    or not all(isinstance(value, str) for value in edit[name])
                ):
                    raise ProposalError("PROPOSAL_INPUT_INVALID")
            edits.append(deepcopy(edit))
        return {**request, "edits": edits}

    def create(
        self,
        conversation_id: str,
        request: dict[str, Any],
        *,
        principal: str,
        key: str,
    ) -> dict[str, Any]:
        request = self._input(request)
        identity = self._identity(conversation_id, request)

        try:
            # Run is acquired first by _command.  The Project guard is entered
            # only inside its operation and this stack remains open until that
            # Run transaction has durably committed.
            with ExitStack() as holds:

                def apply(db: Any) -> dict[str, Any]:
                    run = self.planner._get(db, request["run_id"])
                    self._owner_conversation(db, run, conversation_id, principal)
                    base = self._base(run, request)
                    compiled, assignments = self._compile(run, base, request["edits"])
                    holds.enter_context(
                        self._current_authority_guard(run, assignments, plan=compiled)
                    )
                    proposal_revision = self._next_revision(db, conversation_id)
                    result = self._record(
                        run, base, compiled, assignments, request, proposal_revision
                    )
                    run.setdefault("owner_proposals", []).append(result)
                    self.planner._save(db, run)
                    return result

                return self.planner._command("proposal", identity, principal, key, apply)
        except RunError as error:
            raise ProposalError(error.code) from None

    def read(self, conversation_id: str, *, revision: int | None = None) -> dict[str, Any]:
        try:
            run_items = self.planner.list(
                principal="owner",
                project_id=self.conversations.conversation_project(conversation_id),
            )
        except (RunError, ConversationError) as error:
            raise ProposalError(getattr(error, "code", "CONVERSATION_NOT_FOUND")) from None
        proposals: list[dict[str, Any]] = []
        for item in run_items:
            try:
                if (
                    self.conversations.run_conversation(item["id"], item["project_id"])
                    != conversation_id
                ):
                    continue
            except ConversationError:
                continue
            proposals.extend(item.get("owner_proposals", []))
        self._assert_unique_revisions(proposals)
        proposals.sort(key=lambda item: item["proposal_revision"])
        if revision is not None:
            proposals = [item for item in proposals if item["proposal_revision"] == revision]
            if not proposals:
                raise ProposalError("PROPOSAL_NOT_FOUND")
        return {"items": proposals, "current": proposals[-1] if proposals else None}

    def accept(
        self,
        conversation_id: str,
        revision: int,
        *,
        run_revision: int,
        principal: str,
        key: str,
    ) -> dict[str, Any]:
        request = {
            "conversation_id": conversation_id,
            "proposal_revision": revision,
            "run_revision": run_revision,
        }
        return self._mutate_proposal("proposal_accept", request, principal, key, accept=True)

    def approve(
        self,
        run_id: str,
        request: dict[str, Any],
        *,
        run_revision: int,
        principal: str,
        key: str,
    ) -> dict[str, Any]:
        if not isinstance(request, dict) or not isinstance(request.get("conversation_id"), str):
            raise ProposalError("CONVERSATION_ID_REQUIRED")
        if type(request.get("proposal_revision")) is not int:
            raise ProposalError("PROPOSAL_REVISION_REQUIRED")
        if {"run_id", "run_revision"} & set(request):
            raise ProposalError("APPROVAL_ROUTE_AUTHORITY_OVERRIDE")
        return self._mutate_proposal(
            "proposal_approve",
            {**request, "run_id": run_id, "run_revision": run_revision},
            principal,
            key,
            accept=True,
        )

    def _mutate_proposal(
        self, kind: str, request: dict[str, Any], principal: str, key: str, *, accept: bool
    ) -> dict[str, Any]:
        run_id = request.get("run_id")
        if kind == "proposal_accept":
            found = self.read(
                str(request["conversation_id"]), revision=int(request["proposal_revision"])
            )["items"]
            if len(found) != 1:
                raise ProposalError("PROPOSAL_AMBIGUOUS")
            run_id = found[0]["run_id"]
            request = {**request, "run_id": run_id}
        identity = self._identity(str(request["conversation_id"]), request)

        try:
            # This ExitStack closes only after _command's Run transaction
            # commits (or rolls back), retaining source authority across the
            # durable proposal/acceptance/approval write.
            with ExitStack() as holds:

                def apply(db: Any) -> dict[str, Any]:
                    run = self.planner._get(db, str(run_id))
                    self._owner_conversation(db, run, str(request["conversation_id"]), principal)
                    if run["revision"] != request["run_revision"]:
                        raise RunError("RUN_REVISION_STALE")
                    proposal = self._proposal(run, int(request["proposal_revision"]))
                    self._assert_proposal_current(run, proposal)
                    holds.enter_context(
                        self._current_authority_guard(
                            run, proposal["assignments"], plan=proposal["preview"]
                        )
                    )
                    if accept and proposal.get("accepted_at") is None:
                        proposal["accepted_at"] = self.planner.clock()
                        proposal["accepted_by"] = principal
                    if kind == "proposal_accept":
                        self.planner._save(db, run)
                        return proposal
                    approval = self._approval(run, proposal, request, principal)
                    proposal["approval_id"] = approval["id"]
                    self.planner._save(db, run)
                    return approval

                return self.planner._command(kind, identity, principal, key, apply)
        except RunError as error:
            raise ProposalError(error.code) from None

    def _owner_conversation(
        self, db: Any, run: dict[str, Any], conversation_id: str, principal: str
    ) -> None:
        self.planner._owner(run, principal)
        row = db.execute(
            "SELECT conversation_id, project_id FROM conversation_run_bindings WHERE run_id=?",
            (run["id"],),
        ).fetchone()
        if row is None:
            raise RunError("RUN_CONVERSATION_UNBOUND")
        if row["conversation_id"] != conversation_id or row["project_id"] != run["project_id"]:
            raise RunError("CROSS_PROJECT_REFERENCE")
        conversation = db.execute(
            "SELECT snapshot FROM commander_conversations WHERE id=?", (conversation_id,)
        ).fetchone()
        if conversation is None:
            raise RunError("CONVERSATION_NOT_FOUND")
        # The normalized binding and Run snapshot are both required: an old
        # client must never use omitted conversation identity as an alias.
        if run.get("conversation_id") not in {None, conversation_id}:
            raise RunError("CROSS_PROJECT_REFERENCE")

    def _base(self, run: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
        if request["term"] != run["commander"]["term"]:
            raise RunError("COMMANDER_TERM_STALE")
        if request["base_plan_revision"] != run["latest_plan_revision"] or not run["plans"]:
            raise RunError("PLAN_REVISION_STALE")
        base = next(
            (p for p in run["plans"] if p["plan_revision"] == request["base_plan_revision"]),
            None,
        )
        if base is None or base.get("provenance") not in {
            "fixture",
            "imported_observation",
            "planning_execution",
        }:
            raise RunError("TRUSTED_PLAN_REQUIRED")
        for name, base_name in (
            ("expected_plan_digest", "plan_digest"),
            ("expected_authorization_digest", "authorization_digest"),
        ):
            if name in request and request[name] != base[base_name]:
                raise RunError("PROPOSAL_BINDING_MISMATCH")
        return cast(dict[str, Any], base)

    def _compile(
        self, run: dict[str, Any], base: dict[str, Any], edits: list[dict[str, Any]]
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        plan = cast(dict[str, Any], deepcopy(base["plan"]))
        tasks = {task["id"]: task for task in plan["tasks"]}
        for edit in edits:
            task = tasks.get(edit["task_id"])
            if task is None:
                raise RunError("TASK_REFERENCE_NOT_FOUND")
            before = deepcopy(task)
            if "role" in edit:
                task["role"] = edit["role"]
                if "purpose" in task:
                    task["purpose"] = "lead" if edit["role"] == "commander" else None
            if "dependencies" in edit:
                task["depends_on"] = edit["dependencies"]
            if "write_paths" in edit:
                task["paths"] = edit["write_paths"]
            if "checks" in edit:
                # Existing required checks remain run-wide mandatory.  The
                # task copy binds that full required set into the immutable
                # preview/routing record; it never acts as a UI-only subset.
                task["checks"] = edit["checks"]
            profile = edit.get("profile_ref")
            source = edit.get("source_ref")
            if profile is not None or source is not None:
                if profile is None or source is None:
                    raise RunError("PROFILE_SOURCE_BINDING_REQUIRED")
                task["profile_ref"] = profile
                task["source_ref"] = source
            if task != before:
                task["revision"] += 1
        try:
            validate_plan(plan, run["authorization_ceiling"])
            plan_impact(plan, run)
            if run["schema_version"] == "karajan.run-planning.v2":
                resolve_binding(run, plan)
        except ValueError as error:
            raise RunError(str(error)) from None
        assignments = {
            task["id"]: {
                "profile_ref": task["profile_ref"],
                "source_ref": task["source_ref"],
            }
            for task in plan["tasks"]
            if task.get("profile_ref") is not None or task.get("source_ref") is not None
        }
        return plan, assignments

    def _identity(self, conversation_id: str, request: dict[str, Any]) -> dict[str, Any]:
        try:
            project_id = self.conversations.conversation_project(conversation_id)
        except ConversationError as error:
            raise ProposalError(error.code) from None
        return {
            "run_id": request.get("run_id"),
            "project_id": project_id,
            "conversation_id": conversation_id,
            "resource_id": request.get("run_id"),
            "request": request,
        }

    def _next_revision(self, db: Any, conversation_id: str) -> int:
        """Allocate the durable conversation-wide route identity under the Run lock."""
        row = db.execute(
            "SELECT next_revision FROM conversation_proposal_revisions WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone()
        maximum = 0
        seen: set[int] = set()
        for item in db.execute(
            "SELECT r.snapshot FROM runs r JOIN conversation_run_bindings b ON b.run_id=r.id "
            "WHERE b.conversation_id=?",
            (conversation_id,),
        ):
            run = json.loads(item["snapshot"])
            for proposal in run.get("owner_proposals", []):
                if isinstance(proposal, dict):
                    revision = proposal.get("proposal_revision")
                    if type(revision) is not int or revision < 1:
                        raise RunError("PROPOSAL_REVISION_INVALID")
                    if revision in seen:
                        raise RunError("PROPOSAL_REVISION_AMBIGUOUS")
                    seen.add(revision)
                    maximum = max(maximum, revision)
        if row is None:
            db.execute(
                "INSERT INTO conversation_proposal_revisions VALUES (?, ?)",
                (conversation_id, maximum + 2),
            )
            return maximum + 1
        revision = max(int(row["next_revision"]), maximum + 1)
        db.execute(
            "UPDATE conversation_proposal_revisions SET next_revision=? WHERE conversation_id=?",
            (revision + 1, conversation_id),
        )
        return revision

    @staticmethod
    def _assert_unique_revisions(proposals: list[dict[str, Any]]) -> None:
        """Never expose a route identity that maps to more than one Run."""
        seen: set[int] = set()
        for proposal in proposals:
            revision = proposal.get("proposal_revision")
            if type(revision) is not int or revision < 1:
                raise ProposalError("PROPOSAL_REVISION_INVALID")
            if revision in seen:
                raise ProposalError("PROPOSAL_REVISION_AMBIGUOUS")
            seen.add(revision)

    @contextmanager
    def _current_authority_guard(
        self,
        run: dict[str, Any],
        assignments: dict[str, dict[str, Any]],
        *,
        plan: dict[str, Any] | None = None,
    ) -> Iterator[None]:
        """Fence current source/qualification state through the Run mutation."""
        effective = plan or next(
            (
                p["plan"]
                for p in run["plans"]
                if p["plan_revision"] == run["latest_plan_revision"]
            ),
            None,
        )
        if not isinstance(effective, dict):
            raise RunError("PLAN_REVISION_STALE")
        refs = {
            (ref["id"], ref["revision"])
            for ref in effective["authorization"]["profile_refs"]
        }
        configuration = run["configuration_snapshot"]["configuration"]
        registrations = [
            registration
            for registration in configuration["resources"]["profiles"]
            if (registration["id"], registration["revision"]) in refs
        ]
        if len(registrations) != len(refs):
            raise RunError("PROFILE_SOURCE_REVOKED")
        try:
            with self.qualifications.proposal_authority_guard(
                run["project_id"], registrations, principal="owner"
            ) as view:
                rows = view["profiles"]
                current = view["catalog"]
                for row in rows:
                    if row["qualification"] is None:
                        raise RunError(row["reason_codes"][0])
                profiles = {
                    (item["id"], item["revision"]): item["profile"]
                    for item in current["resources"]["profiles"]
                    if item["profile"] is not None
                }
                for assignment in assignments.values():
                    selected = assignment["profile_ref"]
                    profile_key = (selected["id"], selected["revision"])
                    profile = profiles.get(profile_key)
                    if profile_key not in refs or not isinstance(profile, dict):
                        raise RunError("PROFILE_SOURCE_REVOKED")
                    if profile["binding"]["channel_id"] != assignment["source_ref"]:
                        raise RunError("PROFILE_SOURCE_REVOKED")
                yield
        except (ProjectError, QualificationError) as error:
            raise RunError(error.code) from None

    def _record(
        self,
        run: dict[str, Any],
        base: dict[str, Any],
        plan: dict[str, Any],
        assignments: dict[str, dict[str, Any]],
        request: dict[str, Any],
        revision: int,
    ) -> dict[str, Any]:
        unchanged = plan == base["plan"]
        inserted = None if unchanged else self._inserted_plan(run, base, plan, request)
        approved = base if inserted is None else inserted
        return {
            "proposal_revision": revision,
            "project_id": run["project_id"],
            "conversation_id": run["conversation_id"],
            "run_id": run["id"],
            "term": request["term"],
            "base_term": base["term"],
            "base_plan_revision": base["plan_revision"],
            "plan_revision": base["plan_revision"]
            if inserted is None
            else inserted["plan_revision"],
            "base_plan_digest": base["plan_digest"],
            "base_authorization_digest": base["authorization_digest"],
            "base_configuration_digest": base["configuration_digest"],
            "base_routing_digest": base.get("routing_digest"),
            "base_output_provenance": base["provenance"],
            "user_adjustments": request["edits"],
            "assignments": assignments,
            "task_graph_digest": digest(plan["tasks"]),
            "source_digest": digest(assignments),
            "authorization_digest": approved["authorization_digest"],
            "plan_digest": approved["plan_digest"],
            "configuration_digest": approved["configuration_digest"],
            "routing_digest": approved.get("routing_digest"),
            "preview": plan,
            "inserted_plan": inserted,
            "run_revision": run["revision"] + 1,
            "accepted_at": None,
            "approval_id": None,
        }

    def _inserted_plan(
        self,
        run: dict[str, Any],
        base: dict[str, Any],
        plan: dict[str, Any],
        request: dict[str, Any],
    ) -> dict[str, Any]:
        """Freeze the exact owner Plan record before proposal persistence."""
        routing = (
            resolve_binding(run, plan)
            if run["schema_version"] == "karajan.run-planning.v2"
            else None
        )
        authorization_digest = digest(
            [run["configuration_snapshot"]["digest"], plan["authorization"]]
            + ([routing] if routing is not None else [])
        )
        record: dict[str, Any] = {
            "plan_revision": base["plan_revision"] + 1,
            "term": request["term"],
            "submitted_by": run["owner"],
            "intent_id": None,
            "plan": deepcopy(plan),
            "configuration_digest": run["configuration_snapshot"]["digest"],
            "authorization_digest": authorization_digest,
            "provenance": "owner_adjustment",
            "base_output_provenance": base["provenance"],
            "user_adjustments": deepcopy(request["edits"]),
            "impact": plan_impact(plan, run),
        }
        if routing is not None:
            record["routing_binding"] = routing
            record["routing_digest"] = digest(routing)
        record["plan_digest"] = digest(record)
        return record

    @staticmethod
    def _proposal(run: dict[str, Any], revision: int) -> dict[str, Any]:
        proposal = next(
            (p for p in run.get("owner_proposals", []) if p["proposal_revision"] == revision),
            None,
        )
        if proposal is None:
            raise RunError("PROPOSAL_NOT_FOUND")
        return cast(dict[str, Any], proposal)

    @staticmethod
    def _assert_proposal_current(run: dict[str, Any], proposal: dict[str, Any]) -> None:
        """Bind acceptance and approval to one current base and exact Plan slot."""
        base = next(
            (
                row
                for row in run["plans"]
                if row["plan_revision"] == proposal.get("base_plan_revision")
            ),
            None,
        )
        if base is None:
            raise RunError("PLAN_REVISION_STALE")
        if any(
            proposal.get(proposal_key) != base.get(base_key)
            for proposal_key, base_key in (
                ("base_term", "term"),
                ("base_plan_digest", "plan_digest"),
                ("base_authorization_digest", "authorization_digest"),
                ("base_configuration_digest", "configuration_digest"),
                ("base_routing_digest", "routing_digest"),
            )
        ):
            raise RunError("PROPOSAL_BASE_BINDING_MISMATCH")
        if proposal.get("term") != run["commander"]["term"]:
            raise RunError("COMMANDER_TERM_STALE")
        inserted = proposal.get("inserted_plan")
        if inserted is None:
            if (
                proposal.get("plan_revision") != base["plan_revision"]
                or proposal.get("preview") != base["plan"]
                or proposal.get("plan_digest") != base["plan_digest"]
            ):
                raise RunError("PROPOSAL_BINDING_INCOMPLETE")
        elif (
            not isinstance(inserted, dict)
            or inserted.get("plan_revision") != base["plan_revision"] + 1
            or inserted.get("term") != proposal.get("term")
            or inserted.get("plan") != proposal.get("preview")
            or inserted.get("plan_digest") != proposal.get("plan_digest")
            or inserted.get("authorization_digest") != proposal.get("authorization_digest")
            or inserted.get("configuration_digest") != proposal.get("configuration_digest")
            or inserted.get("routing_digest") != proposal.get("routing_digest")
            or digest({key: value for key, value in inserted.items() if key != "plan_digest"})
            != inserted.get("plan_digest")
        ):
            raise RunError("PROPOSAL_BINDING_INCOMPLETE")
        if run["latest_plan_revision"] != base["plan_revision"]:
            if inserted is not None and any(
                row["plan_revision"] == proposal.get("plan_revision") for row in run["plans"]
            ):
                raise RunError("PROPOSAL_PLAN_COLLISION")
            raise RunError("PLAN_REVISION_STALE")

    def _approval(
        self, run: dict[str, Any], proposal: dict[str, Any], request: dict[str, Any], principal: str
    ) -> dict[str, Any]:
        required = {
            "term",
            "plan_revision",
            "plan_digest",
            "authorization_digest",
            "configuration_digest",
        }
        if run["schema_version"] == "karajan.run-planning.v2":
            required |= {"schema_version", "routing_digest"}
        if not required <= set(request) or any(
            request[name] != proposal.get(name) for name in required - {"schema_version"}
        ):
            raise RunError("APPROVAL_BINDING_MISMATCH")
        if request["term"] != run["commander"]["term"] or proposal["term"] != request["term"]:
            raise RunError("COMMANDER_TERM_STALE")
        if proposal["approval_id"] is not None or run["active_plan_revision"] is not None:
            raise RunError("PLAN_ALREADY_APPROVED")
        inserted = proposal.get("inserted_plan")
        if inserted is not None:
            if not isinstance(inserted, dict):
                raise RunError("PROPOSAL_BINDING_INCOMPLETE")
            if any(
                row["plan_revision"] == proposal["plan_revision"] for row in run["plans"]
            ):
                raise RunError("PROPOSAL_PLAN_COLLISION")
            run["plans"].append(deepcopy(inserted))
            run["latest_plan_revision"] = inserted["plan_revision"]
        receipt = {name: request[name] for name in required}
        receipt.update(
            {
                "id": uuid.uuid4().hex,
                "approved_by": principal,
                "approved_at": self.planner.clock(),
                "dispatch_enabled": False,
                "conversation_id": proposal["conversation_id"],
                "proposal_revision": proposal["proposal_revision"],
            }
        )
        run["approvals"].append(receipt)
        run["active_plan_revision"] = proposal["plan_revision"]
        run["state"] = "executing"
        return receipt
