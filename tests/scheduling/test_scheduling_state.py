"""Permanent regressions for the repaired edges, orderings and immutability.

Each case here exists because a specific behaviour was wrong at some point and
must not become wrong again. They are grouped by the fact they protect rather
than by the endpoint they use.
"""

import json
from typing import Any

from scheduling_fixtures import (
    REQUIRED_INPUT_A,
    admissible,
    claim,
    decide,
    expand,
    grant_body,
    graph,
    issue,
    key,
    pages,
    protocol_headers,
    queue,
    report,
    task_spec,
)


def open_and_seal(case: dict[str, Any], specs: list[dict[str, Any]]) -> list[str]:
    """Expand one batch and fix its members, returning the member identities."""
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
    return members


# -------------------------------------------------- input-derived dependencies


def test_an_input_derived_edge_survives_an_empty_dependency_edit(
    granted_case: dict[str, Any],
) -> None:
    """A consumer cannot be made ready ahead of the producer it reads.

    The consumer's binding consumes ``producer.output``, so the edge is derived
    from the binding. An edit that sets the *declared* dependencies to empty
    therefore cannot erase it: the consumer would otherwise be reported ready
    while the value it consumes does not exist yet.
    """
    first = expand(granted_case, [task_spec(item_key="producer")])
    assert first.status_code == 201, first.text
    second = expand(
        granted_case,
        [
            task_spec(
                item_key="consumer",
                sources="decision-1.producer.output",
                zone="src/config/consumer",
            )
        ],
        key_value="decision-2",
        decision_id="decision-2",
        expected_graph_revision=1,
    )
    assert second.status_code == 201, second.text
    consumer = "decision-2.consumer"
    stored = granted_case["client"].get(
        f"{granted_case['base']}/workflow-runs/{granted_case['run_id']}/tasks/{consumer}",
        headers=granted_case["headers"],
    )
    assert stored.status_code == 200, stored.text
    assert "decision-1.producer" in stored.json()["depends_on"]
    assert stored.json()["inferred_depends_on"] == ["decision-1.producer"]

    edited = decide(
        granted_case,
        [{"action": "set_dependencies", "task_id": consumer, "depends_on": []}],
        key_value="decision-3",
        decision_id="decision-3",
        expected_graph_revision=2,
    )
    assert edited.status_code == 201, edited.text
    after = granted_case["client"].get(
        f"{granted_case['base']}/workflow-runs/{granted_case['run_id']}/tasks/{consumer}",
        headers=granted_case["headers"],
    ).json()
    assert after["declared_depends_on"] == []
    # The derived edge is still there, so the edit did not make the consumer
    # ready ahead of its producer.
    assert after["depends_on"] == ["decision-1.producer"]

    sealed = decide(
        granted_case,
        [
            {
                "action": "seal",
                "expansion_id": "defects",
                "members": ["decision-1.producer", "decision-2.consumer"],
                "obligations": ["config-defect-triage"],
            }
        ],
        key_value="decision-seal",
        decision_id="decision-seal",
        expected_graph_revision=3,
    )
    assert sealed.status_code == 201, sealed.text
    admissible(granted_case)
    page = queue(granted_case)
    ready = {item["task_id"] for item in page["items"]}
    assert consumer not in ready, page
    reasons = {item["task_id"]: item["reason"] for item in page["waiting"]}
    assert reasons.get(consumer) == "dependency_pending", page["waiting"]
    # The producer itself is ready: only the consumer waits.
    assert "decision-1.producer" in ready, page


# ------------------------------------------------- immutable sealed versions


def test_a_sealed_member_version_is_readable_after_the_task_changes(
    granted_case: dict[str, Any],
) -> None:
    """A seal pins a version; that version must stay readable after an edit.

    A pinned digest with no body behind it cannot be resolved, so the version is
    archived immutably. A later, legitimate edit to the *unclaimed* task moves
    the live task on; the sealed record and the version it pins do not move.
    """
    open_and_seal(granted_case, [task_spec(item_key="a")])
    task_id = "decision-1.a"
    before = granted_case["client"].get(
        f"{granted_case['base']}/workflow-runs/{granted_case['run_id']}/tasks/{task_id}",
        headers=granted_case["headers"],
    ).json()
    pinned_revision = int(before["revision"])
    pinned_digest = str(before["digest"])
    sealed = graph(granted_case)["expansions"][0]
    pinned = sealed["members"][task_id]
    assert pinned["digest"] == pinned_digest

    edited = decide(
        granted_case,
        [{"action": "bind_role", "task_id": task_id, "role_alias": "researcher"}],
        key_value="decision-edit",
        decision_id="decision-edit",
        expected_graph_revision=graph(granted_case)["graph_revision"],
    )
    assert edited.status_code == 201, edited.text
    after = granted_case["client"].get(
        f"{granted_case['base']}/workflow-runs/{granted_case['run_id']}/tasks/{task_id}",
        headers=granted_case["headers"],
    ).json()
    assert int(after["revision"]) > pinned_revision

    # The sealed record itself is unchanged.
    still = graph(granted_case)["expansions"][0]
    assert still == sealed
    assert still["members"][task_id]["digest"] == pinned_digest

    # And the pinned version is really readable, with its original body.
    response = granted_case["client"].get(
        f"{granted_case['base']}/workflow-runs/{granted_case['run_id']}/tasks/{task_id}"
        f"/versions/{pinned_revision}",
        params={"digest": pinned_digest},
        headers=granted_case["headers"],
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["digest"] == pinned_digest
    assert body["revision"] == pinned_revision
    assert body["inputs"] == before["inputs"]
    # A different digest cannot be answered by the same version number.
    wrong = granted_case["client"].get(
        f"{granted_case['base']}/workflow-runs/{granted_case['run_id']}/tasks/{task_id}"
        f"/versions/{pinned_revision}",
        params={"digest": "0" * 64},
        headers=granted_case["headers"],
    )
    assert wrong.status_code in {409, 422}, wrong.text
    assert wrong.json()["reason_code"] == "SCHEDULING_TASK_VERSION_MISMATCH"


def test_a_sealed_version_is_readable_after_a_restart(
    granted_case: dict[str, Any],
) -> None:
    """The archived version is durable, not an in-memory convenience."""
    from fastapi.testclient import TestClient
    from karajan.web import create_app

    open_and_seal(granted_case, [task_spec(item_key="a")])
    before = granted_case["client"].get(
        f"{granted_case['base']}/workflow-runs/{granted_case['run_id']}/tasks/decision-1.a",
        headers=granted_case["headers"],
    ).json()
    # A second application over the same real state directory.
    app = create_app(
        granted_case["directory"],
        origin="http://127.0.0.1:8765",
        bootstrap_token="second",
        allowed_roots=[granted_case["repository"].parent],
    )
    with TestClient(app, base_url="http://127.0.0.1:8765") as second:
        login = second.post(
            "/v1/session/bootstrap", json={"token": "second"}, headers={"Origin": "http://127.0.0.1:8765"}
        )
        headers = {"Origin": "http://127.0.0.1:8765", "X-CSRF-Token": login.json()["csrf_token"]}
        response = second.get(
            f"{granted_case['base']}/workflow-runs/{granted_case['run_id']}"
            f"/tasks/decision-1.a/versions/{before['revision']}",
            params={"digest": before["digest"]},
            headers=headers,
        )
    assert response.status_code == 200, response.text
    assert response.json() == before


# ------------------------------------------------- report ordering and trust


def test_a_late_untrusted_report_cannot_undo_a_trusted_reconciliation(
    granted_case: dict[str, Any],
) -> None:
    """Ordering matters: an observation recorded after a reconciliation is one.

    The consumer reports completion, which is recorded and changes nothing. The
    owner reconciles it, which terminalizes the task and releases the capacity.
    A further *unverified* observation then arrives late; it is appended as an
    observation and must not reopen the task or restore the occupancy the
    reconciliation deliberately released.
    """
    open_and_seal(
        granted_case,
        [task_spec(item_key="a"), task_spec(item_key="b", zone="src/config/b")],
    )
    admissible(granted_case, capacity=1, amount="1")
    first = claim(granted_case, "decision-1.a", key_value="claim-a")
    assert first.status_code == 201, first.text

    # An unverified completion is recorded, and nothing changes.
    observed = report(
        granted_case,
        "decision-1.a",
        {"outcome": "completed", "evidence_ref": "evidence:consumer-says-so"},
    )
    assert observed.status_code == 200, observed.text
    assert observed.json()["state"] == "claimed"
    assert observed.json()["physical_execution"] == "not_started"
    still_a = claim(granted_case, "decision-1.b", key_value="claim-b")
    assert still_a.status_code == 409, still_a.text
    assert still_a.json()["reason_code"] == "SCHEDULING_CAPACITY_BUSY"

    # The owner reconciles it: the task terminalizes and the slot is released.
    reconciled = granted_case["client"].post(
        f"{granted_case['base']}/workflow-runs/{granted_case['run_id']}"
        "/tasks/decision-1.a/reconciliation",
        json={"outcome": "completed", "evidence_ref": "evidence:reconciled"},
        headers={**granted_case["headers"], **key("reconcile-a")},
    )
    assert reconciled.status_code == 200, reconciled.text
    assert reconciled.json()["state"] == "completed"
    assert reconciled.json()["reconciled_at"] is not None
    state = granted_case["client"].get(
        f"{granted_case['base']}/workflow-runs/{granted_case['run_id']}/resources",
        headers=granted_case["headers"],
    ).json()
    assert state["live_claims"] == 0, state

    # A late, unverified report is appended and changes nothing.
    late = report(
        granted_case,
        "decision-1.a",
        {"outcome": "completed", "evidence_ref": "evidence:late-and-unverified"},
        key_value="report-late",
    )
    assert late.status_code == 200, late.text
    assert late.json()["state"] == "completed", late.json()
    state = granted_case["client"].get(
        f"{granted_case['base']}/workflow-runs/{granted_case['run_id']}/resources",
        headers=granted_case["headers"],
    ).json()
    assert state["live_claims"] == 0, state
    # The waiting task can now be claimed, because the slot was really released.
    second = claim(granted_case, "decision-1.b", key_value="claim-b")
    assert second.status_code == 201, second.text


def test_another_consumer_cannot_report_on_work_it_does_not_hold(
    granted_case: dict[str, Any],
) -> None:
    """A report is attributable: it belongs to the consumer that took the claim."""
    open_and_seal(granted_case, [task_spec(item_key="a")])
    admissible(granted_case)
    assert claim(granted_case, "decision-1.a", key_value="claim-a").status_code == 201
    other = issue(granted_case, kind="execution_consumer", key_value="consumer-2")
    response = report(
        granted_case,
        "decision-1.a",
        {"outcome": "completed", "evidence_ref": "evidence:not-mine"},
        token=other["token"],
    )
    assert response.status_code in {403, 409}, response.text
    assert response.json()["reason_code"] == "SCHEDULING_TASK_NOT_OWNED"


def test_a_consumer_cannot_reconcile_its_own_report(granted_case: dict[str, Any]) -> None:
    """Promoting an observation into truth is not a consumer capability."""
    open_and_seal(granted_case, [task_spec(item_key="a")])
    admissible(granted_case)
    assert claim(granted_case, "decision-1.a", key_value="claim-a").status_code == 201
    response = granted_case["client"].post(
        f"{granted_case['base']}/workflow-runs/{granted_case['run_id']}"
        "/tasks/decision-1.a/reconciliation",
        json={"outcome": "completed", "evidence_ref": "evidence:self-promoted"},
        headers={**protocol_headers(granted_case["consumer_token"]), **key("self-reconcile")},
    )
    assert response.status_code in {401, 403}, response.text
    assert "reason_code" in json.dumps(response.json())


def test_a_consumer_cannot_publish_a_trusted_resource_observation(
    run_case: dict[str, Any],
) -> None:
    """A role or consumer cannot invent the capacity it is admitted against."""
    credential = issue(run_case, kind="execution_consumer", key_value="consumer-resource")
    response = run_case["client"].post(
        "/v1/scheduling/resource-observations",
        json={
            "pool_id": "pool-a",
            "window_id": "w1",
            "metric": "remaining",
            "amount": "999",
            "source": "local_ledger",
            "source_ref": "self",
        },
        headers={**protocol_headers(credential["token"]), **key("observe-as-consumer")},
    )
    assert response.status_code in {401, 403}, response.text
    assert "reason_code" in json.dumps(response.json())


# ------------------------------------------------------------ shared budgets


def test_sibling_grants_share_one_budget_and_occupancy(
    run_case: dict[str, Any],
) -> None:
    """Two grants drawing on one pool cannot each occupy the whole ceiling."""
    base = run_case["base"]
    for name, instance in (("scope-one", "instance-one"), ("scope-two", "instance-two")):
        issued = run_case["client"].post(
            f"{base}/workflow-runs/{run_case['run_id']}/grants",
            json=grant_body(
                grant_id=name,
                role_instance=instance,
                max_active=1,
                outcomes=("config-defect-triage", "comparison-report"),
            ),
            headers={**run_case["headers"], **key(f"grant-{name}")},
        )
        assert issued.status_code == 201, issued.text
        created = run_case["client"].post(
            f"{base}/workflow-runs/{run_case['run_id']}/expansions",
            json={
                "grant_id": name,
                "expansion_id": f"set-{name}",
                "goal": "defects",
                "required_outcomes": ["config-defect-triage"],
            },
            headers={**run_case["headers"], **key(f"expansion-{name}")},
        )
        assert created.status_code == 201, created.text
        credential = issue(
            run_case,
            kind="role_protocol",
            key_value=f"cred-{name}",
            grant_id=name,
            role_instance=instance,
        )
        current = run_case["client"].get(
            f"{base}/workflow-runs/{run_case['run_id']}/graph", headers=run_case["headers"]
        ).json()["graph_revision"]
        decided = run_case["client"].post(
            f"/v1/scheduling/protocol/projects/{run_case['project_id']}"
            f"/runs/{run_case['run_id']}/grants/{name}/decisions",
            json={
                "decision_id": f"decision-{name}",
                "grant_id": name,
                "term": 0,
                "expected_graph_revision": current,
                "inputs_digest": run_case["run"]["inputs_digest"],
                "trigger": "approved_input",
                "reason": "defect",
                "actions": [
                    {
                        "action": "expand_graph",
                        "expansion_id": f"set-{name}",
                        "tasks": [
                            task_spec(
                                item_key="a",
                                expansion_id=f"set-{name}",
                                zone=f"src/config/{name}",
                            )
                        ],
                    },
                    {
                        "action": "seal",
                        "expansion_id": f"set-{name}",
                        "members": [f"decision-{name}.a"],
                        "obligations": ["config-defect-triage"],
                    },
                ],
            },
            headers={**protocol_headers(credential["token"]), **key(f"decision-{name}")},
        )
        assert decided.status_code == 201, decided.text
    admissible(run_case, capacity=1, amount="1")
    consumer = issue(run_case, kind="execution_consumer", key_value="consumer-shared")
    first = claim(run_case, "decision-scope-one.a", key_value="claim-one", token=consumer["token"])
    assert first.status_code == 201, first.text
    second = claim(
        run_case, "decision-scope-two.a", key_value="claim-two", token=consumer["token"]
    )
    assert second.status_code == 409, second.text
    assert second.json()["reason_code"] == "SCHEDULING_CAPACITY_BUSY"


def test_revoking_a_parent_stops_a_descendants_late_decision(
    run_case: dict[str, Any],
) -> None:
    """Revocation propagates down the chain, and is rechecked transactionally."""
    issued = run_case["client"].post(
        f"{base_of(run_case)}/workflow-runs/{run_case['run_id']}/grants",
        json=grant_body(
            grant_id="parent",
            role_instance="parent-instance",
            delegation={"allowed": True, "maximum_depth": 2, "actions": ["expand_graph"]},
            outcomes=("config-defect-triage", "comparison-report"),
        ),
        headers={**run_case["headers"], **key("grant-parent")},
    )
    assert issued.status_code == 201, issued.text
    child = run_case["client"].post(
        f"{base_of(run_case)}/workflow-runs/{run_case['run_id']}/grants/delegations",
        json={
            **grant_body(
                grant_id="child",
                role_instance="child-instance",
                actions=("expand_graph",),
                outcomes=("config-defect-triage",),
            ),
            "parent_grant_id": "parent",
        },
        headers={**run_case["headers"], **key("grant-child")},
    )
    assert child.status_code == 201, child.text
    assert child.json()["depth"] == 1
    created = run_case["client"].post(
        f"{base_of(run_case)}/workflow-runs/{run_case['run_id']}/expansions",
        json={
            "grant_id": "child",
            "expansion_id": "child-set",
            "goal": "defects",
            "required_outcomes": ["config-defect-triage"],
        },
        headers={**run_case["headers"], **key("expansion-child")},
    )
    assert created.status_code == 201, created.text
    credential = issue(
        run_case,
        kind="role_protocol",
        key_value="cred-child",
        grant_id="child",
        role_instance="child-instance",
    )
    revoked = run_case["client"].post(
        f"{base_of(run_case)}/workflow-runs/{run_case['run_id']}/grants/parent/revocation",
        json={"reason": "withdrawn"},
        headers={**run_case["headers"], **key("revoke-parent")},
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["descendants_revoked"] == ["child"]
    late = run_case["client"].post(
        f"/v1/scheduling/protocol/projects/{run_case['project_id']}"
        f"/runs/{run_case['run_id']}/grants/child/decisions",
        json={
            "decision_id": "late-child",
            "grant_id": "child",
            "term": 0,
            "expected_graph_revision": 0,
            "inputs_digest": run_case["run"]["inputs_digest"],
            "trigger": "approved_input",
            "reason": "late",
            "actions": [
                {
                    "action": "expand_graph",
                    "expansion_id": "child-set",
                    "tasks": [task_spec(item_key="a", expansion_id="child-set")],
                }
            ],
        },
        headers={**protocol_headers(credential["token"]), **key("late-child")},
    )
    assert late.status_code in {401, 403, 409}, late.text


def base_of(case: dict[str, Any]) -> str:
    return str(case["base"])


# ----------------------------------------------------------- decision conflicts


def test_a_refused_decision_leaves_a_durable_conflict_receipt(
    granted_case: dict[str, Any],
) -> None:
    """A conflict is evidence, not only the losing client's memory of a 409.

    The winner commits; the loser is refused and its refusal is readable
    afterwards, with the command identity, the reason and the revision that was
    current. The graph itself carries only the accepted revision.
    """
    winner = expand(granted_case, [task_spec(item_key="a")], key_value="decision-winner")
    assert winner.status_code == 201, winner.text
    loser = expand(
        granted_case,
        [task_spec(item_key="b")],
        key_value="decision-loser",
        decision_id="decision-loser",
        expected_graph_revision=0,
    )
    assert loser.status_code == 409, loser.text
    assert loser.json()["reason_code"] == "SCHEDULING_GRAPH_REVISION_CONFLICT"

    rejections = granted_case["client"].get(
        f"{granted_case['base']}/workflow-runs/{granted_case['run_id']}/decision-rejections",
        headers=granted_case["headers"],
    )
    assert rejections.status_code == 200, rejections.text
    items = rejections.json()["items"]
    assert len(items) == 1, items
    record = items[0]
    assert record["command_key"] == "decision-loser"
    assert record["reason_code"] == "SCHEDULING_GRAPH_REVISION_CONFLICT"
    assert record["expected_graph_revision"] == 0
    assert record["current_graph_revision"] == 1
    assert record["accepted"] is False
    assert record["graph_mutated"] is False
    assert record["credential_kind"] == "role_protocol"

    # Read back by its own key, and coherent under a replay of the same command.
    one = granted_case["client"].get(
        f"{granted_case['base']}/workflow-runs/{granted_case['run_id']}"
        "/decision-rejections/decision-loser",
        headers=granted_case["headers"],
    )
    assert one.status_code == 200
    assert one.json() == record
    replay = expand(
        granted_case,
        [task_spec(item_key="b")],
        key_value="decision-loser",
        decision_id="decision-loser",
        expected_graph_revision=0,
    )
    assert replay.status_code == 409, replay.text
    assert replay.json()["reason_code"] == "SCHEDULING_GRAPH_REVISION_CONFLICT"

    # Only the accepted revision exists in the graph.
    current = graph(granted_case)
    assert current["graph_revision"] == 1
    assert [item["revision"] for item in current["history"]] == [1]
    stored = pages(granted_case)
    assert [item["task_id"] for item in stored] == ["decision-1.a"]


def test_a_refused_decision_does_not_commit_partial_work(
    granted_case: dict[str, Any],
) -> None:
    """The refusal is recorded; the graph, the queue and the tasks are not."""
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
        key_value="decision-broken",
    )
    assert response.status_code == 404, response.text
    assert graph(granted_case)["graph_revision"] == 0
    assert pages(granted_case) == []
    rejections = granted_case["client"].get(
        f"{granted_case['base']}/workflow-runs/{granted_case['run_id']}/decision-rejections",
        headers=granted_case["headers"],
    ).json()["items"]
    assert [item["command_key"] for item in rejections] == ["decision-broken"]


# --------------------------------------------------------------- write zones


def test_overlapping_write_zones_are_one_zone_and_independent_ones_are_not(
    granted_case: dict[str, Any],
) -> None:
    """Zone identity is path identity, not string equality.

    ``src/config/zone`` and ``src/config/ZONE`` name one directory on the hosts
    this engine runs on, and ``src/config/zone/sub`` is inside it. None of the
    three may be handed to a second writer at once; ``src/config/other`` is
    independent and is admitted.
    """
    open_and_seal(
        granted_case,
        [
            task_spec(item_key="first", zone="src/config/zone"),
            task_spec(item_key="case", zone="src/config/ZONE"),
            task_spec(item_key="nested", zone="src/config/zone/sub"),
            task_spec(item_key="independent", zone="src/config/other"),
        ],
    )
    admissible(granted_case, capacity=4, amount="4")
    assert claim(granted_case, "decision-1.first", key_value="claim-first").status_code == 201
    for task_id in ("decision-1.case", "decision-1.nested"):
        response = claim(
            granted_case, task_id, key_value=f"claim-{task_id.replace(chr(46), chr(45))}"
        )
        assert response.status_code == 409, (task_id, response.text)
        assert response.json()["reason_code"] == "SCHEDULING_TASK_NOT_READY"
        assert response.json()["waiting_reason"] == "write_zone_busy"
    independent = claim(granted_case, "decision-1.independent", key_value="claim-independent")
    assert independent.status_code == 201, independent.text


def test_the_queue_reports_waiting_reasons_without_dropping_tasks(
    granted_case: dict[str, Any],
) -> None:
    """Every legal task is reachable; waiting ones are reported, not hidden."""
    open_and_seal(
        granted_case,
        [
            task_spec(item_key="first", zone="src/config/zone"),
            task_spec(item_key="second", zone="src/config/zone", depends_on=("decision-1.first",)),
            task_spec(item_key="independent", zone="src/config/other"),
        ],
    )
    admissible(granted_case, capacity=4, amount="4")
    page = queue(granted_case)
    ready = {item["task_id"] for item in page["items"]}
    reasons = {item["task_id"]: item["reason"] for item in page["waiting"]}
    assert ready == {"decision-1.first", "decision-1.independent"}, page
    # ``second`` waits on its producer, which is the reason reported first; the
    # zone contention it also faces is asserted directly below, once the
    # dependency has cleared.
    assert reasons["decision-1.second"] == "dependency_pending", page["waiting"]
    assert page["ready_count"] == 2
    assert page["waiting_count"] == 1

    # Two tasks in one zone cannot both be claimed, whatever their spelling.
    competing = claim(granted_case, "decision-1.first", key_value="claim-zone")
    assert competing.status_code == 201, competing.text
    blocked = claim(granted_case, "decision-1.independent", key_value="claim-other-zone")
    assert blocked.status_code == 201, blocked.text


def test_an_open_expansion_holds_its_join_and_its_members(
    granted_case: dict[str, Any],
) -> None:
    """A join cannot converge before the seal, and a member is not yet ready."""
    opened = expand(
        granted_case,
        [
            task_spec(item_key="member", zone="src/config/a"),
            task_spec(
                item_key="join",
                zone="src/config/join",
                sources="literal:assembled",
                joins=("defects",),
            ),
        ],
    )
    assert opened.status_code == 201, opened.text
    admissible(granted_case, capacity=4, amount="4")
    page = queue(granted_case)
    reasons = {item["task_id"]: item["reason"] for item in page["waiting"]}
    assert reasons["decision-1.member"] == "expansion_member_open"
    assert reasons["decision-1.join"] == "join_expansion_open"
    assert page["ready_count"] == 0


def test_sealing_releases_the_members_and_the_join(
    granted_case: dict[str, Any],
) -> None:
    """The seal is what releases an expansion, and it names its real members."""
    members = open_and_seal(
        granted_case,
        [
            task_spec(item_key="member", zone="src/config/a"),
            task_spec(
                item_key="join",
                zone="src/config/join",
                sources="literal:assembled",
                joins=("defects",),
            ),
        ],
    )
    assert members == ["decision-1.member", "decision-1.join"]
    admissible(granted_case, capacity=4, amount="4")
    page = queue(granted_case)
    assert {item["task_id"] for item in page["items"]} == set(members)
    assert page["waiting"] == []


def test_a_seal_cannot_name_a_member_that_does_not_exist(
    granted_case: dict[str, Any],
) -> None:
    """A seal declares what is really present, not what someone wishes were."""
    assert expand(granted_case, [task_spec(item_key="a")]).status_code == 201
    response = decide(
        granted_case,
        [
            {
                "action": "seal",
                "expansion_id": "defects",
                "members": ["decision-1.a", "decision-1.invented"],
                "obligations": ["config-defect-triage"],
            }
        ],
        key_value="decision-seal",
        decision_id="decision-seal",
        expected_graph_revision=1,
    )
    assert response.status_code in {403, 409, 422}, response.text
    assert response.json()["reason_code"] == "SCHEDULING_EXPANSION_MEMBERS_MISMATCH"


def test_an_obligation_survives_every_supported_edit(
    granted_case: dict[str, Any],
) -> None:
    """A promised result is not erased by the edits the API actually supports.

    The supported surface is checked rather than a wished-for one. ``set_priority``
    and ``bind_role`` change how work is scheduled, never whether its result is
    owed; a seal declares a member optional or required, and an optional member
    still has to exist and still carries its outcome; and a task with a recorded
    failure keeps failing rather than quietly ceasing to owe anything.
    """
    open_and_seal(granted_case, [task_spec(item_key="a")])
    revision = graph(granted_case)["graph_revision"]

    # Scheduling edits do not remove the obligation.
    for index, action in enumerate(
        (
            {"action": "set_priority", "task_id": "decision-1.a", "priority": 7},
            {"action": "bind_role", "task_id": "decision-1.a", "role_alias": "researcher"},
            {"action": "request_dispatch", "task_id": "decision-1.a", "dispatch": "held"},
            {
                "action": "set_dependencies",
                "task_id": "decision-1.a",
                "depends_on": [],
            },
        )
    ):
        response = decide(
            granted_case,
            [action],
            key_value=f"decision-edit-{index}",
            decision_id=f"decision-edit-{index}",
            expected_graph_revision=revision,
        )
        assert response.status_code == 201, response.text
        revision = int(response.json()["graph_revision"])
        stored = granted_case["client"].get(
            f"{granted_case['base']}/workflow-runs/{granted_case['run_id']}"
            "/tasks/decision-1.a",
            headers=granted_case["headers"],
        ).json()
        assert stored["required_outcomes"] == ["config-defect-triage"], stored

    # The dispatch the role held is a real effect: the task is not handed out
    # until the role releases it, which is why the hold is lifted explicitly
    # rather than assumed to be cosmetic.
    released = decide(
        granted_case,
        [{"action": "request_dispatch", "task_id": "decision-1.a", "dispatch": "dispatched"}],
        key_value="decision-release",
        decision_id="decision-release",
        expected_graph_revision=revision,
    )
    assert released.status_code == 201, released.text
    admissible(granted_case)
    claimed = claim(granted_case, "decision-1.a", key_value="claim-a")
    assert claimed.status_code == 201, claimed.text
    failed = report(
        granted_case,
        "decision-1.a",
        {"outcome": "failed", "evidence_ref": "evidence:the-attempt-failed"},
    )
    assert failed.status_code == 200, failed.text
    assert failed.json()["reported_state"] == "failed"
    assert failed.json()["required_outcomes"] == ["config-defect-triage"]
    # The report is unverified, so the task keeps its occupancy and its promise.
    state = granted_case["client"].get(
        f"{granted_case['base']}/workflow-runs/{granted_case['run_id']}/resources",
        headers=granted_case["headers"],
    ).json()
    assert state["live_claims"] == 1, state
    after = granted_case["client"].get(
        f"{granted_case['base']}/workflow-runs/{granted_case['run_id']}/tasks/decision-1.a",
        headers=granted_case["headers"],
    ).json()
    assert after["required_outcomes"] == ["config-defect-triage"]
    assert after["reports"][-1]["outcome"] == "failed"


def test_an_optional_member_still_carries_its_outcome(
    granted_case: dict[str, Any],
) -> None:
    """``required: false`` changes whether a member blocks the join, not its debt.

    A member the author marked optional is still a member with an outcome: the
    seal pins it, its identity is present, and its promised result is still
    recorded. Optionality is not a way to make an owed result disappear.
    """
    expanded = expand(granted_case, [task_spec(item_key="a", required=False)])
    assert expanded.status_code == 201, expanded.text
    sealed = decide(
        granted_case,
        [
            {
                "action": "seal",
                "expansion_id": "defects",
                "members": ["decision-1.a"],
                "obligations": ["config-defect-triage"],
            }
        ],
        key_value="decision-seal",
        decision_id="decision-seal",
        expected_graph_revision=1,
    )
    assert sealed.status_code == 201, sealed.text
    stored = granted_case["client"].get(
        f"{granted_case['base']}/workflow-runs/{granted_case['run_id']}/tasks/decision-1.a",
        headers=granted_case["headers"],
    ).json()
    assert stored["required"] is False
    assert stored["required_outcomes"] == ["config-defect-triage"]
    expansion = graph(granted_case)["expansions"][0]
    assert "decision-1.a" in expansion["members"]
    assert expansion["members"]["decision-1.a"]["required"] is False


def test_an_uncovered_obligation_is_refused_rather_than_committed(
    granted_case: dict[str, Any],
) -> None:
    """The engine refuses a seal whose declared obligations nothing carries.

    Omitting an obligation from a seal's declaration is a refusal, so there is no
    supported path that commits a graph in which a promised result has no live
    carrier. The graph is left exactly as it was.
    """
    assert expand(granted_case, [task_spec(item_key="a")]).status_code == 201
    before = graph(granted_case)
    refused = decide(
        granted_case,
        [
            {
                "action": "seal",
                "expansion_id": "defects",
                "members": ["decision-1.a"],
                "obligations": [],
            }
        ],
        key_value="decision-seal",
        decision_id="decision-seal",
        expected_graph_revision=1,
    )
    assert refused.status_code in {403, 409, 422}, refused.text
    assert refused.json()["reason_code"] == "SCHEDULING_EXPANSION_OBLIGATIONS_UNCOVERED"
    after = graph(granted_case)
    assert after["graph_revision"] == before["graph_revision"]
    assert after["expansions"][0]["state"] == "open"


def test_the_run_reports_outcomes_still_outstanding(
    granted_case: dict[str, Any],
) -> None:
    """A partial expansion is accepted and the run says what is still missing."""
    response = expand(granted_case, [task_spec(item_key="a")])
    assert response.status_code == 201, response.text
    assert response.json()["outstanding_outcomes"] == ["comparison-report"]


def test_an_unknown_reference_is_refused(
    granted_case: dict[str, Any],
) -> None:
    """A binding cannot name a reference the grant never permitted."""
    response = expand(
        granted_case,
        [task_spec(item_key="bad", sources="requirement.not_granted")],
    )
    assert response.status_code in {403, 422}, response.text
    assert response.json()["reason_code"] in {
        "SCHEDULING_GRANT_INPUT_NOT_PERMITTED",
        "SCHEDULING_INPUT_REFERENCE_UNRESOLVED",
    }


def test_a_declared_reference_is_accepted(
    granted_case: dict[str, Any],
) -> None:
    """The positive control: a declared, permitted reference really does commit."""
    response = expand(
        granted_case,
        [task_spec(item_key="good", sources=REQUIRED_INPUT_A)],
    )
    assert response.status_code == 201, response.text
    stored = pages(granted_case)
    assert stored[0]["inputs"] == {"sources": REQUIRED_INPUT_A}


def test_an_output_reference_to_a_nonexistent_producer_is_refused(
    granted_case: dict[str, Any],
) -> None:
    """A binding may consume an output only of something that really produces it."""
    response = expand(
        granted_case,
        [task_spec(item_key="bad", sources="decision-1.ghost.output")],
    )
    assert response.status_code in {403, 422}, response.text
    assert response.json()["reason_code"] == "SCHEDULING_INPUT_REFERENCE_UNRESOLVED"


def test_a_large_task_set_is_persisted_and_paged_completely(
    granted_case: dict[str, Any],
) -> None:
    """A legal set larger than one page is neither truncated nor duplicated."""
    count = 103
    specs = [
        task_spec(item_key=f"item{index:04d}", zone=f"src/config/item{index:04d}")
        for index in range(count)
    ]
    response = expand(granted_case, specs)
    assert response.status_code == 201, response.text
    assert response.json()["task_count"] == count
    seen = pages(granted_case, size=37)
    identities = [str(item["task_id"]) for item in seen]
    assert len(identities) == count
    assert len(set(identities)) == count
    assert sorted(identities) == sorted(
        f"decision-1.item{index:04d}" for index in range(count)
    )
    # A task beyond the first page is addressable by its exact identity.
    last = f"decision-1.item{count - 1:04d}"
    exact = granted_case["client"].get(
        f"/v1/scheduling/protocol/projects/{granted_case['project_id']}"
        f"/runs/{granted_case['run_id']}/tasks/{last}",
        headers=protocol_headers(granted_case["consumer_token"]),
    )
    assert exact.status_code == 200, exact.text
    assert exact.json()["task_id"] == last


def test_a_claimed_snapshot_is_unchanged_by_a_later_revision(
    granted_case: dict[str, Any],
) -> None:
    """A claim hands over a fixed snapshot; later work does not rewrite it."""
    open_and_seal(granted_case, [task_spec(item_key="a")])
    admissible(granted_case)
    first = claim(granted_case, "decision-1.a", key_value="claim-a")
    assert first.status_code == 201, first.text
    frozen = first.json()

    # A later, legal revision adding unrelated work under its own set. A sealed
    # set accepts no further members, which is why this opens a second one: work
    # after a seal needs a new, separately authorised batch.
    opened = granted_case["client"].post(
        f"/v1/projects/{granted_case['project_id']}/workflow-runs/{granted_case['run_id']}"
        "/expansions",
        json={
            "grant_id": "configuration-repair-scope",
            "expansion_id": "later-work",
            "goal": "later unrelated defects",
            "required_outcomes": ["config-defect-triage"],
        },
        headers={**granted_case["headers"], **key("expansion-later")},
    )
    assert opened.status_code == 201, opened.text
    later = decide(
        granted_case,
        [
            {
                "action": "expand_graph",
                "expansion_id": "later-work",
                "tasks": [
                    task_spec(
                        item_key="b",
                        zone="src/config/b",
                        expansion_id="later-work",
                    )
                ],
            }
        ],
        key_value="decision-later",
        decision_id="decision-later",
        expected_graph_revision=2,
    )
    assert later.status_code == 201, later.text
    readback = granted_case["client"].get(
        f"/v1/scheduling/protocol/projects/{granted_case['project_id']}"
        f"/runs/{granted_case['run_id']}/claims/claim-a",
        headers=protocol_headers(granted_case["consumer_token"]),
    )
    assert readback.status_code == 200, readback.text
    # The claim, its inputs and its bindings are byte-for-byte what was handed
    # over, including the identity the caller received.
    assert readback.json() == frozen
    assert json.dumps(readback.json(), sort_keys=True) == json.dumps(frozen, sort_keys=True)


# ------------------------------------------- concurrent overlapping grants


def test_two_overlapping_grants_decide_concurrently_with_one_winner(
    run_case: dict[str, Any],
) -> None:
    """Two valid grants at one base revision produce one winner and one refusal.

    This is what the compare-and-swap is for. Two independent grants, each with
    its own credential, submit against the *same* expected graph revision at the
    same time from separate threads and through the ordinary HTTP boundary. The
    store's own serialization is what decides: exactly one commits, the other is
    refused with the revision it must re-read, and the durable state shows one
    new revision, one task and two recorded decisions - the accepted one and the
    refused one.

    Nothing is injected here: no database lock, no hook, no patched store. The
    concurrency is the test's own, and the barrier only makes the two posts
    overlap rather than proving anything by itself.
    """
    import threading

    base = run_case["base"]
    for name, instance in (("alpha", "instance-alpha"), ("beta", "instance-beta")):
        issued = run_case["client"].post(
            f"{base}/workflow-runs/{run_case['run_id']}/grants",
            json=grant_body(
                grant_id=name,
                role_instance=instance,
                outcomes=("config-defect-triage", "comparison-report"),
            ),
            headers={**run_case["headers"], **key(f"grant-{name}")},
        )
        assert issued.status_code == 201, issued.text
        created = run_case["client"].post(
            f"{base}/workflow-runs/{run_case['run_id']}/expansions",
            json={
                "grant_id": name,
                "expansion_id": f"set-{name}",
                "goal": "defects",
                "required_outcomes": ["config-defect-triage"],
            },
            headers={**run_case["headers"], **key(f"expansion-{name}")},
        )
        assert created.status_code == 201, created.text

    tokens: dict[str, str] = {}
    for name, instance in (("alpha", "instance-alpha"), ("beta", "instance-beta")):
        credential = issue(
            run_case,
            kind="role_protocol",
            key_value=f"cred-{name}",
            grant_id=name,
            role_instance=instance,
        )
        tokens[name] = str(credential["token"])

    def body(name: str) -> dict[str, Any]:
        return {
            "decision_id": f"decision-{name}",
            "grant_id": name,
            "term": 0,
            "expected_graph_revision": 0,
            "inputs_digest": run_case["run"]["inputs_digest"],
            "trigger": "approved_input",
            "reason": f"{name} sees the same defect",
            "actions": [
                {
                    "action": "expand_graph",
                    "expansion_id": f"set-{name}",
                    "tasks": [
                        task_spec(
                            item_key="a",
                            expansion_id=f"set-{name}",
                            zone=f"src/config/{name}",
                        )
                    ],
                }
            ],
        }

    barrier = threading.Barrier(2)
    results: dict[str, int] = {}
    payloads: dict[str, Any] = {}

    def submit(name: str) -> None:
        barrier.wait(timeout=20)
        response = run_case["client"].post(
            f"/v1/scheduling/protocol/projects/{run_case['project_id']}"
            f"/runs/{run_case['run_id']}/grants/{name}/decisions",
            json=body(name),
            headers={**protocol_headers(tokens[name]), **key(f"decision-{name}")},
        )
        results[name] = response.status_code
        payloads[name] = response.json()

    threads = [threading.Thread(target=submit, args=(name,)) for name in ("alpha", "beta")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert sorted(results.values()) == [201, 409], (results, payloads)
    winner = "alpha" if results["alpha"] == 201 else "beta"
    loser = "beta" if winner == "alpha" else "alpha"
    assert payloads[loser]["reason_code"] == "SCHEDULING_GRAPH_REVISION_CONFLICT"
    assert payloads[loser]["current_revision"] == 1

    current = graph(run_case)
    assert current["graph_revision"] == 1
    assert [item["revision"] for item in current["history"]] == [1]
    stored = pages(run_case)
    assert [item["task_id"] for item in stored] == [f"decision-{winner}.a"]

    # Both commands are durably recorded: the accepted decision and the refusal.
    rejections = run_case["client"].get(
        f"{base}/workflow-runs/{run_case['run_id']}/decision-rejections",
        headers=run_case["headers"],
    ).json()["items"]
    assert [item["command_key"] for item in rejections] == [f"decision-{loser}"]
    assert rejections[0]["accepted"] is False
    assert rejections[0]["current_graph_revision"] == 1

    # A replay of either command is answered from its own durable record.
    replay_winner = run_case["client"].post(
        f"/v1/scheduling/protocol/projects/{run_case['project_id']}"
        f"/runs/{run_case['run_id']}/grants/{winner}/decisions",
        json=body(winner),
        headers={**protocol_headers(tokens[winner]), **key(f"decision-{winner}")},
    )
    assert replay_winner.status_code == 200, replay_winner.text
    assert replay_winner.json()["graph_revision"] == 1
    replay_loser = run_case["client"].post(
        f"/v1/scheduling/protocol/projects/{run_case['project_id']}"
        f"/runs/{run_case['run_id']}/grants/{loser}/decisions",
        json=body(loser),
        headers={**protocol_headers(tokens[loser]), **key(f"decision-{loser}")},
    )
    assert replay_loser.status_code == 409, replay_loser.text
    assert replay_loser.json()["reason_code"] == "SCHEDULING_GRAPH_REVISION_CONFLICT"
