"""Linux-only direct-child fixture for the Reviewer observer claim.

This is deliberately a test entry point, never a production bootstrap.  Its
single JSON port supplies the C-only frozen qualification facts; all stores,
Host registration, and observer claim code are the real implementations.
"""
# ruff: noqa: E402

from __future__ import annotations

import json
import os
import sys
from copy import deepcopy
from pathlib import Path

# `-I` intentionally ignores PYTHONPATH.  The test launcher fixes the source
# root by file location, rather than accepting it from argv or the environment.
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from karajan.candidates import CandidateStore
from karajan.capacity import CapacityStore
from karajan.execution import RunnerHost
from karajan.orchestration.admission import ApprovedTaskAdmission
from karajan.orchestration.reviewer_binding import ApprovedReviewerBindings
from karajan.orchestration.reviewer_execution_intent import (
    ReviewerExecutionIntents,
    ReviewerExecutionSource,
    ReviewerLaunchSpec,
)
from karajan.orchestration.routing import ApprovedRunRouting
from karajan.projects import ProjectRegistry
from karajan.projects.demand import AttemptEstimateStore
from karajan.projects.qualification import ProfileQualificationStore
from karajan.runs import RunPlanner


class FrozenQualificationPort(ProfileQualificationStore):
    """Test-only source: replay the parent's exact current facts, immutably."""

    def __init__(self, projects: ProjectRegistry, facts: dict[object, object]) -> None:
        super().__init__(projects)
        self._frozen = deepcopy(facts)

    def _facts(self, db, project_id, registration, scope, fixture_root):
        return deepcopy(self._frozen)


def _service(directory: Path) -> ReviewerExecutionIntents:
    port = json.loads((directory / "reviewer-execution-test-port.json").read_text())
    stores = port["stores"]

    def project_clock():
        return port["project_clock"]

    def planner_clock():
        return port["planner_clock"]

    def capacity_clock():
        return port["capacity_clock"]

    def estimate_clock():
        return port["estimate_clock"]

    projects = ProjectRegistry(
        Path(stores["projects"]),
        [Path(root) for root in port["allowed_roots"]],
        clock=project_clock,
        existing_only=True,
    )
    planner = RunPlanner(Path(stores["runs"]), projects, clock=planner_clock, existing_only=True)
    qualifications = FrozenQualificationPort(projects, port["qualification_facts"])
    routing = ApprovedRunRouting(
        planner,
        qualifications,
        CapacityStore(Path(stores["capacity"]), clock=capacity_clock, existing_only=True),
        estimates=AttemptEstimateStore(planner, clock=estimate_clock),
    )
    admissions = ApprovedTaskAdmission(Path(stores["admissions"]), routing, existing_only=True)
    candidates = CandidateStore(Path(port["candidate_directory"]), existing_only=True)
    ApprovedReviewerBindings(admissions, candidates, qualifications)
    source = ReviewerExecutionSource(**port["source"])
    service = ReviewerExecutionIntents(
        Path(port["execution_database"]),
        admissions,
        candidates,
        source=source,
        host=RunnerHost(Path(port["host_directory"]), existing_only=True),
        launch_compiler=lambda _: ReviewerLaunchSpec.__new__(ReviewerLaunchSpec),
        current_source=lambda: source,
        existing_only=True,
    )
    return service


def main() -> int:
    run_id, reviewer_id, principal, *mode = sys.argv[1:]
    lost_reply = mode == ["lost-reply"]
    directory = Path.cwd()
    result = {"pid": os.getpid()}
    try:
        result["claim_allowed"] = _service(directory).claim_registered_observer(
            run_id, reviewer_id, principal=principal, timeout_seconds=5
        )["claim_allowed"]
    except Exception as error:  # output is test-local and content-free
        result["error"] = type(error).__name__ + ":" + str(error)
    if lost_reply:
        # The commit has completed; emulate loss between it and the child's
        # controller reply without fabricating a runner identity in the parent.
        os._exit(0)
    (directory / "reviewer-execution-test-child-result.json").write_text(json.dumps(result))
    return 0 if result.get("claim_allowed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
