"""Explicit synthetic planning and fixed local processes; never dispatch a real model."""

import argparse
import hashlib
import json
import os
import platform
import subprocess
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from karajan.candidates import CandidateStore
from karajan.execution import RunnerHost
from karajan.orchestration import LocalFixtureRunner, SerialCoordinator
from karajan.projects import ProjectRegistry
from karajan.runs import RunPlanner


def setup(
    directory: Path, *, approved: bool = True
) -> tuple[RunPlanner, RunnerHost, CandidateStore, dict[str, Any]]:
    fixture = directory / "fixture"
    repository = fixture / "repository"
    repository.mkdir(parents=True)
    (repository / "original.txt").write_text("trusted baseline\n", encoding="utf-8")
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}
    }
    environment.update(
        GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0"
    )
    for arguments in (
        ["init", "-q", "--initial-branch=main"],
        ["add", "."],
        ["commit", "-qm", "fixture"],
    ):
        subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "-c",
                "commit.gpgsign=false",
                *arguments,
            ],
            check=True,
            capture_output=True,
            env=environment,
            timeout=15,
        )
    projects = ProjectRegistry(directory / "projects.sqlite", [fixture])
    project = projects.create(
        {
            "name": "Offline serial probe",
            "repository_path": str(repository),
            "base_ref": "HEAD",
            "target_branch": "main",
            "allowed_target_branches": ["main"],
        },
        command_key="project",
        principal="owner",
    )
    configuration_path = Path(__file__).parents[1] / "projects/offline-configuration.json"
    configuration = json.loads(configuration_path.read_bytes())
    preview = projects.preview_configuration(
        project["id"], configuration, command_key="preview", principal="owner"
    )
    project = projects.apply_configuration(
        project["id"],
        preview["preview_id"],
        expected_revision=1,
        command_key="apply",
        principal="owner",
    )
    # This source is deliberately confined to this example. The product planner
    # receives authority receipts; an example dictionary never grants live admission.
    receipts: dict[str, dict[str, Any]] = {}
    planner = RunPlanner(directory / "runs.sqlite", projects, admissions=receipts.__getitem__)
    reference = {"id": "fixture-profile", "revision": 1}
    run = planner.create(
        {
            "project_id": project["id"],
            "project_revision": project["revision"],
            "configuration_digest": project["configuration"]["digest"],
            "requirement": {
                "goal": "Synthetic report",
                "acceptance": ["Fixed local checks and independent process pass"],
            },
            "participants": [{"principal": "lead", "profile": reference, "purpose": "lead"}],
            "authorization": {
                "profile_refs": [reference],
                "read_paths": ["src"],
                "write_paths": ["src"],
                "budget_ref": "run",
                "checks": ["tests", "independent_review"],
                "delivery": "pull_request",
                "target_branch": "main",
            },
        },
        command_key="run",
        principal="owner",
    )
    intent = planner.planning_intent(run["id"], term=1, command_key="intent", principal="lead")
    receipts["fixture-planning"] = {
        "receipt_ref": "fixture-planning",
        "authority_revision": "example-fixture-v1",
        "run_id": run["id"],
        "intent_id": intent["id"],
        "term": 1,
        "principal": "lead",
        "profile": reference,
        "budget_ref": "planning",
        "state": "admitted",
        "provenance": "fixture",
    }
    planner.attach_planning_receipt(
        run["id"],
        intent["id"],
        receipt_ref="fixture-planning",
        command_key="receipt",
        principal="owner",
    )
    tasks = [
        {
            "id": identity,
            "revision": 1,
            "role": role,
            "readiness": "ready",
            "complexity": "T2",
            "risk": "standard",
            "paths": ["src/report.py"],
            "depends_on": dependencies,
            "acceptance": ["Synthetic fixture passes"],
            "required": True,
        }
        for identity, role, dependencies in [
            ("implement", "worker", []),
            ("review", "reviewer", ["implement"]),
        ]
    ]
    plan = planner.submit_plan(
        run["id"],
        {
            "term": 1,
            "intent_id": intent["id"],
            "expected_plan_revision": 0,
            "plan": {
                "summary": "Explicit synthetic planning result",
                "authorization": run["authorization_ceiling"],
                "tasks": tasks,
            },
        },
        command_key="plan",
        principal="lead",
    )
    if approved:
        planner.approve_plan(
            run["id"],
            {
                key: plan[key]
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
    return (
        planner,
        RunnerHost(directory / "runnerhost"),
        CandidateStore(directory / "candidates"),
        run,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument(
        "--scenario",
        choices=[
            "success",
            "production_blocked",
            "unapproved",
            "check_failed",
            "review_inconclusive",
        ],
        required=True,
    )
    args = parser.parse_args()
    directory = args.directory.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    planner, host, candidates, run = setup(directory, approved=args.scenario != "unapproved")
    runner = (
        None
        if args.scenario == "production_blocked"
        else LocalFixtureRunner(
            directory / "fixture",
            check_outcome="fail" if args.scenario == "check_failed" else "pass",
            review_verdict="inconclusive" if args.scenario == "review_inconclusive" else "passed",
        )
    )
    coordinator = SerialCoordinator(
        directory / "orchestration", planner, host, candidates, fixture_runner=runner
    )
    transitions = []
    result: dict[str, Any] = {}
    cleanup = []
    try:
        coordinator.enqueue(
            run["id"],
            "implement",
            profile_ref={"id": "fixture-profile", "revision": 1},
            command_key="worker",
            principal="owner",
        )
        until = time.monotonic() + 20
        review_queued = False
        while time.monotonic() < until:
            result = coordinator.advance(run["id"])
            if not transitions or transitions[-1] != result["state"]:
                transitions.append(result["state"])
            if result["state"] == "awaiting_review" and not review_queued:
                coordinator.enqueue(
                    run["id"],
                    "review",
                    profile_ref={"id": "fixture-profile", "revision": 1},
                    command_key="review",
                    principal="owner",
                )
                review_queued = True
            elif result["state"] in {"local_gate_passed", "blocked"}:
                break
            time.sleep(0.02)
    finally:
        for observed in host.reconcile():
            stopped = host.cancel(
                observed.attempt_id, "probe-cleanup:" + observed.attempt_id, timeout_seconds=1
            )
            cleanup.append(asdict(stopped.snapshot))
    candidate_id = result.get("tasks", {}).get("implement", {}).get("candidate_id")
    sources = Path(__file__).parents[2] / "backend/karajan/orchestration"
    expected_reason = {
        "production_blocked": "LIVE_QUALIFICATION_NOT_RUN",
        "unapproved": "TASK_SCOPE_NOT_APPROVED",
        "check_failed": "CHECK_NOT_PASSED",
        "review_inconclusive": "REVIEW_NOT_PASSED",
    }.get(args.scenario)
    expected = (
        result.get("state") == "local_gate_passed"
        if expected_reason is None
        else (
            result.get("state") == "blocked" and expected_reason in result.get("reason_codes", [])
        )
    )
    passed = expected and all(row["state"] == "exited" for row in cleanup)
    report = {
        "schema_version": "karajan.serial-probe.v1",
        "status": "passed" if passed else "failed",
        "scenario": args.scenario,
        "observed_at": datetime.now(UTC).isoformat(),
        "os": platform.system(),
        "python_version": platform.python_version(),
        "source_sha256": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(sources.glob("*.py"))
        },
        "probe_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "cash_api_calls": 0,
        "real_model_calls": 0,
        "live_qualification": "not_run",
        "planning_source": "explicit_example_fixture_receipt",
        "review_source": "separate_fixed_process",
        "snapshot": result,
        "transitions": transitions,
        "cleanup": cleanup,
        "candidate": candidates.get(candidate_id) if candidate_id else None,
        "limitations": [
            "No real model, quota admission, sandbox, or delivery qualification.",
            "Cross-database approval/activation is not an atomic revocation protocol.",
        ],
    }
    (directory / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "status": report["status"],
                "state": result.get("state"),
                "report": str(directory / "report.json"),
            }
        )
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
