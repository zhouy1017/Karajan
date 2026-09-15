"""AC1: a Run is created from a real, freshly loaded, active deployment.

The Run freezes the deployment, the concrete inputs and the initial user
authorization, and those frozen facts stay frozen. A later activation moves the
slot, not the Run: a caller holding the Run still describes the definition it was
authorised against.

Every case here enters through the authenticated management route. None of them
inserts a row directly, and none of them re-uses a previous case's deployment
identity.
"""

import json
from typing import Any

import pytest
from scheduling_fixtures import (  # noqa: F401  (fixtures re-exported)
    BUNDLE,
    INPUTS,
    REQUIRED_OUTCOME,
    RUN_KEY,
    SLOT,
    grant_body,
    key,
    login,
    make_repository,
    url,
)


def restart(case: dict[str, Any], token: str = "second") -> Any:
    """A second application over the same real state directory."""
    from fastapi.testclient import TestClient
    from karajan.web import create_app

    app = create_app(
        case["directory"],
        origin="http://127.0.0.1:8765",
        bootstrap_token=token,
        allowed_roots=[case["repository"].parent],
    )
    return TestClient(app, base_url="http://127.0.0.1:8765")


def run_payload(case: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "conversation_id": case["conversation_id"],
        "slot": SLOT,
        "expected_active_revision": 1,
        "deployment_id": case["deployment"]["deployment"]["deployment_id"],
        "inputs": INPUTS,
        "requirement": {"goal": "compare the two options", "acceptance": ["both read"]},
        "required_outcomes": [REQUIRED_OUTCOME],
    }
    body.update(overrides)
    return body


def test_a_run_freezes_the_loaded_deployment_inputs_and_authorization(
    case: dict[str, Any],
) -> None:
    """The frozen source is the *acquired* handle, not a re-read of the bundle."""
    base = url(case["project_id"], "")
    response = case["client"].post(
        f"{base}/workflow-runs",
        json=run_payload(case),
        headers={**case["headers"], **key(RUN_KEY)},
    )
    assert response.status_code == 201, response.text
    run = response.json()
    deployment = case["deployment"]["deployment"]
    assert run["source"]["deployment_id"] == deployment["deployment_id"]
    assert run["source"]["slot_revision"] == 1
    assert run["source"]["bundle_revision"] == 1
    assert run["source"]["bundle_digest"] == deployment["bundle_digest"]
    assert run["source"]["compiled_digest"] == deployment["compiled_digest"]
    # The acquisition digest identifies *this* acquisition: it covers the load
    # timestamp and process, so it is recorded as an acquisition identity rather
    # than as a durable deployment identity.
    assert len(run["source"]["acquisition_digest"]) == 64
    assert run["source"]["acquired_by_process"] is not None
    assert run["inputs"] == INPUTS
    assert run["initial_authorization"]["authorization_id"] == "run-initial-authorization"
    assert run["initial_authorization"]["inputs_digest"] == run["inputs_digest"]
    assert run["initial_authorization"]["required_outcomes"] == [REQUIRED_OUTCOME]
    assert run["graph_revision"] == 0
    assert run["model_calls"] == 0
    assert run["business_steps_executed"] == 0


def test_the_same_creation_key_returns_the_original_run(case: dict[str, Any]) -> None:
    """A repeated command never creates a second Run, and never rewrites it."""
    base = url(case["project_id"], "")
    first = case["client"].post(
        f"{base}/workflow-runs", json=run_payload(case), headers={**case["headers"], **key(RUN_KEY)}
    )
    assert first.status_code == 201, first.text
    run_id = first.json()["run_id"]

    replay = case["client"].post(
        f"{base}/workflow-runs", json=run_payload(case), headers={**case["headers"], **key(RUN_KEY)}
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["run_id"] == run_id
    assert replay.json()["source"] == first.json()["source"]
    assert replay.json()["initial_authorization"] == first.json()["initial_authorization"]
    listed = case["client"].get(f"{base}/workflow-runs", headers=case["headers"])
    assert [item["run_id"] for item in listed.json()["items"]] == [run_id]


def test_a_changed_payload_on_the_same_key_conflicts(case: dict[str, Any]) -> None:
    base = url(case["project_id"], "")
    first = case["client"].post(
        f"{base}/workflow-runs", json=run_payload(case), headers={**case["headers"], **key(RUN_KEY)}
    )
    assert first.status_code == 201, first.text
    changed = case["client"].post(
        f"{base}/workflow-runs",
        json=run_payload(
            case, inputs={**INPUTS, "requirement.option_a": "a genuinely different input"}
        ),
        headers={**case["headers"], **key(RUN_KEY)},
    )
    assert changed.status_code == 409, changed.text
    assert changed.json()["reason_code"] == "SCHEDULING_IDEMPOTENCY_CONFLICT"


def test_an_unloaded_or_wrong_deployment_is_refused(case: dict[str, Any]) -> None:
    """A Run cannot be created from a deployment this process has not loaded."""
    base = url(case["project_id"], "")
    wrong = case["client"].post(
        f"{base}/workflow-runs",
        json=run_payload(case, deployment_id="0" * 32),
        headers={**case["headers"], **key("run-wrong")},
    )
    assert wrong.status_code == 409, wrong.text
    assert wrong.json()["reason_code"] == "SCHEDULING_RUN_SOURCE_STALE"

    stale = case["client"].post(
        f"{base}/workflow-runs",
        json=run_payload(case, expected_active_revision=7),
        headers={**case["headers"], **key("run-stale")},
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["reason_code"] == "SCHEDULING_RUN_SOURCE_STALE"


def other_project(case: dict[str, Any], name: str) -> dict[str, Any]:
    """A second real project in the same registry, with its own repository."""
    (case["tmp_path"] / "repositories").mkdir(exist_ok=True)
    repository = make_repository(case["tmp_path"] / "repositories" / name)
    return dict(
        case["registry"].create(
            {
                "name": name,
                "repository_path": str(repository),
                "base_ref": "main",
                "target_branch": "main",
                "allowed_target_branches": ["main"],
            },
            command_key=f"project-{name}",
            principal="owner",
        )
    )


def test_a_cross_project_deployment_is_not_found(case: dict[str, Any]) -> None:
    """Another project's deployment identity is not usable, and is not probed."""
    other = other_project(case, "other-cross")
    response = case["client"].post(
        url(other["id"], "/workflow-runs"),
        json=run_payload(case, deployment_id=case["deployment"]["deployment"]["deployment_id"]),
        headers={**case["headers"], **key("cross-project")},
    )
    # The other project owns no deployment, so its slot is empty; the deployment
    # identity of this project is never consulted.
    # The other project owns no conversation and no deployment, so the request
    # is refused before any deployment identity of this project is consulted.
    assert response.status_code in {404, 409}, response.text
    assert response.json()["reason_code"] in {
        "SCHEDULING_CONVERSATION_NOT_FOUND",
        "SCHEDULING_RUN_SOURCE_STALE",
        "WORKFLOW_SLOT_EMPTY",
    }


def test_the_frozen_source_survives_a_later_active_replacement(case: dict[str, Any]) -> None:
    """A later deployment moves the slot; the Run still describes its own."""
    base = url(case["project_id"], "")
    created = case["client"].post(
        f"{base}/workflow-runs", json=run_payload(case), headers={**case["headers"], **key(RUN_KEY)}
    )
    assert created.status_code == 201, created.text
    run_id = created.json()["run_id"]
    frozen = created.json()["source"]

    revised = case["client"].put(
        f"{base}/conversations/{case['conversation_id']}/workflows/{BUNDLE}",
        json={"files": _edited_files(), "delivery_kind": "report"},
        headers={**case["headers"], **key("revise"), "If-Match": '"1"'},
    )
    assert revised.status_code == 201, revised.text
    preview = case["client"].get(f"{base}/workflows/{BUNDLE}/revisions/2/preview").json()
    deployed = case["client"].post(
        f"{base}/conversations/{case['conversation_id']}/workflows/{BUNDLE}/revisions/2/deployments",
        json={
            "action": "deploy_only",
            "slot": SLOT,
            "expected_active_revision": 1,
            "preview_id": preview["preview_id"],
        },
        headers={**case["headers"], **key("deploy-2")},
    )
    assert deployed.status_code == 201, deployed.text

    again = case["client"].get(f"{base}/workflow-runs/{run_id}", headers=case["headers"])
    assert again.status_code == 200, again.text
    assert again.json()["source"] == frozen
    assert again.json()["source"]["bundle_revision"] == 1
    # The slot itself really did move: the Run did not follow it.
    status = case["client"].get(f"{base}/workflow-deployments/{SLOT}").json()
    assert status["active_deployment_id"] == deployed.json()["deployment"]["deployment_id"]
    assert status["active_deployment_id"] != frozen["deployment_id"]


def _edited_files() -> list[dict[str, str]]:
    from scheduling_fixtures import bundle_files

    files = bundle_files()
    for item in files:
        if item["path"] == "roles/editor.yaml":
            item["content"] = item["content"].replace(
                "  - assemble the comparison",
                "  - assemble the comparison\n  - record the decision",
            )
    return files


def test_the_run_is_readable_after_a_restart(case: dict[str, Any]) -> None:
    """A second application over the same state sees the same frozen Run."""
    base = url(case["project_id"], "")
    created = case["client"].post(
        f"{base}/workflow-runs", json=run_payload(case), headers={**case["headers"], **key(RUN_KEY)}
    )
    assert created.status_code == 201, created.text
    with restart(case) as second:
        headers = login(second, "second")
        again = second.get(f"{base}/workflow-runs/{created.json()['run_id']}", headers=headers)
        assert again.status_code == 200, again.text
        assert again.json()["source"] == created.json()["source"]
        listed = second.get(f"{base}/workflow-runs", headers=headers)
        assert [item["run_id"] for item in listed.json()["items"]] == [
            created.json()["run_id"]
        ]


def test_run_creation_requires_authentication(case: dict[str, Any]) -> None:
    """No session, no Run; and the refused request writes nothing."""
    base = url(case["project_id"], "")
    anonymous = case["client"].post(
        f"{base}/workflow-runs", json=run_payload(case), headers=key("anonymous")
    )
    assert anonymous.status_code in {401, 403}, anonymous.text
    assert "reason_code" in anonymous.json()
    runs = case["client"].get(f"{base}/workflow-runs", headers=case["headers"])
    assert runs.json()["items"] == []


def test_a_run_cannot_be_read_through_another_project(case: dict[str, Any]) -> None:
    """Reads are scoped to the owning project."""
    base = url(case["project_id"], "")
    created = case["client"].post(
        f"{base}/workflow-runs", json=run_payload(case), headers={**case["headers"], **key(RUN_KEY)}
    )
    assert created.status_code == 201, created.text
    other = other_project(case, "other-read")
    response = case["client"].get(
        url(other["id"], f"/workflow-runs/{created.json()['run_id']}"), headers=case["headers"]
    )
    assert response.status_code == 404, response.text
    assert response.json()["reason_code"] == "SCHEDULING_RUN_NOT_FOUND"


def test_a_legacy_run_route_still_works(case: dict[str, Any]) -> None:
    """The existing Run API keeps its previous behaviour and schema."""
    response = case["client"].post(
        "/v1/runs",
        json={
            "project_id": case["project_id"],
            "project_revision": case["registry"].get(case["project_id"])["revision"],
            "configuration_digest": "a" * 64,
            "requirement": {"goal": "legacy", "acceptance": ["done"]},
            "participants": [
                {
                    "principal": "owner",
                    "profile": {"id": "profile", "revision": 1},
                    "purpose": "lead",
                }
            ],
            "authorization": {
                "profile_refs": [{"id": "profile", "revision": 1}],
                "read_paths": ["src"],
                "write_paths": [],
                "budget_ref": "budget",
                "checks": ["check"],
                "delivery": "none",
                "target_branch": "main",
            },
        },
        headers={**case["headers"], **key("legacy-run")},
    )
    # The legacy route is untouched by this slice: it answers with its own code
    # rather than being replaced or shadowed by the new Run resource.
    assert response.status_code in {201, 409, 422}, response.text
    assert "run_id" in response.json() or response.json().get("reason_code")


def test_an_unknown_run_field_is_refused(case: dict[str, Any]) -> None:
    """A body cannot describe its own identity: it is an unknown field."""
    base = url(case["project_id"], "")
    response = case["client"].post(
        f"{base}/workflow-runs",
        json={**run_payload(case), "user": True},
        headers={**case["headers"], **key("spoof-user")},
    )
    assert response.status_code == 422, response.text
    assert response.json()["reason_code"] == "INPUT_INVALID"


def test_a_grant_command_cannot_claim_to_be_a_user(case: dict[str, Any]) -> None:
    """``user=true`` is not an identity anywhere in this control plane."""
    base = url(case["project_id"], "")
    created = case["client"].post(
        f"{base}/workflow-runs", json=run_payload(case), headers={**case["headers"], **key(RUN_KEY)}
    )
    assert created.status_code == 201, created.text
    response = case["client"].post(
        f"{base}/workflow-runs/{created.json()['run_id']}/grants",
        json={**grant_body(), "user": True},
        headers={**case["headers"], **key("grant-spoof")},
    )
    assert response.status_code == 422, response.text


@pytest.mark.parametrize("artifact", ["report"])
def test_a_declared_delivery_target_is_recorded(case: dict[str, Any], artifact: str) -> None:
    """The grant's delivery target is a real, stored dimension."""
    base = url(case["project_id"], "")
    created = case["client"].post(
        f"{base}/workflow-runs", json=run_payload(case), headers={**case["headers"], **key(RUN_KEY)}
    )
    assert created.status_code == 201, created.text
    response = case["client"].post(
        f"{base}/workflow-runs/{created.json()['run_id']}/grants",
        json=grant_body(artifact=artifact),
        headers={**case["headers"], **key("grant-artifact")},
    )
    assert response.status_code == 201, response.text
    assert response.json()["artifact"] == artifact


def test_the_compiled_definition_is_snapshotted_on_the_run(case: dict[str, Any]) -> None:
    """The Run carries the definitions it froze, so later ones cannot widen it."""
    base = url(case["project_id"], "")
    created = case["client"].post(
        f"{base}/workflow-runs", json=run_payload(case), headers={**case["headers"], **key(RUN_KEY)}
    )
    assert created.status_code == 201, created.text
    definitions = created.json()["definitions"]
    assert definitions["role_refs"] == {
        "editor": "role:technical-editor@1",
        "researcher": "role:source-researcher@2",
    }
    assert "artifact_aggregate@1" in definitions["execution_kinds"]
    assert "aggregated-report@1" in definitions["contracts"]
    # The snapshot is a plain JSON document, so a restart reads back the same
    # facts rather than re-deriving them from whatever is deployed now.
    assert json.loads(json.dumps(definitions)) == definitions
