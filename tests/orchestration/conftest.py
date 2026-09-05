"""Synthetic planning authority and real local repositories, without model access."""

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from karajan.candidates import CandidateStore
from karajan.execution import RunnerHost
from karajan.projects import ProjectRegistry
from karajan.runs import RunPlanner


class PlanningAuthority:
    def __init__(self) -> None:
        self.receipts: dict[str, dict[str, Any]] = {}

    def __call__(self, reference: str) -> dict[str, Any]:
        return self.receipts[reference]


@pytest.fixture
def case(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[dict[str, Any]]:
    fixture_root = tmp_path / "fixture"
    fixture_root.mkdir()
    repository = fixture_root / "repository"
    repository.mkdir()
    (repository / "original.txt").write_text("trusted baseline\n", encoding="utf-8")
    for args in (
        ["init", str(repository)],
        ["-C", str(repository), "add", "."],
        [
            "-C",
            str(repository),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
    ):
        subprocess.run(["git", *args], capture_output=True, check=True)
    projects = ProjectRegistry(tmp_path / "projects.sqlite", [fixture_root])
    project = projects.create(
        {
            "name": "Serial fixture",
            "repository_path": str(repository),
            "base_ref": "HEAD",
            "target_branch": "main",
            "allowed_target_branches": ["main"],
        },
        command_key="project",
        principal="owner",
    )
    configuration = json.loads(
        (Path(__file__).parents[2] / "examples/projects/offline-configuration.json").read_text()
    )
    variant = getattr(request, "param", None)
    if variant == "short_run":
        configuration["resources"]["budgets"][1]["max_duration_seconds"] = 1
    if variant == "one_attempt":
        configuration["resources"]["budgets"][1]["max_total_attempts"] = 1
    preview = projects.preview_configuration(
        project["id"], configuration, command_key="preview", principal="owner"
    )
    project = projects.apply_configuration(
        project["id"],
        preview["preview_id"],
        expected_revision=1,
        command_key="configure",
        principal="owner",
    )
    authority = PlanningAuthority()
    planner = RunPlanner(tmp_path / "runs.sqlite", projects, admissions=authority)
    profile = {"id": "fixture-profile", "revision": 1}
    run = planner.create(
        {
            "project_id": project["id"],
            "project_revision": project["revision"],
            "configuration_digest": project["configuration"]["digest"],
            "requirement": {
                "goal": "Write a repeatable report",
                "acceptance": ["Independent checks pass"],
            },
            "participants": [
                {"principal": "lead", "profile": profile, "purpose": "lead"},
                {"principal": "replacement", "profile": profile, "purpose": "candidate"},
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
        },
        command_key="create",
        principal="owner",
    )
    intent = planner.planning_intent(run["id"], term=1, command_key="intent", principal="lead")
    authority.receipts["fixture-planning"] = {
        "receipt_ref": "fixture-planning",
        "authority_revision": "fixture-v1",
        "run_id": run["id"],
        "intent_id": intent["id"],
        "term": 1,
        "principal": "lead",
        "profile": profile,
        "budget_ref": "planning",
        "state": "admitted",
        "provenance": "fixture",
    }
    planner.attach_planning_receipt(
        run["id"],
        intent["id"],
        receipt_ref="fixture-planning",
        command_key="attach",
        principal="owner",
    )
    plan = planner.submit_plan(
        run["id"],
        {
            "term": 1,
            "intent_id": intent["id"],
            "expected_plan_revision": 0,
            "plan": {
                "summary": "Implement and independently review",
                "authorization": run["authorization_ceiling"],
                "tasks": [
                    {
                        "id": "implement",
                        "revision": 1,
                        "role": "worker",
                        "readiness": "ready",
                        "complexity": "T3" if variant == "same_family_t3" else "T2",
                        "risk": "standard",
                        "paths": ["src/report.py", "src/second.py"]
                        if variant == "review_subset"
                        else ["src/report.py"],
                        "depends_on": [],
                        "acceptance": ["Report is repeatable"],
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
                        "acceptance": ["Independent review passes"],
                        "required": True,
                    },
                ]
                + (
                    [
                        {
                            "id": "second-worker",
                            "revision": 1,
                            "role": "worker",
                            "readiness": "ready",
                            "complexity": "T2",
                            "risk": "standard",
                            "paths": ["src/second.py"],
                            "depends_on": [],
                            "acceptance": ["Second result is checked"],
                            "required": True,
                        }
                    ]
                    if variant == "unfinished_worker"
                    else []
                ),
            },
        },
        command_key="plan",
        principal="lead",
    )
    state = {
        "root": tmp_path,
        "fixture_root": fixture_root,
        "repository": repository,
        "projects": projects,
        "planner": planner,
        "run": run,
        "plan": plan,
        "profile": profile,
        "host": RunnerHost(tmp_path / "runnerhost"),
        "candidates": CandidateStore(tmp_path / "candidates"),
        "authority": authority,
    }
    yield state
    for observed in state["host"].reconcile():
        state["host"].cancel(
            observed.attempt_id, "cleanup:" + observed.attempt_id, timeout_seconds=1
        )


def approve(case: dict[str, Any]) -> None:
    case["planner"].approve_plan(
        case["run"]["id"],
        {
            key: case["plan"][key]
            for key in (
                "term",
                "plan_revision",
                "plan_digest",
                "authorization_digest",
                "configuration_digest",
            )
        },
        command_key="approve",
        principal="owner",
    )
