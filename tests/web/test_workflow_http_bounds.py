"""Second-pass HTTP/store regressions: real files, real restarts, real writes.

These cases reproduce defects an independent review found on the first
candidate: a hidden step-count cap on the real bundle path, authoring text filed
under another conversation's bundle, an edit whose replay returned an incomplete
result, an uncommitted materialisation that permanently blocked its revision, and
a junction that received bytes before the write boundary was checked.
"""

from typing import Any

from fastapi.testclient import TestClient
from karajan.web import create_app
from link_fixture import create_directory_link, remove_directory_link
from workflow_fixtures import (
    BUNDLE,
    ORIGIN,
    bundle_case,
    create_bundle,
    files,
    login,
    make_repository,
    url,
)

case = bundle_case


def wide_document(count: int) -> str:
    """One legal workflow with ``count`` independent steps plus a join.

    Written compactly because the document's own byte size is a transport
    concern; how many steps it declares is not, and nothing here imposes one.
    """
    inputs = "".join(f"  - requirement.item_{index:04d}\n" for index in range(count))
    fan_out = "".join(
        f"  - {{id: fan_{index:04d}, execution_kind: artifact_aggregate@1, "
        f"output_contract: aggregated-report@1, "
        f"inputs: {{sources: requirement.item_{index:04d}}}}}\n"
        for index in range(count)
    )
    names = "[" + ", ".join(f"fan_{index:04d}" for index in range(count)) + "]"
    outputs = "[" + ", ".join(f"fan_{index:04d}.output" for index in range(count)) + "]"
    return (
        "schema_version: workflow.v1\n"
        "id: wide-fan-out\n"
        "revision: 1\n"
        "delivery_kind: report\n"
        "input_contract: text@1\n"
        "inputs:\n"
        f"{inputs}"
        "roles:\n  researcher: role:source-researcher@2\n"
        "steps:\n"
        f"{fan_out}"
        "  - id: join\n"
        "    execution_kind: artifact_aggregate@1\n"
        "    role: researcher\n"
        "    output_contract: aggregated-report@1\n"
        f"    depends_on: {names}\n"
        f"    inputs: {{sources: {outputs}}}\n"
        "completion:\n"
        f"  required_steps: {names.rstrip(']')}, join]\n"
        "  artifact: join.output\n"
    )


def test_five_thousand_steps_round_trip_through_the_real_api(case: dict[str, Any]) -> None:
    """AC2: a large but ordinary configuration is stored, read back and compiled.

    Built through the real HTTP boundary and the real file store, so this also
    proves no transport or parser bound has quietly become a task-count policy.
    """
    project_id = case["project_id"]
    count = 5_000
    document = wide_document(count)
    assert len(document.encode("utf-8")) < 1_048_576, "the document must fit the file bound"

    created = create_bundle(case, bundle_id="wide", workflow=document, key="bundle-wide")
    assert created.status_code == 201, created.text[:400]
    record = created.json()
    assert record["executable"] is True

    preview = case["client"].get(
        url(project_id, "/workflows/wide/revisions/1/preview")
    ).json()
    assert len(preview["graph"]["nodes"]) == count + 1
    assert len(preview["graph"]["edges"]) == count
    assert preview["graph"]["nodes"][0]["id"] == "fan_0000"
    assert preview["graph"]["nodes"][-1]["id"] == "join"
    assert preview["compiled_digest"] == record["compiled_digest"]
    assert preview["graph"]["required_steps"] == [
        *(f"fan_{index:04d}" for index in range(count)),
        "join",
    ]

    reopened = create_app(
        case["directory"],
        origin=ORIGIN,
        bootstrap_token="wide-restart",
        allowed_roots=[case["repository"].parent],
    )
    with TestClient(reopened, base_url=ORIGIN) as client:
        login(client, "wide-restart")
        stored = client.get(url(project_id, "/workflows/wide/revisions/1"))
        assert stored.status_code == 200, stored.text
        body = stored.json()
        assert body["readback"]["compiled_digest"] == record["compiled_digest"]
        assert len(body["readback"]["manifest"]["files"]) == 2
        assert (
            client.get(url(project_id, "/workflows/wide/revisions/1/preview")).json()[
                "compiled_digest"
            ]
            == record["compiled_digest"]
        )


def test_authoring_text_cannot_be_filed_under_another_conversations_bundle(
    case: dict[str, Any],
) -> None:
    """AC4/AC5: bundle ownership is fixed, whether or not a base is supplied."""
    project_id = case["project_id"]
    assert create_bundle(case).status_code == 201
    second = case["client"].post(
        f"/v1/projects/{project_id}/conversations",
        json={"title": "Other session"},
        headers={**case["headers"], "Idempotency-Key": "second-conversation"},
    )
    assert second.status_code == 201, second.text
    other = second.json()["id"]

    for index, payload in enumerate(
        (
            {"instruction": "put the editor before the comparison"},
            {"instruction": "put the editor before the comparison", "base_revision": 1},
        )
    ):
        response = case["client"].post(
            url(project_id, f"/conversations/{other}/workflows/{BUNDLE}/authoring-inputs"),
            json=payload,
            headers={**case["headers"], "Idempotency-Key": f"authoring-{index}"},
        )
        assert response.status_code == 409, response.text
        assert response.json()["reason_code"] == "WORKFLOW_CONVERSATION_MISMATCH"
    assert (
        case["client"].get(url(project_id, f"/workflows/{BUNDLE}/authoring-inputs")).json()[
            "items"
        ]
        == []
    )

    # The instruction may come *before* any revision exists. Whichever command
    # claims the identity first owns it, so this one claims it for this
    # conversation; a create from another conversation must then be refused, and
    # this conversation must still be able to publish its own bundle.
    authored_first = "authored-first"
    first = case["client"].post(
        url(
            project_id,
            f"/conversations/{case['conversation_id']}/workflows/{authored_first}"
            "/authoring-inputs",
        ),
        json={"instruction": "start from the defect report"},
        headers={**case["headers"], "Idempotency-Key": "authored-first"},
    )
    assert first.status_code == 201, first.text
    assert first.json()["state"] == "pending_generation"

    hijack = case["client"].post(
        url(project_id, f"/conversations/{other}/workflows/{authored_first}"),
        json={"files": files(), "delivery_kind": "report"},
        headers={**case["headers"], "Idempotency-Key": "hijack-create"},
    )
    assert hijack.status_code == 409, hijack.text
    assert hijack.json()["reason_code"] == "WORKFLOW_CONVERSATION_MISMATCH"
    # Nothing was written for the hijack: no revision and no directory appeared.
    assert (
        case["client"].get(url(project_id, f"/workflows/{authored_first}/revisions/1")).status_code
        == 404
    )
    assert not (
        case["directory"] / "workflow-bundles" / project_id / authored_first
    ).exists()

    # A later instruction from the other conversation is refused as well.
    crossed = case["client"].post(
        url(project_id, f"/conversations/{other}/workflows/{authored_first}/authoring-inputs"),
        json={"instruction": "hijack"},
        headers={**case["headers"], "Idempotency-Key": "authored-first-other"},
    )
    assert crossed.status_code == 409
    assert crossed.json()["reason_code"] == "WORKFLOW_CONVERSATION_MISMATCH"

    # The original conversation can continue and publish its own bundle.
    own = create_bundle(
        case,
        bundle_id=authored_first,
        key="authored-first-create",
        conversation_id=case["conversation_id"],
    )
    assert own.status_code == 201, own.text
    assert own.json()["revision"] == 1
    assert own.json()["conversation_id"] == case["conversation_id"]


def test_a_replayed_edit_returns_the_complete_original_result(case: dict[str, Any]) -> None:
    """AC4: the ledger stores the exact document the caller received."""
    project_id = case["project_id"]
    create_bundle(case)
    target = url(
        project_id, f"/conversations/{case['conversation_id']}/workflows/{BUNDLE}/edits"
    )
    payload = {
        "edits": [
            {"operation": "set_dependency", "step_id": "comparison", "depends_on": "option-b"}
        ]
    }
    first = case["client"].post(
        target,
        json=payload,
        headers={**case["headers"], "Idempotency-Key": "edit-replay", "If-Match": '"1"'},
    )
    assert first.status_code == 201, first.text
    assert first.json()["applied_edits"]
    assert first.json()["edit_path"] == "deterministic_structural"
    assert first.json()["model_calls"] == 0

    replay = case["client"].post(
        target,
        json=payload,
        headers={**case["headers"], "Idempotency-Key": "edit-replay", "If-Match": '"1"'},
    )
    assert replay.status_code == 200
    # Not merely the revision record: the whole command-specific result.
    assert replay.json() == first.json()

    stale = case["client"].put(
        url(project_id, f"/conversations/{case['conversation_id']}/workflows/{BUNDLE}"),
        json={"files": files(), "delivery_kind": "report"},
        headers={**case["headers"], "Idempotency-Key": "after-edit", "If-Match": '"1"'},
    )
    # The head has advanced, so a write against the old revision is a conflict
    # reporting the current head rather than a missing record.
    assert stale.status_code == 409
    assert stale.json()["current_revision"] == 2


def test_an_uncommitted_materialization_is_recovered_after_reopen(
    case: dict[str, Any],
) -> None:
    """AC1/AC4: a crash before the commit must not block the revision forever."""
    import sqlite3

    project_id = case["project_id"]
    created = create_bundle(case).json()

    # Remove only the committed rows: this is exactly what an interrupted attempt
    # leaves behind, with the revision directory already on disk.
    with sqlite3.connect(case["directory"] / "projects.sqlite") as db:
        db.execute("DELETE FROM workflow_bundles")
        db.execute("DELETE FROM workflow_bundle_files")
        db.execute("DELETE FROM workflow_bundle_current")
        db.execute("DELETE FROM commands")
    assert (
        case["client"].get(url(project_id, f"/workflows/{BUNDLE}/revisions/1")).status_code == 404
    )

    reopened = create_app(
        case["directory"],
        origin=ORIGIN,
        bootstrap_token="recovery",
        allowed_roots=[case["repository"].parent],
    )
    with TestClient(reopened, base_url=ORIGIN) as client:
        headers = login(client, "recovery")
        retried = client.post(
            url(project_id, f"/conversations/{case['conversation_id']}/workflows/{BUNDLE}"),
            json={"files": files(), "delivery_kind": "report"},
            headers={**headers, "Idempotency-Key": "bundle-create"},
        )
        assert retried.status_code == 201, retried.text
        assert retried.json()["compiled_digest"] == created["compiled_digest"]
        assert retried.json()["bundle_digest"] == created["bundle_digest"]
        stored = client.get(url(project_id, f"/workflows/{BUNDLE}/revisions/1"))
        assert stored.status_code == 200, stored.text
        assert stored.json()["readback"]["compiled_digest"] == created["compiled_digest"]


def test_a_link_never_receives_any_bundle_bytes(case: dict[str, Any]) -> None:
    """AC1: the managed chain is validated before the first write."""
    project_id = case["project_id"]
    outside = case["tmp_path"] / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("untouched\n", encoding="utf-8")
    before = sorted(str(path.relative_to(outside)) for path in outside.rglob("*"))
    link = case["directory"] / "workflow-bundles" / project_id
    link.parent.mkdir(parents=True, exist_ok=True)
    create_directory_link(link, outside)
    try:
        response = create_bundle(case, bundle_id="linked", key="linked")
        assert response.status_code == 422, response.text
        assert response.json()["reason_code"] == "WORKFLOW_PATH_LINK_ESCAPE"
        # The outside directory is untouched: this asserts about bytes, not about
        # which exception the call eventually raised.
        assert sorted(str(path.relative_to(outside)) for path in outside.rglob("*")) == before
        assert (outside / "keep.txt").read_text(encoding="utf-8") == "untouched\n"
    finally:
        remove_directory_link(link)


def test_the_store_never_writes_outside_its_managed_root(case: dict[str, Any]) -> None:
    """A successful publish creates files only under the managed data root."""
    case["tmp_path"] / "watched"
    (case["tmp_path"] / "watched").mkdir()
    before = {
        str(path.relative_to(case["tmp_path"])) for path in case["tmp_path"].rglob("*")
    }
    assert create_bundle(case).status_code == 201
    after = {str(path.relative_to(case["tmp_path"])) for path in case["tmp_path"].rglob("*")}
    created = after - before
    assert created
    normalized = {path.replace("\\", "/") for path in created}
    assert all(path.startswith("state/workflow-bundles/") for path in normalized), sorted(
        normalized
    )[:5]


def test_a_second_project_cannot_read_another_projects_revision(case: dict[str, Any]) -> None:
    """AC5: ownership is re-checked on every read, including the preview."""
    other = case["registry"].create(
        {
            "name": "Other",
            "repository_path": str(make_repository(case["tmp_path"], "other")),
            "base_ref": "main",
            "target_branch": "main",
            "allowed_target_branches": ["main"],
        },
        command_key="other-project",
        principal="owner",
    )
    assert create_bundle(case).status_code == 201
    for suffix in (
        f"/workflows/{BUNDLE}/revisions/1",
        f"/workflows/{BUNDLE}/revisions/1/preview",
    ):
        assert case["client"].get(url(other["id"], suffix)).status_code == 404
    # A listing is project-scoped rather than not-found: the other project simply
    # has no record for this identity.
    assert (
        case["client"].get(url(other["id"], f"/workflows/{BUNDLE}/authoring-inputs")).json()[
            "items"
        ]
        == []
    )
    assert case["client"].get(url(other["id"], "/workflows")).json()["items"] == []
    assert case["client"].get(url(other["id"], "/workflow-execution-kinds")).status_code == 200


def test_the_preview_and_the_stored_bytes_describe_one_revision(
    case: dict[str, Any],
) -> None:
    """AC3: graph, table, diagram and file contents come from the same source."""
    project_id = case["project_id"]
    created = create_bundle(case).json()
    stored = case["client"].get(url(project_id, f"/workflows/{BUNDLE}/revisions/1")).json()
    preview = case["client"].get(
        url(project_id, f"/workflows/{BUNDLE}/revisions/1/preview")
    ).json()
    assert stored["compiled_digest"] == preview["compiled_digest"] == created["compiled_digest"]
    assert preview["bundle_digest"] == stored["bundle_digest"] == created["bundle_digest"]
    step_ids = {node["id"] for node in preview["graph"]["nodes"]}
    assert {row["step_id"] for row in preview["table"]} == step_ids
    workflow_file = next(
        item for item in stored["readback"]["files"] if item["path"] == "workflow.yaml"
    )
    for step_id in step_ids:
        assert f"id: {step_id}\n" in workflow_file["content"]
    assert preview["immutable_revision_record"]["digest"] == stored["digest"]


def test_a_restart_app_serves_the_same_preview_identity(case: dict[str, Any]) -> None:
    """A preview identity is derived from the files, not from process memory."""
    project_id = case["project_id"]
    create_bundle(case)
    before = case["client"].get(
        url(project_id, f"/workflows/{BUNDLE}/revisions/1/preview")
    ).json()
    reopened = create_app(
        case["directory"],
        origin=ORIGIN,
        bootstrap_token="preview-restart",
        allowed_roots=[case["repository"].parent],
    )
    with TestClient(reopened, base_url=ORIGIN) as client:
        login(client, "preview-restart")
        after = client.get(url(project_id, f"/workflows/{BUNDLE}/revisions/1/preview")).json()
    assert after["preview_id"] == before["preview_id"]
    assert after["compiled_digest"] == before["compiled_digest"]
    assert after["mermaid"] == before["mermaid"]
    assert after["table"] == before["table"]


def test_the_bundle_route_has_its_own_declared_transport_bound(
    case: dict[str, Any],
) -> None:
    """A workflow upload is a document, not a small command payload.

    The bound is declared in bytes and applies before parsing. It is asserted
    here so a future change cannot quietly shrink it back to the general request
    limit, which would reintroduce a step-count ceiling by the side door.
    """
    from karajan.web.body_limit import (
        DEFAULT_MAXIMUM_BODY,
        WORKFLOW_BUNDLE_MAXIMUM_BODY,
        body_bound,
    )

    assert body_bound("/v1/projects/x/conversations/y/workflows/z") == (
        WORKFLOW_BUNDLE_MAXIMUM_BODY
    )
    assert body_bound("/v1/projects/x/workflows/z/edits") == WORKFLOW_BUNDLE_MAXIMUM_BODY
    assert body_bound("/v1/projects/x/workflows/z/authoring-inputs") == (
        WORKFLOW_BUNDLE_MAXIMUM_BODY
    )
    assert body_bound("/v1/projects/x/conversations") == DEFAULT_MAXIMUM_BODY
    assert WORKFLOW_BUNDLE_MAXIMUM_BODY > DEFAULT_MAXIMUM_BODY
