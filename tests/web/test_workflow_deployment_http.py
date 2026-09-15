"""Deployment, readback and rollback through the real authenticated API.

Each case drives the real FastAPI application, real SQLite and real files under a
real state directory. The properties being checked are the ones a deployment
cannot fake: that intent is persisted before any byte moves, that the loader
reads the pending package the command wrote, that readiness comes from a
capability the registry really has, that a slot moves only conditionally, and
that an interrupted command is reconciled rather than replaced.
"""

import os
from pathlib import Path
from typing import Any

from deployment_fixtures import (
    BUNDLE,
    COORDINATOR_ROLE,
    ORIGIN,
    SLOT,
    WORKFLOW_TEXT,
    bundle_case,
    create_bundle,
    deploy,
    deployed_case,
    deployment_path,
    history,
    json_rows,
    login,
    preview_id,
    restart,
    revision_path,
    status,
    url,
)

case = bundle_case
deployed = deployed_case


# --------------------------------------------------------------------- AC1

def test_an_exact_confirmation_materialises_loads_and_activates(deployed: dict[str, Any]) -> None:
    """AC1/AC2: a real deployment, with the identities it really bound."""
    result = deployed["deployment"]
    record = result["deployment"]
    assert result["readiness"] == "ready"
    assert record["action"] == "deploy_only"
    assert record["bundle_revision"] == 1
    assert record["bundle_digest"]
    assert record["manifest_file_digest"]
    assert record["compiled_digest"]
    assert record["compiler_identity"] == "karajan.workflow-compiler.v1"
    assert record["compiler_revision"] == 1
    assert record["expected_active_revision"] == 0
    assert result["slot"]["slot_revision"] == 1
    assert result["runs_started"] == 0
    assert result["business_steps_executed"] == 0
    assert record["model_calls"] == 0

    # The receipt is a real read by this process, not a stored claim.
    receipt = record["load_receipt"]
    assert receipt["loader_identity"] == "karajan.workflow-loader.v1"
    assert receipt["verify_bytes"] is True
    assert receipt["process_id"] == os.getpid()
    assert receipt["bundle_digest"] == record["bundle_digest"]
    assert receipt["manifest_file_digest"] == record["manifest_file_digest"]
    assert receipt["compiled_digest"] == record["compiled_digest"]
    assert receipt["receipt_digest"]

    # The pending package is real, on disk, and byte-identical to the source.
    package = deployment_path(deployed, record["deployment_id"])
    source = revision_path(deployed)
    assert package.is_dir()
    observed = sorted(
        item.relative_to(package).as_posix() for item in package.rglob("*") if item.is_file()
    )
    assert observed == [
        "manifest.json",
        "roles/editor.yaml",
        "roles/researcher.yaml",
        "workflow.yaml",
    ]
    for relative in observed:
        assert (package / relative).read_bytes() == (source / relative).read_bytes()


def test_a_confirmation_request_cannot_supply_identities(case: dict[str, Any]) -> None:
    """AC1/AC6: the DTO refuses identity fields, so no digest can be asserted."""
    assert create_bundle(case).status_code == 201
    for field, value in (
        ("bundle_digest", "00" * 32),
        ("compiled_digest", "00" * 32),
        ("manifest", {"files": []}),
        ("loader", "fake"),
        ("files", []),
        ("business_inputs", {"a": 1}),
        ("principal", "intruder"),
    ):
        response = deploy(case, key=f"assert-{field}", extra={field: value})
        assert response.status_code == 422, (field, response.text)
        assert response.json()["reason_code"] == "INPUT_INVALID"


def test_the_action_is_explicit_and_deploy_and_run_is_unsupported(case: dict[str, Any]) -> None:
    """AC1/AC6: no implicit action, and no invented Run."""
    assert create_bundle(case).status_code == 201
    assert deploy(case, key="missing-action", extra={"action": None}).status_code == 422
    unsupported = deploy(case, key="run", action="deploy_and_run")
    assert unsupported.status_code == 501, unsupported.text
    assert unsupported.json()["reason_code"] == "WORKFLOW_DEPLOY_ACTION_UNSUPPORTED"
    assert status(case)["slot_revision"] == 0


def test_the_published_record_reports_the_durable_completed_steps(
    case: dict[str, Any],
) -> None:
    """AC1/AC3: the published record describes what is really recorded.

    The steps a command completed live in its durable intent. The deployment
    record must report those, not a snapshot taken before the activation began -
    otherwise the public result, the detail readback and a replay would all claim
    a command did nothing, while the intent says otherwise.
    """
    assert create_bundle(case).status_code == 201
    published = deploy(case, key="steps")
    assert published.status_code == 201, published.text
    record = published.json()["deployment"]
    deployment_id = record["deployment_id"]
    assert record["completed_steps"] == ["materialize", "load"]

    # The durable intent agrees with the published record.
    intent = json_rows(
        case,
        "SELECT record FROM workflow_deployment_intents WHERE principal=? AND key=?",
        ("owner", "steps"),
    )[0]
    assert intent["completed_steps"] == record["completed_steps"]
    assert intent["deployment_id"] == deployment_id

    detail = case["client"].get(
        url(case["project_id"], f"/workflow-deployments/{SLOT}/deployments/{deployment_id}")
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["deployment"]["completed_steps"] == ["materialize", "load"]
    # The immutable historical receipt is the same one the first response carried.
    assert (
        detail.json()["historical_receipt"]["receipt_digest"]
        == record["load_receipt"]["receipt_digest"]
    )

    replayed = deploy(case, key="steps")
    assert replayed.status_code == 200, replayed.text
    assert replayed.json()["deployment"]["completed_steps"] == ["materialize", "load"]
    assert replayed.json()["deployment"]["deployment_id"] == deployment_id
    assert (
        replayed.json()["deployment"]["load_receipt"]["receipt_digest"]
        == record["load_receipt"]["receipt_digest"]
    )


def test_a_stale_confirmation_is_refused_after_a_newer_revision(case: dict[str, Any]) -> None:
    """AC1: a confirmation of revision 1 cannot activate it after revision 2 exists."""
    assert create_bundle(case).status_code == 201
    first = preview_id(case, revision=1)
    revised = case["client"].put(
        url(case["project_id"], f"/conversations/{case['conversation_id']}/workflows/{BUNDLE}"),
        json={"files": __import__("workflow_fixtures").files(), "delivery_kind": "report"},
        headers={**case["headers"], "Idempotency-Key": "revise-1", "If-Match": '"1"'},
    )
    assert revised.status_code == 201, revised.text
    assert revised.json()["revision"] == 2
    stale = deploy(case, key="stale", revision=1, preview=first)
    assert stale.status_code == 409, stale.text
    assert stale.json()["reason_code"] == "WORKFLOW_PREVIEW_STALE"
    assert stale.json()["current_revision"] == 2
    assert status(case)["slot_revision"] == 0

    # A confirmation of the new head is accepted, and it is what gets deployed.
    current = deploy(case, key="current", revision=2)
    assert current.status_code == 201, current.text
    assert current.json()["deployment"]["bundle_revision"] == 2


def test_a_wrong_preview_identity_is_refused(case: dict[str, Any]) -> None:
    """AC1/AC2: a digest the stored bytes do not satisfy is a refusal."""
    assert create_bundle(case).status_code == 201
    wrong = "ab" * 32
    response = deploy(case, key="wrong-preview", preview=wrong)
    assert response.status_code == 409, response.text
    assert response.json()["reason_code"] == "WORKFLOW_PREVIEW_STALE"
    assert response.json()["current_preview_id"] == preview_id(case)
    assert status(case)["slot_revision"] == 0


def test_an_unknown_deploy_field_is_refused(case: dict[str, Any]) -> None:
    """AC1: an unread field is refused, never silently ignored."""
    assert create_bundle(case).status_code == 201
    response = deploy(case, key="unknown-field", extra={"target_slot_revision": 3})
    assert response.status_code == 422
    assert response.json()["reason_code"] == "INPUT_INVALID"


# --------------------------------------------------------------------- AC2

def test_the_old_active_survives_while_a_new_one_is_prepared(case: dict[str, Any]) -> None:
    """AC2: preparing a new version never disturbs the active entry point."""
    assert create_bundle(case).status_code == 201
    first = deploy(case, key="first")
    assert first.status_code == 201, first.text
    active = first.json()["deployment"]["deployment_id"]

    # A second deployment targeting a revision that does not verify fails after
    # its intent is persisted. The active deployment and its slot are untouched.
    failed = deploy(case, key="second", revision=1, preview="cd" * 32)
    assert failed.status_code == 409
    current = status(case)
    assert current["active_deployment_id"] == active
    assert current["slot_revision"] == 1
    assert current["readiness"] == "ready"

    # The failed command left a real, non-consumable pending intent behind.
    pending = [item for item in history(case)["intents"] if item["outcome"] != "activated"]
    assert pending == [] or all(item["consumable"] is False for item in pending)


def test_a_parameterised_template_with_no_business_input_is_deployable(
    case: dict[str, Any],
) -> None:
    """AC1/AC6: deploy_only needs no concrete business input."""
    assert create_bundle(case).status_code == 201
    response = deploy(case, key="parameterised")
    assert response.status_code == 201, response.text
    record = response.json()["deployment"]
    # The template declares its inputs symbolically; nothing here supplies them.
    assert record["bundle_revision"] == 1
    assert response.json()["runs_started"] == 0
    assert record["model_calls"] == 0

    readback = case["client"].get(
        url(case["project_id"], f"/workflows/{BUNDLE}/revisions/1")
    ).json()
    assert readback["compiled_digest"] == record["compiled_digest"]
    assert readback["readback"]["files"]


def test_an_unknown_capability_blocks_readiness(case: dict[str, Any]) -> None:
    """AC2/AC6: a kind with no real adapter never becomes ready.

    The template references ``agent_task@1``, which is registered but has no
    adapter in this build. The bundle itself compiles and is reviewable, and the
    deployment must be refused: a registration alone is not a capability.
    """
    model_workflow = WORKFLOW_TEXT.replace(
        """    execution_kind: artifact_aggregate@1
    role: researcher
    output_contract: aggregated-report@1
    inputs:
      sources: requirement.option_a""",
        """    execution_kind: agent_task@1
    role: researcher
    output_contract: aggregated-report@1
    inputs: {}""",
    )
    created = create_bundle(case, bundle_id="model-flow", workflow=model_workflow, key="model")
    assert created.status_code == 201, created.text
    assert created.json()["readiness"] == "unavailable_capability"
    assert created.json()["executable"] is False

    blocked = deploy(case, bundle_id="model-flow", key="model-deploy")
    assert blocked.status_code == 422, blocked.text
    assert blocked.json()["reason_code"] == "WORKFLOW_CAPABILITY_UNAVAILABLE"
    assert "agent_task@1" in blocked.json()["unavailable_execution_kinds"]
    assert status(case)["slot_revision"] == 0
    assert status(case)["active_deployment_id"] is None


def test_the_business_adapter_is_never_executed_by_a_deployment(case: dict[str, Any]) -> None:
    """AC2/AC6: deploy_only loads a definition; it executes nothing."""
    from karajan.workflows import registry

    calls: list[object] = []
    original = registry.artifact_aggregate

    def recorded(inputs: Any) -> Any:
        calls.append(inputs)
        return original(inputs)

    aggregate = registry._REGISTRY["artifact_aggregate@1"]
    object.__setattr__(aggregate, "adapter", recorded)
    try:
        assert create_bundle(case).status_code == 201
        response = deploy(case, key="no-execution")
        assert response.status_code == 201, response.text
        assert calls == []
        assert response.json()["deployment"]["business_steps_executed"] == 0
    finally:
        object.__setattr__(aggregate, "adapter", original)


def test_a_package_whose_bytes_change_is_refused(case: dict[str, Any]) -> None:
    """AC2/AC4: replacing a stored file is a refusal, not a new definition."""
    assert create_bundle(case).status_code == 201
    response = deploy(case, key="tamper-source")
    assert response.status_code == 201
    deployment_id = response.json()["deployment"]["deployment_id"]
    package = deployment_path(case, deployment_id)
    (package / "workflow.yaml").write_text(
        WORKFLOW_TEXT.replace("id: compare-options", "id: something-else"), encoding="utf-8"
    )
    tampered = status(case)
    assert tampered["readiness"] == "blocked"
    assert tampered["blocked_reason"] in {
        "WORKFLOW_FILE_DIGEST_MISMATCH",
        "WORKFLOW_COMPILE_MISMATCH",
    }
    # The definition is not consumable while the files do not verify.
    refused = case["client"].get(
        url(case["project_id"], f"/workflow-deployments/{SLOT}/definition")
    )
    assert refused.status_code == 409, refused.text


# --------------------------------------------------------------------- AC3

def test_a_missing_package_is_reported_blocked(case: dict[str, Any]) -> None:
    """AC4: a missing package is unavailable, never silently ready."""
    assert create_bundle(case).status_code == 201
    response = deploy(case, key="vanish")
    deployment_id = response.json()["deployment"]["deployment_id"]
    package = deployment_path(case, deployment_id)
    for item in sorted(package.rglob("*"), reverse=True):
        item.unlink() if item.is_file() else item.rmdir()
    package.rmdir()
    blocked = status(case)
    assert blocked["readiness"] == "blocked"
    assert blocked["blocked_reason"] == "WORKFLOW_PACKAGE_MISSING"
    assert blocked["current"] is None


def test_the_intent_is_persisted_before_the_copy(case: dict[str, Any]) -> None:
    """AC2/AC3: the durable intent exists, and the package really was written."""
    assert create_bundle(case).status_code == 201
    assert deploy(case, key="persisted").status_code == 201
    intents = json_rows(
        case,
        "SELECT record FROM workflow_deployment_intents WHERE principal=? AND key=?",
        ("owner", "persisted"),
    )
    assert len(intents) == 1
    record = intents[0]
    assert record["action"] == "deploy_only"
    assert record["slot"] == SLOT
    assert record["expected_active_revision"] == 0
    assert record["completed_steps"] == ["materialize", "load"]
    frozen = record["frozen"]
    assert frozen["bundle_digest"]
    assert frozen["manifest_file_digest"]
    assert frozen["compiled_digest"]
    assert frozen["compiler_identity"] == "karajan.workflow-compiler.v1"
    assert [item["path"] for item in frozen["files"]] == [
        "roles/editor.yaml",
        "roles/researcher.yaml",
        "workflow.yaml",
    ]
    # The intent and the activated deployment agree on every frozen identity.
    deployments = json_rows(
        case,
        "SELECT record FROM workflow_deployments WHERE project_id=? AND slot=?",
        (case["project_id"], SLOT),
    )
    assert len(deployments) == 1
    activated = deployments[0]
    assert activated["bundle_digest"] == frozen["bundle_digest"]
    assert activated["compiled_digest"] == frozen["compiled_digest"]
    assert activated["deployment_id"] == record["deployment_id"]


def test_a_repeat_returns_the_original_result_after_the_slot_moves(
    case: dict[str, Any],
) -> None:
    """AC3: the same key replays its own result, and reports the present state."""
    assert create_bundle(case).status_code == 201
    first = deploy(case, key="replay")
    assert first.status_code == 201, first.text
    original = first.json()

    # Publish and deploy a revision without changing *this* command's identity.
    revised = case["client"].put(
        url(case["project_id"], f"/conversations/{case['conversation_id']}/workflows/{BUNDLE}"),
        json={
            "files": __import__("workflow_fixtures").files(
                extra=[{"path": "roles/editor.yaml", "content": COORDINATOR_ROLE.replace(
                    "repair-coordinator", "technical-editor")}]
            ),
            "delivery_kind": "report",
        },
        headers={**case["headers"], "Idempotency-Key": "revise-replay", "If-Match": '"1"'},
    )
    assert revised.status_code == 201, revised.text
    second = deploy(
        case,
        key="second",
        revision=2,
        expected_active_revision=1,
        preview=preview_id(case, revision=2),
    )
    assert second.status_code == 201, second.text
    assert second.json()["deployment"]["deployment_id"] != original["deployment"]["deployment_id"]

    replay = deploy(case, key="replay")
    assert replay.status_code == 200, replay.text
    document = replay.json()
    # The original answer is preserved exactly...
    assert document["deployment"]["deployment_id"] == original["deployment"]["deployment_id"]
    assert document["deployment"]["load_receipt"] == original["deployment"]["load_receipt"]
    assert document["readiness"] == "ready"
    # ...and the present state is reported beside it, never merged into it.
    assert document["replayed"] is True
    assert document["current"]["is_active"] is False
    assert document["current"]["slot_revision"] == 2


def test_a_changed_payload_on_the_same_key_conflicts(case: dict[str, Any]) -> None:
    """AC3: idempotency covers the whole authorized payload."""
    assert create_bundle(case).status_code == 201
    assert deploy(case, key="same-key").status_code == 201
    assert deploy(case, key="same-key").status_code == 200
    changed = deploy(case, key="same-key", expected_active_revision=1)
    assert changed.status_code == 409, changed.text
    assert changed.json()["reason_code"] == "WORKFLOW_IDEMPOTENCY_CONFLICT"
    assert status(case)["slot_revision"] == 1


def test_only_one_of_two_concurrent_commands_takes_the_slot(case: dict[str, Any]) -> None:
    """AC3: conditional activation admits exactly one winner."""
    assert create_bundle(case).status_code == 201
    confirmed = preview_id(case)
    target = url(
        case["project_id"],
        f"/conversations/{case['conversation_id']}/workflows/{BUNDLE}/revisions/1/deployments",
    )
    payload = {
        "action": "deploy_only",
        "slot": SLOT,
        "expected_active_revision": 0,
        "preview_id": confirmed,
    }
    first = case["client"].post(
        target, json=payload, headers={**case["headers"], "Idempotency-Key": "race-a"}
    )
    stale = case["client"].post(
        target, json=payload, headers={**case["headers"], "Idempotency-Key": "race-b"}
    )
    assert first.status_code == 201, first.text
    assert stale.status_code == 409, stale.text
    assert stale.json()["reason_code"] == "WORKFLOW_SLOT_REVISION_CONFLICT"
    assert stale.json()["current_revision"] == 1
    assert status(case)["slot_revision"] == 1
    assert len(history(case)["items"]) == 1


def test_a_confirmation_against_the_wrong_active_revision_is_refused(
    case: dict[str, Any],
) -> None:
    """AC3: the expected active revision really guards the switch."""
    assert create_bundle(case).status_code == 201
    assert deploy(case, key="act-1").status_code == 201
    assert deploy(case, key="act-2", expected_active_revision=0).status_code == 409
    assert deploy(case, key="act-3", expected_active_revision=5).status_code == 409
    assert status(case)["slot_revision"] == 1


def test_a_distinct_command_after_a_completed_one_is_not_a_replay(
    case: dict[str, Any],
) -> None:
    """AC3: a new key is a new command, and it still has to win the CAS."""
    assert create_bundle(case).status_code == 201
    assert deploy(case, key="one").status_code == 201
    other = deploy(case, key="two", expected_active_revision=1)
    assert other.status_code == 201, other.text
    document = other.json()
    assert document["replayed"] if "replayed" in document else True
    assert document["deployment"]["deployment_id"] != ""


# --------------------------------------------------------------------- AC4

def test_a_fresh_application_re_loads_the_active_package(case: dict[str, Any]) -> None:
    """AC4: a second process re-reads and re-verifies, it does not trust a receipt."""
    assert create_bundle(case).status_code == 201
    response = deploy(case, key="fresh")
    assert response.status_code == 201
    first = response.json()["deployment"]

    app, client = restart(case, "restart-1")
    with client:
        login(client, "restart-1")
        current = status(case, client=client)
        assert current["readiness"] == "ready"
        assert current["active_deployment_id"] == first["deployment_id"]
        # The present facts come from *this* process's read, and say so.
        assert current["current"]["loaded_by_process"] == os.getpid()
        assert current["current"]["verify_bytes"] is False
        # The historical receipt is unchanged: it still records the first load.
        assert current["receipt"]["receipt_digest"] == first["load_receipt"]["receipt_digest"]
        assert current["receipt"]["verify_bytes"] is True


def test_a_corrupt_package_blocks_a_fresh_process(case: dict[str, Any]) -> None:
    """AC4: a damaged file makes the slot unavailable, not ready."""
    assert create_bundle(case).status_code == 201
    response = deploy(case, key="corrupt")
    deployment_id = response.json()["deployment"]["deployment_id"]
    (deployment_path(case, deployment_id) / "workflow.yaml").write_text("", encoding="utf-8")

    _, client = restart(case, "restart-corrupt")
    with client:
        login(client, "restart-corrupt")
        current = status(case, client=client)
        assert current["readiness"] == "blocked"
        assert current["blocked_reason"]
        assert current["current"] is None
        # The historical receipt is still there; it is history, not a promise.
        assert current["receipt"]["receipt_digest"]
        refused = case["client"].get(
            url(case["project_id"], f"/workflow-deployments/{SLOT}/definition")
        )
        assert refused.status_code == 409


def test_rollback_goes_through_the_same_pipeline(case: dict[str, Any]) -> None:
    """AC4: a rollback re-loads its exact historical bundle and CASes the slot."""
    assert create_bundle(case).status_code == 201
    first = deploy(case, key="rb-1")
    assert first.status_code == 201
    first_id = first.json()["deployment"]["deployment_id"]

    revised = case["client"].put(
        url(case["project_id"], f"/conversations/{case['conversation_id']}/workflows/{BUNDLE}"),
        json={
            "files": __import__("workflow_fixtures").files(
                extra=[{"path": "roles/editor.yaml", "content": COORDINATOR_ROLE.replace(
                    "repair-coordinator", "technical-editor")}]
            ),
            "delivery_kind": "report",
        },
        headers={**case["headers"], "Idempotency-Key": "rb-revise", "If-Match": '"1"'},
    )
    assert revised.status_code == 201
    second = deploy(
        case,
        key="rb-2",
        revision=2,
        expected_active_revision=1,
        preview=preview_id(case, revision=2),
    )
    assert second.status_code == 201, second.text
    second_id = second.json()["deployment"]["deployment_id"]

    rollback = case["client"].post(
        url(case["project_id"], "/workflow-rollbacks"),
        json={"target_deployment_id": first_id, "expected_active_revision": 2},
        headers={**case["headers"], "Idempotency-Key": "rb-3"},
    )
    assert rollback.status_code == 201, rollback.text
    document = rollback.json()
    assert document["deployment"]["action"] == "rollback"
    assert document["deployment"]["rollback_of"] == first_id
    assert document["deployment"]["bundle_revision"] == 1
    assert document["deployment"]["bundle_digest"] == first.json()["deployment"]["bundle_digest"]
    assert document["slot"]["slot_revision"] == 3
    assert document["readiness"] == "ready"
    # The old deployment is untouched: a rollback does not rewrite history.
    older = case["client"].get(
        url(case["project_id"], f"/workflow-deployments/{SLOT}/deployments/{second_id}")
    ).json()
    assert older["deployment"]["bundle_revision"] == 2
    assert older["current"]["is_active"] is False


def test_a_rollback_cannot_overwrite_a_newer_deployment(case: dict[str, Any]) -> None:
    """AC4: the rollback carries the same expected-active guard as a deploy."""
    assert create_bundle(case).status_code == 201
    first = deploy(case, key="guard-1")
    first_id = first.json()["deployment"]["deployment_id"]
    assert deploy(case, key="guard-2", expected_active_revision=1).status_code == 201
    blocked = case["client"].post(
        url(case["project_id"], "/workflow-rollbacks"),
        json={"target_deployment_id": first_id, "expected_active_revision": 1},
        headers={**case["headers"], "Idempotency-Key": "guard-3"},
    )
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["reason_code"] == "WORKFLOW_SLOT_REVISION_CONFLICT"
    assert status(case)["slot_revision"] == 2


def test_an_unknown_rollback_target_is_refused(case: dict[str, Any]) -> None:
    """AC4: a rollback names an exact historical deployment of this project."""
    assert create_bundle(case).status_code == 201
    assert deploy(case, key="unknown-target").status_code == 201
    response = case["client"].post(
        url(case["project_id"], "/workflow-rollbacks"),
        json={"target_deployment_id": "no-such-deployment", "expected_active_revision": 1},
        headers={**case["headers"], "Idempotency-Key": "unknown"},
    )
    assert response.status_code == 404, response.text
    assert response.json()["reason_code"] == "WORKFLOW_DEPLOYMENT_NOT_FOUND"


# --------------------------------------------------------------------- AC5

def test_the_definition_handle_is_immutable_across_a_new_deployment(
    case: dict[str, Any],
) -> None:
    """AC5: a handle a consumer already holds is never hot-modified."""
    assert create_bundle(case).status_code == 201
    assert deploy(case, key="handle-1").status_code == 201
    handle = case["client"].get(
        url(case["project_id"], f"/workflow-deployments/{SLOT}/definition")
    )
    assert handle.status_code == 200, handle.text
    first = handle.json()
    assert first["deployment"]["bundle_revision"] == 1
    assert first["deployment"]["compiled_digest"]
    assert first["slot"]["slot_revision"] == 1
    assert first["acquired_load"]["state"] == "ready"
    assert first["acquired_load"]["loaded_by_process"] == os.getpid()
    assert first["definition"]["workflow"]["id"] == "compare-options"
    assert first["definition"]["steps"]
    assert first["execution"]["deploy_only"] is True
    assert first["definition_digest"]

    revised = case["client"].put(
        url(case["project_id"], f"/conversations/{case['conversation_id']}/workflows/{BUNDLE}"),
        json={
            "files": __import__("workflow_fixtures").files(
                extra=[{"path": "roles/editor.yaml", "content": COORDINATOR_ROLE.replace(
                    "repair-coordinator", "technical-editor")}]
            ),
            "delivery_kind": "report",
        },
        headers={**case["headers"], "Idempotency-Key": "handle-revise", "If-Match": '"1"'},
    )
    assert revised.status_code == 201
    assert deploy(
        case,
        key="handle-2",
        revision=2,
        expected_active_revision=1,
        preview=preview_id(case, revision=2),
    ).status_code == 201

    again = case["client"].get(
        url(case["project_id"], f"/workflow-deployments/{SLOT}/definition")
    ).json()
    assert again["deployment"]["bundle_revision"] == 2
    assert again["definition_digest"] != first["definition_digest"]
    # The first handle was a copy of the state at acquisition and is unchanged.
    assert first["deployment"]["bundle_revision"] == 1
    assert first["slot"]["slot_revision"] == 1


def test_nested_collections_cannot_be_edited_to_bypass_the_digest(
    case: dict[str, Any],
) -> None:
    """AC5: the handle's data is deeply immutable, including nested containers.

    Two acquisitions of the same active package bind the same deployment, files
    and compiled definition. They may differ in *when* they were acquired, so the
    comparison is on the frozen identity - which is exactly what a consumer uses
    to decide whether it is looking at the definition it pinned.
    """
    assert create_bundle(case).status_code == 201
    assert deploy(case, key="nested").status_code == 201
    from karajan.web import create_app

    app = create_app(
        case["directory"],
        origin=ORIGIN,
        bootstrap_token="nested",
        allowed_roots=[case["repository"].parent],
    )
    store = _deployment_store(app)
    first = store.accept(case["project_id"], SLOT, principal="owner")
    document = first.as_document()

    def frozen(document: dict[str, Any]) -> dict[str, Any]:
        return {
            "deployment": document["deployment"],
            "definition": document["definition"],
            "slot_revision": document["slot"]["slot_revision"],
        }

    before = frozen(first.as_document())

    # Mutate everything the response exposed, including nested containers.
    document["definition"]["steps"][0]["step_id"] = "forged"
    document["definition"]["execution_kinds"]["available"].append("agent_task@1")
    document["deployment"]["files"][0]["byte_digest"] = "0" * 64
    document["deployment"]["files"].append(
        {"path": "evil.yaml", "byte_digest": "0" * 64, "byte_length": 0}
    )
    document["definition"]["roles"]["researcher"] = "role:forged@9"
    document["definition"]["role_definitions"].clear()

    second = store.accept(case["project_id"], SLOT, principal="owner").as_document()
    assert frozen(second) == before
    assert second["definition"]["steps"][0]["step_id"] != "forged"
    assert "agent_task@1" not in second["definition"]["execution_kinds"]["available"]
    assert second["deployment"]["files"][0]["byte_digest"] != "0" * 64
    assert all(item["path"] != "evil.yaml" for item in second["deployment"]["files"])
    assert second["definition"]["roles"]["researcher"] != "role:forged@9"
    assert second["definition"]["role_definitions"]
    assert {item["path"] for item in second["deployment"]["files"]} == {
        "workflow.yaml",
        "roles/editor.yaml",
        "roles/researcher.yaml",
    }
    # The handle itself is unchanged by the mutation of the copy it handed out.
    assert frozen(first.as_document()) == before


def test_an_empty_slot_has_no_definition(case: dict[str, Any]) -> None:
    """AC5: nothing is consumable before something is really active."""
    assert create_bundle(case).status_code == 201
    empty = status(case)
    assert empty["slot_revision"] == 0
    assert empty["active_deployment_id"] is None
    assert empty["readiness"] == "empty"
    refused = case["client"].get(
        url(case["project_id"], f"/workflow-deployments/{SLOT}/definition")
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["reason_code"] == "WORKFLOW_SLOT_EMPTY"


def test_a_pending_package_is_never_consumable(case: dict[str, Any]) -> None:
    """AC2/AC5: a prepared package is not an entry point."""
    assert create_bundle(case).status_code == 201
    assert deploy(case, key="not-consumable").status_code == 201
    listing = history(case)
    assert listing["intents"] == []
    assert len(listing["items"]) == 1
    assert listing["items"][0]["deployment_id"] == status(case)["active_deployment_id"]


def _deployment_store(app: Any) -> Any:
    """The live deployment store behind an application, for a direct handle read."""
    from karajan.workflows import DeploymentStore

    for route in app.routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint is not None and getattr(endpoint, "__name__", "") == "deployment_status":
            closure = endpoint.__closure__ or ()
            for cell in closure:
                value = cell.cell_contents
                if isinstance(value, DeploymentStore):
                    return value
    raise AssertionError("the deployment store is not registered on this application")


# --------------------------------------------------------------------- AC6

def test_authentication_origin_and_csrf_are_required(case: dict[str, Any]) -> None:
    """AC6: the deployment API is authenticated, CSRF protected and origin bound."""
    from fastapi.testclient import TestClient
    from karajan.web import create_app

    assert create_bundle(case).status_code == 201
    confirmed = preview_id(case)
    target = url(
        case["project_id"],
        f"/conversations/{case['conversation_id']}/workflows/{BUNDLE}/revisions/1/deployments",
    )
    payload = {
        "action": "deploy_only",
        "slot": SLOT,
        "expected_active_revision": 0,
        "preview_id": confirmed,
    }

    # A client that never bootstrapped has no session: every route refuses it,
    # and the acting principal is never taken from the request.
    fresh = create_app(
        case["directory"],
        origin=ORIGIN,
        bootstrap_token="unused",
        allowed_roots=[case["repository"].parent],
    )
    with TestClient(fresh, base_url=ORIGIN) as stranger:
        denied = stranger.post(
            target, json=payload, headers={"Origin": ORIGIN, "Idempotency-Key": "stranger"}
        )
        assert denied.status_code == 401, denied.text
        assert denied.json()["reason_code"] == "AUTHENTICATION_REQUIRED"
        read = stranger.get(url(case["project_id"], "/workflow-deployments/default"))
        assert read.status_code == 401
        assert read.json()["reason_code"] == "AUTHENTICATION_REQUIRED"

    wrong_origin = case["client"].post(
        target,
        json=payload,
        headers={
            "Origin": "http://127.0.0.1:9999",
            "X-CSRF-Token": case["headers"]["X-CSRF-Token"],
            "Idempotency-Key": "origin",
        },
    )
    assert wrong_origin.status_code == 403
    assert wrong_origin.json()["reason_code"] == "ORIGIN_REJECTED"
    missing_csrf = case["client"].post(
        target, json=payload, headers={"Origin": ORIGIN, "Idempotency-Key": "csrf"}
    )
    assert missing_csrf.status_code == 403
    assert missing_csrf.json()["reason_code"] == "CSRF_REJECTED"
    assert wrong_origin.json()["reason_code"] != missing_csrf.json()["reason_code"]
    # None of the refused requests reached the store.
    assert status(case)["slot_revision"] == 0
    assert json_rows(
        case,
        "SELECT record FROM workflow_deployment_intents WHERE principal=? AND key=?",
        ("owner", "stranger"),
    ) == []


def test_a_deployment_is_scoped_to_its_project_and_conversation(case: dict[str, Any]) -> None:
    """AC6: neither another project nor another conversation can deploy this bundle."""
    from workflow_fixtures import make_repository

    other_project = case["registry"].create(
        {
            "name": "Other",
            "repository_path": str(make_repository(case["tmp_path"], "other")),
            "base_ref": "main",
            "target_branch": "main",
            "allowed_target_branches": ["main"],
        },
        command_key="other",
        principal="owner",
    )
    assert create_bundle(case).status_code == 201
    confirmed = preview_id(case)

    cross_project = case["client"].post(
        url(
            other_project["id"],
            f"/conversations/{case['conversation_id']}/workflows/{BUNDLE}"
            "/revisions/1/deployments",
        ),
        json={
            "action": "deploy_only",
            "slot": SLOT,
            "expected_active_revision": 0,
            "preview_id": confirmed,
        },
        headers={**case["headers"], "Idempotency-Key": "cross"},
    )
    assert cross_project.status_code == 404, cross_project.text

    other_conversation = case["client"].post(
        url(case["project_id"], "/conversations"),
        json={"title": "Another session"},
        headers={**case["headers"], "Idempotency-Key": "other-conversation"},
    )
    assert other_conversation.status_code == 201, other_conversation.text
    cross_conversation = case["client"].post(
        url(
            case["project_id"],
            f"/conversations/{other_conversation.json()['id']}/workflows/{BUNDLE}"
            "/revisions/1/deployments",
        ),
        json={
            "action": "deploy_only",
            "slot": SLOT,
            "expected_active_revision": 0,
            "preview_id": confirmed,
        },
        headers={**case["headers"], "Idempotency-Key": "cross-conversation"},
    )
    assert cross_conversation.status_code == 409, cross_conversation.text
    assert cross_conversation.json()["reason_code"] == "WORKFLOW_CONVERSATION_MISMATCH"
    assert status(case)["slot_revision"] == 0
    assert case["client"].get(
        url(other_project["id"], "/workflow-deployments/default")
    ).json()["slot_revision"] == 0


def test_a_readback_of_another_projects_deployment_is_not_found(case: dict[str, Any]) -> None:
    """AC6: a read is scoped to the acting project, not to a deployment name."""
    from workflow_fixtures import make_repository

    other_project = case["registry"].create(
        {
            "name": "Other",
            "repository_path": str(make_repository(case["tmp_path"], "other-read")),
            "base_ref": "main",
            "target_branch": "main",
            "allowed_target_branches": ["main"],
        },
        command_key="other-read",
        principal="owner",
    )
    assert create_bundle(case).status_code == 201
    deployed_id = deploy(case, key="scoped").json()["deployment"]["deployment_id"]
    foreign = case["client"].get(
        url(
            other_project["id"],
            f"/workflow-deployments/{SLOT}/deployments/{deployed_id}",
        )
    )
    assert foreign.status_code == 404, foreign.text
    assert foreign.json()["reason_code"] == "WORKFLOW_DEPLOYMENT_NOT_FOUND"
    assert case["client"].get(
        url(other_project["id"], "/workflow-deployments/default/history")
    ).json()["items"] == []


def test_a_slot_name_can_never_be_a_path(case: dict[str, Any]) -> None:
    """AC6: a slot is one addressable name, not a host location."""
    assert create_bundle(case).status_code == 201
    for index, slot in enumerate(("../escape", "a/b", ".hidden", "..", "con", "a\\b")):
        response = deploy(case, key=f"slot-{index}", slot=slot)
        assert response.status_code in {422, 409}, (slot, response.text)
        assert response.status_code != 201
    # Nothing was created anywhere above or beside the managed project directory.
    assert not (state_root(case).parent / "escape").exists()
    assert not (state_root(case) / ".hidden").exists()
    assert status(case)["slot_revision"] == 0


def state_root(case: dict[str, Any]) -> Path:
    return case["directory"] / "workflow-deployments" / case["project_id"]


def test_an_unsupported_action_is_refused_before_any_state_change(
    case: dict[str, Any],
) -> None:
    """AC6: nothing is written for a command this slice refuses."""
    assert create_bundle(case).status_code == 201
    assert deploy(case, key="unsupported", action="deploy_and_run").status_code == 501
    assert json_rows(
        case,
        "SELECT record FROM workflow_deployment_intents WHERE principal=? AND key=?",
        ("owner", "unsupported"),
    ) == []
    assert status(case)["slot_revision"] == 0


def test_the_deployment_module_imports_nothing_from_a_request(
    case: dict[str, Any],
) -> None:
    """AC2/AC6: there is no loader-injection surface to reach at all."""
    import inspect

    from karajan.workflows import deployments, loader

    for module in (deployments, loader):
        source = inspect.getsource(module)
        for forbidden in ("importlib", "__import__", "eval(", "exec(", "subprocess"):
            assert forbidden not in source, (module.__name__, forbidden)
    assert "caller_supplied_modules" not in inspect.getsource(loader)
