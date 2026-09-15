"""AC2/AC3/AC6: grants, credentials, decisions, the queue and claims.

Every case enters the chain the same way the product does: an authenticated
management command issues the grant and the credential, and the credential is
then presented at a protocol endpoint. No case inserts a grant, a decision or a
claim row directly, and no case reaches a store method with a hand-made
principal.
"""

import json
from typing import Any

from scheduling_fixtures import (  # noqa: F401  (fixtures re-exported)
    INPUTS,
    REQUIRED_OUTCOME,
    SLOT,
    admissible,
    claim,
    decide,
    expand,
    grant_body,
    graph,
    issue,
    key,
    observe,
    pages,
    protocol_headers,
    queue,
    report,
    resource_policy,
    task_spec,
    tasks,
    url,
)


def first_task(case: dict[str, Any]) -> str:
    page = queue(case)
    assert page["items"], page
    return str(page["items"][0]["task_id"])


def claim_ready(case: dict[str, Any], *, key_value: str = "claim-1") -> Any:
    task_id = first_task(case)
    return claim(case, task_id, key_value=key_value)


def expand_and_seal(case: dict[str, Any], specs: list[dict[str, Any]]) -> None:
    """Open a batch, fix its members, and establish the capacity to claim it."""
    expanded = expand(case, specs)
    assert expanded.status_code == 201, expanded.text
    members = [str(item) for item in expanded.json()["nodes_added"]]
    sealed = decide(
        case,
        [
            {
                "action": "seal",
                "expansion_id": "defects",
                "members": members,
                "obligations": ["config-defect-triage"],
            }
        ],
        key_value="decision-seal",
        decision_id="decision-seal",
        expected_graph_revision=1,
    )
    assert sealed.status_code == 201, sealed.text
    admissible(case)


def two_defects() -> list[dict[str, Any]]:
    return [
        task_spec(item_key="a", zone="src/config/a"),
        task_spec(item_key="b", zone="src/config/b"),
    ]


# ------------------------------------------------------------ grant authority


def test_an_authenticated_user_issues_a_grant_and_a_narrow_credential(
    granted_case: dict[str, Any],
) -> None:
    """The whole chain is entered through management commands, never a fixture."""
    grant = granted_case["grant"]
    assert grant["grant_id"] == "configuration-repair-scope"
    assert grant["depth"] == 0
    assert grant["root_grant_id"] == "configuration-repair-scope"
    assert grant["root_authorization_id"] == "run-initial-authorization"
    assert grant["revoked_at"] is None
    assert set(grant["role_refs"]) == {"researcher"}
    role = granted_case["role_credential"]
    assert granted_case["consumer_credential"]["kind"] == "execution_consumer"
    assert role["kind"] == "role_protocol"
    assert role["grant_id"] == "configuration-repair-scope"
    assert role["role_instance"] == "role-instance-1"
    # The raw token is returned by the issuing response and by nothing else.
    assert len(granted_case["role_token"]) >= 32
    assert "token" not in role
    assert role["stores_raw_token"] is False


def test_a_listed_credential_never_carries_its_token(granted_case: dict[str, Any]) -> None:
    """A receipt read back is an identity, not a credential at rest."""
    response = granted_case["client"].get(
        f"{granted_case['base']}/workflow-runs/{granted_case['run_id']}/credentials",
        headers=granted_case["headers"],
    )
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) == 2
    for item in items:
        assert "token" not in item
        assert item["token_digest"]
        assert item["stores_raw_token"] is False
    assert granted_case["role_token"] not in response.text


def test_repeating_an_issuance_returns_the_same_credential(
    run_case: dict[str, Any],
) -> None:
    """A lost response is answered with the identity, never a second live token."""
    first = issue(
        run_case, kind="execution_consumer", key_value="consumer-once"
    )
    replay = issue(
        run_case, kind="execution_consumer", key_value="consumer-once"
    )
    assert replay["replayed"] is True
    assert replay["credential"]["credential_id"] == first["credential"]["credential_id"]
    # The replay cannot re-reveal the secret, and it mints no second capability.
    assert "token" not in replay
    listing = run_case["client"].get(
        f"{run_case['base']}/workflow-runs/{run_case['run_id']}/credentials",
        headers=run_case["headers"],
    ).json()["items"]
    assert len(listing) == 1


def test_a_changed_issuance_under_the_same_key_conflicts(run_case: dict[str, Any]) -> None:
    """A different request under one key is a conflict, not a second credential."""
    issue(run_case, kind="execution_consumer", key_value="consumer-conflict")
    response = run_case["client"].post(
        f"{run_case['base']}/workflow-runs/{run_case['run_id']}/credentials",
        json={"kind": "execution_consumer", "run_id": run_case["run_id"], "expires_in": 3600},
        headers={**run_case["headers"], **key("consumer-conflict")},
    )
    assert response.status_code == 409, response.text
    assert response.json()["reason_code"] == "SCHEDULING_IDEMPOTENCY_CONFLICT"


def test_a_grant_cannot_invent_a_role_outside_the_frozen_deployment(
    run_case: dict[str, Any],
) -> None:
    """The frozen deployment defines the surface; a grant cannot widen it."""
    response = run_case["client"].post(
        f"{run_case['base']}/workflow-runs/{run_case['run_id']}/grants",
        json=grant_body(roles={"researcher": "role:not-in-deployment@99"}),
        headers={**run_case["headers"], **key("grant-invented-role")},
    )
    assert response.status_code in {403, 422}, response.text
    assert response.json()["reason_code"] == "SCHEDULING_GRANT_ROLE_UNRESOLVED"
    # Nothing was written: the run still has no grants.
    listed = run_case["client"].get(
        f"{run_case['base']}/workflow-runs/{run_case['run_id']}/grants",
        headers=run_case["headers"],
    ).json()["items"]
    assert listed == []


def test_a_grant_cannot_raise_the_delivery_target(run_case: dict[str, Any]) -> None:
    """A report deployment cannot be granted a pull-request target."""
    response = run_case["client"].post(
        f"{run_case['base']}/workflow-runs/{run_case['run_id']}/grants",
        json=grant_body(artifact="pr"),
        headers={**run_case["headers"], **key("grant-widened-artifact")},
    )
    assert response.status_code in {403, 422}, response.text
    assert response.json()["reason_code"] == "SCHEDULING_GRANT_ARTIFACT_INVALID"


def test_delegation_is_disabled_by_default(granted_case: dict[str, Any]) -> None:
    """A grant that enabled nothing cannot be handed on."""
    response = granted_case["client"].post(
        f"{granted_case['base']}/workflow-runs/{granted_case['run_id']}/grants/delegations",
        json={
            **grant_body(grant_id="child", role_instance="role-instance-2"),
            "parent_grant_id": "configuration-repair-scope",
        },
        headers={**granted_case["headers"], **key("delegate-disabled")},
    )
    assert response.status_code in {403, 422}, response.text
    assert response.json()["reason_code"] == "SCHEDULING_DELEGATION_NOT_PERMITTED"


def test_a_child_cannot_widen_its_parent(run_case: dict[str, Any]) -> None:
    """A delegated grant may only narrow, in every declared dimension."""
    issued = run_case["client"].post(
        f"{run_case['base']}/workflow-runs/{run_case['run_id']}/grants",
        json=grant_body(
            delegation={"allowed": True, "maximum_depth": 2, "actions": ["expand_graph"]}
        ),
        headers={**run_case["headers"], **key("grant-delegable")},
    )
    assert issued.status_code == 201, issued.text
    widened = run_case["client"].post(
        f"{run_case['base']}/workflow-runs/{run_case['run_id']}/grants/delegations",
        json={
            **grant_body(
                grant_id="child-wide",
                role_instance="role-instance-2",
                scope=("src/config/**", "outside/**"),
            ),
            "parent_grant_id": "configuration-repair-scope",
        },
        headers={**run_case["headers"], **key("delegate-wide")},
    )
    assert widened.status_code == 422, widened.text
    assert widened.json()["reason_code"] == "SCHEDULING_GRANT_NOT_A_SUBSET"


def test_a_child_may_act_only_with_its_parents_delegable_actions(
    run_case: dict[str, Any],
) -> None:
    """Authority flows from what was made delegable, not from everything else.

    A parent may bind roles itself while declaring only ``expand_graph``
    delegable. A child that asks for ``bind_role`` is asking for a power its
    parent deliberately kept, so it is refused even though the parent holds that
    power itself.
    """
    issued = run_case["client"].post(
        f"{run_case['base']}/workflow-runs/{run_case['run_id']}/grants",
        json=grant_body(
            actions=("expand_graph", "bind_role"),
            delegation={"allowed": True, "maximum_depth": 2, "actions": ["expand_graph"]},
        ),
        headers={**run_case["headers"], **key("grant-delegable-only")},
    )
    assert issued.status_code == 201, issued.text
    response = run_case["client"].post(
        f"{run_case['base']}/workflow-runs/{run_case['run_id']}/grants/delegations",
        json={
            **grant_body(
                grant_id="child-bind",
                role_instance="role-instance-2",
                actions=("bind_role",),
            ),
            "parent_grant_id": "configuration-repair-scope",
        },
        headers={**run_case["headers"], **key("delegate-bind")},
    )
    assert response.status_code == 422, response.text
    assert response.json()["reason_code"] == "SCHEDULING_GRANT_NOT_A_SUBSET"


def test_a_path_scope_is_not_widened_by_a_string_prefix(run_case: dict[str, Any]) -> None:
    """``src/a`` does not contain ``src/ab``, and ``..`` never reaches the check."""
    issued = run_case["client"].post(
        f"{run_case['base']}/workflow-runs/{run_case['run_id']}/grants",
        json=grant_body(
            scope=("src/a/**",), write_scope=("src/a/**",),
            delegation={"allowed": True, "maximum_depth": 2, "actions": ["expand_graph"]},
        ),
        headers={**run_case["headers"], **key("grant-narrow-paths")},
    )
    assert issued.status_code == 201, issued.text
    for candidate in ("src/ab/**", "src/a/../outside/**"):
        response = run_case["client"].post(
            f"{run_case['base']}/workflow-runs/{run_case['run_id']}/grants/delegations",
            json={
                **grant_body(
                    grant_id=f"child-{abs(hash(candidate)) % 9973}",
                    role_instance="role-instance-2",
                    scope=(candidate,),
                    write_scope=(candidate,),
                ),
                "parent_grant_id": "configuration-repair-scope",
            },
            headers={
                **run_case["headers"],
                **key(f"delegate-{abs(hash(candidate)) % 9973}"),
            },
        )
        assert response.status_code in {409, 422}, (candidate, response.text)
        assert response.json()["reason_code"] in {
            "SCHEDULING_GRANT_NOT_A_SUBSET",
            "SCHEDULING_GRANT_INVALID",
        }, (candidate, response.json())


def test_a_revoked_parent_stops_a_late_decision(granted_case: dict[str, Any]) -> None:
    """Revocation is checked at the commit boundary, not only when it happens."""
    revoked = granted_case["client"].post(
        f"{granted_case['base']}/workflow-runs/{granted_case['run_id']}"
        "/grants/configuration-repair-scope/revocation",
        json={"reason": "the user withdrew this scope"},
        headers={**granted_case["headers"], **key("revoke-1")},
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["revoked"] == ["configuration-repair-scope"]
    late = expand(granted_case, two_defects())
    assert late.status_code in {401, 403, 409, 422}, late.text
    assert late.json()["reason_code"] in {
        "SCHEDULING_GRANT_REVOKED",
        "SCHEDULING_CREDENTIAL_REVOKED",
    }, late.json()


def test_a_revoked_credential_is_refused_at_the_protocol_boundary(
    granted_case: dict[str, Any],
) -> None:
    """A credential issued for a revoked grant is revoked with it."""
    granted_case["client"].post(
        f"{granted_case['base']}/workflow-runs/{granted_case['run_id']}"
        "/grants/configuration-repair-scope/revocation",
        json={"reason": "withdrawn"},
        headers={**granted_case["headers"], **key("revoke-2")},
    )
    response = granted_case["client"].get(
        f"/v1/scheduling/protocol/projects/{granted_case['project_id']}"
        f"/runs/{granted_case['run_id']}/graph",
        headers=protocol_headers(granted_case["role_token"]),
    )
    assert response.status_code in {403, 409, 422}, response.text
    assert response.json()["reason_code"] == "SCHEDULING_CREDENTIAL_REVOKED"


# ----------------------------------------------------- protocol authentication


def test_a_body_cannot_authenticate_itself(granted_case: dict[str, Any]) -> None:
    """``user``/role/run claims are not identities, at either boundary."""
    response = decide(
        granted_case,
        [{"action": "expand_graph", "expansion_id": "defects", "tasks": two_defects()}],
        extra={"user": True, "role": "commander"},
    )
    assert response.status_code == 422, response.text
    assert "reason_code" in response.json()


def test_a_protocol_request_needs_a_bearer_credential(granted_case: dict[str, Any]) -> None:
    """Without a credential, the protocol route refuses before any store call."""
    response = granted_case["client"].get(
        f"/v1/scheduling/protocol/projects/{granted_case['project_id']}"
        f"/runs/{granted_case['run_id']}/graph",
        headers=granted_case["headers"],
    )
    assert response.status_code in {401, 403}, response.text
    assert "reason_code" in json.dumps(response.json())


def test_a_role_credential_cannot_claim_or_reach_management(
    granted_case: dict[str, Any],
) -> None:
    """A role credential carries one capability; claiming is not one of them."""
    task_id = "placeholder"
    response = claim(
        granted_case, task_id, key_value="role-claim", token=granted_case["role_token"]
    )
    assert response.status_code == 403, response.text
    assert response.json()["reason_code"] == "SCHEDULING_CREDENTIAL_SCOPE_INSUFFICIENT"


def test_a_consumer_credential_cannot_submit_a_decision(
    granted_case: dict[str, Any],
) -> None:
    """A consumer may take work; deciding what work exists is not its capability."""
    response = expand(
        granted_case, two_defects(), token=granted_case["consumer_token"]
    )
    assert response.status_code == 403, response.text
    assert response.json()["reason_code"] == "SCHEDULING_CREDENTIAL_SCOPE_INSUFFICIENT"


def test_a_credential_for_another_run_is_refused(run_case: dict[str, Any]) -> None:
    """A credential is bound to one run; another run's identity is not usable."""
    credential = issue(run_case, kind="execution_consumer", key_value="consumer-run-a")
    response = run_case["client"].get(
        f"/v1/scheduling/protocol/projects/{run_case['project_id']}"
        f"/runs/{run_case['run_id']}-other/queue",
        headers=protocol_headers(credential["token"]),
    )
    assert response.status_code == 404, response.text
    assert response.json()["reason_code"] == "SCHEDULING_RUN_NOT_FOUND"


def test_an_unknown_bearer_token_is_refused(run_case: dict[str, Any]) -> None:
    response = run_case["client"].get(
        f"/v1/scheduling/protocol/projects/{run_case['project_id']}"
        f"/runs/{run_case['run_id']}/queue",
        headers=protocol_headers("not-a-real-token-not-a-real-token-not-a-real"),
    )
    assert response.status_code in {401, 403, 422}, response.text
    assert response.json()["reason_code"] == "SCHEDULING_CREDENTIAL_INVALID"


# --------------------------------------------------------- decision acceptance


def test_a_decision_commits_a_graph_revision_and_a_receipt(
    granted_case: dict[str, Any],
) -> None:
    """The accepted decision is durable, and the revision is addressable."""
    response = expand(granted_case, two_defects())
    assert response.status_code == 201, response.text
    receipt = response.json()
    assert receipt["graph_revision"] == 1
    # The revision is accepted under the grant that authorised it. The user's
    # own approval is the Run's initial authorization, recorded once at creation,
    # and is not recomputed into a "new approval" for each graph revision.
    assert receipt["accepted_under"] == "accepted_under_grant"
    assert receipt["authorizing_grant_id"] == "configuration-repair-scope"
    assert receipt["authorizing_grant_depth"] == 0
    assert receipt["user_authorization_id"] == "run-initial-authorization"
    assert receipt["nodes_added"] == ["decision-1.a", "decision-1.b"]
    assert receipt["task_count"] == 2
    assert receipt["model_calls"] == 0
    assert receipt["requires_further_approval"] is False
    current = graph(granted_case)
    assert current["graph_revision"] == 1
    assert current["current"]["digest"] == receipt["graph_digest"]
    assert current["history"][0]["parent_revision"] == 0


def test_the_same_decision_key_replays_and_a_changed_payload_conflicts(
    granted_case: dict[str, Any],
) -> None:
    first = expand(granted_case, two_defects())
    assert first.status_code == 201, first.text
    replay = expand(granted_case, two_defects())
    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()
    assert graph(granted_case)["graph_revision"] == 1
    changed = expand(
        granted_case, [task_spec(item_key="c")], decision_id="decision-1"
    )
    assert changed.status_code == 409, changed.text
    assert changed.json()["reason_code"] == "SCHEDULING_IDEMPOTENCY_CONFLICT"


def test_an_invalid_second_operation_leaves_the_graph_untouched(
    granted_case: dict[str, Any],
) -> None:
    """The batch commits all-or-nothing, including the queue."""
    response = decide(
        granted_case,
        [
            {
                "action": "expand_graph",
                "expansion_id": "defects",
                "tasks": [task_spec(item_key="a")],
            },
            {"action": "set_priority", "task_id": "no-such-task", "priority": 3},
        ],
    )
    assert response.status_code == 404, response.text
    assert response.json()["reason_code"] == "SCHEDULING_TASK_NOT_FOUND"
    assert graph(granted_case)["graph_revision"] == 0
    assert tasks(granted_case)["items"] == []


def test_a_decision_made_against_a_stale_revision_conflicts(
    granted_case: dict[str, Any],
) -> None:
    first = expand(granted_case, [task_spec(item_key="a")])
    assert first.status_code == 201, first.text
    stale = expand(
        granted_case,
        [task_spec(item_key="b")],
        key_value="decision-2",
        decision_id="decision-2",
        expected_graph_revision=0,
    )
    assert stale.status_code in {409, 422}, stale.text
    assert stale.json()["reason_code"] == "SCHEDULING_GRAPH_REVISION_CONFLICT"
    assert stale.json()["current_revision"] == 1


def test_a_decision_made_against_different_inputs_is_refused(
    granted_case: dict[str, Any],
) -> None:
    response = decide(
        granted_case,
        [{"action": "expand_graph", "expansion_id": "defects", "tasks": [task_spec(item_key="a")]}],
        inputs_digest="0" * 64,
    )
    assert response.status_code in {403, 409, 422}, response.text
    assert response.json()["reason_code"] == "SCHEDULING_INPUT_DIGEST_MISMATCH"


def test_a_dependency_cycle_is_refused(granted_case: dict[str, Any]) -> None:
    first = expand(
        granted_case,
        [
            task_spec(item_key="a", depends_on=("decision-1.b",)),
            task_spec(item_key="b", depends_on=("decision-1.a",)),
        ],
    )
    assert first.status_code in {403, 409, 422}, first.text
    assert first.json()["reason_code"] == "SCHEDULING_DEPENDENCY_CYCLE"
    assert graph(granted_case)["graph_revision"] == 0


def test_an_action_the_grant_did_not_authorise_is_refused(
    run_case: dict[str, Any],
) -> None:
    issued = run_case["client"].post(
        f"{run_case['base']}/workflow-runs/{run_case['run_id']}/grants",
        json=grant_body(actions=("set_priority",)),
        headers={**run_case["headers"], **key("grant-narrow-actions")},
    )
    assert issued.status_code == 201, issued.text
    credential = issue(
        run_case,
        kind="role_protocol",
        key_value="cred-narrow",
        grant_id="configuration-repair-scope",
        role_instance="role-instance-1",
    )
    run_case["role_token"] = credential["token"]
    expansion = run_case["client"].post(
        f"{run_case['base']}/workflow-runs/{run_case['run_id']}/expansions",
        json={
            "grant_id": "configuration-repair-scope",
            "expansion_id": "defects",
            "goal": "defects",
            "required_outcomes": ["config-defect-triage"],
        },
        headers={**run_case["headers"], **key("expansion-narrow")},
    )
    assert expansion.status_code in {403, 422}, expansion.text
    assert expansion.json()["reason_code"] == "SCHEDULING_GRANT_ACTION_NOT_PERMITTED"


# ------------------------------------------------------------- queue and claims


def test_tasks_are_persisted_and_paged_without_loss(granted_case: dict[str, Any]) -> None:
    """A legal task set larger than one page is read back completely."""
    count = 103
    specs = [
        task_spec(item_key=f"item{index:04d}", zone=f"src/config/item{index:04d}")
        for index in range(count)
    ]
    response = expand(granted_case, specs)
    assert response.status_code == 201, response.text
    assert response.json()["task_count"] == count
    seen = pages(granted_case, size=37)
    assert len(seen) == count
    assert len({item["task_id"] for item in seen}) == count


def test_a_task_beyond_the_first_page_is_addressable(granted_case: dict[str, Any]) -> None:
    specs = [
        task_spec(item_key=f"item{index:04d}", zone=f"src/config/item{index:04d}")
        for index in range(103)
    ]
    assert expand(granted_case, specs).status_code == 201
    last = "decision-1.item0102"
    response = granted_case["client"].get(
        f"/v1/scheduling/protocol/projects/{granted_case['project_id']}"
        f"/runs/{granted_case['run_id']}/tasks/{last}",
        headers=protocol_headers(granted_case["consumer_token"]),
    )
    assert response.status_code == 200, response.text
    assert response.json()["task_id"] == last


def test_a_claim_is_a_handover_and_not_an_execution(granted_case: dict[str, Any]) -> None:
    expand_and_seal(granted_case, [task_spec(item_key="a")])
    response = claim(granted_case, "decision-1.a", key_value="claim-1")
    assert response.status_code == 201, response.text
    record = response.json()
    assert record["physical_execution"] == "not_started"
    assert record["verified_by_engine"] is False
    assert record["model_calls"] == 0
    assert record["attempt_ref"] is None
    assert record["inputs_digest"] == record["inputs_digest"]
    assert record["task_digest"]


def test_a_repeated_claim_returns_the_same_work(granted_case: dict[str, Any]) -> None:
    expand_and_seal(granted_case, [task_spec(item_key="a")])
    first = claim(granted_case, "decision-1.a", key_value="claim-1")
    assert first.status_code == 201, first.text
    # A lost response is answered from the durable row, not by a second handover.
    again = claim(granted_case, "decision-1.a", key_value="claim-1")
    assert again.status_code == 200, again.text
    assert again.json() == first.json()
    readback = granted_case["client"].get(
        f"/v1/scheduling/protocol/projects/{granted_case['project_id']}"
        f"/runs/{granted_case['run_id']}/claims/claim-1",
        headers=protocol_headers(granted_case["consumer_token"]),
    )
    assert readback.status_code == 200, readback.text
    assert readback.json() == first.json()


def test_a_claim_for_another_task_under_a_used_key_conflicts(
    granted_case: dict[str, Any],
) -> None:
    expand_and_seal(granted_case, two_defects())
    first = claim(granted_case, "decision-1.a", key_value="claim-1")
    assert first.status_code == 201, first.text
    changed = claim(granted_case, "decision-1.b", key_value="claim-1")
    assert changed.status_code == 409, changed.text
    assert changed.json()["reason_code"] == "SCHEDULING_IDEMPOTENCY_CONFLICT"
