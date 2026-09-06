"""In-process ASGI boundary, real production wiring, synthetic stored Run only."""

from admission_spec_fixture import prepare
from fastapi.testclient import TestClient
from karajan.web.app import create_app


def test_real_app_wiring_blocks_unqualified_sources_and_rejects_uploaded_authority(tmp_path):
    case = prepare(tmp_path, qualification_double=False)
    origin = "http://127.0.0.1:8123"
    app = create_app(
        tmp_path,
        origin=origin,
        bootstrap_token="synthetic-spec-bootstrap",
        allowed_roots=[tmp_path],
    )
    run = case["run"]["id"]
    with TestClient(app, base_url=origin) as client:
        login = client.post(
            "/v1/session/bootstrap",
            json={"token": "synthetic-spec-bootstrap"},
            headers={"Origin": origin},
        )
        assert login.status_code == 200
        headers = {
            "Origin": origin,
            "X-CSRF-Token": login.json()["csrf_token"],
            "Idempotency-Key": "independent-http-enqueue",
        }
        url = f"/v1/runs/{run}/tasks/implement/admissions"
        assert client.post(url, json={"profile_ref": {}}, headers=headers).status_code == 422
        response = client.post(url, json={}, headers=headers)
        assert response.status_code == 201
        blocked = response.json()
        assert blocked["state"] == "blocked"
        assert blocked["request"] is None
        assert blocked["activation_allowed"] is False and blocked["dispatch_enabled"] is False
        item = f"/v1/runs/{run}/task-admissions/{blocked['id']}"
        assert client.get(item).json() == blocked
        for action in ("advance", "cancel"):
            assert (
                client.post(
                    item + "/" + action, json={"authorization": True}, headers=headers
                ).status_code
                == 422
            )
            assert (
                client.post(item + "/" + action, json={}, headers={"Origin": origin}).status_code
                == 403
            )
        assert client.post(item + "/advance", json={}, headers=headers).json() == blocked
        assert (
            client.post(item + "/cancel", json={}, headers=headers).json()["state"] == "cancelled"
        )
        client.cookies.clear()
        assert client.get(item).status_code == 401
    assert case["capacity"].snapshot()["reservations"] == []
