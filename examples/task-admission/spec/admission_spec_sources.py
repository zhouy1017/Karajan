"""Independent persisted assessment checks; no model, admission or runner effect."""

import json
import subprocess
from pathlib import Path

from karajan.capacity import CapacityStore
from karajan.orchestration.routing import ApprovedRunRouting
from karajan.projects import ProjectRegistry
from karajan.projects.demand import AttemptEstimateStore
from karajan.projects.qualification import ProfileQualificationStore
from karajan.runs import RunPlanner

WORKTREE = next(
    parent for parent in Path(__file__).resolve().parents if (parent / "pyproject.toml").is_file()
)
REF = {"id": "fixture-profile", "revision": 1}


def seeded(tmp_path, *, normal=True, approve=True, custom_rule=False, renamed_commander=False):
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "README.md").write_text("Independent assessment fixture.\n")
    for arguments in (
        ["init", "--initial-branch=main", str(repository)],
        ["-C", str(repository), "add", "README.md"],
        [
            "-C",
            str(repository),
            "-c",
            "user.name=Spec",
            "-c",
            "user.email=spec@example.invalid",
            "commit",
            "-qm",
            "synthetic fixture",
        ],
    ):
        subprocess.run(["git", *arguments], capture_output=True, check=True)
    now = [1000.0]
    registry = ProjectRegistry(tmp_path / "projects.sqlite", [tmp_path], clock=lambda: now[0])
    created = registry.create(
        {
            "name": "Independent assessment Spec",
            "repository_path": str(repository),
            "base_ref": "main",
            "target_branch": "main",
            "allowed_target_branches": ["main"],
        },
        command_key="project",
        principal="owner",
    )
    config = json.loads(
        (WORKTREE / "examples/projects/offline-configuration.json").read_text(encoding="utf-8")
    )
    if renamed_commander:
        from karajan.routing import compile_rulebook

        config["rulebook"]["profile_groups"]["owner_lead_pool"] = config["rulebook"][
            "profile_groups"
        ].pop("commander_qualified")
        next(rule for rule in config["rulebook"]["rules"] if rule["id"] == "lead-planning")[
            "eligible_groups"
        ] = ["owner_lead_pool"]
        assert compile_rulebook(config["rulebook"])["issues"] == []
    preview = registry.preview_configuration(
        created["id"], config, command_key="preview", principal="owner"
    )
    project = registry.apply_configuration(
        created["id"],
        preview["preview_id"],
        expected_revision=1,
        command_key="apply",
        principal="owner",
    )
    if custom_rule:
        config["rulebook"]["revision"] = 2
        bounded = next(
            rule for rule in config["rulebook"]["rules"] if rule["id"] == "bounded-worker"
        )
        bounded["eligible_groups"] = ["fast_qualified"]
        rule_preview = registry.preview_rulebook(
            created["id"],
            config["rulebook"],
            expected_revision=project["revision"],
            command_key="custom-preview",
            principal="owner",
        )
        assert rule_preview["can_publish"] is True
        registry.publish_rulebook(
            created["id"],
            rule_preview["preview_id"],
            expected_revision=project["revision"],
            command_key="custom-publish",
            principal="owner",
        )
        project = registry.get(created["id"])
    hard = {
        "profile_refs": [REF],
        "channel_ids": ["fixture-channel"],
        "tools": ["fixture-tools"],
        "data_destinations": ["synthetic-local"],
        "required_capabilities": [],
        "min_isolation": "tool_sandboxed",
    }
    policy = registry.register_execution_policy(
        project["id"],
        {
            "schema_version": "karajan.execution-policy.v1",
            "id": "independent-policy",
            "revision": 1,
            "configuration_digest": project["configuration"]["digest"],
            "constraints": hard,
            "risk_policy": {
                "id": "independent-risk",
                "revision": 1,
                "mapping": {"standard": "T1", "critical": "T3"},
                "path_floors": [{"prefix": "src/auth", "minimum_class": "T3"}],
            },
            "channel_destinations": {"fixture-channel": "synthetic-local"},
            "tool_policy": {
                "id": "independent-tools",
                "revision": 1,
                "tool_permissions": {"fixture-tools": ["fixture-tools"]},
            },
            "context_policy": {
                "id": "independent-context",
                "revision": 1,
                "input_accounting": "explicit_approved_upper_bound",
                "reserved_output_tokens": 1024,
            },
            "max_context_tokens": 8192,
        },
        command_key="policy",
        principal="owner",
    )
    receipts = {}
    planner = RunPlanner(
        tmp_path / "runs.sqlite", registry, clock=lambda: now[0], admissions=receipts.__getitem__
    )
    run = planner.create(
        {
            "schema_version": "karajan.create-run.v2",
            "project_id": project["id"],
            "project_revision": project["revision"],
            "configuration_digest": project["configuration"]["digest"],
            "execution_policy": {key: policy[key] for key in ("id", "revision", "digest")},
            "requirement": {
                "goal": "Synthetic approved report",
                "acceptance": ["Public receipt preserves real sources"],
            },
            "participants": [{"principal": "lead", "profile": REF, "purpose": "lead"}],
            "authorization": {
                **hard,
                "read_paths": ["src", "tests"],
                "write_paths": ["src", "tests"],
                "budget_ref": "run",
                "checks": ["tests", "independent_review"],
                "delivery": "pull_request",
                "target_branch": "main",
                "currency_limits": {"USD": "0", "CNY": "0"},
                "max_attempt_duration_seconds": 25,
                "max_quality_repair_rounds": 2,
                "stage_permissions": {
                    "bounded-worker": {"normal": normal, "quality_indices": [0]},
                    "mechanical-worker": {"normal": True, "quality_indices": []},
                    "standard-review": {"normal": True, "quality_indices": []},
                },
            },
        },
        command_key="run",
        principal="owner",
    )
    intent = planner.planning_intent(run["id"], term=1, command_key="intent", principal="lead")
    receipt_ref = "synthetic-planning-admission"
    receipts[receipt_ref] = {
        "receipt_ref": receipt_ref,
        "authority_revision": "spec-fixture",
        **{
            key: intent[key]
            for key in ("run_id", "intent_id", "term", "principal", "profile", "budget_ref")
            if key in intent
        },
        "intent_id": intent["id"],
        "state": "admitted",
        "provenance": "fixture",
    }
    planner.attach_planning_receipt(
        run["id"], intent["id"], receipt_ref=receipt_ref, command_key="attach", principal="owner"
    )
    task = {
        "id": "implement",
        "revision": 1,
        "role": "worker",
        "readiness": "ready",
        "complexity": "T2",
        "risk": "standard",
        "paths": ["src/report.py"],
        "depends_on": [],
        "acceptance": ["Synthetic report passes"],
        "required": True,
        "purpose": None,
        "domains": ["report"],
        "required_capabilities": [],
        "tools": ["fixture-tools"],
        "context_tokens": 3072,
        "duration_seconds": 21,
    }
    proposal = {
        "schema_version": "karajan.submit-plan.v2",
        "term": 1,
        "intent_id": intent["id"],
        "expected_plan_revision": 0,
        "plan": {
            "summary": "Synthetic independent assessment",
            "authorization": run["authorization_ceiling"],
            "tasks": [
                task,
                {**task, "id": "review", "role": "reviewer", "depends_on": ["implement"]},
            ],
        },
    }
    plan = planner.submit_plan(run["id"], proposal, command_key="plan", principal="lead")
    if approve:
        planner.approve_plan(run["id"], approval(plan), command_key="approve", principal="owner")
    capacity = CapacityStore(tmp_path / "capacity.sqlite", clock=lambda: now[0])
    capacity.register_pool(
        {
            "id": "service-fixture",
            "account_id": "fixture-account",
            "kind": "service",
            "unit": "percent",
            "window_kind": "fixed",
        },
        command_key="pool",
    )
    capacity.register_profile(
        {
            "id": "fixture-profile",
            "revision": 1,
            "account_id": "fixture-account",
            "pool_ids": ["service-fixture"],
        },
        command_key="profile",
    )
    capacity.activate_policy(
        {
            "account_id": "fixture-account",
            "max_active_attempts": 4,
            "max_attempt_duration_seconds": 60,
            "observation_max_age_seconds": 60,
            "require_official_observation": False,
            "safety_margin": {},
            "lead_reserve": {"service-fixture": "5"},
            "lead_reserved_slots": 1,
            "conservative_mode": {
                "enabled": True,
                "max_local_active_attempts": 4,
                "max_attempt_duration_seconds": 60,
                "observation_max_age_seconds": 60,
                "cooldown_seconds": 10,
            },
        },
        expected_revision=0,
        command_key="capacity-policy",
    )
    qualifications = ProfileQualificationStore(registry, clock=lambda: now[0])
    estimates = AttemptEstimateStore(planner, clock=lambda: now[0])
    case = {
        "planner": planner,
        "run": run,
        "plan": plan,
        "proposal": proposal,
        "registry": registry,
        "config": config,
        "capacity": capacity,
        "estimates": estimates,
        "qualifications": qualifications,
        "now": now,
        "project": project["id"],
    }
    observe(case)
    case["service"] = ApprovedRunRouting(planner, qualifications, capacity, estimates=estimates)
    return case


def approval(plan):
    return {
        "schema_version": "karajan.approve-plan.v2",
        **{
            key: plan[key]
            for key in (
                "term",
                "plan_revision",
                "plan_digest",
                "authorization_digest",
                "configuration_digest",
                "routing_digest",
            )
        },
    }


def assess(case, key="assessment", task="implement"):
    return case["service"].assess(case["run"]["id"], task, principal="owner", command_key=key)


def observe(case, *, metric="remaining", amount="40", covered=None):
    return case["capacity"].observe(
        {
            "pool_id": "service-fixture",
            "window_id": "fixed-current",
            "observed_at": case["now"][0],
            "reset_at": 1200.0,
            "source": "fixture",
            "source_ref": "spec-observation-" + str(case["now"][0]),
            "metric": metric,
            "amount": amount,
            "limit": "100",
            "covered_usage_ids": covered or [],
            "coverage_ref": "attributed-cover" if covered else None,
        },
        command_key="observe-" + str(case["now"][0]),
    )


def estimate(case):
    return case["estimates"].register(
        case["run"]["id"],
        "implement",
        REF,
        {
            "id": "explicit-prediction",
            "revision": 1,
            "source_kind": "owner_conservative_estimate",
            "validity_seconds": 900,
            "measurement_semantics": "window_independent_attempt",
            "demand": [
                {
                    "pool_id": "service-fixture",
                    "unit": "percent",
                    "window_kind": "fixed",
                    "amount": "7.25",
                }
            ],
            "completion_seconds": None,
            "basis": "Synthetic explicit prediction; no provider calibration",
        },
        principal="owner",
        command_key="estimate",
    )
