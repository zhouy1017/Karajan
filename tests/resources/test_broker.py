"""Observe the public broker, SQLite snapshots and a real local HTTP receiver."""

import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from karajan.resources import Price, Profile, ResourceBroker


class Receiver(ThreadingHTTPServer):
    records: list[dict[str, Any]]
    charge = "1"

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), Handler)
        self.records = []
        self.response_overrides: dict[str, Any] = {}

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.server_port}/infer"


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        assert isinstance(self.server, Receiver)
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.records.append(request)
        body = json.dumps(
            {
                "request_id": request["call_id"],
                "usage_event_id": "usage:" + request["call_id"],
                "currency": "USD",
                "actual_charge": self.server.charge,
                "output": "local fixture response",
                **self.server.response_overrides,
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        pass


@pytest.fixture
def receiver() -> Iterator[Receiver]:
    server = Receiver()
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def prepare(
    tmp_path: Path,
    receiver: Receiver,
    *,
    reservation: str = "4",
    price: Price | None = None,
    checkpoint: Callable[[str], None] | None = None,
    logical_id_evidence_ref: str | None = None,
    clock: Callable[[], float] = time.time,
    authorization_expires_at: float | None = None,
) -> ResourceBroker:
    broker = ResourceBroker(tmp_path / "resources.sqlite", checkpoint=checkpoint, clock=clock)
    broker.configure_budget("USD", "10")
    profile = Profile(
        id="fake-profile-r1",
        model="fixture-model",
        endpoint=receiver.endpoint,
        logical_id_evidence_ref=logical_id_evidence_ref,
        price=price
        or Price(
            revision="fixture-price-r1",
            currency="USD",
            fixed_charge="2",
            input_byte_rate="0",
            output_token_rate="0",
            covers_all_charges=True,
            valid_until=time.time() + 60,
        ),
    )
    broker.reserve_attempt(
        "attempt-1",
        profile=profile,
        amount=reservation,
        authorization_id="auth-1",
        fence=1,
        authorization_expires_at=(
            clock() + 60 if authorization_expires_at is None else authorization_expires_at
        ),
    )
    return broker


def test_parent_reservation_covers_the_child_without_double_charging(
    tmp_path: Path,
    receiver: Receiver,
) -> None:
    broker = prepare(tmp_path, receiver)

    receipt = broker.submit("attempt-1", fence=1, prompt="hello", max_output_tokens=10)

    assert receipt.state == "settled"
    assert len(receiver.records) == 1
    snapshot = broker.snapshot()
    assert snapshot["budgets"]["USD"] == {
        "limit": "10.000000",
        "held": "4.000000",
        "available": "6.000000",
    }
    assert snapshot["attempts"][0]["future"] == "3.000000"
    assert snapshot["calls"][0]["actual_charge"] == "1.000000"


@pytest.mark.parametrize(
    "verified, logical_id, sends",
    [
        (False, None, 2),
        (False, "same-client-id", 2),
        (True, "same-client-id", 1),
    ],
)
def test_only_trusted_transport_evidence_enables_logical_id_reuse(
    tmp_path: Path,
    receiver: Receiver,
    verified: bool,
    logical_id: str | None,
    sends: int,
) -> None:
    broker = prepare(
        tmp_path,
        receiver,
        logical_id_evidence_ref="fixture:stable-http-id" if verified else None,
    )
    receipts = [
        broker.submit(
            "attempt-1",
            fence=1,
            prompt="identical body",
            max_output_tokens=10,
            logical_call_id=logical_id,
        )
        for _ in range(2)
    ]

    assert len(receiver.records) == sends
    assert len({receipt.receipt_id for receipt in receipts}) == 2
    assert len(broker.snapshot()["calls"]) == sends
    assert len(broker.snapshot()["receipts"]) == 2


@pytest.mark.parametrize("decision", ["revoke", "expire"])
def test_authorization_change_before_send_intent_prevents_the_request(
    tmp_path: Path,
    receiver: Receiver,
    decision: str,
) -> None:
    current_time = [100.0]

    def checkpoint(phase: str) -> None:
        if phase == "before_send_intent":
            if decision == "revoke":
                broker.revoke_attempt("attempt-1")
            else:
                current_time[0] = 200

    broker = prepare(
        tmp_path,
        receiver,
        checkpoint=checkpoint,
        clock=lambda: current_time[0],
        authorization_expires_at=150,
    )
    result = broker.submit("attempt-1", fence=1, prompt="hello", max_output_tokens=10)

    assert result.state == "not_sent"
    assert result.reason_code == "AUTHORIZATION_INVALID"
    assert receiver.records == []


def test_concurrent_receipts_compete_for_one_parent_and_rejection_never_sends(
    tmp_path: Path,
    receiver: Receiver,
) -> None:
    broker = prepare(tmp_path, receiver, reservation="3")
    receiver.charge = "2"
    barrier = threading.Barrier(2)

    def submit() -> Any:
        barrier.wait(timeout=3)
        return broker.submit("attempt-1", fence=1, prompt="same", max_output_tokens=10)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: submit(), range(2)))

    assert sorted(result.state for result in results) == ["rejected", "settled"]
    assert len({result.receipt_id for result in results}) == 2
    assert len(receiver.records) == 1
    snapshot = broker.snapshot()
    assert snapshot["budgets"]["USD"]["held"] == "3.000000"
    assert len(snapshot["receipts"]) == 2
    assert sum(row["reason_code"] == "BUDGET_EXHAUSTED" for row in snapshot["receipts"]) == 1


@pytest.mark.parametrize(
    "changed",
    [
        {"valid_until": 0},
        {"input_byte_rate": None},
        {"output_token_rate": None},
        {"covers_all_charges": False},
    ],
)
def test_stale_or_incomplete_price_never_sends(
    tmp_path: Path,
    receiver: Receiver,
    changed: dict[str, Any],
) -> None:
    fields = {
        "revision": "p1",
        "currency": "USD",
        "fixed_charge": "0",
        "input_byte_rate": "0.01",
        "output_token_rate": "0.01",
        "covers_all_charges": True,
        "valid_until": time.time() + 60,
    }
    fields.update(changed)
    broker = prepare(tmp_path, receiver, price=Price(**fields))

    result = broker.submit("attempt-1", fence=1, prompt="hello", max_output_tokens=10)

    assert result.state == "rejected"
    assert result.reason_code == "PRICE_UNBOUNDED_OR_EXPIRED"
    assert receiver.records == []
    assert broker.snapshot()["attempts"][0]["future"] == "4.000000"


def test_unknown_send_survives_restart_finish_and_late_duplicate_usage(
    tmp_path: Path,
    receiver: Receiver,
) -> None:
    def crash(phase: str) -> None:
        if phase == "after_response":
            raise SystemExit("simulated process loss")

    broker = prepare(tmp_path, receiver, checkpoint=crash)
    with pytest.raises(SystemExit):
        broker.submit("attempt-1", fence=1, prompt="hello", max_output_tokens=10)

    restarted = ResourceBroker(tmp_path / "resources.sqlite")
    restarted.recover()
    restarted.finish_attempt("attempt-1")
    snapshot = restarted.snapshot()
    assert len(receiver.records) == 1
    assert snapshot["calls"][0]["state"] == "send_unknown"
    assert snapshot["budgets"]["USD"]["held"] == "2.000000"
    call_id = snapshot["calls"][0]["id"]
    for _ in range(2):
        restarted.settle(
            call_id,
            usage_event_id="late-usage",
            actual_charge="1",
            currency="USD",
            provider_request_id=receiver.records[0]["call_id"],
        )
    assert restarted.snapshot()["budgets"]["USD"]["held"] == "1.000000"
    assert len(restarted.snapshot()["usage"]) == 1
    assert len(receiver.records) == 1


@pytest.mark.parametrize(
    "phase, state, held, sends",
    [
        ("before_send_intent", "not_sent", "0.000000", 0),
        ("after_send_intent", "send_unknown", "2.000000", 0),
        ("after_response", "send_unknown", "2.000000", 1),
        ("after_settlement", "settled", "1.000000", 1),
    ],
)
def test_actual_process_exit_at_each_send_window_does_not_refund_unknown(
    tmp_path: Path,
    receiver: Receiver,
    phase: str,
    state: str,
    held: str,
    sends: int,
) -> None:
    prepare(tmp_path, receiver)
    script = """
import os, sys
from pathlib import Path
from karajan.resources import ResourceBroker
def checkpoint(phase):
    if phase == sys.argv[2]:
        os._exit(71)
broker = ResourceBroker(Path(sys.argv[1]), checkpoint=checkpoint)
broker.submit('attempt-1', fence=1, prompt='hello', max_output_tokens=10)
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "backend")
    child = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "resources.sqlite"), phase],
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert child.returncode == 71, child.stderr
    restarted = ResourceBroker(tmp_path / "resources.sqlite")
    restarted.recover()
    restarted.finish_attempt("attempt-1")
    assert restarted.snapshot()["calls"][0]["state"] == state
    assert restarted.snapshot()["budgets"]["USD"]["held"] == held
    assert len(receiver.records) == sends


@pytest.mark.parametrize(
    "billing_path, endpoint",
    [
        ("api_cash", None),
        ("local_fake", "https://example.invalid/infer"),
        ("local_fake", "http://192.0.2.1/infer"),
        ("local_fake", "http://127.0.0.1:bad/infer"),
    ],
)
def test_only_local_fake_billing_can_reach_the_receiver(
    tmp_path: Path,
    receiver: Receiver,
    billing_path: str,
    endpoint: str | None,
) -> None:
    broker = ResourceBroker(tmp_path / "resources.sqlite")
    broker.configure_budget("USD", "10")
    broker.reserve_attempt(
        "attempt-1",
        amount="4",
        authorization_id="auth",
        fence=1,
        authorization_expires_at=time.time() + 60,
        profile=Profile(
            id="test-r1",
            model="fixture",
            endpoint=endpoint or receiver.endpoint,
            billing_path=billing_path,
            price=Price(
                revision="price1",
                currency="USD",
                fixed_charge="2",
                input_byte_rate="0",
                output_token_rate="0",
                covers_all_charges=True,
                valid_until=time.time() + 60,
            ),
        ),
    )

    result = broker.submit("attempt-1", fence=1, prompt="hello", max_output_tokens=10)

    assert result.state == "rejected"
    assert result.reason_code == "CASH_API_DISABLED"
    assert receiver.records == []
    assert broker.snapshot()["calls"] == []


def test_snapshot_exposes_native_currency_and_price_bound_for_independent_audit(
    tmp_path: Path,
    receiver: Receiver,
) -> None:
    broker = prepare(tmp_path, receiver)
    broker.configure_budget("CNY", "100")
    price = Price(
        revision="cny-price-r1",
        currency="CNY",
        fixed_charge="0",
        input_byte_rate="0.1",
        output_token_rate="0.2",
        covers_all_charges=True,
        valid_until=time.time() + 60,
    )
    broker.reserve_attempt(
        "attempt-cny",
        profile=Profile(id="cny-r1", model="fixture", endpoint=receiver.endpoint, price=price),
        amount="5",
        authorization_id="cny-auth",
        fence=1,
        authorization_expires_at=time.time() + 60,
    )
    broker.submit("attempt-1", fence=1, prompt="hello", max_output_tokens=10)

    snapshot = broker.snapshot()
    assert snapshot["budgets"]["USD"]["held"] == "4.000000"
    assert snapshot["budgets"]["CNY"] == {
        "limit": "100.000000",
        "held": "5.000000",
        "available": "95.000000",
    }
    assert snapshot["attempts"][1]["profile"]["price"]["revision"] == "cny-price-r1"
    assert snapshot["calls"][0]["price_revision"] == "fixture-price-r1"
    assert snapshot["calls"][0]["input_bytes"] == 5
    assert snapshot["calls"][0]["max_output_tokens"] == 10
    assert snapshot["live_qualified"] is False


def test_a_reported_charge_above_the_bound_is_recorded_and_blocks_more_calls(
    tmp_path: Path,
    receiver: Receiver,
) -> None:
    broker = prepare(tmp_path, receiver, reservation="8")
    receiver.charge = "3"

    broker.submit("attempt-1", fence=1, prompt="hello", max_output_tokens=10)
    next_receipt = broker.submit("attempt-1", fence=1, prompt="another", max_output_tokens=10)

    assert next_receipt.state == "rejected"
    assert len(receiver.records) == 1
    snapshot = broker.snapshot()
    assert snapshot["calls"][0]["actual_charge"] == "3.000000"
    assert snapshot["calls"][0]["bound_exceeded"] is True
    assert snapshot["attempts"][0]["state"] == "suspended"


@pytest.mark.parametrize(
    "envelope",
    [
        {"prompt": None, "max_output_tokens": 10},
        {"prompt": "hello", "max_output_tokens": None},
        {"prompt": "hello", "max_output_tokens": True},
        {"prompt": "hello", "max_output_tokens": 10, "logical_call_id": ""},
    ],
)
def test_unbounded_or_invalid_client_envelopes_are_recorded_without_sending(
    tmp_path: Path,
    receiver: Receiver,
    envelope: dict[str, Any],
) -> None:
    broker = prepare(tmp_path, receiver)

    result = broker.submit("attempt-1", fence=1, **envelope)

    assert result.state == "rejected"
    assert result.reason_code == "REQUEST_INVALID"
    assert len(broker.snapshot()["receipts"]) == 1
    assert receiver.records == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"request_id": ""},
        {"usage_event_id": ""},
        {"currency": "CNY"},
        {"actual_charge": "NaN"},
    ],
)
def test_invalid_provider_evidence_retains_upper_and_a_durable_unknown_reason(
    tmp_path: Path,
    receiver: Receiver,
    overrides: dict[str, Any],
) -> None:
    broker = prepare(tmp_path, receiver)
    receiver.response_overrides = overrides

    receipt = broker.submit("attempt-1", fence=1, prompt="hello", max_output_tokens=10)
    broker.finish_attempt("attempt-1")

    snapshot = ResourceBroker(tmp_path / "resources.sqlite").snapshot()
    assert receipt.state == "send_unknown"
    assert snapshot["budgets"]["USD"]["held"] == "2.000000"
    assert snapshot["receipts"][0]["reason_code"] == "SEND_OUTCOME_UNKNOWN"
    assert snapshot["usage"] == []
    assert len(receiver.records) == 1


def test_reconciliation_cannot_lower_a_confirmed_total_or_change_request_identity(
    tmp_path: Path,
    receiver: Receiver,
) -> None:
    broker = prepare(tmp_path, receiver)
    receipt = broker.submit("attempt-1", fence=1, prompt="hello", max_output_tokens=10)
    assert receipt.call_id is not None
    for charge, request_id, reason in [
        ("0", receipt.call_id, "USAGE_TOTAL_REGRESSION"),
        ("1", "a-different-request", "PROVIDER_REQUEST_ID_CONFLICT"),
    ]:
        with pytest.raises(ValueError, match=reason):
            broker.settle(
                receipt.call_id,
                usage_event_id="conflicting-late-event",
                actual_charge=charge,
                currency="USD",
                provider_request_id=request_id,
            )

    snapshot = broker.snapshot()
    assert snapshot["calls"][0]["actual_charge"] == "1.000000"
    assert snapshot["budgets"]["USD"]["held"] == "4.000000"
    assert len(snapshot["usage"]) == 1
    assert len(receiver.records) == 1


def test_ledger_rejects_precision_that_decimal_context_would_silently_round(
    tmp_path: Path,
) -> None:
    broker = ResourceBroker(tmp_path / "resources.sqlite")

    with pytest.raises(ValueError, match="six-place"):
        broker.configure_budget("USD", "0.00000100000000000000000000000000000000001")

    assert broker.snapshot()["budgets"] == {}


@pytest.mark.parametrize("recover, expected_state", [(False, "prepared"), (True, "not_sent")])
def test_an_unsent_call_cannot_settle_or_inflate_the_parent_slice(
    tmp_path: Path,
    receiver: Receiver,
    recover: bool,
    expected_state: str,
) -> None:
    def crash(phase: str) -> None:
        if phase == "before_send_intent":
            raise SystemExit("fixture exits before send intent")

    broker = prepare(tmp_path, receiver, checkpoint=crash)
    with pytest.raises(SystemExit):
        broker.submit("attempt-1", fence=1, prompt="never sent", max_output_tokens=10)
    restarted = ResourceBroker(tmp_path / "resources.sqlite")
    if recover:
        restarted.recover()
    before = restarted.snapshot()
    assert before["calls"][0]["state"] == expected_state
    assert before["budgets"]["USD"]["held"] == "4.000000"

    with pytest.raises(ValueError, match="CALL_NOT_SENT"):
        restarted.settle(
            before["calls"][0]["id"],
            usage_event_id="impossible-usage",
            actual_charge="1",
            currency="USD",
            provider_request_id="impossible-request",
        )

    assert restarted.snapshot() == before
    assert receiver.records == []
