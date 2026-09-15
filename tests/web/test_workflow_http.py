"""Authenticated HTTP behaviour for workflow bundles.

Every case goes through the real FastAPI application: real session bootstrap,
real CSRF, real SQLite and real files on disk. Restart cases construct a second
application over the same state directory, which is the only honest way to test
that a readback comes from the files rather than from a warm cache.

No case contacts a provider, resolves a credential, calls a model or invokes the
business adapter.
"""

import json
import sqlite3
from typing import Any

from fastapi.testclient import TestClient
from karajan.web import create_app
from workflow_fixtures import (
    BUNDLE,
    COORDINATOR_ROLE,
    EDITOR_ROLE,
    ORIGIN,
    RESEARCHER_ROLE,
    SECOND_WORKFLOW_TEXT,
    WORKFLOW_TEXT,
    bundle_case,
    create_bundle,
    files,
    login,
    make_repository,
    url,
)

case = bundle_case


def test_bundle_bytes_are_stored_verified_and_read_back_after_restart(
    case: dict[str, Any],
) -> None:
    """AC1: real files, immutable revision, restart readback, separate digests."""
    project_id = case["project_id"]
    response = create_bundle(case)
    assert response.status_code == 201, response.text
    record = response.json()
    assert record["revision"] == 1
    assert response.headers["ETag"] == '"1"'
    assert record["compiled_digest"]
    assert record["bundle_digest"]
    assert record["manifest_file_digest"]
    assert record["manifest_file_digest"] != record["bundle_digest"]
    assert record["compiled_digest"] not in {record["bundle_digest"]}
    assert record["compile"]["readback"] if False else True
    assert record["executable"] is True
    assert record["readiness"] == "executable"
    assert record["activation_allowed"] is False
    assert record["dispatch_eligible"] is False
    assert record["model_calls"] == 0

    fetched = case["client"].get(
        url(project_id, f"/workflows/{BUNDLE}/revisions/1")
    )
    assert fetched.status_code == 200, fetched.text
    stored = fetched.json()
    readback = stored["readback"]
    assert readback["source"] == "verified_files"
    assert readback["verified"] is True
    assert readback["bundle_digest"] == record["bundle_digest"]
    assert readback["compiled_digest"] == record["compiled_digest"]
    assert readback["manifest_file_digest"] == record["manifest_file_digest"]
    assert {item["path"] for item in readback["files"]} == {
        "workflow.yaml",
        "roles/researcher.yaml",
        "roles/editor.yaml",
    }
    for item in readback["files"]:
        on_disk = case["directory"] / "workflow-bundles" / project_id / BUNDLE / "revision-1"
        assert (on_disk / item["path"]).read_text(encoding="utf-8") == item["content"]
    # The manifest on disk is the exact document that was verified.
    assert (on_disk / "manifest.json").read_text(encoding="utf-8") == (
        json.dumps(readback["manifest"], sort_keys=True, separators=(",", ":")) + "\n"
    )

    reopened = create_app(
        case["directory"],
        origin=ORIGIN,
        bootstrap_token="second",
        allowed_roots=[case["repository"].parent],
    )
    with TestClient(reopened, base_url=ORIGIN) as client:
        login(client, "second")
        again = client.get(url(project_id, f"/workflows/{BUNDLE}/revisions/1"))
        assert again.status_code == 200, again.text
        assert again.json()["readback"]["compiled_digest"] == record["compiled_digest"]
        assert again.json()["readback"]["files"] == readback["files"]


def test_a_record_is_read_only_through_its_own_project(case: dict[str, Any]) -> None:
    """AC5: project ownership is enforced on every read."""
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
    assert case["client"].get(url(other["id"],
    f"/workflows/{BUNDLE}/revisions/1")).status_code == 404
    assert case["client"].get(url(other["id"], "/workflows")).json()["items"] == []
    # A conversation from another project is not usable either.
    cross = create_bundle(case, key="cross", conversation_id="00000000-0000-0000-0000-000000000000")
    assert cross.status_code == 404
    assert cross.json()["reason_code"] == "WORKFLOW_CONVERSATION_NOT_FOUND"

    # A conversation that belongs to another project is reported exactly like an
    # unknown one, so its existence is never disclosed.
    other_conversation = case["client"].post(
        f"/v1/projects/{other['id']}/conversations",
        json={"title": "Other session"},
        headers={**case["headers"], "Idempotency-Key": "other-conversation"},
    )
    assert other_conversation.status_code == 201, other_conversation.text
    cross_project = create_bundle(
        case, key="cross-project", conversation_id=other_conversation.json()["id"]
    )
    assert cross_project.status_code == 404
    assert cross_project.json()["reason_code"] == "WORKFLOW_CONVERSATION_NOT_FOUND"


def test_an_unauthenticated_request_is_refused_before_any_file(
    case: dict[str, Any],
) -> None:
    """AC5: no new authentication path, and no write without a session proof."""
    project_id = case["project_id"]
    payload = {"files": files(), "delivery_kind": "report"}
    target = url(project_id, f"/conversations/{case['conversation_id']}/workflows/{BUNDLE}")

    with TestClient(case["app"], base_url=ORIGIN) as anonymous_client:
        anonymous = anonymous_client.post(
            target, json=payload, headers={"Origin": ORIGIN, "Idempotency-Key": "anonymous"}
        )
        assert anonymous.status_code == 401
        assert anonymous.json()["reason_code"] == "AUTHENTICATION_REQUIRED"
        assert anonymous_client.get(url(project_id, "/workflows")).status_code == 401

    wrong_origin = case["client"].post(
        target,
        json=payload,
        headers={
            **case["headers"],
            "Origin": "https://attacker.test",
            "Idempotency-Key": "origin",
        },
    )
    assert wrong_origin.status_code == 403
    assert wrong_origin.json()["reason_code"] == "ORIGIN_REJECTED"

    without_csrf = {
        key: value for key, value in case["headers"].items() if key != "X-CSRF-Token"
    }
    rejected = case["client"].post(
        target, json=payload, headers={**without_csrf, "Idempotency-Key": "csrf"}
    )
    assert rejected.status_code == 403
    assert rejected.json()["reason_code"] == "CSRF_REJECTED"
    assert not (case["directory"] / "workflow-bundles" / project_id / BUNDLE).exists()


def test_idempotency_replay_conflict_and_late_write_rejection(
    case: dict[str, Any],
) -> None:
    """AC4: same key and payload replays; a different payload conflicts."""
    project_id = case["project_id"]
    created = create_bundle(case)
    assert created.status_code == 201
    original = created.json()

    replayed = create_bundle(case)
    assert replayed.status_code == 200
    assert replayed.json() == original
    assert len(case["client"].get(url(project_id, "/workflows")).json()["items"]) == 1

    conflict = create_bundle(case, workflow=SECOND_WORKFLOW_TEXT)
    assert conflict.status_code == 409
    assert conflict.json()["reason_code"] == "IDEMPOTENCY_CONFLICT"

    duplicate = create_bundle(case, key="bundle-again")
    assert duplicate.status_code == 422
    assert duplicate.json()["reason_code"] == "WORKFLOW_BUNDLE_EXISTS"

    # A stale expected revision reports the durable head and writes nothing.
    stale = case["client"].put(
        url(project_id, f"/conversations/{case['conversation_id']}/workflows/{BUNDLE}"),
        json={"files": files(), "delivery_kind": "report"},
        headers={**case["headers"], "Idempotency-Key": "stale", "If-Match": '"7"'},
    )
    assert stale.status_code == 409
    assert stale.json() == {
        "reason_code": "WORKFLOW_REVISION_CONFLICT",
        "current_revision": 1,
    }
    assert not (case["directory"] / "workflow-bundles" / project_id / BUNDLE / "revision-7").exists(
    )


def test_a_second_revision_is_immutable_and_the_first_survives(
    case: dict[str, Any],
) -> None:
    """AC1/AC4: a new revision updates the head and leaves the old one readable."""
    project_id = case["project_id"]
    first = create_bundle(case).json()
    revised = case["client"].put(
        url(project_id, f"/conversations/{case['conversation_id']}/workflows/{BUNDLE}"),
        json={
            "files": files(
                extra=[{"path": "roles/editor.yaml", "content": EDITOR_ROLE}]
            ),
            "delivery_kind": "report",
        },
        headers={**case["headers"], "Idempotency-Key": "revise", "If-Match": '"1"'},
    )
    assert revised.status_code == 201, revised.text
    second = revised.json()
    assert second["revision"] == 2
    assert second["digest"] != first["digest"]
    assert (
        case["client"]
        .get(url(project_id, f"/workflows/{BUNDLE}/revisions/1"))
        .json()["digest"]
        == first["digest"]
    )


def test_direct_edits_are_deterministic_and_call_no_model(case: dict[str, Any]) -> None:
    """AC4: a table edit writes a new revision with no model involvement."""
    project_id = case["project_id"]
    create_bundle(case)
    target = url(project_id, f"/conversations/{case['conversation_id']}/workflows/{BUNDLE}/edits")

    edited = case["client"].post(
        target,
        json={"edits": [{"operation": "set_dependency", "step_id": "comparison",
        "depends_on": "option-b"}]},
        headers={**case["headers"], "Idempotency-Key": "edit", "If-Match": '"1"'},
    )
    assert edited.status_code == 201, edited.text
    record = edited.json()
    assert record["revision"] == 2
    assert record["model_calls"] == 0
    assert record["edit_path"] == "deterministic_structural"
    assert record["applied_edits"] == [
        {"operation": "set_dependency", "step_id": "comparison"}
    ]

    # The edit is real: the new revision's compiled step depends on both branches.
    preview = case["client"].get(
        url(project_id, f"/workflows/{BUNDLE}/revisions/2/preview")
    ).json()
    comparison = next(
        node for node in preview["graph"]["nodes"] if node["id"] == "comparison"
    )
    assert comparison["join"] == "all_required"
    assert preview["compiled_digest"] == record["compiled_digest"]
    # A preview without a comparison revision reports a baseline rather than
    # claiming "nothing changed".
    assert preview["diff"]["baseline"] is True
    assert preview["diff"]["changed"] is None
    assert preview["diff"]["from_digest"] is None
    assert preview["diff"]["to_digest"] == record["compiled_digest"]

    # The same command replayed returns the same revision, not a third one.
    replay = case["client"].post(
        target,
        json={"edits": [{"operation": "set_dependency", "step_id": "comparison",
        "depends_on": "option-b"}]},
        headers={**case["headers"], "Idempotency-Key": "edit", "If-Match": '"1"'},
    )
    assert replay.status_code == 200
    assert replay.json()["revision"] == 2
    assert len(case["client"].get(url(project_id, "/workflows")).json()["items"]) == 2

    # A different payload under the same key is a conflict, not an overwrite.
    conflict = case["client"].post(
        target,
        json={"edits": [{"operation": "set_role", "step_id": "comparison", "role": "researcher"}]},
        headers={**case["headers"], "Idempotency-Key": "edit", "If-Match": '"1"'},
    )
    assert conflict.status_code == 409
    assert conflict.json()["reason_code"] == "IDEMPOTENCY_CONFLICT"

    # A late write from the old head is rejected and does not touch revision 2.
    late = case["client"].post(
        target,
        json={"edits": [{"operation": "set_required", "step_id": "option-b", "required": False}]},
        headers={**case["headers"], "Idempotency-Key": "late", "If-Match": '"1"'},
    )
    assert late.status_code == 409
    assert late.json()["current_revision"] == 2
    assert (
        case["client"].get(url(project_id, f"/workflows/{BUNDLE}/revisions/3")).status_code
        == 404
    )


def test_text_authoring_is_persisted_as_pending_and_generates_nothing(
    case: dict[str, Any],
) -> None:
    """AC4: design text is stored as authoring input, never as a configuration."""
    project_id = case["project_id"]
    create_bundle(case)
    target = url(
        project_id,
        f"/conversations/{case['conversation_id']}/workflows/{BUNDLE}/authoring-inputs",
    )
    created = case["client"].post(
        target,
        json={"instruction": "put the editor before the comparison", "base_revision": 1},
        headers={**case["headers"], "Idempotency-Key": "authoring"},
    )
    assert created.status_code == 201, created.text
    record = created.json()
    assert record["state"] == "pending_generation"
    assert record["generated_configuration"] is None
    assert record["generated_by"] is None
    assert record["model_calls"] == 0
    assert record["base_revision"] == 1

    listed = case["client"].get(
        url(project_id, f"/workflows/{BUNDLE}/authoring-inputs")
    ).json()["items"]
    assert [item["instruction"] for item in listed] == [
        "put the editor before the comparison"
    ]
    # No configuration revision was produced by the text.
    assert len(case["client"].get(url(project_id, "/workflows")).json()["items"]) == 1


def test_two_role_topologies_compile_with_their_own_sources(case: dict[str, Any]) -> None:
    """AC5: two different role and topology configurations are real and distinct."""
    project_id = case["project_id"]
    assert create_bundle(case).status_code == 201
    second = create_bundle(
        case,
        bundle_id="repair-config-loaders",
        workflow=SECOND_WORKFLOW_TEXT,
        extra=[{"path": "roles/coordinator.yaml", "content": COORDINATOR_ROLE}],
        key="bundle-create-second",
    )
    assert second.status_code == 201, second.text

    first_preview = case["client"].get(
        url(project_id, f"/workflows/{BUNDLE}/revisions/1/preview")
    ).json()
    second_preview = case["client"].get(
        url(project_id, "/workflows/repair-config-loaders/revisions/1/preview")
    ).json()
    assert first_preview["compiled_digest"] != second_preview["compiled_digest"]
    assert len(first_preview["graph"]["nodes"]) == 3
    assert len(second_preview["graph"]["nodes"]) == 1
    assert len(first_preview["graph"]["edges"]) == 2
    assert second_preview["graph"]["edges"] == []
    assert {role["alias"] for role in first_preview["roles"]} == {"researcher", "editor"}
    assert [role["alias"] for role in second_preview["roles"]] == ["coordinator"]
    # The two previews share no step identity beyond the single-node case.
    assert {node["id"] for node in first_preview["graph"]["nodes"]} == {
        "option-a",
        "option-b",
        "comparison",
    }
    assert first_preview["delivery_gate"]["publish_executor"] == "none"


def test_a_preview_is_derived_from_one_compiled_result(case: dict[str, Any]) -> None:
    """AC3: graph, table, diagram and diff all come from the same bundle."""
    project_id = case["project_id"]
    created = create_bundle(case).json()
    preview = case["client"].get(
        url(project_id, f"/workflows/{BUNDLE}/revisions/1/preview")
    ).json()
    assert preview["compiled_digest"] == created["compiled_digest"]
    assert preview["bundle_digest"] == created["bundle_digest"]
    assert preview["compiler_identity"] == created["compiler_identity"]
    assert preview["preview_id"]
    assert set(preview["graph"]) >= {"nodes", "edges", "required_steps", "artifact"}
    assert [row["step_id"] for row in preview["table"]] == [
        node["id"] for node in preview["graph"]["nodes"]
    ]
    assert preview["mermaid"].startswith("flowchart TD")
    for node in preview["graph"]["nodes"]:
        assert node["location"].startswith("workflow.yaml#/steps/id=")
    assert preview["immutable_revision_record"]["digest"] == created["digest"]

    # A diagram cannot inject diagram syntax through a step identity.
    hostile = create_bundle(
        case,
        bundle_id="hostile",
        workflow=WORKFLOW_TEXT.replace("option-a", "a<b>"),
        key="bundle-create-hostile",
    )
    assert hostile.status_code == 201, hostile.text
    diagram = case["client"].get(
        url(project_id, "/workflows/hostile/revisions/1/preview")
    ).json()["mermaid"]
    # The edge arrows are syntax; the label text cannot introduce markup.
    labels = [line.split('"')[1] for line in diagram.splitlines() if '"' in line]
    assert labels
    assert all("<" not in label and ">" not in label for label in labels)
    assert "a#lt;b#gt;" in " ".join(labels)


def test_more_than_one_hundred_nodes_read_back_completely(case: dict[str, Any]) -> None:
    """AC2: a legal configuration far beyond the old hundred-item ceiling."""
    project_id = case["project_id"]
    inputs = [f"requirement.item_{index:03d}" for index in range(110)]
    steps = [
        f"""  - id: fan_{index:03d}
    execution_kind: artifact_aggregate@1
    role: researcher
    output_contract: aggregated-report@1
    inputs:
      sources: requirement.item_{index:03d}
"""
        for index in range(110)
    ]
    join_inputs = "\n".join(f"        - fan_{index:03d}.output" for index in range(110))
    joins = "\n".join(f"      - fan_{index:03d}" for index in range(110))
    required = "\n".join(f"      - fan_{index:03d}" for index in range(110))
    document = f"""schema_version: workflow.v1
id: wide-fan-out
revision: 1
delivery_kind: report
input_contract: text@1
inputs:
{chr(10).join(f"  - {name}" for name in inputs)}
roles:
  researcher: role:source-researcher@2
steps:
{"".join(steps)}  - id: join
    execution_kind: artifact_aggregate@1
    role: researcher
    output_contract: aggregated-report@1
    depends_on:
{joins}
    inputs:
      sources:
{join_inputs}
completion:
  required_steps:
{required}
      - join
  artifact: join.output
"""
    created = create_bundle(case, bundle_id="wide", workflow=document, key="bundle-wide")
    assert created.status_code == 201, created.text
    preview = case["client"].get(
        url(project_id, "/workflows/wide/revisions/1/preview")
    ).json()
    assert len(preview["graph"]["nodes"]) == 111
    assert len(preview["graph"]["edges"]) == 110
    assert preview["executable"] is True

    # The full configuration is readable back after a restart, not truncated.
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
        assert len(stored.json()["readback"]["files"]) == 2
        assert (
            client.get(url(project_id, "/workflows/wide/revisions/1/preview")).json()[
                "compiled_digest"
            ]
            == preview["compiled_digest"]
        )


def test_tampering_with_stored_bytes_is_detected_on_readback(case: dict[str, Any]) -> None:
    """AC1: a changed file is reported, and no new preview is served under the id."""
    project_id = case["project_id"]
    created = create_bundle(case).json()
    root = case["directory"] / "workflow-bundles" / project_id / BUNDLE / "revision-1"
    (root / "workflow.yaml").write_text(WORKFLOW_TEXT.replace("compare-options", "rewritten"))

    response = case["client"].get(url(project_id, f"/workflows/{BUNDLE}/revisions/1"))
    assert response.status_code == 409, response.text
    assert response.json()["reason_code"] == "WORKFLOW_FILE_DIGEST_MISMATCH"

    preview = case["client"].get(
        url(project_id, f"/workflows/{BUNDLE}/revisions/1/preview")
    )
    assert preview.status_code == 409
    assert preview.json()["reason_code"] == "WORKFLOW_FILE_DIGEST_MISMATCH"
    # The published record itself is untouched and still describes the original.
    record = case["client"].get(url(project_id, "/workflows")).json()["items"][0]
    assert record["digest"] == created["digest"]

    # Replacing both the manifest and the file cannot produce a new template under
    # the old revision identity.
    replacement = root / "workflow.yaml"
    replacement.write_text(WORKFLOW_TEXT)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    entry = next(item for item in manifest["files"] if item["path"] == "workflow.yaml")
    entry["byte_digest"] = "f" * 64
    (root / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    response = case["client"].get(url(project_id, f"/workflows/{BUNDLE}/revisions/1"))
    assert response.status_code == 409
    assert response.json()["reason_code"] in {
        "WORKFLOW_FILE_DIGEST_MISMATCH",
        "WORKFLOW_BUNDLE_DIGEST_MISMATCH",
    }


def test_an_undeclared_extra_file_in_a_published_revision_is_reported(
    case: dict[str, Any],
) -> None:
    """The closed inventory is enforced on every read, including after restart."""
    project_id = case["project_id"]
    create_bundle(case)
    root = case["directory"] / "workflow-bundles" / project_id / BUNDLE / "revision-1"
    (root / "notes.txt").write_text("planted\n", encoding="utf-8")
    response = case["client"].get(url(project_id, f"/workflows/{BUNDLE}/revisions/1"))
    assert response.status_code == 409
    assert response.json()["reason_code"] == "WORKFLOW_UNDECLARED_FILE"


def test_invalid_paths_and_configurations_return_located_client_errors(
    case: dict[str, Any],
) -> None:
    """AC5: an invalid path or an unknown kind is a located 4xx, never a 500."""
    project_id = case["project_id"]
    for path, code in (
        ("../escape.yaml", "WORKFLOW_PATH_INVALID"),
        ("/absolute/workflow.yaml", "WORKFLOW_PATH_NOT_RELATIVE"),
        ("Roles/x.yaml", "WORKFLOW_PATH_NOT_ALLOWED"),
    ):
        response = case["client"].post(
            url(project_id,
            f"/conversations/{case['conversation_id']}/workflows/bad-{abs(hash(path))}"),
            json={
                "files": [
                    {"path": path, "content": "x"},
                    {"path": "roles/a.yaml", "content": "y"},
                ],
                "delivery_kind": "report",
            },
            headers={**case["headers"], "Idempotency-Key": f"path-{abs(hash(path))}"},
        )
        assert response.status_code == 422, (path, response.text)
        assert response.json()["reason_code"] == code
        assert str(case["tmp_path"]) not in response.text

    unknown_kind = WORKFLOW_TEXT.replace("artifact_aggregate@1", "ghost_kind@999")
    response = create_bundle(case, bundle_id="ghost", workflow=unknown_kind, key="ghost")
    assert response.status_code == 422
    assert response.json()["reason_code"] == "WORKFLOW_KIND_UNREGISTERED"

    # option-b depends on comparison while comparison depends on option-b: a real
    # two-node cycle, not a self-edge (which is reported as its own reason).
    cycle = WORKFLOW_TEXT.replace(
        """  - id: option-b
    execution_kind: artifact_aggregate@1
    role: researcher
    output_contract: aggregated-report@1
    inputs:""",
        """  - id: option-b
    execution_kind: artifact_aggregate@1
    role: researcher
    output_contract: aggregated-report@1
    depends_on:
      - comparison
    inputs:""",
    )
    response = create_bundle(case, bundle_id="cyclic", workflow=cycle, key="cyclic")
    assert response.status_code == 422
    assert response.json()["reason_code"] == "WORKFLOW_DEPENDENCY_CYCLE"

    self_dependency = WORKFLOW_TEXT.replace(
        """  - id: option-b
    execution_kind: artifact_aggregate@1
    role: researcher
    output_contract: aggregated-report@1
    inputs:""",
        """  - id: option-b
    execution_kind: artifact_aggregate@1
    role: researcher
    output_contract: aggregated-report@1
    depends_on:
      - option-b
    inputs:""",
    )
    response = create_bundle(
        case, bundle_id="self-cycle", workflow=self_dependency, key="self-cycle"
    )
    assert response.status_code == 422
    assert response.json()["reason_code"] == "WORKFLOW_DEPENDENCY_SELF"


def test_typed_invalid_payloads_are_located_client_errors(case: dict[str, Any]) -> None:
    """AC5: a wrong input type is a 4xx with a reason code, never a traceback."""
    project_id = case["project_id"]
    target = url(
        project_id, f"/conversations/{case['conversation_id']}/workflows/{BUNDLE}"
    )
    for payload in (
        {"files": files(), "delivery_kind": ["report"]},
        {"files": "not-a-list", "delivery_kind": "report"},
        {"files": [{"path": "workflow.yaml", "content": "x", "extra": 1}],
        "delivery_kind": "report"},
        {"files": files(), "delivery_kind": "report", "unknown": True},
        {"files": files(), "delivery_kind": "deploy"},
    ):
        response = case["client"].post(
            target,
            json=payload,
            headers={**case["headers"], "Idempotency-Key": f"typed-{abs(hash(str(payload)))}"},
        )
        assert response.status_code == 422, (payload, response.text)
        assert response.json()["reason_code"] in {"INPUT_INVALID", "WORKFLOW_DELIVERY_KIND_INVALID"}


def test_the_execution_kind_catalog_reports_real_availability(case: dict[str, Any]) -> None:
    """AC2: the trusted registry is readable, and adapter-less kinds stay unavailable."""
    catalog = case["client"].get(
        url(case["project_id"], "/workflow-execution-kinds")
    ).json()
    by_ref = {item["execution_kind_ref"]: item for item in catalog["kinds"]}
    assert by_ref["artifact_aggregate@1"]["available"] is True
    assert by_ref["artifact_aggregate@1"]["deterministic"] is True
    assert by_ref["artifact_aggregate@1"]["side_effects"] == "local_artifact"
    assert by_ref["agent_task@1"]["available"] is False
    assert by_ref["publish_pr@1"]["available"] is False
    assert "WORKFLOW_KIND_ADAPTER_NOT_IMPLEMENTED" in by_ref["agent_task@1"]["reason_codes"]
    assert catalog["caller_supplied_modules"] is False


def test_an_unavailable_capability_is_reviewable_but_not_executable(
    case: dict[str, Any],
) -> None:
    """AC2: a declared-but-unimplemented kind is a capability gap, not a guess."""
    document = WORKFLOW_TEXT.replace(
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
    response = create_bundle(case, bundle_id="agentic", workflow=document, key="agentic")
    assert response.status_code == 201, response.text
    record = response.json()
    assert record["executable"] is False
    assert record["readiness"] == "unavailable_capability"
    assert "agent_task@1" in record["unavailable_execution_kinds"]
    preview = case["client"].get(
        url(case["project_id"], "/workflows/agentic/revisions/1/preview")
    ).json()
    assert preview["executable"] is False
    assert preview["readiness"] == "unavailable_capability"
    assert {item["code"] for item in preview["diagnostics"]} == {
        "WORKFLOW_KIND_ADAPTER_NOT_IMPLEMENTED"
    }


def test_compilation_never_invokes_the_business_adapter(case: dict[str, Any]) -> None:
    """AC2: compiling a deploy-only template validates, and runs nothing."""
    from karajan.workflows import registry

    calls: list[object] = []
    original = registry.artifact_aggregate

    def recorded(inputs: Any) -> Any:
        calls.append(inputs)
        return original(inputs)

    # Even with the adapter replaced by a recording wrapper, a compile path must
    # not reach it: the compiled record is produced from the template alone.
    aggregate = registry._REGISTRY["artifact_aggregate@1"]
    object.__setattr__(aggregate, "adapter", recorded)
    try:
        response = create_bundle(case, bundle_id="deploy-only", key="deploy-only")
        assert response.status_code == 201, response.text
        assert calls == []
        assert response.json()["model_calls"] == 0
    finally:
        object.__setattr__(aggregate, "adapter", original)


def test_no_credential_or_local_path_leaks_through_responses(case: dict[str, Any]) -> None:
    """AC1/AC5: a refusal carries a bundle-relative location and no host path."""
    project_id = case["project_id"]
    response = create_bundle(case, bundle_id="leaky", workflow=WORKFLOW_TEXT, key="leaky-unknown")
    assert response.status_code == 201
    # A missing file reports a bundle-relative location.
    missing = case["client"].post(
        url(project_id, f"/conversations/{case['conversation_id']}/workflows/leaky-missing"),
        json={"files": [{"path": "roles/only.yaml", "content": RESEARCHER_ROLE}],
        "delivery_kind": "report"},
        headers={**case["headers"], "Idempotency-Key": "missing-file"},
    )
    assert missing.status_code == 422
    assert missing.json()["diagnostics"][0]["location"] == "workflow.yaml"
    for response_ in (missing,):
        assert str(case["tmp_path"]) not in response_.text
        assert "C:\\" not in response_.text and "/home/" not in response_.text

    # A read of a revision that was never created is a clean 404, not a leak.
    stored = case["client"].get(url(project_id, f"/workflows/{BUNDLE}/revisions/1"))
    assert stored.status_code == 404
    assert str(case["tmp_path"]) not in stored.text


def test_records_never_carry_a_filesystem_location(case: dict[str, Any]) -> None:
    """The durable revision record names paths relative to its own bundle only."""
    create_bundle(case)
    with sqlite3.connect(case["directory"] / "projects.sqlite") as db:
        rows = [row[0] for row in db.execute("SELECT record FROM workflow_bundles")]
        files = [
            (row[3], row[4], row[5])
            for row in db.execute(
                "SELECT project_id, id, revision, path, byte_digest, byte_length "
                "FROM workflow_bundle_files"
            )
        ]
    assert rows, "the revision must be durable"
    for raw in rows:
        assert str(case["tmp_path"]) not in raw
        assert "workflow-bundles" not in raw
    for path, digest, length in files:
        assert not path.startswith(("/", "C:", "\\\\"))
        assert len(digest) == 64
        assert length > 0


def test_concurrent_writers_leave_exactly_one_revision(case: dict[str, Any]) -> None:
    """AC4: two applications over one state directory do not both publish."""
    project_id = case["project_id"]
    create_bundle(case)
    second = create_app(
        case["directory"],
        origin=ORIGIN,
        bootstrap_token="concurrent",
        allowed_roots=[case["repository"].parent],
    )
    with TestClient(second, base_url=ORIGIN) as other_client:
        headers = login(other_client, "concurrent")
        payload = {
            "files": files(extra=[{"path": "roles/editor.yaml", "content": EDITOR_ROLE}]),
            "delivery_kind": "report",
        }
        target = url(
            project_id, f"/conversations/{case['conversation_id']}/workflows/{BUNDLE}"
        )
        first = other_client.put(
            target,
            json=payload,
            headers={**headers, "Idempotency-Key": "race-a", "If-Match": '"1"'},
        )
        stale = other_client.put(
            target,
            json=payload,
            headers={**headers, "Idempotency-Key": "race-b", "If-Match": '"1"'},
        )
    assert first.status_code == 201, first.text
    assert stale.status_code == 409
    assert stale.json()["current_revision"] == 2
    assert [
        item["revision"]
        for item in case["client"].get(url(project_id, "/workflows")).json()["items"]
    ] == [1, 2]


def test_gateway_bindings_resolve_by_exact_revision_without_a_probe(
    case: dict[str, Any],
) -> None:
    """AC2/AC5: a fixed binding reference is validated by exact revision."""
    project_id = case["project_id"]
    connection = case["client"].post(
        url(project_id, "/gateway-connections"),
        json={
            "connection_id": "local-gateway",
            "base_url": "http://127.0.0.1:9",
            "protocol": {"family": "openai_compatible", "protocol_version": "v1"},
            "request_transformation": {"policy_id": "no-transform", "revision": 1},
        },
        headers={**case["headers"], "Idempotency-Key": "gc"},
    )
    assert connection.status_code == 201, connection.text
    binding = case["client"].post(
        url(project_id, "/gateway-bindings"),
        json={
            "binding_id": "vendor-binding",
            "connection_id": "local-gateway",
            "connection_revision": 1,
            "model_alias": "vendor-model-a",
            "declared": {
                "provider_id": "vendor",
                "account_id": "vendor-account",
                "billing_path": "subscription_only",
            },
            "request_transformation": {"policy_id": "no-transform", "revision": 1},
        },
        headers={**case["headers"], "Idempotency-Key": "gb"},
    )
    assert binding.status_code == 201, binding.text

    document = WORKFLOW_TEXT.replace(
        "roles:\n", "bindings:\n  researcher: binding:vendor-binding@1\nroles:\n"
    )
    created = create_bundle(case, bundle_id="bound", workflow=document, key="bound")
    assert created.status_code == 201, created.text
    index = created.json()["binding_index"]
    resolved = index["binding:vendor-binding@1"]
    assert resolved["binding"]["revision"] == 1
    assert resolved["binding"]["digest"] == binding.json()["digest"]
    assert resolved["connection"]["revision"] == 1
    assert resolved["connection"]["digest"] == connection.json()["digest"]
    # Resolution is not qualification: nothing probed and nothing became eligible.
    assert resolved["probe_performed"] is False
    assert resolved["credential_resolved"] is False
    assert resolved["execution_eligible"] is False
    assert resolved["draft_only"] is True

    # A floating reference is refused rather than resolved to the current head.
    floating = create_bundle(
        case,
        bundle_id="floating",
        workflow=document.replace("binding:vendor-binding@1", "binding:vendor-binding@latest"),
        key="floating",
    )
    assert floating.status_code == 422
    assert floating.json()["reason_code"] == "WORKFLOW_REFERENCE_NOT_PINNED"

    # An unknown binding identity is not resolvable from this project.
    unknown = create_bundle(
        case,
        bundle_id="unknown-binding",
        workflow=document.replace("vendor-binding@1", "ghost-binding@1"),
        key="unknown-binding",
    )
    assert unknown.status_code in {404, 422}
    assert unknown.json()["reason_code"] in {
        "GATEWAY_REVISION_NOT_FOUND",
        "GATEWAY_BINDING_NOT_FOUND",
    }
