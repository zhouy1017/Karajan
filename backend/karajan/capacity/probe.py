"""Fixed synthetic contention, durable accounting and an actual loopback-only effect."""

import hashlib
import http.client
import json
import platform
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Barrier, Lock, Thread
from typing import Annotated, Any, Literal

from pydantic import Field

from .models import AdmissionRequest, Contract, Observation, Policy, Pool, Profile
from .store import CapacityError, CapacityStore, validate


class ProbeSpec(Contract):
    schema_version: Literal["karajan.capacity.fixture.v1"]
    pools: Annotated[list[Pool], Field(min_length=1, max_length=32)]
    profiles: Annotated[list[Profile], Field(min_length=2, max_length=32)]
    observations: Annotated[list[Observation], Field(min_length=1, max_length=32)]
    policy: Policy
    blocked_request: AdmissionRequest
    contenders: Annotated[list[AdmissionRequest], Field(min_length=2, max_length=2)]
    lead_request: AdmissionRequest


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise CapacityError("CAPACITY_INPUT_INVALID")
        result[key] = value
    return result


def run_probe(case_path: Path, directory: Path) -> dict[str, Any]:
    raw = case_path.read_bytes()
    if len(raw) > 1_000_000:
        raise CapacityError("CAPACITY_INPUT_INVALID")
    try:
        case = validate(ProbeSpec, json.loads(raw, object_pairs_hook=_unique))
    except (ValueError, UnicodeError, RecursionError):
        raise CapacityError("CAPACITY_INPUT_INVALID") from None
    if any(item["source"] != "fixture" for item in case["observations"]):
        raise CapacityError("FIXTURE_OBSERVATIONS_ONLY")
    if directory.exists():
        raise CapacityError("CAPACITY_OUTPUT_EXISTS")
    directory.mkdir(parents=True)
    (directory / "input.json").write_bytes(raw)
    now = max(item["observed_at"] for item in case["observations"])
    store = CapacityStore(directory / "capacity.sqlite", clock=lambda: now)
    for index, pool in enumerate(case["pools"]):
        store.register_pool(pool, command_key=f"pool-{index}")
    for index, profile in enumerate(case["profiles"]):
        store.register_profile(profile, command_key=f"profile-{index}")
    for index, observation in enumerate(case["observations"]):
        store.observe(observation, command_key=f"observation-{index}")
    store.activate_policy(case["policy"], expected_revision=0, command_key="policy")
    rejected = store.admit(case["blocked_request"], command_key="blocked-first")
    barrier = Barrier(2)

    def contend(index: int) -> dict[str, Any]:
        reopened = CapacityStore(store.path, clock=lambda: now)
        barrier.wait(timeout=5)
        return reopened.admit(case["contenders"][index], command_key=f"contender-{index}")

    with ThreadPoolExecutor(max_workers=2) as workers:
        contention = list(workers.map(contend, [0, 1]))

    received: list[dict[str, Any]] = []
    lock = Lock()

    class Receiver(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            size = int(self.headers.get("Content-Length", "0"))
            if self.path != "/fixture" or not 0 < size <= 4096:
                self.send_error(400)
                return
            payload = json.loads(self.rfile.read(size))
            with lock:
                received.append(payload)
            data = b'{"fixture":"received"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    effects: list[dict[str, Any]] = []

    def fixture_effect(admission: dict[str, Any]) -> None:
        if admission["decision"] != "admitted":
            return
        identity = admission["admission_id"]
        activated = store.activate(identity, command_key="activate-" + identity)
        if activated["decision"] != "capacity_revalidated":
            effects.append({"activation": activated, "sent": False})
            return
        # This explicit fixture has no provider adapter, credentials or configurable endpoint.
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        try:
            connection.request(
                "POST",
                "/fixture",
                body=json.dumps(
                    {
                        "admission_id": identity,
                        "attempt_id": admission["request"]["attempt_id"],
                    }
                ).encode(),
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            response.read(4096)
            if response.status != 200:
                raise CapacityError("FIXTURE_RECEIVER_FAILED")
        finally:
            connection.close()
        receipt = store.record_usage(
            {
                "id": "fixture-call-" + identity,
                "admission_id": identity,
                "amounts": {p: "1" for p in admission["request"]["demand"]},
                "window_ids": {
                    p: observed["window_id"] for p, observed in activated["observations"].items()
                },
                "evidence_ref": "local-http-receiver",
                "attribution_ref": "scripted-fixture-window",
            },
            command_key="usage-" + identity,
        )
        reconciled = store.reconcile(
            identity,
            local_ended=True,
            remote_ended=True,
            usage_complete=True,
            not_sent=False,
            evidence_ref="local-http-response-and-usage",
            command_key="end-" + identity,
        )
        effects.append(
            {
                "activation": activated,
                "receipt": receipt,
                "reconciliation": reconciled,
                "sent": True,
            }
        )

    try:
        fixture_effect(rejected)
        for admission in contention:
            fixture_effect(admission)
        leader = store.admit(case["lead_request"], command_key="lead")
        fixture_effect(leader)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    snapshot = store.snapshot()
    reopened_snapshot = CapacityStore(store.path, clock=lambda: now).snapshot()
    rejected_ids = {
        item["request"]["attempt_id"]
        for item in [rejected, *contention, leader]
        if item["decision"] == "rejected"
    }
    blocked_count = sum(item["attempt_id"] in rejected_ids for item in received)
    conditions = {
        "blocked_request_rejected": rejected["decision"] == "rejected",
        "one_contender_admitted": sorted(item["decision"] for item in contention)
        == ["admitted", "rejected"],
        "lead_admitted": leader["decision"] == "admitted",
        "exactly_two_actual_fixture_requests": len(received) == 2,
        "rejected_have_no_actual_fixture_requests": blocked_count == 0,
        "reopened_snapshot_matches": snapshot == reopened_snapshot,
        "two_durable_receipts": len(snapshot["usage"]) == 2,
    }
    report = {
        "schema_version": "karajan.capacity.fixture-report.v1",
        "case_status": "passed" if all(conditions.values()) else "failed",
        "conditions": conditions,
        "recorded_at": datetime.now(UTC).isoformat(),
        "os": platform.platform(),
        "python": platform.python_version(),
        "input_sha256": hashlib.sha256(raw).hexdigest(),
        "source_sha256": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(Path(__file__).parent.glob("*.py"))
        },
        "blocked": rejected,
        "contention": contention,
        "lead": leader,
        "effects": effects,
        "receiver_count": len(received),
        "receiver_requests": received,
        "blocked_request_receiver_count": blocked_count,
        "reopened_snapshot_matches": snapshot == reopened_snapshot,
        "snapshot": snapshot,
        "official_model_calls": 0,
        "cash_api_calls": 0,
        "live_qualification": "not_run",
        "profile_enabled": False,
        "activation_allowed": False,
    }
    (directory / "report.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return report
