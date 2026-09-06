"""Approved routing HTTP commands cannot accept client-authored authority or facts."""

import pytest
from test_v2_approval_workbench import approval, run_client, v2_plan

__all__ = ["run_client", "v2_plan"]


def test_authenticated_approved_run_reaches_persistent_routing_assessment(v2_plan: tuple) -> None:
    client, headers, _, run, _, plan = v2_plan
    assert (
        client.post(
            f"/v1/runs/{run['id']}/plan-approval",
            json=approval(plan),
            headers={**headers, "Idempotency-Key": "approve"},
        ).status_code
        == 200
    )
    url = f"/v1/runs/{run['id']}/tasks/feature/routing-assessments"
    key = {**headers, "Idempotency-Key": "assess"}
    response = client.post(url, json={}, headers=key)
    assert response.status_code == 201
    receipt = response.json()
    assert receipt["state"] == "blocked"
    assert receipt["route"]["rule_id"] == "bounded-worker"
    assert receipt["activation_allowed"] is False
    assert client.post(url, json={}, headers=key).json() == receipt
    stored = client.get(f"/v1/runs/{run['id']}/routing-assessments/{receipt['id']}")
    assert stored.status_code == 200
    assert stored.json() == receipt


@pytest.mark.parametrize(
    "field", ["profile_ref", "authorization", "authors", "capacity", "policy", "stage"]
)
def test_assessment_rejects_untrusted_source_substitution(v2_plan: tuple, field: str) -> None:
    client, headers, _, run, _, _ = v2_plan
    url = f"/v1/runs/{run['id']}/tasks/feature/routing-assessments"
    response = client.post(url, json={field: "not-authority"}, headers=headers)
    assert response.status_code == 422


def test_assessment_keeps_session_csrf_and_idempotency_boundaries(v2_plan: tuple) -> None:
    client, headers, _, run, _, _ = v2_plan
    url = f"/v1/runs/{run['id']}/tasks/feature/routing-assessments"
    assert client.post(url, json={}, headers={"Origin": headers["Origin"]}).status_code == 403
    assert (
        client.post(
            url, json={}, headers={k: v for k, v in headers.items() if k != "Idempotency-Key"}
        ).status_code
        == 400
    )
    client.cookies.clear()
    assert client.post(url, json={}, headers=headers).status_code == 401
