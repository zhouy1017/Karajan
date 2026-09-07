"""Public-store Checks integration fixture; planning/native author are explicit doubles.

Each returned subject is an actual CandidateStore CAS freeze from a newly
registered owner policy and approved Run. No operation/approval/Candidate JSON
is edited. The seed Run only establishes the existing synthetic qualification
configuration; it does not execute or supply this subject's approval.
"""

import time
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from karajan.candidates import CandidateStore
from karajan.orchestration.admission import ApprovedTaskAdmission
from karajan.orchestration.go_task_collector import ApprovedGoCollector
from karajan.orchestration.routing import ApprovedRunRouting
from karajan.orchestration.workspace import ApprovedTaskWorkspace
from karajan.projects.demand import AttemptEstimateStore
from karajan.projects.execution_policy import ExecutionPolicyV2
from karajan.runs import RunPlanner
from test_approved_routing_capacity import capacity_for_plan
from test_go_task_collector import captured_case, collection_case
from test_planning import ScriptedAdmissionReader
from test_projected_go_routing import approved_task
from test_routing_authorization import approve_request, request_v2, submit_request
from test_task_workspace import git


@dataclass
class ApprovedCheckCase:
    admissions: ApprovedTaskAdmission
    candidates: CandidateStore
    args: tuple[str, str]
    candidate: dict[str, Any]
    repository: Path
    environment: dict[str, Any]


def approved_check_candidate(
    projected: dict[str, Any],
    directory: Path,
    *,
    environment: dict[str, Any],
    checks: list[dict[str, Any]],
    worker_output: bytes = b"print('collected')\n",
) -> ApprovedCheckCase:
    """Register supplied environment/argv through owner APIs before any capture."""
    directory.mkdir(parents=True, exist_ok=True)
    repository = projected["repository"]
    for name, body in {
        "src/report.py": b"print('approved task')\n",
        "src/reference.txt": b"Reference contract\n",
        "tests/test_report.py": b"assert True\n",
        "docs/private.txt": b"Unprojected baseline file\n",
    }.items():
        path = repository / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    git(repository, "add", ".")
    git(
        repository,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-qm",
        "checks baseline",
    )
    projects = projected["projects"]
    configured = projects.get(projected["project_id"])
    projects.update(
        configured["id"],
        {
            "name": configured["name"],
            "base_ref": "HEAD",
            "target_branch": "main",
            "allowed_target_branches": ["main"],
        },
        expected_revision=configured["revision"],
        command_key="checks-base",
        principal="owner",
    )
    (directory / "seed").mkdir()
    _, _, seed, _ = approved_task(projected, directory / "seed")
    policy = {
        key: deepcopy(seed["execution_policy_snapshot"][key])
        for key in ExecutionPolicyV2.model_fields
    }
    policy.update(id="checks-execution", revision=1)
    reference = {key: environment[key] for key in ("id", "revision")}
    policy["validation"] = {
        "id": "checks-validation",
        "revision": 1,
        "checks": deepcopy(checks),
        "environments": [deepcopy(environment)],
        "review": {
            "id": "independent_review",
            "revision": 2,
            "environment_ref": reference,
            "context_policy": "candidate_and_acceptance_only",
            "independence_policy": "existing_candidate_independence_v1",
        },
    }
    registered = projects.register_execution_policy(
        seed["project_id"], policy, principal="owner", command_key="checks-policy"
    )
    authority = ScriptedAdmissionReader()
    planner = RunPlanner(directory / "runs.sqlite", projects, admissions=authority, clock=time.time)
    creation = request_v2(projects.get(seed["project_id"]), registered)
    creation["authorization"] = deepcopy(seed["authorization_ceiling"])
    creation["authorization"]["checks"] = [row["id"] for row in checks] + ["independent_review"]
    run = planner.create(creation, principal="owner", command_key="checks-run")
    intent = planner.planning_intent(
        run["id"], term=1, principal="lead", command_key="checks-intent"
    )
    planner.attach_planning_receipt(
        run["id"],
        intent["id"],
        receipt_ref=authority.grant(intent),
        principal="owner",
        command_key="checks-receipt",
    )
    submission = submit_request(run, intent)
    for task in submission["plan"]["tasks"]:
        task.update(tools=["read", "edit"], complexity="T1", risk="standard", context_tokens=6000)
    plan = planner.submit_plan(run["id"], submission, principal="lead", command_key="checks-plan")
    planner.approve_plan(
        run["id"], approve_request(plan), principal="owner", command_key="checks-approve"
    )
    config = run["configuration_snapshot"]["configuration"]
    capacity = capacity_for_plan(directory, config)
    # The explicitly synthetic quota/qualification producers use this shared
    # fixture epoch. The actual Check Run clock above remains real wall time.
    estimates = AttemptEstimateStore(planner, clock=lambda: 1000.0)
    estimates.register(
        run["id"],
        "implement",
        {"id": "fixture-profile", "revision": 1},
        {
            "id": "checks-worker-estimate",
            "revision": 1,
            "source_kind": "owner_conservative_estimate",
            "validity_seconds": 600,
            "measurement_semantics": "window_independent_attempt",
            "demand": [
                {"pool_id": pool["id"], "unit": pool["unit"], "window_kind": "fixed", "amount": "3"}
                for pool in config["resources"]["quota_pools"]
            ],
            "completion_seconds": None,
            "basis": "Synthetic author only; no real provider calls.",
        },
        principal="owner",
        command_key="checks-estimate",
    )
    routing = ApprovedRunRouting(planner, projected["store"], capacity, estimates=estimates)
    admissions = ApprovedTaskAdmission(directory / "task-admissions.sqlite", routing)
    queued = admissions.enqueue(
        run["id"], "implement", principal="owner", command_key="checks-worker"
    )
    admitted = admissions.advance(run["id"], queued["id"], principal="owner")
    assert admitted["state"] == "reserved", [
        row["reason_codes"] for row in admitted["assessment"]["route"]["candidates"]
    ]
    candidates = CandidateStore(directory / "candidates")
    workspace = ApprovedTaskWorkspace(admissions, candidates).prepare(
        run["id"], admitted["id"], principal="owner"
    )
    # Reuse the existing explicit synthetic producer fixture, not a mutable
    # product record. The actual Collector compiles and freezes the result.
    captured = captured_case.__wrapped__(
        (workspace, candidates, repository, admissions, routing), directory
    )
    service, args, candidates, journal, result = collection_case.__wrapped__(captured, directory)
    result = replace(
        result,
        capture=replace(
            result.capture,
            files=tuple(
                (name, worker_output if name == "src/report.py" else body)
                for name, body in result.capture.files
            ),
        ),
    )
    receipt = ApprovedGoCollector(service, candidates, journal, source_check=lambda: None).collect(
        *args, principal="owner", runner=service.host.runner, result=result
    )
    return ApprovedCheckCase(
        admissions, candidates, args, receipt, repository, deepcopy(environment)
    )
