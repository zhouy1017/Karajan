"""Seed explicit synthetic v2/v1 approval data using public persisted commands.

No Git mutation, credentials, provider calls, qualification grant or dispatch.
The repository must be an existing fixture supplied explicitly by the caller.
"""

import argparse
import copy
import json
from pathlib import Path

from karajan.projects import ProjectRegistry
from karajan.runs import RunPlanner
from karajan.runs import planning as planning_module

WORKTREE = Path(__file__).resolve().parents[3]


def verify_imports():
    assert Path.cwd().resolve() == WORKTREE
    assert Path(planning_module.__file__).resolve().is_relative_to(WORKTREE / "backend")


def policy_input(configured):
    return {
        "schema_version": "karajan.execution-policy.v1",
        "id": "synthetic-ui-policy",
        "revision": 1,
        "configuration_digest": configured["configuration"]["digest"],
        "constraints": {
            "profile_refs": [{"id": "fixture-profile", "revision": 1}],
            "channel_ids": ["fixture-channel"],
            "tools": ["fixture-tools"],
            "data_destinations": ["synthetic-local-fixture"],
            "required_capabilities": ["controlled_tools"],
            "min_isolation": "tool_sandboxed",
        },
        "risk_policy": {
            "id": "synthetic-ui-risk",
            "revision": 1,
            "mapping": {"standard": "T1", "critical": "T3"},
            "path_floors": [{"prefix": "src/auth", "minimum_class": "T3"}],
        },
        "channel_destinations": {"fixture-channel": "synthetic-local-fixture"},
        "tool_policy": {
            "id": "synthetic-ui-tools",
            "revision": 1,
            "tool_permissions": {"fixture-tools": ["fixture-tools"]},
        },
        "context_policy": {
            "id": "synthetic-ui-context",
            "revision": 1,
            "input_accounting": "explicit_approved_upper_bound",
            "reserved_output_tokens": 1024,
        },
        "max_context_tokens": 8192,
    }


def creation_input(project, policy=None):
    profile = {"id": "fixture-profile", "revision": 1}
    request = {
        "project_id": project["id"],
        "project_revision": project["revision"],
        "configuration_digest": project["configuration"]["digest"],
        "requirement": {
            "goal": "SYNTHETIC v1 兼容审批示例",
            "acceptance": ["仅验证页面与持久批准，不执行工具"],
        },
        "participants": [
            {"principal": "synthetic-lead", "profile": profile, "purpose": "lead"},
            {"principal": "synthetic-next", "profile": profile, "purpose": "candidate"},
        ],
        "authorization": {
            "profile_refs": [profile],
            "read_paths": ["src", "tests"],
            "write_paths": ["src", "tests"],
            "budget_ref": "run",
            "checks": ["tests", "independent_review"],
            "delivery": "pull_request",
            "target_branch": "main",
        },
    }
    if policy is not None:
        request["schema_version"] = "karajan.create-run.v2"
        request["requirement"]["goal"] = "SYNTHETIC v2 完整授权审批示例"
        request["execution_policy"] = {key: policy[key] for key in ("id", "revision", "digest")}
        request["authorization"].update(
            channel_ids=["fixture-channel"],
            tools=["fixture-tools"],
            data_destinations=["synthetic-local-fixture"],
            required_capabilities=["controlled_tools"],
            min_isolation="tool_sandboxed",
            currency_limits={"USD": "0", "CNY": "0"},
            max_attempt_duration_seconds=25,
            max_quality_repair_rounds=2,
            stage_permissions={
                "bounded-worker": {"normal": True, "quality_indices": [0]},
                "standard-review": {"normal": True, "quality_indices": []},
                "mechanical-worker": {"normal": False, "quality_indices": []},
            },
        )
    return request


def proposal_input(run, intent):
    request = {
        "term": 1,
        "intent_id": intent["id"],
        "expected_plan_revision": 0,
        "plan": {
            "summary": "合成审批样例：固定当前权限，保留独立审查与用户合并决定。",
            "authorization": copy.deepcopy(run["authorization_ceiling"]),
            "tasks": [
                {
                    "id": "implement",
                    "revision": 1,
                    "role": "worker",
                    "readiness": "ready",
                    "complexity": "T2",
                    "risk": "standard",
                    "paths": ["src/report.py"],
                    "depends_on": [],
                    "acceptance": ["合成报告符合明确需求"],
                    "required": True,
                },
                {
                    "id": "review",
                    "revision": 1,
                    "role": "reviewer",
                    "readiness": "ready",
                    "complexity": "T2",
                    "risk": "standard",
                    "paths": ["src/report.py"],
                    "depends_on": ["implement"],
                    "acceptance": ["独立审查通过"],
                    "required": True,
                },
            ],
        },
    }
    request["plan"]["authorization"]["write_paths"] = ["src"]
    if run["schema_version"] == "karajan.run-planning.v2":
        request["schema_version"] = "karajan.submit-plan.v2"
        for task in request["plan"]["tasks"]:
            task.update(
                purpose=None,
                domains=["code", "synthetic-report"],
                tools=["fixture-tools"],
                required_capabilities=["controlled_tools"],
                context_tokens=4096,
                duration_seconds=20,
            )
    return request


def approval_input(plan, *, v2=True):
    keys = ("term", "plan_revision", "plan_digest", "authorization_digest", "configuration_digest")
    result = {key: plan[key] for key in keys}
    if v2:
        result.update(
            schema_version="karajan.approve-plan.v2", routing_digest=plan["routing_digest"]
        )
    return result


def seed(directory, repository):
    verify_imports()
    directory, repository = directory.resolve(), repository.resolve(strict=True)
    assert directory.is_relative_to(WORKTREE / ".cache/v2-ui-spec")
    assert not directory.is_relative_to(repository)
    directory.mkdir(parents=True, exist_ok=False)
    registry = ProjectRegistry(directory / "projects.sqlite", [repository])
    project = registry.create(
        {
            "name": "SYNTHETIC v2 审批独立验收",
            "repository_path": str(repository),
            "base_ref": "main",
            "target_branch": "main",
            "allowed_target_branches": ["main"],
        },
        command_key="spec-project",
        principal="owner",
    )
    configuration = json.loads(
        (WORKTREE / "examples/projects/offline-configuration.json").read_text(encoding="utf-8")
    )
    preview = registry.preview_configuration(
        project["id"], configuration, command_key="spec-preview", principal="owner"
    )
    configured = registry.apply_configuration(
        project["id"],
        preview["preview_id"],
        expected_revision=1,
        command_key="spec-apply",
        principal="owner",
    )
    policy = registry.register_execution_policy(
        configured["id"], policy_input(configured), command_key="spec-policy", principal="owner"
    )
    receipts = {}
    planner = RunPlanner(directory / "runs.sqlite", registry, admissions=receipts.__getitem__)
    records = {}
    for version in ("v2", "v1"):
        creation = creation_input(configured, policy if version == "v2" else None)
        run = planner.create(creation, command_key="spec-run-" + version, principal="owner")
        intent = planner.planning_intent(
            run["id"], term=1, command_key="spec-intent-" + version, principal="synthetic-lead"
        )
        reference = "synthetic-ui-receipt-" + version
        receipts[reference] = {
            "receipt_ref": reference,
            "authority_revision": "synthetic-ui-authority-v1",
            "run_id": run["id"],
            "intent_id": intent["id"],
            "term": 1,
            "principal": "synthetic-lead",
            "profile": intent["profile"],
            "budget_ref": intent["budget_ref"],
            "state": "admitted",
            "provenance": "fixture",
        }
        planner.attach_planning_receipt(
            run["id"],
            intent["id"],
            receipt_ref=reference,
            command_key="spec-receipt-" + version,
            principal="owner",
        )
        proposal = proposal_input(run, intent)
        plan = planner.submit_plan(
            run["id"], proposal, command_key="spec-plan-" + version, principal="synthetic-lead"
        )
        records[version] = {
            "run_id": run["id"],
            "intent": intent,
            "creation": creation,
            "proposal": proposal,
            "plan": plan,
            "approval": approval_input(plan, v2=version == "v2"),
        }
        stored = planner.get(run["id"], principal="owner")
        assert stored["active_plan_revision"] is None and stored["dispatch_enabled"] is False
        assert stored["live_qualification"] == "not_run"
    manifest = {
        "scope": "synthetic-ui-fixture-only",
        "state_directory": str(directory),
        "repository": str(repository),
        "project_id": configured["id"],
        "owner": "owner",
        "no_provider_calls": True,
        "cash_limits": {"USD": "0", "CNY": "0"},
        "profile_evidence_provenance": "fixture",
        "dispatch_enabled": False,
        "policy": policy,
        "receipts": receipts,
        "runs": records,
        "import_path": planning_module.__file__,
    }
    (directory / "seed.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    # Recognized local-workbench directory marker, containing no credentials.
    (directory / "karajan-local-state.v1").write_text("karajan-local-state.v1\n", encoding="utf-8")
    return manifest


def advance(directory):
    verify_imports()
    manifest = json.loads((directory / "seed.json").read_text(encoding="utf-8"))
    registry = ProjectRegistry(directory / "projects.sqlite", [Path(manifest["repository"])])
    planner = RunPlanner(
        directory / "runs.sqlite", registry, admissions=manifest["receipts"].__getitem__
    )
    row = manifest["runs"]["v2"]
    request = copy.deepcopy(row["proposal"])
    request["expected_plan_revision"] = 1
    request["plan"]["authorization"]["max_attempt_duration_seconds"] = 24
    request["plan"]["authorization"]["stage_permissions"]["bounded-worker"]["quality_indices"] = []
    plan = planner.submit_plan(
        row["run_id"], request, command_key="spec-advance-v2", principal="synthetic-lead"
    )
    return {"run_id": row["run_id"], "plan": plan, "approval": approval_input(plan)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["seed", "advance"])
    parser.add_argument("--state-directory", type=Path, required=True)
    parser.add_argument("--repository", type=Path)
    args = parser.parse_args()
    if args.operation == "seed":
        if args.repository is None:
            parser.error("seed requires an explicit --repository fixture")
        output = seed(args.state_directory, args.repository)
        print(
            json.dumps(
                {
                    key: output[key]
                    for key in ("scope", "state_directory", "project_id", "owner", "import_path")
                },
                ensure_ascii=False,
            )
        )
        print(json.dumps({version: data["run_id"] for version, data in output["runs"].items()}))
    else:
        print(json.dumps(advance(args.state_directory), ensure_ascii=False))
