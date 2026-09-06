"""Authenticated routing simulation over explicit snapshots, without execution."""

import copy
import hashlib
import json
import sqlite3
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from karajan.capacity import CapacityStore
from karajan.runs import RunPlanner
from test_rulebook_http import ORIGIN, login
from test_rulebook_http import publication_case as publication_case


def test_fixture_example_and_repeated_simulation_are_deterministic_without_apply_headers(
    publication_case: dict[str, Any],
) -> None:
    case = publication_case
    base = f"/v1/projects/{case['project']['id']}/rulebook"
    with TestClient(case["app"], base_url=ORIGIN) as client:
        headers = login(client)
        headers.pop("If-Match")
        headers.pop("Idempotency-Key")
        example = client.get(base + "/simulation-example")
        assert example.status_code == 200
        snapshots = example.json()
        assert set(snapshots) == {"task", "policy", "capacity"}
        first = client.post(base + "/simulate", json=snapshots, headers=headers)
        second = client.post(base + "/simulate", json=snapshots, headers=headers)
        assert first.status_code == 200
        assert first.content == second.content
        result = first.json()
        assert result["schema_version"] == "karajan.rulebook-simulation.v1"
        assert result["scope"] == "explicit_simulation"
        assert result["activation_allowed"] is False
        assert result["model_calls"] == 0
        assert result["result"]["selected_profile"] == {"id": "fixture-profile", "revision": 1}
        assert result["result"]["rule_id"] == "bounded-worker"
        assert result["result"]["live_qualification"] == "not_run"
        assert result["result"]["scope"] == "simulation_only"


@pytest.mark.parametrize("nested", [False, True])
def test_repeated_json_keys_are_rejected_like_the_public_cli(
    publication_case: dict[str, Any],
    nested: bool,
) -> None:
    case = publication_case
    payload = Path("examples/routing/fixed-input.json").read_text(encoding="utf-8")
    if nested:
        payload = payload.replace('"complexity": "T2"', '"complexity": "T1", "complexity": "T2"')
    else:
        payload = '{"task": {},' + payload.lstrip()[1:]
    with TestClient(case["app"], base_url=ORIGIN) as client:
        headers = login(client)
        response = client.post(
            f"/v1/projects/{case['project']['id']}/rulebook/simulate",
            content=payload.encode(),
            headers={**headers, "Content-Type": "application/json"},
        )
        assert response.status_code == 422
        assert response.json()["reason_code"] == "ROUTING_INPUT_INVALID"


@pytest.mark.parametrize(
    "case_name,reason",
    [
        ("ambiguous", "RULE_AMBIGUOUS"),
        ("not-ready", "TASK_NOT_READY"),
        ("missing", "NO_RULE"),
    ],
)
def test_full_snapshot_controls_normal_routing_diagnostics(
    publication_case: dict[str, Any],
    case_name: str,
    reason: str,
) -> None:
    case = publication_case
    snapshots = json.loads(Path("examples/routing/fixed-input.json").read_bytes())
    rules = snapshots["policy"]["rulebook"]["rules"]
    matching = next(rule for rule in rules if rule["id"] == "bounded-worker")
    if case_name == "ambiguous":
        duplicate = copy.deepcopy(matching)
        duplicate["id"] = "equally-qualified-rule"
        rules.append(duplicate)
    elif case_name == "not-ready":
        snapshots["task"]["readiness"] = "T0"
    else:
        rules.remove(matching)
    with TestClient(case["app"], base_url=ORIGIN) as client:
        response = client.post(
            f"/v1/projects/{case['project']['id']}/rulebook/simulate",
            json=snapshots,
            headers=login(client),
        )
        assert response.status_code == 200
        assert response.json()["result"]["reason_codes"] == [reason]
        assert response.json()["result"]["selected_profile"] is None


@pytest.mark.parametrize(
    "case_name,reason",
    [
        ("extra", "ROUTING_INPUT_INVALID"),
        ("missing", "ROUTING_INPUT_INVALID"),
        ("not-object", "ROUTING_INPUT_INVALID"),
        ("invalid-task", "TASK_SNAPSHOT_INVALID"),
        ("invalid-budget", "POLICY_SNAPSHOT_INVALID"),
        ("secret", "CREDENTIAL_VALUE_FORBIDDEN"),
    ],
)
def test_invalid_input_has_safe_structured_errors(
    publication_case: dict[str, Any],
    case_name: str,
    reason: str,
) -> None:
    case = publication_case
    snapshots: Any = json.loads(Path("examples/routing/fixed-input.json").read_bytes())
    canary = "FAKE-SIMULATION-SECRET-CANARY"
    if case_name == "extra":
        snapshots["activate"] = True
    elif case_name == "missing":
        del snapshots["capacity"]
    elif case_name == "not-object":
        snapshots = [snapshots]
    elif case_name == "invalid-task":
        snapshots["task"]["complexity"] = canary
    elif case_name == "invalid-budget":
        snapshots["policy"]["resources"]["budgets"][0]["currency_limits"]["USD"] = "NaN"
    else:
        snapshots["policy"]["resources"]["profiles"][0]["profile"]["binding"]["native_settings"][
            "api_key"
        ] = canary
    with TestClient(case["app"], base_url=ORIGIN) as client:
        response = client.post(
            f"/v1/projects/{case['project']['id']}/rulebook/simulate",
            json=snapshots,
            headers=login(client),
        )
        assert response.status_code == 422
        assert response.json()["reason_code"] == reason
        assert response.json()["activation_allowed"] is False
        assert response.json()["model_calls"] == 0
        assert isinstance(response.json()["issues"], list)
        assert canary not in response.text


@pytest.mark.parametrize(
    "invalid,status",
    [
        ("session", 401),
        ("csrf", 403),
        ("origin", 403),
        ("host", 403),
        ("project", 404),
        ("body-limit", 413),
        ("malformed-json", 422),
        ("unicode", 422),
    ],
)
def test_simulation_uses_existing_authenticated_and_bounded_http_boundary(
    publication_case: dict[str, Any],
    invalid: str,
    status: int,
) -> None:
    case = publication_case
    payload = Path("examples/routing/fixed-input.json").read_bytes()
    base = f"/v1/projects/{case['project']['id']}/rulebook"
    with TestClient(case["app"], base_url=ORIGIN) as client:
        assert client.get(base + "/simulation-example").status_code == 401
        headers = login(client)
        if invalid == "session":
            client.cookies.clear()
        elif invalid in {"csrf", "origin"}:
            headers.pop("X-CSRF-Token" if invalid == "csrf" else "Origin")
        elif invalid == "host":
            headers["Host"] = "external.invalid"
        elif invalid == "project":
            base = "/v1/projects/not-a-project/rulebook"
            assert client.get(base + "/simulation-example").status_code == 404
        elif invalid == "body-limit":
            payload = b" " * 65537
        elif invalid == "malformed-json":
            payload = b"{"
        else:
            payload = b'{"bad": "\\ud800"}'
        response = client.post(
            base + "/simulate",
            content=payload,
            headers={**headers, "Content-Type": "application/json"},
        )
        assert response.status_code == status


def logical_tables(directory: Path) -> dict[str, Any]:
    result = {}
    for name in ("projects", "runs", "capacity"):
        with sqlite3.connect(directory / f"{name}.sqlite") as db:
            result[name] = {
                row[0]: sorted(
                    db.execute('SELECT * FROM "' + row[0] + '"').fetchall(),
                    key=lambda values: json.dumps(values, sort_keys=True),
                )
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
    return result


def test_explicit_snapshot_simulation_preserves_actual_control_tables_and_calls_no_model(
    publication_case: dict[str, Any],
    record_testsuite_property: Callable[[str, Any], None],
) -> None:
    case = publication_case
    received = []

    class ModelSentinel(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            received.append(self.path)
            self.send_response(503)
            self.end_headers()

        do_GET = do_POST

        def log_message(self, format: str, *args: Any) -> None:
            pass

    listener = ThreadingHTTPServer(("127.0.0.1", 0), ModelSentinel)
    worker = threading.Thread(
        target=lambda: listener.serve_forever(poll_interval=0.01), daemon=True
    )
    worker.start()
    snapshots = json.loads(Path("examples/routing/fixed-input.json").read_bytes())
    reference = {"id": "fixture-profile", "revision": 1}
    project = case["project"]
    run = RunPlanner(case["directory"] / "runs.sqlite", case["registry"]).create(
        {
            "project_id": project["id"],
            "project_revision": project["revision"],
            "configuration_digest": project["configuration"]["digest"],
            "requirement": {"goal": "Preserve this existing Run", "acceptance": ["No mutation"]},
            "participants": [{"principal": "lead", "profile": reference, "purpose": "lead"}],
            "authorization": {
                "profile_refs": [reference],
                "read_paths": ["src"],
                "write_paths": ["src"],
                "budget_ref": "run",
                "checks": ["tests", "independent_review"],
                "delivery": "pull_request",
                "target_branch": "main",
            },
        },
        command_key="existing-run",
        principal="owner",
    )
    capacity = CapacityStore(case["directory"] / "capacity.sqlite", clock=lambda: 1000.0)
    for pool in snapshots["capacity"]["pools"]:
        capacity.register_pool(
            {key: pool[key] for key in ("id", "account_id", "kind", "unit", "window_kind")},
            command_key="pool-" + pool["id"],
        )
        capacity.observe(
            {
                "pool_id": pool["id"],
                "window_id": pool["window_id"],
                "observed_at": pool["observed_at"],
                "reset_at": pool["reset_at"],
                "source": "fixture",
                "source_ref": "fixture:existing-quota",
                "metric": "remaining",
                "amount": pool["reported_remaining"],
                "limit": pool["reported_limit"],
                "covered_usage_ids": [],
            },
            command_key="observe-" + pool["id"],
        )
    capacity.register_profile(
        {
            **reference,
            "account_id": "fixture-account",
            "pool_ids": ["service-fixture"],
        },
        command_key="existing-profile",
    )
    capacity.activate_policy(
        snapshots["capacity"]["accounts"][0]["policy"],
        expected_revision=0,
        command_key="existing-policy",
    )
    admitted = capacity.admit(
        {
            "attempt_id": "existing-attempt",
            "run_id": run["id"],
            "profile_id": reference["id"],
            "profile_revision": 1,
            "role": "worker",
            "purpose": None,
            "authorization_ref": "fixture:approved-scope",
            "rulebook_revision": "fixture:rulebook:1",
            "duration_seconds": 30,
            "demand": {"service-fixture": "2"},
        },
        command_key="existing-admission",
    )
    assert admitted["decision"] == "admitted"
    registration = snapshots["policy"]["resources"]["profiles"][0]
    profile = registration["profile"]
    profile["binding"]["native_settings"]["endpoint"] = (
        f"http://127.0.0.1:{listener.server_port}/model"
    )
    hashed = hashlib.sha256(
        json.dumps(profile, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    for evidence in registration["capability_evidence"]:
        evidence["profile_digest"] = hashed
    snapshots["policy"]["profile_facts"][0]["profile_digest"] = hashed
    next(
        rule for rule in snapshots["policy"]["rulebook"]["rules"] if rule["id"] == "bounded-worker"
    )["id"] = "supplied-worker"
    try:
        with TestClient(case["app"], base_url=ORIGIN) as client:
            headers = login(client)
            before = logical_tables(case["directory"])
            assert before["runs"]["runs"]
            assert before["capacity"]["reservations"]
            assert before["projects"]["previews"]
            response = client.post(
                f"/v1/projects/{case['project']['id']}/rulebook/simulate",
                json=snapshots,
                headers=headers,
            )
            assert response.status_code == 200
            result = response.json()["result"]
            assert result["rule_id"] == "supplied-worker"
            assert result["selected_profile"] == {"id": "fixture-profile", "revision": 1}
            after = logical_tables(case["directory"])
            assert before == after
    finally:
        listener.shutdown()
        listener.server_close()
        worker.join(timeout=1)
    assert received == []
    record_testsuite_property(
        "simulation_actual_control_tables",
        json.dumps(
            {
                phase: {
                    database: {
                        table: {
                            "rows": len(rows),
                            "sha256": hashlib.sha256(
                                json.dumps(rows, sort_keys=True).encode()
                            ).hexdigest(),
                        }
                        for table, rows in tables.items()
                    }
                    for database, tables in state.items()
                }
                for phase, state in (("before", before), ("after", after))
            },
            sort_keys=True,
        ),
    )
    record_testsuite_property("simulation_model_listener_requests", len(received))
