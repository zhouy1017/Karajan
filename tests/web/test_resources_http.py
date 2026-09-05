"""Owner sessions read shared resources and update a policy with a revision precondition."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from karajan.capacity import CapacityStore
from karajan.web import create_app

ORIGIN = "http://127.0.0.1:8765"


def seed(directory: Path) -> CapacityStore:
    directory.mkdir()
    case = json.loads(
        (Path(__file__).resolve().parents[2] / "examples/capacity/shared-pools.json").read_bytes()
    )
    store = CapacityStore(directory / "capacity.sqlite", clock=lambda: 1000.0)
    for i, pool in enumerate(case["pools"]):
        store.register_pool(pool, command_key=f"pool-{i}")
    for i, profile in enumerate(case["profiles"]):
        store.register_profile(profile, command_key=f"profile-{i}")
    for i, observation in enumerate(case["observations"]):
        store.observe(observation, command_key=f"observation-{i}")
    store.activate_policy(case["policy"], expected_revision=0, command_key="policy")
    return store


def headers(client: TestClient) -> dict[str, str]:
    login = client.post(
        "/v1/session/bootstrap", json={"token": "bootstrap"}, headers={"Origin": ORIGIN}
    )
    assert login.status_code == 200
    return {
        "Origin": ORIGIN,
        "X-CSRF-Token": login.json()["csrf_token"],
        "If-Match": '"1"',
        "Idempotency-Key": "policy-change",
    }


def test_owner_resource_snapshot_and_revision_update_survive_restart(tmp_path: Path) -> None:
    store = seed(tmp_path / "state")
    before = store.snapshot()
    app = create_app(tmp_path / "state", origin=ORIGIN, bootstrap_token="bootstrap")
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.get("/v1/resources").status_code == 401
        authorized = headers(client)
        response = client.get("/v1/resources")
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        account = response.json()["accounts"][0]
        assert account["policy_revision"] == 1
        assert store.snapshot() == before
        policy = account["policy"]
        policy["lead_reserve"]["weekly"] = "3"
        result = client.post(
            "/v1/resources/policy?account_id=shared-account",
            json={"policy": policy},
            headers=authorized,
        )
        assert result.status_code == 200
        assert result.json()["revision"] == 2
        duplicate = client.post(
            "/v1/resources/policy?account_id=shared-account",
            json={"policy": policy},
            headers=authorized,
        )
        assert duplicate.json() == result.json()
        stale = client.post(
            "/v1/resources/policy?account_id=shared-account",
            json={"policy": policy},
            headers=dict(authorized, **{"Idempotency-Key": "stale"}),
        )
        assert stale.status_code == 409
        assert stale.json()["reason_code"] == "CAPACITY_POLICY_STALE"
    reopened = create_app(tmp_path / "state", origin=ORIGIN, bootstrap_token="bootstrap-2")
    with TestClient(reopened, base_url=ORIGIN) as client:
        client.post(
            "/v1/session/bootstrap", json={"token": "bootstrap-2"}, headers={"Origin": ORIGIN}
        )
        account = client.get("/v1/resources").json()["accounts"][0]
        assert account["policy_revision"] == 2
        assert next(p for p in account["pools"] if p["id"] == "weekly")["lead_reserve"] == "3"
    assert store.snapshot()["reservations"] == []


@pytest.mark.parametrize("case", ["csrf", "origin", "revision", "account", "extra"])
def test_invalid_or_unauthorized_policy_writes_leave_current_policy_unchanged(
    tmp_path: Path,
    case: str,
) -> None:
    store = seed(tmp_path / "state")
    before = store.snapshot()
    app = create_app(tmp_path / "state", origin=ORIGIN, bootstrap_token="bootstrap")
    with TestClient(app, base_url=ORIGIN) as client:
        authorized = headers(client)
        policy = before["policies"][-1]["policy"]
        if case == "csrf":
            authorized.pop("X-CSRF-Token")
        elif case == "origin":
            authorized["Origin"] = "https://attacker.test"
        elif case == "revision":
            authorized.pop("If-Match")
        elif case == "account":
            policy = dict(policy, account_id="other")
        else:
            policy = dict(policy, api_key="SECRET-CANARY")
        result = client.post(
            "/v1/resources/policy?account_id=shared-account",
            json={"policy": policy},
            headers=authorized,
        )
        assert result.status_code in (403, 422, 428)
        assert "SECRET-CANARY" not in result.text
    assert store.snapshot() == before


def test_query_identity_preserves_dot_segments_and_policy_edits_are_limited(tmp_path: Path) -> None:
    directory = tmp_path / "state"
    store = seed(directory)
    policy = dict(
        store.snapshot()["policies"][-1]["policy"],
        account_id="..",
        lead_reserve={},
        safety_margin={},
    )
    store.activate_policy(policy, expected_revision=0, command_key="dot-policy")
    app = create_app(directory, origin=ORIGIN, bootstrap_token="bootstrap")
    with TestClient(app, base_url=ORIGIN) as client:
        authorized = headers(client)
        updated = dict(policy, lead_reserved_slots=1)
        response = client.post(
            "/v1/resources/policy",
            params={"account_id": ".."},
            json={"policy": updated},
            headers=authorized,
        )
        assert response.status_code == 200
        assert response.json()["revision"] == 2
        changed_scope = dict(updated, max_active_attempts=100)
        broad = client.post(
            "/v1/resources/policy",
            params={"account_id": ".."},
            json={"policy": changed_scope},
            headers=dict(authorized, **{"If-Match": '"2"', "Idempotency-Key": "wider"}),
        )
        assert broad.status_code == 422
        assert broad.json()["reason_code"] == "PROTECTION_UPDATE_ONLY"
        # Replaying the accepted command still returns its receipt after an internal policy update.
        store.activate_policy(
            dict(updated, max_active_attempts=5),
            expected_revision=2,
            command_key="internal-policy-change",
        )
        replay = client.post(
            "/v1/resources/policy",
            params={"account_id": ".."},
            json={"policy": updated},
            headers=authorized,
        )
        assert replay.json() == response.json()


def test_known_limit_rejects_excess_protection_without_any_policy_write(tmp_path: Path) -> None:
    store = seed(tmp_path / "state")
    before = store.snapshot()
    app = create_app(tmp_path / "state", origin=ORIGIN, bootstrap_token="bootstrap")
    with TestClient(app, base_url=ORIGIN) as client:
        authorized = headers(client)
        account = client.get("/v1/resources").json()["accounts"][0]
        weekly = next(pool for pool in account["pools"] if pool["id"] == "weekly")
        assert weekly["reported_limit"] == "100.000000"
        policy = account["policy"]
        policy["lead_reserve"]["weekly"] = "100.000001"
        response = client.post(
            "/v1/resources/policy?account_id=shared-account",
            json={"policy": policy},
            headers=authorized,
        )
        assert response.status_code == 422
        assert response.json()["reason_code"] == "PROTECTION_EXCEEDS_POOL_LIMIT"
        assert store.snapshot() == before
        policy["lead_reserve"]["weekly"] = "100.000000"
        assert (
            client.post(
                "/v1/resources/policy?account_id=shared-account",
                json={"policy": policy},
                headers=authorized,
            ).status_code
            == 200
        )
