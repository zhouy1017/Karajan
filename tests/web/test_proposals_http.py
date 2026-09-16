"""Conversation-bound owner-intent proposals use the real v2 controller fixture."""

import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Event, Thread
from typing import Any, cast

import pytest
from karajan.conversations import ConversationStore
from karajan.projects.qualification import ProfileQualificationStore
from karajan.proposals import ProposalStore
from karajan.runs import RunError, RunPlanner
from test_runs_http import run_client
from test_v2_approval_workbench import approval, v2_plan

__all__ = ["run_client", "v2_plan"]


def _proposal_request(run: dict, plan: dict, edits: list[dict] | None = None) -> dict:
    return {
        "run_id": run["id"],
        "base_plan_revision": plan["plan_revision"],
        "term": plan["term"],
        "expected_plan_digest": plan["plan_digest"],
        "expected_authorization_digest": plan["authorization_digest"],
        "edits": edits or [],
    }


def _second_trusted_run(planner: RunPlanner, run: dict, submitted: dict) -> tuple[dict, dict]:
    current = planner.get(run["id"])
    policy = current["execution_policy_snapshot"]
    request = {
        "schema_version": "karajan.create-run.v2",
        "project_id": current["project_id"],
        "conversation_id": current["conversation_id"],
        "project_revision": current["configuration_snapshot"]["project_revision"],
        "configuration_digest": current["configuration_snapshot"]["digest"],
        "requirement": current["requirement"],
        "participants": current["participants"],
        "authorization": current["authorization_ceiling"],
        "execution_policy": {key: policy[key] for key in ("id", "revision", "digest")},
    }
    created = planner.create(request, command_key="second-run", principal="owner")
    intent = planner.planning_intent(
        created["id"], term=1, command_key="second-intent", principal="commander-1"
    )
    receipts = cast(dict[str, dict[str, Any]], planner.admissions.__self__)
    receipts["second-receipt"] = {
        "receipt_ref": "second-receipt",
        "authority_revision": "second-authority",
        "run_id": created["id"],
        "intent_id": intent["id"],
        "term": 1,
        "principal": "commander-1",
        "profile": current["commander"]["profile"],
        "budget_ref": intent["budget_ref"],
        "state": "admitted",
        "provenance": "fixture",
    }
    planner.attach_planning_receipt(
        created["id"],
        intent["id"],
        receipt_ref="second-receipt",
        command_key="second-attach",
        principal="owner",
    )
    submission = deepcopy(submitted)
    submission.update(intent_id=intent["id"], expected_plan_revision=0)
    plan = planner.submit_plan(
        created["id"], submission, command_key="second-plan", principal="commander-1"
    )
    return created, plan


def _next_trusted_plan(planner: RunPlanner, run: dict, submitted: dict) -> dict:
    """Submit a distinct trusted plan in the same Run without owner approval."""
    current = planner.get(run["id"])
    intent = planner.planning_intent(
        run["id"], term=1, command_key="next-intent", principal="commander-1"
    )
    receipts = cast(dict[str, dict[str, Any]], planner.admissions.__self__)
    receipts["next-receipt"] = {
        "receipt_ref": "next-receipt",
        "authority_revision": "next-authority",
        "run_id": run["id"],
        "intent_id": intent["id"],
        "term": 1,
        "principal": "commander-1",
        "profile": current["commander"]["profile"],
        "budget_ref": intent["budget_ref"],
        "state": "admitted",
        "provenance": "fixture",
    }
    planner.attach_planning_receipt(
        run["id"],
        intent["id"],
        receipt_ref="next-receipt",
        command_key="next-attach",
        principal="owner",
    )
    request = deepcopy(submitted)
    request.update(intent_id=intent["id"], expected_plan_revision=current["latest_plan_revision"])
    request["plan"]["summary"] = "A distinct trusted plan"
    return planner.submit_plan(
        run["id"], request, command_key="next-plan", principal="commander-1"
    )


def test_default_proposal_is_replayable_and_approves_once(v2_plan: tuple) -> None:
    client, headers, planner, run, _, plan = v2_plan
    conversation_id = planner.get(run["id"])["conversation_id"]
    url = f"/v1/conversations/{conversation_id}/proposals"
    request = _proposal_request(run, plan)
    created = client.post(url, json=request, headers={**headers, "Idempotency-Key": "proposal"})
    assert created.status_code == 201
    proposal = created.json()
    assert proposal["base_output_provenance"] == "fixture"
    assert proposal["user_adjustments"] == []
    replay = client.post(
        url, json=request, headers={**headers, "Idempotency-Key": "proposal"}
    )
    assert replay.json() == proposal
    body = {
        **approval(plan),
        "conversation_id": conversation_id,
        "proposal_revision": proposal["proposal_revision"],
    }
    approved = client.post(
        f"/v1/runs/{run['id']}/plan-approval",
        json=body,
        headers={
            **headers,
            "Idempotency-Key": "approve-proposal",
            "If-Match": f'"{proposal["run_revision"]}"',
        },
    )
    assert approved.status_code == 200, approved.json()
    assert approved.json()["proposal_revision"] == proposal["proposal_revision"]
    assert planner.get(run["id"])["active_plan_revision"] == plan["plan_revision"]
    assert client.post(
        f"/v1/runs/{run['id']}/plan-approval",
        json=body,
        headers={
            **headers,
            "Idempotency-Key": "approve-proposal",
            "If-Match": f'"{proposal["run_revision"]}"',
        },
    ).json() == approved.json()


def test_proposal_rejects_stale_cross_conversation_and_unapproved_scope(v2_plan: tuple) -> None:
    client, headers, planner, run, _, plan = v2_plan
    conversation_id = planner.get(run["id"])["conversation_id"]
    url = f"/v1/conversations/{conversation_id}/proposals"
    stale = deepcopy(_proposal_request(run, plan))
    stale["base_plan_revision"] += 1
    stale_response = client.post(
        url, json=stale, headers={**headers, "Idempotency-Key": "stale"}
    )
    assert stale_response.status_code == 409
    escaped = _proposal_request(
        run, plan, [{"task_id": "feature", "write_paths": ["outside/secret.py"]}]
    )
    escaped_response = client.post(
        url, json=escaped, headers={**headers, "Idempotency-Key": "escaped"}
    )
    assert escaped_response.status_code == 422
    revoked_source = _proposal_request(
        run,
        plan,
        [
            {
                "task_id": "feature",
                "profile_ref": {"id": "fixture-profile", "revision": 1},
                "source_ref": "wrong-source",
            }
        ],
    )
    source_response = client.post(
        url, json=revoked_source, headers={**headers, "Idempotency-Key": "revoked-source"}
    )
    assert source_response.json()["reason_code"] == "PROFILE_SOURCE_REVOKED"
    wrong = client.post(
        f"/v1/projects/{run['project_id']}/conversations",
        json={"title": "other"},
        headers={**headers, "Idempotency-Key": "other-conversation"},
    ).json()
    assert client.post(
        f"/v1/conversations/{wrong['id']}/proposals",
        json=_proposal_request(run, plan),
        headers={**headers, "Idempotency-Key": "cross"},
    ).status_code == 409


def test_changed_owner_intent_is_immutable_projected_and_approved(v2_plan: tuple) -> None:
    client, headers, planner, run, _, plan = v2_plan
    conversation_id = planner.get(run["id"])["conversation_id"]
    created = client.post(
        f"/v1/conversations/{conversation_id}/proposals",
        json=_proposal_request(
            run,
            plan,
            [
                {
                    "task_id": "feature",
                    "role": "reviewer",
                    "profile_ref": {"id": "fixture-profile", "revision": 1},
                    "source_ref": "fixture-channel",
                }
            ],
        ),
        headers={**headers, "Idempotency-Key": "changed-proposal"},
    )
    assert created.status_code == 201
    proposal = created.json()
    assert proposal["preview"]["tasks"][0]["role"] == "reviewer"
    assert proposal["assignments"]["feature"]["source_ref"] == "fixture-channel"
    assert proposal["base_output_provenance"] == "fixture"
    hub = client.get(f"/v1/conversations/{conversation_id}/hub").json()
    assert hub["current_proposal"] == proposal
    body = {
        **approval(plan),
        **{
            name: proposal[name]
            for name in (
                "term",
                "plan_revision",
                "plan_digest",
                "authorization_digest",
                "configuration_digest",
                "routing_digest",
            )
        },
        "conversation_id": conversation_id,
        "proposal_revision": proposal["proposal_revision"],
    }
    response = client.post(
        f"/v1/runs/{run['id']}/plan-approval",
        json=body,
        headers={
            **headers,
            "Idempotency-Key": "changed-approval",
            "If-Match": f'"{proposal["run_revision"]}"',
        },
    )
    assert response.status_code == 200
    current = planner.get(run["id"])
    assert current["plans"][-1]["provenance"] == "owner_adjustment"
    assert current["plans"][-1]["base_output_provenance"] == "fixture"
    assert current["plans"][-1]["user_adjustments"] == proposal["user_adjustments"]


def test_profile_source_and_checks_edits_bind_the_approved_routing(v2_plan: tuple) -> None:
    client, headers, planner, run, _, plan = v2_plan
    conversation_id = planner.get(run["id"])["conversation_id"]
    created = client.post(
        f"/v1/conversations/{conversation_id}/proposals",
        json=_proposal_request(
            run,
            plan,
            [
                {
                    "task_id": "feature",
                    "profile_ref": {"id": "fixture-profile", "revision": 1},
                    "source_ref": "fixture-channel",
                    "checks": ["unit-tests", "independent_review"],
                }
            ],
        ),
        headers={**headers, "Idempotency-Key": "profile-source-routing"},
    )
    assert created.status_code == 201, created.text
    proposal = created.json()
    task = proposal["preview"]["tasks"][0]
    assert task["profile_ref"] == {"id": "fixture-profile", "revision": 1}
    assert task["source_ref"] == "fixture-channel"
    assert task["checks"] == ["unit-tests", "independent_review"]
    assert proposal["routing_digest"] != plan["routing_digest"]
    rejected_checks = client.post(
        f"/v1/conversations/{conversation_id}/proposals",
        json=_proposal_request(
            run, plan, [{"task_id": "feature", "checks": ["unit-tests"]}]
        ),
        headers={**headers, "Idempotency-Key": "required-check-removed"},
    )
    assert rejected_checks.json()["reason_code"] == "REQUIRED_CHECKS_REMOVED"
    checks_only = client.post(
        f"/v1/conversations/{conversation_id}/proposals",
        json=_proposal_request(
            run,
            plan,
            [{"task_id": "feature", "checks": ["unit-tests", "independent_review"]}],
        ),
        headers={**headers, "Idempotency-Key": "checks-only-routing"},
    ).json()
    assert checks_only["preview"]["tasks"][0]["checks"] == ["unit-tests", "independent_review"]
    assert checks_only["routing_digest"] != plan["routing_digest"]
    assert checks_only["inserted_plan"]["routing_binding"]["task_requirements"]["feature"][
        "checks"
    ] == ["unit-tests", "independent_review"]
    response = client.post(
        f"/v1/runs/{run['id']}/plan-approval",
        json={
            **approval(plan),
            **{
                key: proposal[key]
                for key in (
                    "term",
                    "plan_revision",
                    "plan_digest",
                    "authorization_digest",
                    "configuration_digest",
                    "routing_digest",
                )
            },
            "conversation_id": conversation_id,
            "proposal_revision": proposal["proposal_revision"],
        },
        headers={
                **headers,
                "Idempotency-Key": "profile-source-routing-approval",
                "If-Match": f'"{planner.get(run["id"])["revision"]}"',
            },
    )
    assert response.status_code == 200, response.text
    inserted = planner.get(run["id"])["plans"][-1]
    assert inserted == proposal["inserted_plan"]
    binding = inserted["routing_binding"]["task_requirements"]
    assert binding["feature"]["profile_ref"] == {"id": "fixture-profile", "revision": 1}
    assert binding["feature"]["source_ref"] == "fixture-channel"


def test_proposal_approval_rejects_body_authority_overrides(v2_plan: tuple) -> None:
    client, headers, planner, run, _, plan = v2_plan
    conversation_id = planner.get(run["id"])["conversation_id"]
    proposal = client.post(
        f"/v1/conversations/{conversation_id}/proposals",
        json=_proposal_request(run, plan),
        headers={**headers, "Idempotency-Key": "body-override-proposal"},
    ).json()
    body = {
        **approval(plan),
        "conversation_id": conversation_id,
        "proposal_revision": proposal["proposal_revision"],
        "run_id": run["id"],
        "run_revision": proposal["run_revision"],
    }
    rejected = client.post(
        f"/v1/runs/{run['id']}/plan-approval",
        json=body,
        headers={
            **headers,
            "Idempotency-Key": "body-override",
            "If-Match": f'"{proposal["run_revision"] - 1}"',
        },
    )
    assert rejected.status_code == 422
    assert rejected.json()["reason_code"] == "APPROVAL_ROUTE_AUTHORITY_OVERRIDE"
    assert planner.get(run["id"])["active_plan_revision"] is None


def test_stale_proposal_cannot_accept_or_approve_a_colliding_trusted_plan(v2_plan: tuple) -> None:
    client, headers, planner, run, submitted, plan = v2_plan
    conversation_id = planner.get(run["id"])["conversation_id"]
    proposal = client.post(
        f"/v1/conversations/{conversation_id}/proposals",
        json=_proposal_request(run, plan, [{"task_id": "feature", "role": "reviewer"}]),
        headers={**headers, "Idempotency-Key": "colliding-proposal"},
    ).json()
    newer = _next_trusted_plan(planner, run, submitted)
    assert newer["plan_revision"] == proposal["plan_revision"]
    stale_revision = planner.get(run["id"])["revision"]
    accepted = client.post(
        f"/v1/conversations/{conversation_id}/proposals/{proposal['proposal_revision']}/accept",
        json={"run_revision": stale_revision},
        headers={**headers, "Idempotency-Key": "colliding-accept"},
    )
    assert accepted.status_code == 409
    assert accepted.json()["reason_code"] == "PROPOSAL_PLAN_COLLISION"
    approved = client.post(
        f"/v1/runs/{run['id']}/plan-approval",
        json={
            **approval(plan),
            **{
                key: proposal[key]
                for key in (
                    "term",
                    "plan_revision",
                    "plan_digest",
                    "authorization_digest",
                    "configuration_digest",
                    "routing_digest",
                )
            },
            "conversation_id": conversation_id,
            "proposal_revision": proposal["proposal_revision"],
        },
        headers={
            **headers,
            "Idempotency-Key": "colliding-approval",
            "If-Match": f'"{stale_revision}"',
        },
    )
    assert approved.status_code == 409
    assert approved.json()["reason_code"] == "PROPOSAL_PLAN_COLLISION"
    current = planner.get(run["id"])
    assert current["active_plan_revision"] is None
    assert current["plans"][-1]["plan_digest"] == newer["plan_digest"]


def test_proposal_bearing_run_cannot_bypass_binding_with_legacy_approval(v2_plan: tuple) -> None:
    client, headers, planner, run, _, plan = v2_plan
    conversation_id = planner.get(run["id"])["conversation_id"]
    proposal = client.post(
        f"/v1/conversations/{conversation_id}/proposals",
        json=_proposal_request(run, plan),
        headers={**headers, "Idempotency-Key": "legacy-bypass-proposal"},
    ).json()
    legacy = client.post(
        f"/v1/runs/{run['id']}/plan-approval",
        json=approval(plan),
        headers={**headers, "Idempotency-Key": "legacy-bypass"},
    )
    assert legacy.status_code == 422
    assert legacy.json()["reason_code"] == "CONVERSATION_PROPOSAL_BINDING_REQUIRED"
    with pytest.raises(RunError, match="CONVERSATION_PROPOSAL_BINDING_REQUIRED"):
        planner.approve_plan(
            run["id"], approval(plan), command_key="legacy-non-http", principal="owner"
        )
    assert planner.get(run["id"])["active_plan_revision"] is None
    assert proposal["proposal_revision"] == 1


def test_replay_identity_includes_conversation_project_and_approval_path(
    v2_plan: tuple, tmp_path
) -> None:
    client, headers, planner, run, _, plan = v2_plan
    conversation_id = planner.get(run["id"])["conversation_id"]
    request = _proposal_request(run, plan)
    created = client.post(
        f"/v1/conversations/{conversation_id}/proposals",
        json=request,
        headers={**headers, "Idempotency-Key": "path-bound"},
    ).json()
    same_project = client.post(
        f"/v1/projects/{run['project_id']}/conversations",
        json={"title": "same-project"},
        headers={**headers, "Idempotency-Key": "same-project"},
    ).json()
    wrong_conversation = client.post(
        f"/v1/conversations/{same_project['id']}/proposals",
        json=request,
        headers={**headers, "Idempotency-Key": "path-bound"},
    )
    assert wrong_conversation.status_code == 409
    accepted_view = client.post(
        f"/v1/conversations/{conversation_id}/proposals/{created['proposal_revision']}/accept",
        json={"run_revision": created["run_revision"]},
        headers={**headers, "Idempotency-Key": "acceptance-path"},
    )
    assert accepted_view.status_code == 200
    wrong_accept = client.post(
        f"/v1/conversations/{same_project['id']}/proposals/{created['proposal_revision']}/accept",
        json={"run_revision": created["run_revision"]},
        headers={**headers, "Idempotency-Key": "acceptance-path"},
    )
    assert wrong_accept.status_code == 404
    other_repository = tmp_path / "repositories" / "other"
    subprocess.run(["git", "init", "--initial-branch=main", str(other_repository)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(other_repository),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "fixture",
        ],
        check=True,
    )
    other_project = client.post(
        "/v1/projects",
        json={
            "name": "Other",
            "repository_path": str(other_repository),
            "base_ref": "main",
            "target_branch": "main",
            "allowed_target_branches": ["main"],
        },
        headers={**headers, "Idempotency-Key": "other-project"},
    ).json()
    other_conversation = client.post(
        f"/v1/projects/{other_project['id']}/conversations",
        json={"title": "other-project"},
        headers={**headers, "Idempotency-Key": "other-project-conversation"},
    ).json()
    wrong_project = client.post(
        f"/v1/conversations/{other_conversation['id']}/proposals",
        json=request,
        headers={**headers, "Idempotency-Key": "path-bound"},
    )
    assert wrong_project.status_code == 409
    body = {
        **approval(plan),
        "conversation_id": conversation_id,
        "proposal_revision": created["proposal_revision"],
    }
    accepted = client.post(
        f"/v1/runs/{run['id']}/plan-approval",
        json=body,
        headers={
            "If-Match": f'"{planner.get(run["id"])["revision"]}"',
            **headers,
            "Idempotency-Key": "approval-path",
        },
    )
    assert accepted.status_code == 200
    wrong_approval = client.post(
        f"/v1/runs/{run['id']}/plan-approval",
        json={**body, "conversation_id": same_project["id"]},
        headers={
            "If-Match": f'"{created["run_revision"]}"',
            **headers,
            "Idempotency-Key": "approval-path",
        },
    )
    assert wrong_approval.status_code == 409


def test_conversation_revisions_are_unique_concurrent_and_reopenable(v2_plan: tuple) -> None:
    client, _, planner, run, submitted, plan = v2_plan
    second, second_plan = _second_trusted_run(planner, run, submitted)
    conversation_id = planner.get(run["id"])["conversation_id"]
    store = client.app.state.proposals
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                store.create,
                conversation_id,
                _proposal_request(candidate, candidate_plan),
                principal="owner",
                key=key,
            )
            for candidate, candidate_plan, key in (
                (run, plan, "concurrent-one"),
                (second, second_plan, "concurrent-two"),
            )
        ]
        proposals = [future.result(timeout=10) for future in futures]
    assert {item["proposal_revision"] for item in proposals} == {1, 2}
    reopened_planner = RunPlanner(planner.database, planner.projects)
    reopened_conversations = ConversationStore(planner.projects, reopened_planner)
    reopened = ProposalStore(
        reopened_planner, reopened_conversations, ProfileQualificationStore(planner.projects)
    )
    read = reopened.read(conversation_id)
    assert {item["proposal_revision"] for item in read["items"]} == {1, 2}
    chosen = read["items"][0]
    specific = reopened.read(conversation_id, revision=chosen["proposal_revision"])["current"]
    assert specific["run_id"] == chosen["run_id"]
    accepted = reopened.accept(
        conversation_id,
        chosen["proposal_revision"],
        run_revision=chosen["run_revision"],
        principal="owner",
        key="reopened-accept",
    )
    assert accepted["proposal_revision"] == chosen["proposal_revision"]


def test_hub_current_proposal_uses_the_greatest_conversation_revision(v2_plan: tuple) -> None:
    client, headers, planner, run, submitted, plan = v2_plan
    second, second_plan = _second_trusted_run(planner, run, submitted)
    conversation_id = planner.get(run["id"])["conversation_id"]
    first = client.post(
        f"/v1/conversations/{conversation_id}/proposals",
        json=_proposal_request(run, plan),
        headers={**headers, "Idempotency-Key": "hub-first"},
    ).json()
    client.post(
        f"/v1/conversations/{conversation_id}/proposals",
        json=_proposal_request(second, second_plan),
        headers={**headers, "Idempotency-Key": "hub-second-run"},
    )
    newest = client.post(
        f"/v1/conversations/{conversation_id}/proposals",
        json=_proposal_request(run, plan, [{"task_id": "feature", "role": "reviewer"}]),
        headers={**headers, "Idempotency-Key": "hub-newest-first-run"},
    ).json()
    assert newest["proposal_revision"] > first["proposal_revision"]
    hub = client.get(f"/v1/conversations/{conversation_id}/hub").json()
    assert hub["current_proposal"] == newest


def test_revoked_current_qualification_blocks_proposal_approval(v2_plan: tuple) -> None:
    client, headers, planner, run, _, plan = v2_plan
    conversation_id = planner.get(run["id"])["conversation_id"]
    proposal = client.post(
        f"/v1/conversations/{conversation_id}/proposals",
        json=_proposal_request(run, plan),
        headers={**headers, "Idempotency-Key": "revoked-proposal"},
    ).json()
    qualifications = ProfileQualificationStore(planner.projects)
    with planner.projects._transaction() as db:
        observation_id = db.execute(
            "SELECT id FROM profile_qualification_records ORDER BY rowid DESC"
        ).fetchone()["id"]
    qualifications.revoke(run["project_id"], observation_id, principal="owner", reason="revoke")
    rejected = client.post(
        f"/v1/runs/{run['id']}/plan-approval",
        json={
            **approval(plan),
            "conversation_id": conversation_id,
            "proposal_revision": proposal["proposal_revision"],
        },
        headers={
            "If-Match": f'"{proposal["run_revision"]}"',
            **headers,
            "Idempotency-Key": "revoked-approval",
        },
    )
    assert rejected.json()["reason_code"] == "QUALIFICATION_REVOKED"
    assert planner.get(run["id"])["active_plan_revision"] is None


def test_revocation_cannot_commit_between_source_check_and_proposal_commit(v2_plan: tuple) -> None:
    client, headers, planner, run, _, plan = v2_plan
    conversation_id = planner.get(run["id"])["conversation_id"]
    proposal = client.post(
        f"/v1/conversations/{conversation_id}/proposals",
        json=_proposal_request(run, plan),
        headers={**headers, "Idempotency-Key": "interleaved-proposal"},
    ).json()
    with planner.projects._transaction() as db:
        observation_id = db.execute(
            "SELECT id FROM profile_qualification_records ORDER BY rowid DESC"
        ).fetchone()["id"]
    started, revoked = Event(), Event()
    app_planner = client.app.state.proposals.planner
    original_event = app_planner._conversation_event
    worker: Thread | None = None

    def revoke() -> None:
        started.set()
        ProfileQualificationStore(planner.projects).revoke(
            run["project_id"], observation_id, principal="owner", reason="interleaved-revoke"
        )
        revoked.set()

    def interleave(db: Any, run_id: object, kind: str) -> None:
        nonlocal worker
        if kind == "proposal_approve":
            worker = Thread(target=revoke)
            worker.start()
            assert started.wait(1)
            # _conversation_event is still inside the Run transaction.  The
            # concurrent revocation must not acquire the held Project source.
            assert not revoked.wait(0.1)
        original_event(db, run_id, kind)

    app_planner._conversation_event = interleave
    try:
        approved = client.post(
            f"/v1/runs/{run['id']}/plan-approval",
            json={
                **approval(plan),
                "conversation_id": conversation_id,
                "proposal_revision": proposal["proposal_revision"],
            },
            headers={
                **headers,
                "Idempotency-Key": "interleaved-approval",
                "If-Match": f'"{proposal["run_revision"]}"',
            },
        )
    finally:
        app_planner._conversation_event = original_event
    assert approved.status_code == 200, approved.text
    assert worker is not None
    worker.join(timeout=2)
    assert revoked.is_set()


def test_expired_or_current_profile_changed_authority_blocks_without_effects(
    v2_plan: tuple,
) -> None:
    client, headers, planner, run, _, plan = v2_plan
    conversation_id = planner.get(run["id"])["conversation_id"]
    client.app.state.proposals.qualifications.clock = lambda: time.time() + 3_600
    expired = client.post(
        f"/v1/conversations/{conversation_id}/proposals",
        json=_proposal_request(run, plan),
        headers={**headers, "Idempotency-Key": "expired-authority"},
    )
    assert expired.json()["reason_code"] == "QUALIFICATION_EXPIRED"
    client.app.state.proposals.qualifications.clock = time.time
    configuration = deepcopy(planner.projects.get_configuration(run["project_id"])["configuration"])
    configuration["resources"]["profiles"][0]["enabled"] = False
    preview = planner.projects.preview_configuration(
        run["project_id"], configuration, command_key="disable-profile", principal="owner"
    )
    planner.projects.apply_configuration(
        run["project_id"],
        preview["preview_id"],
        expected_revision=planner.projects.get(run["project_id"])["revision"],
        command_key="apply-disabled-profile",
        principal="owner",
    )
    changed = client.post(
        f"/v1/conversations/{conversation_id}/proposals",
        json=_proposal_request(run, plan),
        headers={**headers, "Idempotency-Key": "changed-profile"},
    )
    assert changed.json()["reason_code"] in {"PROFILE_DISABLED", "PROFILE_IDENTITY_MISMATCH"}
    assert planner.get(run["id"])["active_plan_revision"] is None


def test_current_source_binding_change_blocks_proposal_approval(v2_plan: tuple) -> None:
    client, headers, planner, run, _, plan = v2_plan
    conversation_id = planner.get(run["id"])["conversation_id"]
    proposal = client.post(
        f"/v1/conversations/{conversation_id}/proposals",
        json=_proposal_request(run, plan),
        headers={**headers, "Idempotency-Key": "source-before-change"},
    ).json()
    configuration = deepcopy(planner.projects.get_configuration(run["project_id"])["configuration"])
    replacement_channel = deepcopy(configuration["resources"]["channels"][0])
    replacement_channel["id"] = "fixture-channel-b"
    configuration["resources"]["channels"].append(replacement_channel)
    binding = configuration["resources"]["profiles"][0]["profile"]["binding"]
    binding["channel_id"] = "fixture-channel-b"
    preview = planner.projects.preview_configuration(
        run["project_id"], configuration, command_key="change-source", principal="owner"
    )
    planner.projects.apply_configuration(
        run["project_id"],
        preview["preview_id"],
        expected_revision=planner.projects.get(run["project_id"])["revision"],
        command_key="apply-source-change",
        principal="owner",
    )
    blocked = client.post(
        f"/v1/runs/{run['id']}/plan-approval",
        json={
            **approval(plan),
            "conversation_id": conversation_id,
            "proposal_revision": proposal["proposal_revision"],
        },
        headers={
            "If-Match": f'"{proposal["run_revision"]}"',
            **headers,
            "Idempotency-Key": "source-after-change",
        },
    )
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["reason_code"] == "PROFILE_IDENTITY_MISMATCH"
    assert planner.get(run["id"])["active_plan_revision"] is None
