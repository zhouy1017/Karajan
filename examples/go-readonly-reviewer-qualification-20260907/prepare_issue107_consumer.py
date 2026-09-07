"""Prepare and exercise #107's fixed controller Plan/Candidate consumer fixture.

This operator artifact has no model/client/relay entry point.  It intentionally
uses the real, already-configured ProfileQualificationStore and the production
ApprovedReviewerBindings consumer.  The small Plan and Candidate are marked as
controller fixtures: their durable lineage is used solely to reach the current
qualification guard, never as a Commander, Worker, Reviewer Task, or Candidate
Review execution.
"""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import sys
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(Path(__file__).parent))

from karajan.candidates import CandidateStore
from karajan.orchestration.admission import ApprovedTaskAdmission
from karajan.orchestration.reviewer_binding import ApprovedReviewerBindings
from karajan.orchestration.routing import ApprovedRunRouting
from karajan.projects.demand import AttemptEstimateStore
from karajan.routing.compiler import digest
from karajan.runs import RunPlanner
from karajan.runs.planning import encoded

from run_official_issue107 import PRINCIPAL, SUITE, open_controller, paths


FIXTURE_OWNER = "issue107-controller"
FIXTURE_PLAN_AUTHOR = "issue107-fixed-plan-author"
POLICY_ID = "issue107-fixed-reviewer-consumer"
RUN_COMMAND = "issue107-fixed-consumer-run-v1"


class FixedPlanReceiptAuthority:
    """Private controller fixture receipt, explicitly not a Commander admission."""

    def __init__(self) -> None:
        self.receipts: dict[str, dict[str, Any]] = {}

    def __call__(self, reference: str) -> dict[str, Any]:
        return self.receipts[reference]

    def grant(self, intent: dict[str, Any]) -> str:
        reference = "issue107-fixed-plan-receipt"
        self.receipts[reference] = {
            "receipt_ref": reference,
            "authority_revision": "issue107-controller-fixture-v1",
            "run_id": intent["run_id"],
            "intent_id": intent["id"],
            "term": intent["term"],
            "principal": intent["principal"],
            "profile": intent["profile"],
            "budget_ref": intent["budget_ref"],
            "state": "admitted",
            "provenance": "fixture",
        }
        return reference


def sha(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def ensure_fixture_configuration(store: Any, project_id: str) -> dict[str, Any]:
    """Prepare only controller fixture metadata; dynamic qualification stays in Store."""
    projects = store.projects
    current = projects.get(project_id)
    configuration = projects.get_configuration(project_id)["configuration"]
    reviewer_ref = {"id": "readonly-reviewer-107", "revision": 1}
    group = configuration["rulebook"]["profile_groups"]["review_standard_qualified"]
    required = {"fixture-profile": "fixture-tools", "readonly-reviewer-107": "read"}
    evidence_ok = all(
        any(row.get("capability") == capability and row.get("status") == "passed"
            and row.get("profile_digest") == digest(profile["profile"])
            for row in profile["capability_evidence"])
        for profile_id, capability in required.items()
        for profile in configuration["resources"]["profiles"] if profile["id"] == profile_id
    )
    if reviewer_ref in group and evidence_ok and current["configuration"]["status"] == "offline_valid":
        return current
    updated = copy.deepcopy(configuration)
    # The original example profile remains a non-executable fixture member.  Its
    # auth reference is metadata only, but must agree with the private account
    # record for the configuration validator; no credential is resolved here.
    fixture_profile = next(row for row in updated["resources"]["profiles"] if row["id"] == "fixture-profile")
    fixture_profile["profile"]["auth_ref"] = "go-reviewer-secret-107"
    for evidence in fixture_profile["capability_evidence"]:
        evidence["profile_digest"] = digest(fixture_profile["profile"])
        evidence["runtime_version"] = fixture_profile["profile"]["binding"]["runtime_version"]
    updated["rulebook"]["profile_groups"]["review_standard_qualified"].append(reviewer_ref)
    updated["rulebook"]["profile_groups"]["review_standard_qualified"] = list({
        (row["id"], row["revision"]): row
        for row in updated["rulebook"]["profile_groups"]["review_standard_qualified"]
    }.values())
    for profile in updated["resources"]["profiles"]:
        capability = required.get(profile["id"])
        if capability is not None and not any(
            row.get("capability") == capability and row.get("status") == "passed"
            and row.get("profile_digest") == digest(profile["profile"])
            for row in profile["capability_evidence"]
        ):
            profile["capability_evidence"] = [
                row for row in profile["capability_evidence"] if row.get("capability") != capability
            ]
            profile["capability_evidence"].append({
                "capability": capability, "status": "passed", "profile_digest": digest(profile["profile"]),
                "runtime_version": profile["profile"]["binding"]["runtime_version"],
                "evidence_ref": "issue107-controller-fixed-config-permission", "provenance": "fixture",
            })
    preview = projects.preview_configuration(
        project_id, updated, principal=PRINCIPAL, command_key="issue107-consumer-config-preview-v4"
    )
    return projects.apply_configuration(
        project_id,
        preview["preview_id"],
        expected_revision=current["revision"],
        principal=PRINCIPAL,
        command_key="issue107-consumer-config-apply-v4",
    )


def policy_request(configuration: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    context = source["probe_spec"]["context"]
    environment = {"id": "issue107-fixed-validation", "revision": 1}
    return {
        "schema_version": "karajan.execution-policy.v2",
        "id": POLICY_ID,
        "revision": 1,
        "configuration_digest": digest(configuration),
        "constraints": {
            "profile_refs": [
                {"id": "fixture-profile", "revision": 1},
                {"id": "readonly-reviewer-107", "revision": 1},
            ],
            "channel_ids": ["fixture-channel"],
            "tools": ["read", "edit"],
            "data_destinations": ["opencode-go"],
            "required_capabilities": [],
            "min_isolation": "tool_sandboxed",
        },
        "risk_policy": {
            "id": "issue107-fixed-risk", "revision": 1,
            "mapping": {"standard": "T1", "critical": "T3"},
            "path_floors": [],
        },
        "channel_destinations": {"fixture-channel": "opencode-go"},
        "tool_policy": {
            "id": "issue107-fixed-tools", "revision": 1,
            "tool_permissions": {"read": ["read"], "edit": ["edit"]},
        },
        "context_policy": {
            "id": "issue107-fixed-context", "revision": 1,
            "input_accounting": "explicit_approved_upper_bound",
            "reserved_output_tokens": 4096,
            "measurement": {
                "method": "reference_tokenizer_estimate",
                "source_sha256": context["source_sha256"],
                "fixed_margin": 2048,
                "ratio_margin_basis_points": 2000,
            },
        },
        "max_context_tokens": 16384,
        "validation": {
            "id": "issue107-fixed-validation", "revision": 1,
            "checks": [{
                "id": "fixture-static-check", "revision": 1,
                "argv": ["true"], "environment_ref": environment, "timeout_seconds": 1,
            }],
            "environments": [{
                **environment, "runtime_kind": "isolated-command", "platform": "linux_x64",
                "source_sha256": "0" * 64, "filesystem": "candidate_copy", "network": "none",
                "env": {}, "max_log_bytes": 1024,
            }],
            "review": {
                "id": "independent_review", "revision": 1, "environment_ref": environment,
                "context_policy": "candidate_and_acceptance_only",
                "independence_policy": "existing_candidate_independence_v1",
            },
        },
    }


def run_request(project: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    refs = policy["constraints"]["profile_refs"]
    return {
        "schema_version": "karajan.create-run.v2",
        "project_id": project["id"], "project_revision": project["revision"],
        "configuration_digest": project["configuration"]["digest"],
        "execution_policy": {key: policy[key] for key in ("id", "revision", "digest")},
        "requirement": {
            "goal": "Issue 107 fixed controller consumer fixture only",
            "acceptance": ["Read current readonly Reviewer qualification without model admission"],
        },
        "participants": [{
            "principal": FIXTURE_PLAN_AUTHOR, "profile": {"id": "fixture-profile", "revision": 1},
            "purpose": "lead",
        }],
        "authorization": {
            "profile_refs": refs, "read_paths": ["README.md"], "write_paths": ["README.md"],
            "budget_ref": "run", "checks": ["fixture-static-check", "independent_review"],
            "delivery": "none", "target_branch": project["target_branch"],
            "channel_ids": ["fixture-channel"], "tools": ["read", "edit"],
            "data_destinations": ["opencode-go"], "required_capabilities": ["controlled_tools"],
            "min_isolation": "tool_sandboxed", "currency_limits": {"USD": "0", "CNY": "0"},
            "max_attempt_duration_seconds": 60, "max_quality_repair_rounds": 0,
            "stage_permissions": {
                "mechanical-worker": {"normal": True, "quality_indices": []},
                "standard-review": {"normal": True, "quality_indices": []},
            },
        },
    }


def submit_request(run: dict[str, Any], intent: dict[str, Any]) -> dict[str, Any]:
    authorization = copy.deepcopy(run["authorization_ceiling"])
    return {
        "schema_version": "karajan.submit-plan.v2", "term": 1, "intent_id": intent["id"],
        "expected_plan_revision": 0,
        "plan": {
            "summary": "Static controller Plan/Candidate fixture; no task execution",
            "authorization": authorization,
            "tasks": [
                {
                    "id": "fixture-worker", "revision": 1, "role": "worker", "readiness": "ready",
                    "complexity": "T1", "risk": "standard", "paths": ["README.md"], "depends_on": [],
                    "acceptance": ["static candidate only"], "required": True, "purpose": None,
                    "domains": ["fixture"], "required_capabilities": ["controlled_tools"],
                    "tools": ["read", "edit"], "context_tokens": 4096, "duration_seconds": 60,
                },
                {
                    "id": "fixture-reviewer", "revision": 1, "role": "reviewer", "readiness": "ready",
                    "complexity": "T1", "risk": "standard", "paths": ["README.md"],
                    "depends_on": ["fixture-worker"], "acceptance": ["membership only"], "required": True,
                    "purpose": None, "domains": ["fixture"],
                    "required_capabilities": ["code_review", "structured_findings"],
                    "tools": ["read"], "context_tokens": 12288, "duration_seconds": 60,
                },
            ],
        },
    }


def fixed_candidate(candidates: CandidateStore, repository: Path, workspace: Path, *, input_sha256: str) -> tuple[dict[str, Any], dict[str, Any]]:
    base_sha = __import__("subprocess").check_output(["git", "-C", str(repository), "rev-parse", "HEAD"], text=True).strip()
    repository_identity = sha({"issue": 107, "repository": "fixed-controller-private"})
    baseline = candidates.register_baseline(repository, repository_identity=repository_identity, base_sha=base_sha)
    workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
    (workspace / "README.md").write_text("static controller candidate fixture\n", encoding="utf-8")
    request = {
        "series_id": "issue107-controller-fixed-candidate", "baseline_id": baseline["id"],
        "input_sha256": input_sha256, "allowed_paths": ["README.md"], "task_class": "T1",
        "writer": {"attempt_id": "controller-fixture-no-worker", "fence": 1, "stopped": True,
                   "observation_ref": "controller-fixture-no-worker-stop"},
        "authors": [{"attempt_id": "controller-fixture-no-worker", "fence": 1,
                     "profile_id": "fixture-profile", "profile_revision": 1,
                     "model_family": "controller-fixture", "context_id": "controller-fixture-no-worker-context",
                     "provenance_ref": "controller-fixed-plan-candidate-fixture"}],
        "policy": {"id": "issue107-fixed-policy", "revision": 1, "checks": [{
            "id": "fixture-static-check", "revision": 1, "argv": ["true"],
            "environment_sha256": "0" * 64,
        }], "review": {"revision": 1, "environment_sha256": "0" * 64, "approved_reviewers": []}},
    }
    candidate = candidates.freeze(workspace, request)
    baseline = candidates.get_baseline(request["baseline_id"])
    projection = [{"path": row["path"], "sha256": row["artifact"]["sha256"], "writable": row["path"] == "README.md"} for row in baseline["manifest"]]
    captured = [{"path": row["path"], "sha256": row["artifact"]["sha256"], "size": row["artifact"]["size"]} for row in candidate["manifest"]]
    return candidate, {"freeze_request": request, "projection": projection, "captured_files": captured}


def ensure_fixture(private_root: Path) -> tuple[ApprovedReviewerBindings, tuple[str, str], dict[str, Any]]:
    store, project_id, reviewer, _journal = open_controller(private_root)
    project = ensure_fixture_configuration(store, project_id)
    configuration = store.projects.get_configuration(project_id)["configuration"]
    source = store.reviewer_suite.source()
    policy = store.projects.register_execution_policy(
        project_id, policy_request(configuration, source), principal=PRINCIPAL,
        command_key="issue107-consumer-policy-v1",
    )
    fixture_root = private_root / "consumer-fixture"
    fixture_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    authority = FixedPlanReceiptAuthority()
    planner = RunPlanner(fixture_root / "runs.sqlite", store.projects, admissions=authority)
    run = planner.create(run_request(project, policy), principal=FIXTURE_OWNER, command_key=RUN_COMMAND)
    intent = planner.planning_intent(run["id"], term=1, principal=FIXTURE_PLAN_AUTHOR, command_key="issue107-fixed-plan-intent-v1")
    planner.attach_planning_receipt(run["id"], intent["id"], receipt_ref=authority.grant(intent), principal=FIXTURE_OWNER, command_key="issue107-fixed-plan-receipt-v2")
    plan = planner.submit_plan(run["id"], submit_request(run, intent), principal=FIXTURE_PLAN_AUTHOR, command_key="issue107-fixed-plan-submit-v1")
    planner.approve_plan(run["id"], {"schema_version": "karajan.approve-plan.v2", **{key: plan[key] for key in ("term", "plan_revision", "plan_digest", "authorization_digest", "configuration_digest", "routing_digest")}}, principal=FIXTURE_OWNER, command_key="issue107-fixed-plan-approve-v1")
    routing = ApprovedRunRouting(planner, store, __import__("karajan.capacity", fromlist=["CapacityStore"]).CapacityStore(fixture_root / "capacity.sqlite"), estimates=AttemptEstimateStore(planner))
    admissions = ApprovedTaskAdmission(fixture_root / "admission.sqlite", routing)
    candidates = CandidateStore(fixture_root / "candidates")
    repository = paths(private_root)["repo"]
    # First construct the source-bound workspace identity, then freeze a static candidate using it.
    operation_id = "issue107-fixed-consumer-operation"
    source_binding = {
        "requirement": run["requirement"], "approval": planner.get(run["id"], principal=FIXTURE_OWNER)["approvals"][-1],
        "plan": plan, "execution_policy": policy, "configuration_digest": run["configuration_snapshot"]["digest"],
        "repository": run["configuration_snapshot"]["repository"], "assessment_id": "controller-fixed-assessment",
        "assessment_digest": sha({"issue": 107, "fixture": "no-routing-assessment"}),
        "selected_profile": {"id": "fixture-profile", "revision": 1},
        "profile_registration": next(row for row in configuration["resources"]["profiles"] if row["id"] == "fixture-profile"),
        "profile_source": {"fixture": "controller-fixed-plan-candidate"},
    }
    baseline = candidates.register_baseline(repository, repository_identity=sha({"issue": 107, "repository": "fixed-controller-private"}), base_sha=__import__("subprocess").check_output(["git", "-C", str(repository), "rev-parse", "HEAD"], text=True).strip())
    workspace = {
        "schema_version": "karajan.approved-task-workspace.v1", "run_id": run["id"], "operation_id": operation_id,
        "task_id": "fixture-worker", "planned_attempt_id": "controller-fixture-no-worker",
        "planned_context_id": "controller-fixture-no-worker-context", "source_binding": source_binding,
        "baseline": baseline, "read_paths": ["README.md"], "write_paths": ["README.md"],
        "files": [{**row, "access": ["read", "write"]} for row in baseline["manifest"]],
        "new_files_supported": False, "activation_allowed": False, "dispatch_enabled": False,
    }
    workspace["input_sha256"] = digest(workspace)
    workspace["digest"] = digest(workspace)
    candidate, capture = fixed_candidate(candidates, repository, fixture_root / "candidate-workspace", input_sha256=workspace["input_sha256"])
    capture_digest = digest(capture)
    identity = {key: candidate[key] for key in ("id", "series_id", "revision", "repository_identity", "base_sha", "tree_sha", "content_sha256", "manifest_sha256", "input_sha256", "policy_sha256")}
    identity.update(baseline_id=candidate["request"]["baseline_id"], request_sha256=digest(candidate["request"]))
    candidate_reference = {
        key: candidate[key]
        for key in ("id", "series_id", "revision", "content_sha256", "manifest_sha256", "input_sha256", "policy_sha256")
    }
    operation = {
        "schema_version": "karajan.approved-task-admission.v1", "id": operation_id, "run_id": run["id"],
        "task_id": "fixture-worker", "planned_attempt_id": "controller-fixture-no-worker",
        "planned_context_id": "controller-fixture-no-worker-context", "state": "controller_fixture_only",
        "reason_codes": [], "assessment": {
            "sources": {"approval": source_binding["approval"], "routing_digest": plan["routing_digest"],
                        "execution_policy_digest": policy["digest"]},
            "route": {"snapshots": {"task": {"root_task_id": "fixture-worker"}}},
        },
        "request": None, "capacity_receipt": None, "revalidation": None, "cancel_requested": False,
        "cancellation_receipt": None, "activation_allowed": False, "dispatch_enabled": False,
        "workspace": workspace,
        "execution": {"collection": {"capture": capture, "capture_digest": capture_digest, "candidate": candidate_reference}},
        "validation": {"subject": {"schema_version": "karajan.candidate-subject.v1", "revision": 1,
            "candidate": identity, "source_candidate": identity, "source_capture_digest": capture_digest,
            "approval_digest": digest(source_binding["approval"]), "plan_digest": plan["plan_digest"],
            "execution_policy_digest": policy["digest"]}},
    }
    with admissions._transaction() as db:
        admissions._save(db, operation)
    service = ApprovedReviewerBindings(admissions, candidates, store)
    return service, (run["id"], operation_id), {"project_id": project_id, "reviewer": reviewer, "source": source, "candidate": candidate}


def negative(private_root: Path, report: Path) -> None:
    service, args, facts = ensure_fixture(private_root)
    before = facts["source"]
    result = service.advance(*args, principal=FIXTURE_OWNER)
    assessment = result.get("assessment") or {}
    diagnostic: dict[str, Any]
    operation = service.admissions.get(*args, principal=FIXTURE_OWNER)
    try:
        with service._current(operation, FIXTURE_OWNER) as (project_db, run):
            compiled = service._compiled(project_db, run, operation, FIXTURE_OWNER)
        diagnostic = {"compiled": compiled["binding"] is not None, "reason_codes": compiled["reason_codes"],
                      "qualification_issues": compiled["assessment"]["qualification_issues"]}
    except Exception as error:
        diagnostic = {"exception_type": type(error).__name__, "reason_code": getattr(error, "code", None),
                      "detail": str(error)[:120]}
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps({
        "schema_version": "karajan.issue107-consumer-negative.v1", "issue": 107,
        "fixture": "controller_fixed_plan_candidate_only", "no_commander_or_worker_execution": True,
        "current_source": {"origin": before["observation_origin"], "suite_ref": before["suite_ref"], "source_digest": digest(before)},
        "result": {"state": result.get("state"), "reason_codes": result.get("reason_codes"),
                   "transition": result.get("transition"), "actual_reviewer_attempt": assessment.get("actual_reviewer_attempt")},
        "expected_revoke_reason_observed": "QUALIFICATION_REVOKED" in {row.get("reason_code") for row in assessment.get("qualification_issues", [])},
        "qualification_issues": assessment.get("qualification_issues"),
        "direct_consumer_diagnostic": diagnostic,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("negative", "inspect_config"))
    parser.add_argument("--private-root", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    if args.mode == "negative":
        negative(args.private_root, args.report)
    else:
        store, project_id, _reviewer, _journal = open_controller(args.private_root)
        project = store.projects.get(project_id)
        configuration = store.projects.get_configuration(project_id)["configuration"]
        preview_issues: Any = None
        with sqlite3.connect(paths(args.private_root)["state"]) as db:
            row = db.execute("SELECT result FROM previews WHERE id=?", (project["configuration"]["preview_id"],)).fetchone()
            preview_issues = None if row is None else json.loads(row[0]).get("issues")
            latest = db.execute("SELECT result FROM previews ORDER BY rowid DESC LIMIT 1").fetchone()
            latest_preview = None if latest is None else json.loads(latest[0])
        args.report.write_text(json.dumps({
            "project": {"revision": project["revision"], "configuration": project["configuration"]},
            "reviewer_in_group": {"id": "readonly-reviewer-107", "revision": 1}
            in configuration["rulebook"]["profile_groups"]["review_standard_qualified"],
            "preview_issues": preview_issues,
            "latest_preview": None if latest_preview is None else {
                "can_save_draft": latest_preview.get("can_save_draft"),
                "issues": latest_preview.get("issues"), "compile_issues": latest_preview.get("compile_issues"),
            },
        }, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
