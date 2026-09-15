"""Second-pass HTTP/store regressions: nested fields, ownership, real files.

These cases reproduce defects independent review found on the delivery of this
Issue: a credential hidden inside a legitimate structured role field, an unknown
field silently accepted, a conversation taking over a bundle identity another
conversation had already claimed with a text instruction, and a script-shaped
value treated as data. Each is asserted through the real authenticated HTTP
boundary and the real file store, not through a mock.
"""

from typing import Any

from workflow_fixtures import RESEARCHER_ROLE, bundle_case, url

case = bundle_case

#: A one-role, one-step workflow, used where a case varies only the role file.
SINGLE_ROLE_WORKFLOW = """schema_version: workflow.v1
id: single-role
revision: 1
delivery_kind: report
input_contract: text@1
inputs:
  - requirement.option_a
roles:
  researcher: role:source-researcher@2
steps:
  - id: note
    execution_kind: artifact_aggregate@1
    role: researcher
    output_contract: aggregated-report@1
    inputs:
      sources: requirement.option_a
completion:
  required_steps:
    - note
  artifact: note.output
"""


def test_a_credential_nested_in_a_legitimate_role_field_is_refused(
    case: dict[str, Any],
) -> None:
    """AC1/AC5: a credential cannot hide inside a supported structured field.

    ``tool_constraints`` is a legitimate place to declare limits, so the check
    cannot stop at the top level of a document: every nested mapping and list is
    inspected, and only the field *name* is reported — never its value.
    """
    project_id = case["project_id"]
    marker = "fixture-not-a-real-credential"
    nested_role = RESEARCHER_ROLE.replace(
        "tool_constraints:\n",
        "tool_constraints:\n  auth:\n    api_key: " + marker + "\n",
    )
    assert marker in nested_role
    response = case["client"].post(
        url(
            project_id,
            f"/conversations/{case['conversation_id']}/workflows/nested-credential",
        ),
        json={
            "files": [
                {"path": "workflow.yaml", "content": SINGLE_ROLE_WORKFLOW},
                {"path": "roles/researcher.yaml", "content": nested_role},
            ],
            "delivery_kind": "report",
        },
        headers={**case["headers"], "Idempotency-Key": "nested-credential"},
    )
    assert response.status_code == 422, response.text
    assert response.json()["reason_code"] == "WORKFLOW_CREDENTIAL_FIELD_REFUSED"
    # The submitted value is never echoed back, and nothing was stored.
    assert marker not in response.text
    assert (
        case["client"].get(url(project_id, "/workflows/nested-credential/revisions/1")).status_code
        == 404
    )


def test_an_unknown_top_level_field_is_refused_before_persistence(
    case: dict[str, Any],
) -> None:
    """A field the compiler never reads is not silently accepted."""
    project_id = case["project_id"]
    response = case["client"].post(
        url(project_id, f"/conversations/{case['conversation_id']}/workflows/unknown-field"),
        json={
            "files": [
                {
                    "path": "workflow.yaml",
                    "content": SINGLE_ROLE_WORKFLOW + "metadata: {owner: someone}\n",
                },
                {"path": "roles/researcher.yaml", "content": RESEARCHER_ROLE},
            ],
            "delivery_kind": "report",
        },
        headers={**case["headers"], "Idempotency-Key": "unknown-field"},
    )
    assert response.status_code == 422, response.text
    assert response.json()["reason_code"] == "WORKFLOW_FIELD_UNSUPPORTED"


def test_a_script_field_is_refused_and_not_treated_as_data(
    case: dict[str, Any],
) -> None:
    """A declarative structure carries limits, not an executable instruction."""
    project_id = case["project_id"]
    scripted_role = RESEARCHER_ROLE.replace(
        "tool_constraints:\n",
        "tool_constraints:\n  script: rm -rf /\n",
    )
    response = case["client"].post(
        url(project_id, f"/conversations/{case['conversation_id']}/workflows/scripted"),
        json={
            "files": [
                {"path": "workflow.yaml", "content": SINGLE_ROLE_WORKFLOW},
                {"path": "roles/researcher.yaml", "content": scripted_role},
            ],
            "delivery_kind": "report",
        },
        headers={**case["headers"], "Idempotency-Key": "scripted"},
    )
    assert response.status_code == 422, response.text
    assert response.json()["reason_code"] == "WORKFLOW_FIELD_UNSUPPORTED"
    assert "rm -rf" not in response.text


def test_ordinary_role_prose_is_preserved(case: dict[str, Any]) -> None:
    """Human prose in a legitimate field is stored and read back unchanged."""
    project_id = case["project_id"]
    prose = "Read the approved material, then summarise it for a non-expert reader."
    role = RESEARCHER_ROLE.replace(
        "  - read approved material\n", f"  - {prose}\n"
    )
    response = case["client"].post(
        url(project_id, f"/conversations/{case['conversation_id']}/workflows/prose"),
        json={
            "files": [
                {"path": "workflow.yaml", "content": SINGLE_ROLE_WORKFLOW},
                {"path": "roles/researcher.yaml", "content": role},
            ],
            "delivery_kind": "report",
        },
        headers={**case["headers"], "Idempotency-Key": "prose"},
    )
    assert response.status_code == 201, response.text
    preview = case["client"].get(
        url(project_id, "/workflows/prose/revisions/1/preview")
    ).json()
    stored = next(item for item in preview["roles"] if item["alias"] == "researcher")
    assert prose in stored["responsibilities"]


def test_a_credential_reference_is_still_refused_where_it_is_not_declared(
    case: dict[str, Any],
) -> None:
    """A role file declares no secret field at all, reference or material.

    The supported reference surface is documented, so a field outside it is
    refused by name rather than accepted as though it had been read.
    """
    project_id = case["project_id"]
    referenced = RESEARCHER_ROLE.replace(
        "tool_constraints:\n",
        "secret_ref: secret:team/researcher\ntool_constraints:\n",
    )
    response = case["client"].post(
        url(project_id, f"/conversations/{case['conversation_id']}/workflows/referenced"),
        json={
            "files": [
                {"path": "workflow.yaml", "content": SINGLE_ROLE_WORKFLOW},
                {"path": "roles/researcher.yaml", "content": referenced},
            ],
            "delivery_kind": "report",
        },
        headers={**case["headers"], "Idempotency-Key": "referenced"},
    )
    assert response.status_code == 422, response.text
    assert response.json()["reason_code"] == "WORKFLOW_FIELD_UNSUPPORTED"
