"""The controller keeps current approval and qualification fixed through reservation."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pathlib import Path
from threading import Event

import pytest
from karajan.capacity import CapacityStore
from karajan.orchestration.routing import ApprovedRunRouting
from karajan.projects.qualification import ProfileQualificationStore
from test_routing_authorization import admitted_v2, approve_request, project, submit_request

__all__ = ["project"]


def test_guard_uses_controller_attempt_identity_without_creating_an_assessment(
    tmp_path: Path, project: tuple
) -> None:
    planner, run, intent = admitted_v2(tmp_path, project)
    plan = planner.submit_plan(
        run["id"], submit_request(run, intent), command_key="plan", principal="lead"
    )
    planner.approve_plan(run["id"], approve_request(plan), command_key="approve", principal="owner")
    capacity = CapacityStore(tmp_path / "capacity.sqlite")
    routing = ApprovedRunRouting(planner, ProfileQualificationStore(planner.projects), capacity)
    with routing.admission_guard(
        run["id"], "implement", principal="owner", attempt_id="durable-attempt", context_id="fresh"
    ) as receipt:
        assert receipt["planned_attempt_id"] == "durable-attempt"
        assert receipt["planned_context_id"] == "fresh"
        assert receipt["state"] == "blocked"
        assert receipt["activation_allowed"] is False
        assert receipt["sources"]["profiles"][0]["reason_codes"] == ["RUNTIME_TOOLS_NOT_QUALIFIED"]
    assert capacity.snapshot()["reservations"] == []


@pytest.mark.parametrize("source", ["run", "project"])
def test_guard_holds_public_source_reads_until_consumer_finishes(tmp_path, project, source):
    planner, run, intent = admitted_v2(tmp_path, project)
    plan = planner.submit_plan(
        run["id"], submit_request(run, intent), command_key="plan", principal="lead"
    )
    planner.approve_plan(run["id"], approve_request(plan), command_key="approve", principal="owner")
    routing = ApprovedRunRouting(
        planner,
        ProfileQualificationStore(planner.projects),
        CapacityStore(tmp_path / "capacity.sqlite"),
    )
    started = Event()

    def read_source():
        started.set()
        if source == "run":
            return planner.get(run["id"], principal="owner")
        return planner.projects.get_configuration(run["project_id"])

    with ThreadPoolExecutor(max_workers=1) as pool:
        with routing.admission_guard(
            run["id"], "implement", principal="owner", attempt_id="attempt", context_id="context"
        ):
            future = pool.submit(read_source)
            assert started.wait(2)
            with pytest.raises(TimeoutError):
                future.result(timeout=0.15)
        assert future.result(timeout=5)
