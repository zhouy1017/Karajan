"""Frozen Run policy and current revocations around real local activation."""

import copy
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from karajan.orchestration import LocalFixtureRunner, SerialCoordinator
from karajan.projects import ProjectRegistry
from karajan.runs import RunPlanner

from .conftest import approve
from .test_serial import revise


def coordinator(case: dict[str, Any]) -> SerialCoordinator:
    return SerialCoordinator(
        case["root"] / "orchestration",
        case["planner"],
        case["host"],
        case["candidates"],
        fixture_runner=LocalFixtureRunner(case["fixture_root"]),
    )


def save(case: dict[str, Any], document: dict[str, Any], key: str) -> None:
    projects = case["projects"]
    project_id = case["run"]["project_id"]
    preview = projects.preview_configuration(
        project_id, document, command_key="preview:" + key, principal="owner"
    )
    projects.apply_configuration(
        project_id,
        preview["preview_id"],
        expected_revision=projects.get(project_id)["revision"],
        command_key="apply:" + key,
        principal="owner",
    )


def test_new_rulebook_does_not_replace_old_run_inputs_or_budget(case: dict[str, Any]) -> None:
    approve(case)
    controller = coordinator(case)
    original = copy.deepcopy(case["planner"].get(case["run"]["id"]))
    config = copy.deepcopy(original["configuration_snapshot"]["configuration"])
    config["rulebook"]["revision"] += 1
    config["rulebook"]["description"] = "Future runs use this new version"
    config["rulebook"]["collaboration"]["max_quality_repair_rounds"] += 1
    config["resources"]["budgets"][1]["max_total_attempts"] += 20
    save(case, config, "new-rules")

    queued = controller.enqueue(
        case["run"]["id"],
        "implement",
        profile_ref=case["profile"],
        command_key="start-old-run",
        principal="owner",
    )

    assert queued["state"] == "queued", queued["reason_codes"]
    frozen = original["configuration_snapshot"]["configuration"]
    binding = queued["tasks"]["implement"]["binding"]
    assert binding["budget"] == frozen["resources"]["budgets"][1]
    assert binding["limits"] == frozen["rulebook"]["collaboration"]
    assert binding["configuration_digest"] == original["configuration_snapshot"]["digest"]
    assert case["planner"].get(case["run"]["id"]) == original
    assert case["host"].reconcile() == []


def test_publishing_empty_future_groups_does_not_rewrite_a_previously_approved_run(
    case: dict[str, Any],
) -> None:
    approve(case)
    projects = case["projects"]
    original = case["planner"].get(case["run"]["id"])
    rulebook = copy.deepcopy(original["configuration_snapshot"]["configuration"]["rulebook"])
    rulebook["revision"] += 1
    rulebook["profile_groups"] = {name: [] for name in rulebook["profile_groups"]}
    revision = projects.get(original["project_id"])["revision"]
    preview = projects.preview_rulebook(
        original["project_id"],
        rulebook,
        expected_revision=revision,
        command_key="preview-future-groups",
        principal="owner",
    )
    assert preview["can_publish"] is True
    publication = projects.publish_rulebook(
        original["project_id"],
        preview["preview_id"],
        expected_revision=revision,
        command_key="publish-future-groups",
        principal="owner",
    )
    assert publication["state"] == "waiting_qualification"
    state = coordinator(case).enqueue(
        original["id"],
        "implement",
        profile_ref=case["profile"],
        command_key="old-group-still-approved",
        principal="owner",
    )
    assert state["state"] == "queued", state["reason_codes"]
    assert case["planner"].get(original["id"]) == original
    assert state["tasks"]["implement"]["binding"]["profile"]["id"] == case["profile"]["id"]


@pytest.mark.parametrize("revocation", ["disabled", "approval", "channel", "binding", "capability"])
def test_current_restrictions_block_a_frozen_run_before_enqueue(
    case: dict[str, Any],
    revocation: str,
) -> None:
    approve(case)
    config = copy.deepcopy(case["run"]["configuration_snapshot"]["configuration"])
    if revocation == "disabled":
        config["resources"]["profiles"][0]["enabled"] = False
    elif revocation == "approval":
        config["approved_profile_refs"] = []
    elif revocation == "channel":
        config["resources"]["channels"][0]["approved_data_destination"] = False
    elif revocation == "binding":
        config["resources"]["profiles"][0]["profile"]["binding"]["model_id"] = "different-model"
    else:
        for evidence in config["resources"]["profiles"][0]["capability_evidence"]:
            evidence["status"] = "failed"
    save(case, config, "revoke")
    state = coordinator(case).enqueue(
        case["run"]["id"],
        "implement",
        profile_ref=case["profile"],
        command_key="start-revoked",
        principal="owner",
    )
    assert state["state"] == "blocked"
    assert state["reason_codes"] == ["CURRENT_PROFILE_RESTRICTED"]
    assert case["host"].reconcile() == []


def test_replayed_enqueue_cannot_activate_after_current_profile_is_revoked(
    case: dict[str, Any],
) -> None:
    approve(case)
    controller = coordinator(case)
    args = dict(profile_ref=case["profile"], command_key="enqueue", principal="owner")
    original = controller.enqueue(case["run"]["id"], "implement", **args)
    assert original["state"] == "queued"
    config = copy.deepcopy(case["run"]["configuration_snapshot"]["configuration"])
    config["resources"]["profiles"][0]["enabled"] = False
    save(case, config, "disable-after-queue")
    reopened = coordinator(case)
    assert reopened.enqueue(case["run"]["id"], "implement", **args) == original
    state = reopened.advance(case["run"]["id"])
    assert state["state"] == "invalidating"
    assert state["reason_codes"] == ["CURRENT_PROFILE_RESTRICTED"]
    assert case["host"].reconcile() == []


@pytest.mark.parametrize("change", ["revoke", "revise-plan"])
def test_policy_or_plan_change_cannot_commit_inside_activation_acceptance(
    case: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    approve(case)
    controller = coordinator(case)
    controller.enqueue(
        case["run"]["id"],
        "implement",
        profile_ref=case["profile"],
        command_key="queue-before-race",
        principal="owner",
    )
    other_projects = ProjectRegistry(case["projects"].database, [case["fixture_root"]])
    other_case = {
        **case,
        "projects": other_projects,
        "planner": RunPlanner(
            case["planner"].database,
            other_projects,
            admissions=case["authority"],
        ),
    }
    preparing = threading.Event()
    release = threading.Event()
    update_entered = threading.Event()
    original_prepare = case["host"].prepare

    def observed_prepare(*args: Any, **kwargs: Any) -> Any:
        preparing.set()
        assert release.wait(5)
        return original_prepare(*args, **kwargs)

    monkeypatch.setattr(case["host"], "prepare", observed_prepare)

    def update_current() -> None:
        update_entered.set()
        if change == "revise-plan":
            revise(other_case, "implement")
        else:
            config = copy.deepcopy(case["run"]["configuration_snapshot"]["configuration"])
            config["resources"]["profiles"][0]["enabled"] = False
            save(other_case, config, "race-revoke")

    with ThreadPoolExecutor(max_workers=2) as pool:
        advancing = pool.submit(controller.advance, case["run"]["id"])
        assert preparing.wait(5)
        changing = pool.submit(update_current)
        try:
            assert update_entered.wait(2)
            with pytest.raises(TimeoutError):
                changing.result(timeout=0.25)
        finally:
            release.set()
        accepted = advancing.result(timeout=5)
        changing.result(timeout=5)
    assert len(case["host"].reconcile()) == 1
    assert accepted["attempts"][0]["physical"] is not None
    invalidated = controller.advance(case["run"]["id"])
    assert invalidated["state"] == "invalidating"
    assert len(invalidated["attempts"]) == 1


@pytest.mark.parametrize("change", ["revoke", "revise-plan"])
def test_change_committed_after_cached_reads_is_rechecked_before_materialization(
    case: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    approve(case)
    controller = coordinator(case)
    queued = controller.enqueue(
        case["run"]["id"],
        "implement",
        profile_ref=case["profile"],
        command_key="queue",
        principal="owner",
    )
    other_projects = ProjectRegistry(case["projects"].database, [case["fixture_root"]])
    other_case = {
        **case,
        "projects": other_projects,
        "planner": RunPlanner(
            case["planner"].database, other_projects, admissions=case["authority"]
        ),
    }
    reached = threading.Event()
    release = threading.Event()
    original_guard = case["planner"].activation_guard

    @contextmanager
    def before_guard(run_id: str):
        reached.set()
        assert release.wait(5)
        with original_guard(run_id) as snapshot:
            yield snapshot

    monkeypatch.setattr(case["planner"], "activation_guard", before_guard)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(controller.advance, case["run"]["id"])
        try:
            assert reached.wait(5)
            if change == "revise-plan":
                revise(other_case, "implement")
            else:
                config = copy.deepcopy(case["run"]["configuration_snapshot"]["configuration"])
                config["resources"]["profiles"][0]["enabled"] = False
                save(other_case, config, "before-guard")
        finally:
            release.set()
        refused = future.result(timeout=5)
    assert refused["state"] == "invalidating"
    assert case["host"].reconcile() == []
    assert not Path(queued["attempts"][0]["workspace"]).exists()


@pytest.mark.parametrize("point", ["before_spawn", "after_spawn"])
def test_guard_rollback_releases_authority_locks_without_erasing_host_identity(
    case: dict[str, Any],
    point: str,
) -> None:
    from karajan.execution import ProbeCrash

    approve(case)
    controller = coordinator(case)
    queued = controller.enqueue(
        case["run"]["id"],
        "implement",
        profile_ref=case["profile"],
        command_key="enqueue-before-crash",
        principal="owner",
    )
    other_projects = ProjectRegistry(case["projects"].database, [case["fixture_root"]])
    other_case = {
        **case,
        "projects": other_projects,
        "planner": RunPlanner(
            case["planner"].database, other_projects, admissions=case["authority"]
        ),
    }
    with pytest.raises(ProbeCrash):
        controller.advance(case["run"]["id"], crash_at=point)
    config = copy.deepcopy(case["run"]["configuration_snapshot"]["configuration"])
    config["resources"]["profiles"][0]["enabled"] = False
    save(other_case, config, "after-crash")
    revise(other_case, "implement")
    reopened = coordinator(case)
    refused = reopened.advance(case["run"]["id"])
    assert refused["state"] == "invalidating"
    assert refused["counters"]["total_attempts"] == 1
    observed = case["host"].reconcile()
    assert len(observed) == 1
    assert observed[0].attempt_id == queued["attempts"][0]["id"]
    assert refused["attempts"][0]["start_key"] == queued["attempts"][0]["start_key"]
