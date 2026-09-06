"""Real approved Run/Project/Capacity stores; positive qualification is synthetic."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import Event

import pytest
from karajan.projects.qualification import ProfileQualificationStore
from karajan.runs import RunError
from test_task_admission import prepared, project
from test_task_workspace import revise_plan

__all__ = ["prepared", "project"]


def reserve(prepared):
    service, routing, run = prepared
    queued = service.enqueue(run["id"], "implement", principal="owner", command_key="enqueue")
    operation = service.advance(run["id"], queued["id"], principal="owner")
    assert operation["state"] == "reserved"
    return routing, run, operation


def test_original_assessment_is_loaded_and_original_profile_rechecked_without_new_reservation(
    prepared,
):
    routing, run, operation = reserve(prepared)
    before = routing.capacity.snapshot()
    with routing.reserved_execution_guard(
        run["id"], operation["assessment"]["id"], principal="owner"
    ) as current:
        assert current["state"] == "selected", current
        assert current["scope"] == "reserved_execution_revalidation"
        assert current["planned_attempt_id"] == operation["planned_attempt_id"]
        assert current["planned_context_id"] == operation["planned_context_id"]
        assert (
            current["route"]["selected_profile"]
            == operation["assessment"]["route"]["selected_profile"]
        )
        assert current["route"]["quota_revalidation_required"] is True
        assert current["original_assessment_digest"] == operation["assessment"]["digest"]
        assert current["activation_allowed"] is False
        assert current["dispatch_enabled"] is False
    assert routing.capacity.snapshot() == before
    assert (
        routing.get(run["id"], operation["assessment"]["id"], principal="owner")
        == operation["assessment"]
    )


def test_different_owner_cannot_recheck_an_assessment(prepared):
    routing, run, operation = reserve(prepared)
    with pytest.raises(RunError, match="USER_DECISION_REQUIRED"):
        with routing.reserved_execution_guard(
            run["id"], operation["assessment"]["id"], principal="other"
        ):
            pytest.fail("A foreign owner entered the guard")


def test_full_worker_slots_do_not_count_this_reservation_again(prepared):
    routing, run, operation = reserve(prepared)
    # Three worker reservations fill the real policy's three worker slots.
    # Keep the other two holds; only Capacity excludes this exact admission.
    for index in range(2):
        request = {
            **operation["request"],
            "attempt_id": f"other-{index}",
            "run_id": f"other-{index}",
        }
        assert (
            routing.capacity.admit(request, command_key=f"other-{index}")["decision"] == "admitted"
        )
    with routing.admission_guard(
        run["id"],
        "implement",
        principal="owner",
        attempt_id=operation["planned_attempt_id"],
        context_id=operation["planned_context_id"],
    ) as new_route:
        assert new_route["state"] == "blocked"
        assert "CONCURRENCY_UNAVAILABLE" in new_route["route"]["candidates"][0]["reason_codes"]
    with routing.reserved_execution_guard(
        run["id"], operation["assessment"]["id"], principal="owner"
    ) as current:
        assert current["state"] == "selected"
        identity = operation["capacity_receipt"]["admission_id"]
        activated = routing.capacity.activate(identity, command_key="activate-original")
        assert activated["decision"] == "capacity_revalidated"
        with routing.capacity.pre_effect_guard(identity, expected_request=operation["request"]):
            pass  # This test exercises the held gates; it launches no process.
    assert len(routing.capacity.snapshot()["reservations"]) == 3


@pytest.mark.parametrize(
    "change", ["revoked_estimate", "new_estimate", "new_approval", "unqualified"]
)
def test_changed_original_execution_material_cannot_enter_as_selected(prepared, change):
    routing, run, operation = reserve(prepared)
    if change == "revoked_estimate":
        routing.estimates.revoke(
            run["project_id"], "prediction", 1, principal="owner", reason="withdrawn"
        )
    elif change == "new_estimate":
        original = routing.estimates.get(run["project_id"], "prediction", 1, principal="owner")[
            "record"
        ]
        request = {
            key: original[key]
            for key in (
                "id",
                "revision",
                "source_kind",
                "validity_seconds",
                "measurement_semantics",
                "demand",
                "completion_seconds",
                "basis",
            )
        }
        request["revision"] = 2
        routing.estimates.register(
            run["id"],
            "implement",
            operation["assessment"]["route"]["selected_profile"],
            request,
            principal="owner",
            command_key="new-prediction",
        )
    elif change == "new_approval":
        revise_plan(prepared)
    else:
        routing.qualifications = ProfileQualificationStore(routing.planner.projects)
    before = routing.capacity.snapshot()
    with routing.reserved_execution_guard(
        run["id"], operation["assessment"]["id"], principal="owner"
    ) as current:
        assert current["state"] == "blocked"
        assert current["reason_codes"]
        assert current["route"] is None or current["route"]["selected_profile"] is None
        assert current["activation_allowed"] is False
    assert routing.capacity.snapshot() == before


def test_a_missing_or_different_run_assessment_does_not_become_execution_input(prepared):
    routing, run, operation = reserve(prepared)
    with pytest.raises(RunError, match="ROUTING_ASSESSMENT_NOT_FOUND"):
        with routing.reserved_execution_guard(run["id"], "missing", principal="owner"):
            pytest.fail("Missing assessment entered")
    with pytest.raises(RunError, match="RUN_NOT_FOUND"):
        with routing.reserved_execution_guard(
            "different-run", operation["assessment"]["id"], principal="owner"
        ):
            pytest.fail("Different Run entered")


@pytest.mark.parametrize("store", ["run", "project"])
def test_fresh_execution_recheck_keeps_run_and_project_locked_until_the_consumer_finishes(
    prepared, store
):
    routing, run, operation = reserve(prepared)
    started = Event()

    def read():
        started.set()
        if store == "run":
            return routing.planner.get(run["id"], principal="owner")
        return routing.planner.projects.get_configuration(run["project_id"])

    with ThreadPoolExecutor(max_workers=1) as pool:
        with routing.reserved_execution_guard(
            run["id"], operation["assessment"]["id"], principal="owner"
        ) as current:
            assert current["state"] == "selected"
            future = pool.submit(read)
            assert started.wait(2)
            with pytest.raises(TimeoutError):
                future.result(timeout=0.15)
        assert future.result(timeout=5)
