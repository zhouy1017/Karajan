"""Admission commands expose identities, never caller-provided execution authority."""

import pytest
from test_v2_approval_workbench import approval, run_client, v2_plan

__all__ = ["run_client", "v2_plan"]


def test_unqualified_run_persists_a_blocker_and_can_cancel_without_effects(v2_plan):
    client, headers, _, run, _, plan = v2_plan
    assert (
        client.post(
            f"/v1/runs/{run['id']}/plan-approval",
            json=approval(plan),
            headers={**headers, "Idempotency-Key": "approve"},
        ).status_code
        == 200
    )
    url = f"/v1/runs/{run['id']}/tasks/feature/admissions"
    response = client.post(url, json={}, headers=headers)
    assert response.status_code == 201
    operation = response.json()
    assert operation["state"] == "blocked"
    assert operation["request"] is None
    assert operation["activation_allowed"] is False
    assert client.post(url, json={}, headers=headers).json() == operation
    item = f"/v1/runs/{run['id']}/task-admissions/{operation['id']}"
    assert client.get(item).json() == operation
    assert client.post(item + "/advance", json={}, headers=headers).json() == operation
    cancelled = client.post(item + "/cancel", json={}, headers=headers)
    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "cancelled"


@pytest.mark.parametrize("field", ["profile_ref", "request", "authorization", "capacity", "stage"])
def test_admission_rejects_uploaded_authority(v2_plan, field):
    client, headers, _, run, _, _ = v2_plan
    url = f"/v1/runs/{run['id']}/tasks/feature/admissions"
    assert client.post(url, json={field: {}}, headers=headers).status_code == 422


def test_admission_requires_session_origin_csrf_and_enqueue_key(v2_plan):
    client, headers, _, run, _, _ = v2_plan
    url = f"/v1/runs/{run['id']}/tasks/feature/admissions"
    assert client.post(url, json={}, headers={"Origin": headers["Origin"]}).status_code == 403
    assert (
        client.post(
            url, json={}, headers={k: v for k, v in headers.items() if k != "Idempotency-Key"}
        ).status_code
        == 400
    )
    client.cookies.clear()
    assert client.post(url, json={}, headers=headers).status_code == 401
