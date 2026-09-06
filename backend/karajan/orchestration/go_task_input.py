"""Compile immutable approved Workspace content, without reading a working tree."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from pydantic import ValidationError

from karajan.candidates import CandidateError, CandidateStore
from karajan.isolation.go_task import GoTaskFile, GoTaskInput
from karajan.projects.execution_policy import ExecutionPolicy, ExecutionPolicyV2
from karajan.routing.compiler import canonical, digest
from karajan.runs import RunError
from karajan.runs.models import Requirement
from karajan.runs.routing_authorization import PlanV2
from karajan.runs.validation import covered

from .workspace import _unambiguous


def build_task_input(
    workspace: dict[str, Any],
    candidates: CandidateStore,
    *,
    native_source_sha256: str,
    runner_source_digest: str,
) -> GoTaskInput:
    """Internal compiler; current Run and source authority remain controller gates.

    Digests prove fixed input consistency, not caller authorization. The caller
    supplies the persisted ApprovedTaskWorkspace under its own current guards.
    CAS restoration uses a private temporary sibling of control storage. No
    original repository path is read and no model or provider is invoked.
    """
    try:
        return _build(workspace, candidates, native_source_sha256, runner_source_digest)
    except RunError:
        raise
    except CandidateError as error:
        raise RunError(error.code) from None
    except (KeyError, TypeError, ValueError, ValidationError, OSError, StopIteration):
        raise RunError("TASK_INPUT_INVALID") from None


def _build(
    workspace: dict[str, Any],
    candidates: CandidateStore,
    native_source_sha256: str,
    runner_source_digest: str,
) -> GoTaskInput:
    workspace = json.loads(canonical(workspace))
    document = {key: value for key, value in workspace.items() if key != "digest"}
    if workspace.get("digest") != digest(document) or workspace.get("input_sha256") != digest(
        {key: value for key, value in document.items() if key != "input_sha256"}
    ):
        raise RunError("TASK_INPUT_WORKSPACE_DIGEST_MISMATCH")
    source, selected = _validate_workspace(workspace, candidates)
    brief = {
        "schema_version": "karajan.go-approved-task-brief.v1",
        "instruction": (
            "Implement the approved task using only read and edit. Read paths are relative to "
            "/workspace. Edit only existing write_paths; do not create files. Stop after "
            "implementation. Validation and review are performed separately by the controller."
        ),
        "requirement": source["requirement"],
        "plan_summary": source["plan"]["plan"]["summary"],
        "task": selected,
        "read_paths": workspace["read_paths"],
        "write_paths": workspace["write_paths"],
    }
    prompt = json.dumps(
        brief, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    if len(prompt) > 8192:
        raise RunError("TASK_INPUT_PROMPT_TOO_LARGE")
    with TemporaryDirectory(prefix=".task-input-", dir=candidates.directory.parent) as temporary:
        root = Path(temporary) / "baseline"
        candidates.materialize_baseline(workspace["baseline"]["id"], root)
        files = tuple(
            GoTaskFile(
                row["path"],
                row["artifact"]["sha256"],
                "write" in row["access"],
                (root / row["path"]).read_bytes(),
            )
            for row in workspace["files"]
        )
    return GoTaskInput(
        workspace["digest"],
        native_source_sha256,
        runner_source_digest,
        prompt,
        files,
        selected["duration_seconds"],
    )


def _validate_workspace(
    workspace: dict[str, Any],
    candidates: CandidateStore,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if workspace["schema_version"] != "karajan.approved-task-workspace.v1" or any(
        workspace[key] is not False
        for key in ("activation_allowed", "dispatch_enabled", "new_files_supported")
    ):
        raise RunError("TASK_INPUT_WORKSPACE_INVALID")
    source = workspace["source_binding"]
    Requirement.model_validate(source["requirement"])
    plan = source["plan"]
    PlanV2.model_validate(plan["plan"])
    if plan["plan_digest"] != digest(
        {key: value for key, value in plan.items() if key != "plan_digest"}
    ):
        raise RunError("TASK_INPUT_PLAN_BINDING_MISMATCH")
    authorization = plan["plan"]["authorization"]
    routing = plan["routing_binding"]
    approval = source["approval"]
    if (
        source["configuration_digest"] != plan["configuration_digest"]
        or plan["routing_digest"] != digest(routing)
        or plan["authorization_digest"]
        != digest([source["configuration_digest"], authorization, routing])
        or approval["run_id"] != workspace["run_id"]
        or any(
            approval[key] != plan[key]
            for key in (
                "plan_revision",
                "term",
                "plan_digest",
                "authorization_digest",
                "configuration_digest",
                "routing_digest",
            )
        )
    ):
        raise RunError("TASK_INPUT_APPROVAL_BINDING_MISMATCH")
    policy = source["execution_policy"]
    metadata = {"project_id", "digest", "registered_by", "registered_at", "activation_allowed"}
    document = {key: value for key, value in policy.items() if key not in metadata}
    (
        ExecutionPolicyV2
        if document["schema_version"] == "karajan.execution-policy.v2"
        else ExecutionPolicy
    ).model_validate(document)
    if (
        policy["digest"] != digest(document)
        or policy["configuration_digest"] != source["configuration_digest"]
        or routing["configuration_digest"] != source["configuration_digest"]
        or routing["execution_policy"]
        != {key: policy[key] for key in ("id", "revision", "digest", "project_id", "registered_by")}
    ):
        raise RunError("TASK_INPUT_POLICY_BINDING_MISMATCH")
    tasks = plan["plan"]["tasks"]
    matches = [row for row in tasks if row["id"] == workspace["task_id"]]
    if len(matches) != 1 or len({row["id"] for row in tasks}) != len(tasks):
        raise RunError("TASK_INPUT_UNIQUE_TASK_REQUIRED")
    task = matches[0]
    requirements = routing["task_requirements"][task["id"]]
    if requirements != {
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
    }:
        raise RunError("TASK_INPUT_TASK_BINDING_MISMATCH")
    if (
        task["role"] != "worker"
        or task["readiness"] != "ready"
        or task["depends_on"]
        or task["duration_seconds"] > authorization["max_attempt_duration_seconds"]
    ):
        raise RunError("TASK_INPUT_TASK_SCOPE_UNSUPPORTED")
    if (
        set(task["tools"]) != {"read", "edit"}
        or len(task["tools"]) != 2
        or not {"read", "edit"} <= set(authorization["tools"]) & set(policy["constraints"]["tools"])
        or any(
            policy["tool_policy"]["tool_permissions"].get(tool) != [tool]
            for tool in ("read", "edit")
        )
    ):
        raise RunError("TASK_INPUT_TOOL_SCOPE_UNSUPPORTED")
    baseline = candidates.get_baseline(workspace["baseline"]["id"])
    if (
        baseline != workspace["baseline"]
        or baseline["repository_identity"] != source["repository"]["identity_sha256"]
        or baseline["base_sha"] != source["repository"]["base_sha"]
    ):
        raise RunError("TASK_INPUT_BASELINE_BINDING_MISMATCH")
    paths = sorted(row["path"] for row in baseline["manifest"])
    for values in (paths, authorization["read_paths"], authorization["write_paths"], task["paths"]):
        _unambiguous(values)
    reads = [path for path in paths if covered(path, authorization["read_paths"])]
    writes = [
        path
        for path in paths
        if covered(path, task["paths"]) and covered(path, authorization["write_paths"])
    ]
    expected = [
        {**row, "access": ["read", "write"] if row["path"] in writes else ["read"]}
        for row in baseline["manifest"]
        if row["path"] in reads
    ]
    if (
        not reads
        or not writes
        or not set(writes) <= set(reads)
        or reads != workspace["read_paths"]
        or writes != workspace["write_paths"]
        or expected != workspace["files"]
        or any(not any(covered(path, [root]) for path in paths) for root in task["paths"])
    ):
        raise RunError("TASK_INPUT_PATH_BINDING_MISMATCH")
    return source, task
