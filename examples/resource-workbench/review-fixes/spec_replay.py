"""Replay fixed local resource Spec inputs; no remote model or cash API requests."""

import argparse
import hashlib
import json
import tempfile
import time
from pathlib import Path

from fastapi.testclient import TestClient
from karajan.capacity import CapacityStore
from karajan.web import create_app


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=["limit", "exhaustion"], required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--directory", type=Path)
    arguments = parser.parse_args()
    input_path = Path(__file__).with_name(f"spec-{arguments.case}.input.json")
    material = json.loads(input_path.read_bytes())
    if arguments.directory is None:
        directory = Path(tempfile.mkdtemp(prefix="karajan-resource-spec-"))
    else:
        directory = arguments.directory.resolve()
        directory.mkdir(parents=True, exist_ok=False)
    store = CapacityStore(
        directory / "capacity.sqlite",
        clock=(lambda: material["failure_at"]) if arguments.case == "exhaustion" else time.time,
    )
    for index, pool in enumerate(material["pools"]):
        store.register_pool(pool, command_key=f"pool-{index}")
    for index, profile in enumerate(material.get("profiles", [])):
        store.register_profile(profile, command_key=f"profile-{index}")
    for index, observation in enumerate(material.get("observations", [])):
        store.observe(observation, command_key=f"observation-{index}")
    store.activate_policy(material["policy"], expected_revision=0, command_key="initial")
    if arguments.case == "limit":
        origin = "http://127.0.0.1:8765"
        with TestClient(
            create_app(directory, origin=origin, bootstrap_token="fixture-bootstrap"),
            base_url=origin,
        ) as client:
            csrf = client.post(
                "/v1/session/bootstrap",
                json={"token": "fixture-bootstrap"},
                headers={"Origin": origin},
            ).json()["csrf_token"]
            policy = json.loads(json.dumps(material["policy"]))
            policy["lead_reserve"]["weekly"] = "999"
            response = client.post(
                "/v1/resources/policy",
                params={"account_id": "shared-account"},
                json={"policy": policy},
                headers={
                    "Origin": origin,
                    "X-CSRF-Token": csrf,
                    "If-Match": '"1"',
                    "Idempotency-Key": "exceeds-known-weekly-limit",
                },
            )
            after = client.get("/v1/resources").json()
        result = {
            "response_status": response.status_code,
            "response": response.json(),
            "after": after,
            "transport": {
                "before": "/v1/resources/accounts/shared-account/policy",
                "after": "/v1/resources/policy?account_id=shared-account",
            },
        }
        passed = (
            response.status_code == 422
            and response.json() == {"reason_code": "PROTECTION_EXCEEDS_POOL_LIMIT"}
            and after["accounts"][0]["policy_revision"] == 1
        )
    else:
        store.record_failure(
            "shared-account",
            reason=material["failure"],
            retry_after_seconds=material["cooldown_until"] - material["failure_at"],
            evidence_ref="fixture-known-exhaustion-without-quota-report",
            command_key="failure",
        )
        before = store.resource_view()
        store.clock = lambda: material["cooldown_until"] + 1.0
        after = store.resource_view()
        result = {"before": before, "after": after}
        passed = after["accounts"][0]["blockers"] == [
            {"reason_code": "EXHAUSTION_REQUIRES_NEW_OBSERVATION", "until": None}
        ]
    root = Path(__file__).resolve().parents[3]
    sources = [
        "backend/karajan/capacity/store.py",
        "backend/karajan/web/resources.py",
        "backend/karajan/web/app.py",
    ]
    report = {
        "schema_version": "karajan.resource-spec-replay.v1",
        "case": arguments.case,
        "status": "passed" if passed else "failed",
        "input": material,
        "input_sha256": hashlib.sha256(input_path.read_bytes()).hexdigest(),
        "directory": str(directory),
        "source_sha256": {
            path: hashlib.sha256((root / path).read_bytes()).hexdigest() for path in sources
        },
        "model_calls": 0,
        "cash_calls": 0,
        **result,
    }
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {"case": arguments.case, "status": report["status"], "output": str(arguments.output)}
        )
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
