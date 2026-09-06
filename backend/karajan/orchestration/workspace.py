"""Freeze approved Task paths and baseline provenance before any execution effect."""

from copy import deepcopy
from pathlib import Path
from typing import Any

from karajan.candidates import CandidateError, CandidateStore
from karajan.routing.compiler import digest
from karajan.runs import RunError
from karajan.runs.planning import identifier
from karajan.runs.validation import covered, path_parts

from .admission import ApprovedTaskAdmission


class ApprovedTaskWorkspace:
    """One immutable workspace manifest per reserved operation.

    Preparation captures existing Git files only. It grants no runtime authority;
    execution must separately revalidate the Profile, credentials and capacity.
    """

    def __init__(self, admissions: ApprovedTaskAdmission, candidates: CandidateStore) -> None:
        routing = admissions.routing
        if (candidates.directory / "candidates.sqlite").resolve() in {
            admissions.database.resolve(),
            routing.planner.database.resolve(),
            routing.planner.projects.database.resolve(),
            routing.capacity.path.resolve(),
        }:
            raise RunError("TASK_WORKSPACE_DATABASE_MUST_BE_SEPARATE")
        self.admissions, self.candidates = admissions, candidates

    def get(self, run_id: str, operation_id: str, *, principal: str) -> dict[str, Any]:
        for value in (run_id, operation_id, principal):
            identifier(value)
        self.admissions._owner(run_id, principal)
        with self.admissions._transaction() as db:
            operation = self.admissions._load(db, run_id, operation_id)
            if "workspace" not in operation:
                raise RunError("TASK_WORKSPACE_NOT_PREPARED")
            return dict(operation["workspace"])

    def prepare(self, run_id: str, operation_id: str, *, principal: str) -> dict[str, Any]:
        for value in (run_id, operation_id, principal):
            identifier(value)
        self.admissions._owner(run_id, principal)
        # Match admission's coordinator -> Run order. No public Run read is
        # re-entered while activation_guard holds its transaction.
        with self.admissions._transaction() as db:
            operation = self.admissions._refresh(
                db, self.admissions._load(db, run_id, operation_id)
            )
            if operation["state"] != "reserved" or operation["cancel_requested"]:
                raise RunError("TASK_WORKSPACE_RESERVATION_REQUIRED")
            with self.admissions.routing.planner.activation_guard(run_id) as run:
                plan, task = _approved_task(run, operation, principal)
                if "workspace" in operation:
                    return dict(operation["workspace"])
                manifest = self._capture(run, operation, plan, task)
                operation["workspace"] = manifest
                self.admissions._save(db, operation)
                return deepcopy(manifest)

    def _capture(
        self,
        run: dict[str, Any],
        operation: dict[str, Any],
        plan: dict[str, Any],
        task: dict[str, Any],
    ) -> dict[str, Any]:
        auth = plan["plan"]["authorization"]
        for roots in (auth["read_paths"], auth["write_paths"], task["paths"]):
            _unambiguous(roots)
        if not task["paths"]:
            raise RunError("TASK_WORKSPACE_SCOPE_EMPTY")
        repository = run["configuration_snapshot"]["repository"]
        try:
            baseline = self.candidates.register_baseline(
                Path(repository["root"]),
                repository_identity=repository["identity_sha256"],
                base_sha=repository["base_sha"],
            )
        except CandidateError as error:
            raise RunError(error.code) from None
        paths = sorted(entry["path"] for entry in baseline["manifest"])
        _unambiguous(paths)
        read_paths = [path for path in paths if covered(path, auth["read_paths"])]
        write_paths = [
            path
            for path in paths
            if covered(path, task["paths"]) and covered(path, auth["write_paths"])
        ]
        if any(not any(covered(path, [root]) for path in paths) for root in task["paths"]):
            raise RunError("TASK_WORKSPACE_NEW_FILES_NOT_SUPPORTED")
        if not read_paths or not write_paths:
            raise RunError("TASK_WORKSPACE_SCOPE_EMPTY")
        if not set(write_paths) <= set(read_paths):
            raise RunError("TASK_WORKSPACE_WRITE_NOT_READABLE")
        assessment = operation["assessment"]
        selected = assessment["route"]["selected_profile"]
        registration = next(
            row
            for row in run["configuration_snapshot"]["configuration"]["resources"]["profiles"]
            if {"id": row["id"], "revision": row["revision"]} == selected
        )
        profile_source = next(
            row for row in assessment["sources"]["profiles"] if row["profile"] == selected
        )
        source = {
            "requirement": run["requirement"],
            "approval": assessment["sources"]["approval"],
            "plan": plan,
            "execution_policy": run["execution_policy_snapshot"],
            "configuration_digest": run["configuration_snapshot"]["digest"],
            "repository": repository,
            "assessment_id": assessment["id"],
            "assessment_digest": assessment["digest"],
            "selected_profile": selected,
            "profile_registration": registration,
            "profile_source": profile_source,
        }
        result = {
            "schema_version": "karajan.approved-task-workspace.v1",
            "run_id": run["id"],
            "operation_id": operation["id"],
            "task_id": task["id"],
            "planned_attempt_id": operation["planned_attempt_id"],
            "planned_context_id": operation["planned_context_id"],
            "source_binding": source,
            "baseline": baseline,
            "read_paths": read_paths,
            "write_paths": write_paths,
            "files": [
                {**entry, "access": ["read", "write"] if entry["path"] in write_paths else ["read"]}
                for entry in baseline["manifest"]
                if entry["path"] in read_paths
            ],
            "new_files_supported": False,
            "activation_allowed": False,
            "dispatch_enabled": False,
        }
        result["input_sha256"] = digest(result)
        result["digest"] = digest(result)
        return result


def _approved_task(
    run: dict[str, Any], operation: dict[str, Any], principal: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    if run["owner"] != principal:
        raise RunError("RUN_NOT_FOUND")
    if run["schema_version"] != "karajan.run-planning.v2" or run["state"] != "executing":
        raise RunError("APPROVED_PLAN_REQUIRED")
    plan = next(
        (row for row in run["plans"] if row["plan_revision"] == run["active_plan_revision"]), None
    )
    approval = next(
        (row for row in run["approvals"] if row["plan_revision"] == run["active_plan_revision"]),
        None,
    )
    sources = operation["assessment"]["sources"]
    if (
        plan is None
        or approval is None
        or sources["approval"] != approval
        or any(
            approval[key] != plan[key]
            for key in (
                "term",
                "plan_digest",
                "authorization_digest",
                "configuration_digest",
                "routing_digest",
            )
        )
        or sources["routing_digest"] != plan["routing_digest"]
        or sources["execution_policy_digest"] != run["execution_policy_snapshot"]["digest"]
        or plan["configuration_digest"] != run["configuration_snapshot"]["digest"]
    ):
        raise RunError("APPROVAL_BINDING_MISMATCH")
    task = next((row for row in plan["plan"]["tasks"] if row["id"] == operation["task_id"]), None)
    if (
        task is None
        or task["role"] != "worker"
        or task["depends_on"]
        or task["readiness"] != "ready"
    ):
        raise RunError("TASK_WORKSPACE_SCOPE_NOT_SUPPORTED")
    return plan, task


def _unambiguous(paths: list[str]) -> None:
    """Reject case aliases, duplicate roots, and file/directory prefix conflicts."""
    prefixes: dict[tuple[str, ...], tuple[str, ...]] = {}
    files: set[tuple[str, ...]] = set()
    try:
        for path in paths:
            parts = path_parts(path)
            folded = tuple(part.casefold() for part in parts)
            if folded in prefixes:
                raise RunError("TASK_WORKSPACE_PATH_CONFLICT")
            for end in range(1, len(parts) + 1):
                key, spelling = folded[:end], parts[:end]
                if key in files or prefixes.get(key, spelling) != spelling:
                    raise RunError("TASK_WORKSPACE_PATH_CONFLICT")
                prefixes[key] = spelling
            files.add(folded)
    except ValueError as error:
        raise RunError(str(error)) from None
