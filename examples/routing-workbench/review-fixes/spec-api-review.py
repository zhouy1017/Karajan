"""Independent public HTTP checks; only temporary fixture state is created.

Run from the repository with PYTHONPATH=backend;tests/web and pytest this file.
"""

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from karajan.routing import evaluate_route
from test_rulebook_http import ORIGIN, login
from test_rulebook_http import publication_case as publication_case

INPUT = Path(__file__).with_name("spec-api-inputs.json")


def snapshots() -> dict[str, Any]:
    return json.loads(INPUT.read_bytes())["snapshots"]


def post(client: TestClient, case: dict[str, Any], body: bytes, headers: dict[str, str]):
    return client.post(
        f"/v1/projects/{case['project']['id']}/rulebook/simulate",
        content=body,
        headers={**headers, "Content-Type": "application/json"},
    )


def test_same_snapshot_http_is_the_public_solver_and_byte_deterministic(
    publication_case: dict[str, Any], record_testsuite_property: Any
) -> None:
    original = snapshots()
    frozen = copy.deepcopy(original)
    original["policy"]["rulebook"]["id"] = "supplied-independent-rulebook"
    original["policy"]["rulebook"]["revision"] = 31
    body = json.dumps(original, ensure_ascii=False).encode()
    expected = evaluate_route(original["task"], original["policy"], original["capacity"])
    assert frozen["task"] == original["task"]
    assert original == json.loads(body)
    with TestClient(publication_case["app"], base_url=ORIGIN) as client:
        headers = login(client)
        headers.pop("If-Match")
        headers.pop("Idempotency-Key")
        first = post(client, publication_case, body, headers)
        second = post(client, publication_case, body, headers)
        assert first.status_code == second.status_code == 200
        assert first.content == second.content
        report = first.json()
        assert report["result"] == expected
        assert report["result"]["rulebook_id"] == "supplied-independent-rulebook"
        assert report["result"]["rulebook_revision"] == 31
        assert (
            report["result"]["snapshots"]["task"]["authorization"]
            == original["task"]["authorization"]
        )
        assert report["activation_allowed"] is False
        assert report["result"]["live_qualification"] == "not_run"
        record_testsuite_property("spec_repeat_input_sha256", hashlib.sha256(body).hexdigest())
        record_testsuite_property(
            "spec_repeat_response_sha256", hashlib.sha256(first.content).hexdigest()
        )


@pytest.mark.parametrize("variant", ["new-group", "empty-ceiling", "new-quality-stage"])
def test_rule_replacement_does_not_expand_raw_approval(
    publication_case: dict[str, Any], variant: str, record_testsuite_property: Any
) -> None:
    original = snapshots()
    task, rulebook = original["task"], original["policy"]["rulebook"]
    rulebook["revision"] += 1
    rule = next(row for row in rulebook["rules"] if row["id"] == "bounded-worker")
    if variant == "new-group":
        rulebook["profile_groups"]["unapproved-new-group"] = copy.deepcopy(
            rulebook["profile_groups"]["standard_qualified"]
        )
        rule["eligible_groups"] = ["unapproved-new-group"]
        reason = "GROUP_PROFILE_NOT_APPROVED"
    elif variant == "empty-ceiling":
        task["authorization"]["ceiling_profile_refs"] = []
        reason = "RUN_CEILING_PROFILE_DENIED"
    else:
        rulebook["profile_groups"]["unapproved-new-stage"] = copy.deepcopy(
            rulebook["profile_groups"]["critical_qualified"]
        )
        rule["quality_escalation_groups"].append("unapproved-new-stage")
        task.update(
            stage="quality",
            quality_stage_index=1,
            quality_repair_rounds_used=1,
            failure_reason="QUALITY_FAILED",
            previous_profile={"id": "fixture-profile", "revision": 1},
        )
        reason = "QUALITY_STAGE_NOT_AUTHORIZED"
    authority = copy.deepcopy(task["authorization"])
    body = json.dumps(original).encode()
    with TestClient(publication_case["app"], base_url=ORIGIN) as client:
        response = post(client, publication_case, body, login(client))
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["selected_profile"] is None
    assert result["snapshots"]["task"]["authorization"] == authority
    reasons = result["reason_codes"] + [
        code for row in result["candidates"] for code in row["reason_codes"]
    ]
    assert reason in reasons
    record_testsuite_property(
        "spec_authority_" + variant,
        json.dumps({"input": original, "reason_codes": reasons, "selected_profile": None}),
    )


@pytest.mark.parametrize("variant", ["root", "nested-escaped", "escaped-reversed"])
def test_duplicate_keys_cannot_be_normalized_into_an_accepted_snapshot(
    publication_case: dict[str, Any], variant: str
) -> None:
    raw = json.dumps(snapshots(), ensure_ascii=False)
    if variant == "root":
        raw = '{"task": {},' + raw[1:]
    elif variant == "nested-escaped":
        raw = raw.replace('"complexity": "T2"', '"complexity": "T1", "complexit\\u0079": "T2"')
    else:
        raw = raw.replace('"complexity": "T2"', '"complexit\\u0079": "T1", "complexity": "T2"')
    with TestClient(publication_case["app"], base_url=ORIGIN) as client:
        response = post(client, publication_case, raw.encode(), login(client))
    assert response.status_code == 422
    assert response.json()["reason_code"] == "ROUTING_INPUT_INVALID"


@pytest.mark.parametrize("extra", [0, 1])
def test_body_limit_counts_utf8_bytes_at_exact_boundary(
    publication_case: dict[str, Any], extra: int
) -> None:
    source = snapshots()
    source["policy"]["rulebook"]["description"] = "多字节边界核对"
    body = json.dumps(source, ensure_ascii=False).encode()
    body += b" " * (65536 - len(body) + extra)
    assert len(body) == 65536 + extra
    with TestClient(publication_case["app"], base_url=ORIGIN) as client:
        response = post(client, publication_case, body, login(client))
    assert response.status_code == (413 if extra else 200)
    if extra:
        assert response.json()["reason_code"] == "REQUEST_TOO_LARGE"


def test_revoked_session_cannot_fetch_example_or_simulate(publication_case: dict[str, Any]) -> None:
    body = json.dumps(snapshots()).encode()
    with TestClient(publication_case["app"], base_url=ORIGIN) as client:
        headers = login(client)
        cookie = client.cookies.get("karajan_session")
        assert client.post("/v1/session/logout", headers=headers).status_code == 204
        client.cookies.set("karajan_session", cookie)
        example = client.get(
            f"/v1/projects/{publication_case['project']['id']}/rulebook/simulation-example"
        )
        assert example.status_code == 401
        assert post(client, publication_case, body, headers).status_code == 401
