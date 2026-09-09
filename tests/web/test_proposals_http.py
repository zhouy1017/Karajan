"""Conversation-bound owner-intent proposals use the real v2 controller fixture."""

from copy import deepcopy

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
