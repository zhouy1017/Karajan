"""Reproducible M0 evidence using a local receiver and an actually terminated child."""

import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .broker import Price, Profile, Receipt, ResourceBroker, units


class FakeProvider(ThreadingHTTPServer):
    def __init__(self, currency: str, charge: str) -> None:
        super().__init__(("127.0.0.1", 0), FakeHandler)
        self.currency = currency
        self.charge = charge
        self.records: list[dict[str, Any]] = []


class FakeHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        assert isinstance(self.server, FakeProvider)
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.records.append(body)
        response = json.dumps(
            {
                "request_id": "fake:" + body["call_id"],
                "usage_event_id": "fake-usage:" + body["call_id"],
                "actual_charge": self.server.charge,
                "currency": self.server.currency,
                "output": "synthetic response; no model called",
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, format: str, *args: Any) -> None:
        pass


def run_demo(scenario_path: Path, directory: Path) -> dict[str, Any]:
    scenario = json.loads(scenario_path.read_text(encoding="utf-8"))
    required = {
        "schema_version",
        "currency",
        "account_limit",
        "contending_parent",
        "call_upper",
        "first_actual_charge",
        "late_actual_charge",
    }
    if not isinstance(scenario, dict) or set(scenario) != required:
        raise ValueError("INVALID_PROBE_SCENARIO")
    if scenario["schema_version"] != "karajan.resources.probe.v1":
        raise ValueError("INVALID_PROBE_SCHEMA")
    for name in required - {"schema_version", "currency"}:
        units(scenario[name])
    if (
        not isinstance(scenario["currency"], str)
        or len(scenario["currency"]) != 3
        or not scenario["currency"].isascii()
        or not scenario["currency"].isupper()
    ):
        raise ValueError("INVALID_CURRENCY")
    upper, parent = Decimal(scenario["call_upper"]), Decimal(scenario["contending_parent"])
    if not (
        0 < upper <= parent < 2 * upper
        and Decimal(scenario["first_actual_charge"]) == upper
        and 0 <= Decimal(scenario["late_actual_charge"]) <= upper
        and Decimal(scenario["account_limit"]) >= 3 * upper
    ):
        raise ValueError("Scenario must support one winner and a separate unknown-send probe")
    directory.mkdir(parents=True, exist_ok=False)
    provider = FakeProvider(scenario["currency"], scenario["first_actual_charge"])
    thread = threading.Thread(
        target=lambda: provider.serve_forever(poll_interval=0.01), daemon=True
    )
    thread.start()
    try:
        broker = ResourceBroker(directory / "resources.sqlite")
        broker.configure_budget(scenario["currency"], scenario["account_limit"])
        profile = Profile(
            id="local-fixture-profile-r1",
            model="synthetic-model",
            endpoint=f"http://127.0.0.1:{provider.server_port}/infer",
            price=Price(
                revision="local-flat-price-r1",
                currency=scenario["currency"],
                fixed_charge=scenario["call_upper"],
                input_byte_rate="0",
                output_token_rate="0",
                covers_all_charges=True,
                valid_until=time.time() + 120,
            ),
        )
        broker.reserve_attempt(
            "contending",
            profile=profile,
            amount=scenario["contending_parent"],
            authorization_id="fixture-auth",
            fence=1,
            authorization_expires_at=time.time() + 120,
        )
        barrier = threading.Barrier(2)

        def submit(index: int) -> Receipt:
            barrier.wait(timeout=5)
            return broker.submit(
                "contending", fence=1, prompt=f"fixture {index}", max_output_tokens=1
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            receipts = list(pool.map(submit, range(2)))
        contending_snapshot = broker.snapshot()
        first_receive_count = len(provider.records)
        broker.finish_attempt("contending")
        provider.charge = scenario["late_actual_charge"]
        broker.reserve_attempt(
            "unknown",
            profile=profile,
            amount=str(upper * 2),
            authorization_id="fixture-auth",
            fence=1,
            authorization_expires_at=time.time() + 120,
        )
        child_script = """
import os, sys
from pathlib import Path
from karajan.resources import ResourceBroker
def checkpoint(phase):
    if phase == 'after_response':
        os._exit(71)
broker = ResourceBroker(Path(sys.argv[1]), checkpoint=checkpoint)
broker.submit('unknown', fence=1, prompt='fixture unknown response', max_output_tokens=1)
"""
        child = subprocess.run(
            [sys.executable, "-c", child_script, str(directory / "resources.sqlite")],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        broker.recover()
        broker.finish_attempt("unknown")
        unknown_snapshot = broker.snapshot()
        unknown_calls = [
            call for call in unknown_snapshot["calls"] if call["attempt_id"] == "unknown"
        ]
        if len(unknown_calls) == 1 and child.returncode == 71:
            for _ in range(2):
                broker.settle(
                    unknown_calls[0]["id"],
                    usage_event_id="late-fixture-usage",
                    actual_charge=scenario["late_actual_charge"],
                    currency=scenario["currency"],
                    provider_request_id="fake:" + unknown_calls[0]["id"],
                )
        final = broker.snapshot()
        conditions = {
            "one_admitted_one_rejected": sorted(r.state for r in receipts)
            == ["rejected", "settled"],
            "only_one_first_request_received": first_receive_count == 1,
            "parent_not_double_charged": contending_snapshot["budgets"][scenario["currency"]][
                "held"
            ]
            == f"{parent:.6f}",
            "actual_child_exit": child.returncode == 71,
            "unknown_retained": len(unknown_calls) == 1
            and unknown_calls[0]["state"] == "send_unknown",
            "no_retransmission": len(provider.records) == 2,
            "duplicate_usage_coalesced": len(final["usage"]) == 2,
            "late_charge_recorded": final["budgets"][scenario["currency"]]["held"]
            == f"{upper + Decimal(scenario['late_actual_charge']):.6f}",
        }
        report = {
            "schema_version": "karajan.resource_probe.report.v1",
            "case_id": "local-parent-slice-and-unknown-send",
            "status": "passed" if all(conditions.values()) else "failed",
            "qualification_scope": "offline_local_fake",
            "live_qualified": False,
            "cash_api_enabled": False,
            "observed_at": datetime.now(UTC).isoformat(),
            "os": sys.platform,
            "python_version": sys.version.split()[0],
            "scenario": scenario,
            "conditions": conditions,
            "contending_snapshot": contending_snapshot,
            "unknown_before_reconciliation": unknown_snapshot,
            "final_snapshot": final,
            "provider_records": provider.records,
            "child_exit_code": child.returncode,
            "limitations": [
                "One synthetic HTTP protocol, flat fake prices, no live service or model calls.",
                "SQLite and process-exit evidence is for this OS; not full A19/A26 qualification.",
            ],
        }
        (directory / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        return report
    finally:
        provider.shutdown()
        provider.server_close()
        thread.join(timeout=3)
