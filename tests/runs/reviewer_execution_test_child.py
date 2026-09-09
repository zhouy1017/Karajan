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
import threading
import time
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path

import httpx

# `-I` intentionally ignores PYTHONPATH.  The test launcher fixes the source
# root by file location, rather than accepting it from argv or the environment.
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from karajan.adapters.opencode import go_journal
from karajan.candidates import CandidateStore, review_output
from karajan.capacity import CapacityStore
from karajan.execution import RunnerHost
from karajan.isolation import go_reviewer_probe
from karajan.isolation.opencode_runtime import IsolatedOpenCode
from karajan.orchestration.admission import ApprovedTaskAdmission
from karajan.orchestration.reviewer_binding import ApprovedReviewerBindings
from karajan.orchestration.reviewer_execution_intent import (
    ReviewerExecutionIntents,
    ReviewerExecutionSource,
    ReviewerLaunchSpec,
)
from karajan.orchestration.routing import ApprovedRunRouting
from karajan.projects import ProjectRegistry, go_reviewer_suite
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
    replacement = port.get("changed_source")

    def current_source() -> ReviewerExecutionSource:
        if (directory / "reviewer-execution-test-source-changed").exists():
            if not isinstance(replacement, dict):
                raise RuntimeError("missing changed source test port")
            return ReviewerExecutionSource(**replacement)
        return source

    service = ReviewerExecutionIntents(
        Path(port["execution_database"]),
        admissions,
        candidates,
        source=source,
        host=RunnerHost(Path(port["host_directory"]), existing_only=True),
        launch_compiler=lambda _: ReviewerLaunchSpec.__new__(ReviewerLaunchSpec),
        current_source=current_source,
        existing_only=True,
    )
    return service


def _install_forbidden_effect_counters(directory: Path):
    """Persist child-local proof that a real claim did not cross an effect port."""
    counters = {
        name: 0
        for name in (
            "observer",
            "host_start",
            "native_start",
            "http_send",
            "parser_suite",
            "parser_output",
            "journal_grant",
            "journal_call",
            "evidence_write",
            "quality_effect",
        )
    }
    lock = threading.Lock()
    evidence = directory / "reviewer-execution-test-child-effects.json"

    def persist(*, claim_observed: bool) -> None:
        with lock:
            value = {"claim_observed": claim_observed, "counters": counters}
            evidence.write_text(
                json.dumps(value, sort_keys=True),
                encoding="utf-8",
            )

    def forbid(name, original):
        def wrapped(*args, **kwargs):
            with lock:
                counters[name] += 1
            persist(claim_observed=False)
            raise AssertionError(f"forbidden Reviewer child boundary called: {name}")

        return wrapped

    go_reviewer_probe.observe_go_reviewer_tools = forbid(
        "observer", go_reviewer_probe.observe_go_reviewer_tools
    )
    IsolatedOpenCode.start = forbid("native_start", IsolatedOpenCode.start)
    RunnerHost.start = forbid("host_start", RunnerHost.start)
    httpx.Client.send = forbid("http_send", httpx.Client.send)
    go_reviewer_suite.parse_review_output = forbid(
        "parser_suite", go_reviewer_suite.parse_review_output
    )
    review_output.parse_review_output = forbid("parser_output", review_output.parse_review_output)
    go_journal.GoCallJournal.create_grant = forbid(
        "journal_grant", go_journal.GoCallJournal.create_grant
    )
    go_journal.GoCallJournal.begin_call = forbid(
        "journal_call", go_journal.GoCallJournal.begin_call
    )
    CandidateStore._save_evidence = forbid("evidence_write", CandidateStore._save_evidence)
    CandidateStore.record_check = forbid("quality_effect", CandidateStore.record_check)
    persist(claim_observed=False)
    return persist


def main() -> int:
    run_id, reviewer_id, principal, *mode = sys.argv[1:]
    lost_reply = mode == ["lost-reply"]
    concurrent = mode == ["concurrent"]
    lifecycle = mode == ["lifecycle"]
    paused = mode == ["paused"]
    source_wait = mode == ["source-wait"]
    directory = Path.cwd()
    result = {"pid": os.getpid()}
    persist_effects = _install_forbidden_effect_counters(directory)
    try:
        if concurrent:
            # Two independently reopened facades contend as the one actual
            # registered direct child.  No controller-supplied identity or
            # synthetic second child participates in the claim.
            barrier = threading.Barrier(2)
            replies: list[dict[str, object] | None] = [None, None]

            def claim(slot: int) -> None:
                service = _service(directory)
                barrier.wait(timeout=5)
                replies[slot] = service.claim_registered_observer(
                    run_id, reviewer_id, principal=principal, timeout_seconds=5
                )

            threads = [threading.Thread(target=claim, args=(slot,)) for slot in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
                if thread.is_alive():
                    raise RuntimeError("claim thread did not complete")
            if any(reply is None for reply in replies):
                raise RuntimeError("claim reply missing")
            result["claims"] = [bool(reply["claim_allowed"]) for reply in replies if reply]
        elif lifecycle:
            service = _service(directory)
            result["claim_allowed"] = service.claim_registered_observer(
                run_id, reviewer_id, principal=principal, timeout_seconds=5
            )["claim_allowed"]
            result["replay_allowed"] = service.claim_registered_observer(
                run_id, reviewer_id, principal=principal, timeout_seconds=5
            )["claim_allowed"]
            result["cancel_requested"] = service.cancel(
                run_id, reviewer_id, principal=principal
            )["cancel_requested"]
            try:
                service.claim_registered_observer(
                    run_id, reviewer_id, principal=principal, timeout_seconds=5
                )
            except Exception as error:
                result["cancel_error"] = str(error)
        else:
            service = _service(directory)
            if source_wait:
                (directory / "reviewer-execution-test-child-service-ready").write_text("ready")
                begin = directory / "reviewer-execution-test-child-final-writer-begin"
                deadline = time.monotonic() + 5
                while not begin.exists():
                    if time.monotonic() >= deadline:
                        raise RuntimeError("parent did not begin final writer wait")
                    time.sleep(0.01)
                original_db = service._db

                @contextmanager
                def final_writer_boundary(*, write=True):
                    if write:
                        (directory / "reviewer-execution-test-child-final-writer-ready").write_text(
                            "ready"
                        )
                        release = directory / "reviewer-execution-test-child-final-writer-release"
                        deadline = time.monotonic() + 5
                        while not release.exists():
                            if time.monotonic() >= deadline:
                                raise RuntimeError("parent did not release final writer")
                            time.sleep(0.01)
                        attempt = directory / "reviewer-execution-test-child-final-writer-attempt"
                        attempt.write_text("attempt")
                    with original_db(write=write) as db:
                        yield db

                service._db = final_writer_boundary
            if paused:
                (directory / "reviewer-execution-test-child-ready").write_text("ready")
                deadline = time.monotonic() + 5
                release = directory / "reviewer-execution-test-child-release"
                while not release.exists():
                    if time.monotonic() >= deadline:
                        raise RuntimeError("parent did not release direct child")
                    time.sleep(0.01)
            result["claim_allowed"] = service.claim_registered_observer(
                run_id, reviewer_id, principal=principal, timeout_seconds=5
            )["claim_allowed"]
    except Exception as error:  # output is test-local and content-free
        result["error"] = type(error).__name__ + ":" + str(error)
    persist_effects(
        claim_observed=bool(result.get("claim_allowed"))
        or any(bool(value) for value in result.get("claims", []))
    )
    if lost_reply:
        # The commit has completed; emulate loss between it and the child's
        # controller reply without fabricating a runner identity in the parent.
        os._exit(0)
    (directory / "reviewer-execution-test-child-result.json").write_text(json.dumps(result))
    return 0 if result.get("claim_allowed") or result.get("claims") else 1


if __name__ == "__main__":
    raise SystemExit(main())
