"""A SQLite-backed, local fake-provider boundary; never a live API adapter."""

import hashlib
import http.client
import json
import math
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

SCALE = Decimal(1_000_000)


def units(value: str) -> int:
    if not isinstance(value, str):
        raise ValueError("Amounts must be decimal strings, never floats.")
    try:
        amount = Decimal(value)
    except InvalidOperation as error:
        raise ValueError("Invalid decimal amount") from error
    if not amount.is_finite():
        raise ValueError("Amounts must be finite.")
    if amount < 0 or amount > Decimal("9223372036854.775807"):
        raise ValueError("Amounts require nonnegative, finite, six-place precision.")
    six_places = amount.quantize(Decimal("0.000001"))
    if six_places != amount:
        raise ValueError("Amounts require nonnegative, finite, six-place precision.")
    return int(six_places * SCALE)


def money(value: int) -> str:
    return f"{Decimal(value) / SCALE:.6f}"


@dataclass(frozen=True)
class Price:
    revision: str
    currency: str
    fixed_charge: str
    input_byte_rate: str | None = None
    output_token_rate: str | None = None
    covers_all_charges: bool = False
    valid_until: float | None = None


@dataclass(frozen=True)
class Profile:
    id: str
    model: str
    endpoint: str
    price: Price
    logical_id_evidence_ref: str | None = None
    billing_path: str = "local_fake"


@dataclass(frozen=True)
class Receipt:
    receipt_id: str
    call_id: str | None
    state: str
    reason_code: str | None = None


class ResourceBroker:
    def __init__(
        self,
        path: Path,
        *,
        checkpoint: Callable[[str], None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = path
        self.checkpoint = checkpoint
        self.clock = clock
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS budgets (
                    currency TEXT PRIMARY KEY, limit_units INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS attempts (
                    id TEXT PRIMARY KEY, profile TEXT NOT NULL, currency TEXT NOT NULL,
                    reserved INTEGER NOT NULL, future INTEGER NOT NULL,
                    authorization_id TEXT NOT NULL, fence INTEGER NOT NULL,
                    state TEXT NOT NULL, authorization_expires_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS calls (
                    id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL REFERENCES attempts(id),
                    state TEXT NOT NULL, upper_units INTEGER NOT NULL,
                    actual_units INTEGER, price_revision TEXT NOT NULL,
                    provider_request_id TEXT, logical_call_id TEXT,
                    request_digest TEXT NOT NULL, request_json TEXT NOT NULL,
                    UNIQUE(attempt_id, logical_call_id));
                CREATE TABLE IF NOT EXISTS receipts (
                    id TEXT PRIMARY KEY, call_id TEXT REFERENCES calls(id),
                    attempt_id TEXT NOT NULL, reason_code TEXT);
                CREATE TABLE IF NOT EXISTS usage (
                    event_id TEXT PRIMARY KEY, call_id TEXT NOT NULL REFERENCES calls(id),
                    currency TEXT NOT NULL, actual_units INTEGER NOT NULL,
                    provider_request_id TEXT NOT NULL);
            """)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def configure_budget(self, currency: str, amount: str) -> None:
        with self._transaction() as db:
            db.execute("INSERT INTO budgets VALUES (?, ?)", (currency, units(amount)))

    def reserve_attempt(
        self,
        attempt_id: str,
        *,
        profile: Profile,
        amount: str,
        authorization_id: str,
        fence: int,
        authorization_expires_at: float,
    ) -> None:
        if (
            not attempt_id
            or not profile.id
            or not profile.model
            or not authorization_id
            or type(fence) is not int
            or fence <= 0
            or not math.isfinite(authorization_expires_at)
        ):
            raise ValueError("INVALID_AUTHORIZATION_BINDING")
        reservation = units(amount)
        with self._transaction() as db:
            budget = db.execute(
                "SELECT limit_units FROM budgets WHERE currency=?",
                (profile.price.currency,),
            ).fetchone()
            if budget is None or reservation + self._held(db, profile.price.currency) > budget[0]:
                raise ValueError("BUDGET_EXHAUSTED")
            db.execute(
                "INSERT INTO attempts VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?)",
                (
                    attempt_id,
                    json.dumps(asdict(profile)),
                    profile.price.currency,
                    reservation,
                    reservation,
                    authorization_id,
                    fence,
                    authorization_expires_at,
                ),
            )

    def submit(
        self,
        attempt_id: str,
        *,
        fence: int,
        prompt: str,
        max_output_tokens: int,
        logical_call_id: str | None = None,
    ) -> Receipt:
        receipt_id, call_id = str(uuid.uuid4()), str(uuid.uuid4())
        with self._transaction() as db:
            attempt = db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if (
                attempt is None
                or type(fence) is not int
                or attempt["fence"] != fence
                or attempt["state"] != "active"
                or attempt["authorization_expires_at"] <= self.clock()
            ):
                return self._reject(db, receipt_id, attempt_id, "AUTHORIZATION_INVALID")
            profile = json.loads(attempt["profile"])
            try:
                endpoint = urlsplit(profile["endpoint"])
                if (
                    profile["billing_path"] != "local_fake"
                    or endpoint.scheme != "http"
                    or endpoint.hostname != "127.0.0.1"
                    or endpoint.port is None
                    or not 0 < endpoint.port <= 65535
                    or endpoint.path != "/infer"
                    or endpoint.query
                    or endpoint.fragment
                    or endpoint.username is not None
                    or endpoint.password is not None
                ):
                    raise ValueError("Only explicitly registered local fake HTTP is allowed")
            except ValueError:
                return self._reject(db, receipt_id, attempt_id, "CASH_API_DISABLED")
            if (
                not isinstance(prompt, str)
                or type(max_output_tokens) is not int
                or (
                    logical_call_id is not None
                    and (
                        not isinstance(logical_call_id, str)
                        or not logical_call_id.strip()
                        or len(logical_call_id) > 256
                    )
                )
            ):
                return self._reject(db, receipt_id, attempt_id, "REQUEST_INVALID")
            request_data = {
                "model": profile["model"],
                "prompt": prompt,
                "max_output_tokens": max_output_tokens,
            }
            request_json = json.dumps(request_data, sort_keys=True)
            request_digest = hashlib.sha256(request_json.encode("utf-8")).hexdigest()
            verified_logical_id = logical_call_id if profile["logical_id_evidence_ref"] else None
            if verified_logical_id is not None:
                original = db.execute(
                    "SELECT * FROM calls WHERE attempt_id=? AND logical_call_id=?",
                    (attempt_id, verified_logical_id),
                ).fetchone()
                if original is not None:
                    if original["request_digest"] != request_digest:
                        return self._reject(db, receipt_id, attempt_id, "LOGICAL_ID_CONFLICT")
                    db.execute(
                        "INSERT INTO receipts VALUES (?, ?, ?, NULL)",
                        (receipt_id, original["id"], attempt_id),
                    )
                    return Receipt(receipt_id, original["id"], original["state"])
            price = profile["price"]
            try:
                if (
                    price["covers_all_charges"] is not True
                    or not price["revision"]
                    or price["valid_until"] is None
                    or not math.isfinite(price["valid_until"])
                    or price["valid_until"] <= self.clock()
                ):
                    raise ValueError("Unproven price")
                if (
                    type(max_output_tokens) is not int
                    or not 0 < max_output_tokens <= 1_000_000
                    or len(prompt.encode("utf-8")) > 1_000_000
                ):
                    return self._reject(db, receipt_id, attempt_id, "REQUEST_UNBOUNDED")
                upper = (
                    units(price["fixed_charge"])
                    + units(price["input_byte_rate"]) * len(prompt.encode("utf-8"))
                    + units(price["output_token_rate"]) * max_output_tokens
                )
                if upper > 2**63 - 1:
                    raise ValueError("Upper bound out of range")
            except (ValueError, TypeError, InvalidOperation):
                return self._reject(db, receipt_id, attempt_id, "PRICE_UNBOUNDED_OR_EXPIRED")
            if attempt["future"] < upper:
                return self._reject(db, receipt_id, attempt_id, "BUDGET_EXHAUSTED")
            budget = db.execute(
                "SELECT limit_units FROM budgets WHERE currency=?",
                (attempt["currency"],),
            ).fetchone()
            if self._held(db, attempt["currency"]) > budget[0]:
                return self._reject(db, receipt_id, attempt_id, "ACCOUNT_OVERDRAWN")
            db.execute("UPDATE attempts SET future=future-? WHERE id=?", (upper, attempt_id))
            db.execute(
                "INSERT INTO calls VALUES (?, ?, 'prepared', ?, NULL, ?, NULL, ?, ?, ?)",
                (
                    call_id,
                    attempt_id,
                    upper,
                    profile["price"]["revision"],
                    verified_logical_id,
                    request_digest,
                    request_json,
                ),
            )
            db.execute(
                "INSERT INTO receipts VALUES (?, ?, ?, NULL)",
                (receipt_id, call_id, attempt_id),
            )
        if self.checkpoint:
            self.checkpoint("before_send_intent")
        with self._transaction() as db:
            current = db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            call = db.execute("SELECT state FROM calls WHERE id=?", (call_id,)).fetchone()
            if (
                current["state"] != "active"
                or current["fence"] != fence
                or current["authorization_expires_at"] <= self.clock()
            ):
                self._release_prepared(db, call_id)
                db.execute(
                    "UPDATE receipts SET reason_code='AUTHORIZATION_INVALID' WHERE id=?",
                    (receipt_id,),
                )
                return Receipt(receipt_id, call_id, "not_sent", "AUTHORIZATION_INVALID")
            if call["state"] != "prepared":
                return Receipt(receipt_id, call_id, call["state"])
            if price["valid_until"] <= self.clock():
                self._release_prepared(db, call_id)
                db.execute(
                    "UPDATE receipts SET reason_code='PRICE_UNBOUNDED_OR_EXPIRED' WHERE id=?",
                    (receipt_id,),
                )
                return Receipt(receipt_id, call_id, "not_sent", "PRICE_UNBOUNDED_OR_EXPIRED")
            db.execute("UPDATE calls SET state='send_pending' WHERE id=?", (call_id,))
        if self.checkpoint:
            self.checkpoint("after_send_intent")
        connection = http.client.HTTPConnection("127.0.0.1", endpoint.port, timeout=2)
        try:
            connection.request(
                "POST",
                "/infer",
                json.dumps(
                    {
                        "call_id": call_id,
                        **request_data,
                    }
                ),
                {"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            if response.status != 200:
                raise ValueError("No authoritative usage response")
            payload = json.loads(response.read())
            if self.checkpoint:
                self.checkpoint("after_response")
            self.settle(
                call_id,
                usage_event_id=payload["usage_event_id"],
                actual_charge=payload["actual_charge"],
                currency=payload["currency"],
                provider_request_id=payload["request_id"],
            )
            if self.checkpoint:
                self.checkpoint("after_settlement")
        except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError):
            with self._transaction() as db:
                db.execute(
                    "UPDATE calls SET state='send_unknown' WHERE id=? AND state='send_pending'",
                    (call_id,),
                )
                state = db.execute("SELECT state FROM calls WHERE id=?", (call_id,)).fetchone()[0]
                reason = "SEND_OUTCOME_UNKNOWN" if state == "send_unknown" else None
                if reason:
                    db.execute(
                        "UPDATE receipts SET reason_code=? WHERE call_id=?",
                        (reason, call_id),
                    )
            return Receipt(receipt_id, call_id, state, reason)
        finally:
            connection.close()
        return Receipt(receipt_id, call_id, "settled")

    def settle(
        self,
        call_id: str,
        *,
        usage_event_id: str,
        actual_charge: str,
        currency: str,
        provider_request_id: str,
    ) -> None:
        if any(
            not isinstance(value, str) or not value.strip() or len(value) > 256
            for value in (usage_event_id, provider_request_id)
        ):
            raise ValueError("USAGE_IDENTITY_INVALID")
        actual = units(actual_charge)
        with self._transaction() as db:
            previous = db.execute(
                "SELECT * FROM usage WHERE event_id=?", (usage_event_id,)
            ).fetchone()
            if previous is not None:
                if (
                    previous["call_id"],
                    previous["currency"],
                    previous["actual_units"],
                    previous["provider_request_id"],
                ) != (call_id, currency, actual, provider_request_id):
                    raise ValueError("USAGE_EVENT_CONFLICT")
                return
            call = db.execute("SELECT * FROM calls WHERE id=?", (call_id,)).fetchone()
            if call is None:
                raise ValueError("CALL_UNKNOWN")
            if call["state"] not in {"send_pending", "send_unknown", "settled"}:
                raise ValueError("CALL_NOT_SENT")
            attempt = db.execute(
                "SELECT * FROM attempts WHERE id=?",
                (call["attempt_id"],),
            ).fetchone()
            if attempt["currency"] != currency:
                raise ValueError("CURRENCY_MISMATCH")
            if call["provider_request_id"] not in (None, provider_request_id):
                raise ValueError("PROVIDER_REQUEST_ID_CONFLICT")
            if call["actual_units"] is not None and actual < call["actual_units"]:
                raise ValueError("USAGE_TOTAL_REGRESSION")
            previously_held = (
                call["upper_units"] if call["actual_units"] is None else call["actual_units"]
            )
            db.execute(
                "INSERT INTO usage VALUES (?, ?, ?, ?, ?)",
                (usage_event_id, call_id, currency, actual, provider_request_id),
            )
            db.execute(
                "UPDATE calls SET state='settled', actual_units=?, provider_request_id=? "
                "WHERE id=?",
                (actual, provider_request_id, call_id),
            )
            if actual > call["upper_units"]:
                db.execute(
                    "UPDATE attempts SET state='suspended', future=0 WHERE id=? AND state='active'",
                    (attempt["id"],),
                )
                db.execute(
                    "UPDATE receipts SET reason_code='COST_BOUND_EXCEEDED' WHERE call_id=?",
                    (call_id,),
                )
            elif attempt["state"] == "active":
                db.execute(
                    "UPDATE attempts SET future=MAX(0, future+?) WHERE id=?",
                    (previously_held - actual, attempt["id"]),
                )

    def recover(self) -> dict[str, Any]:
        with self._transaction() as db:
            for row in db.execute("SELECT id FROM calls WHERE state='prepared'").fetchall():
                self._release_prepared(db, row["id"])
            db.execute("UPDATE calls SET state='send_unknown' WHERE state='send_pending'")
            db.execute(
                "UPDATE receipts SET reason_code='SEND_OUTCOME_UNKNOWN' "
                "WHERE call_id IN (SELECT id FROM calls WHERE state='send_unknown')",
            )
        return self.snapshot()

    def _release_prepared(self, db: sqlite3.Connection, call_id: str) -> None:
        call = db.execute("SELECT * FROM calls WHERE id=?", (call_id,)).fetchone()
        if call is not None and call["state"] == "prepared":
            db.execute("UPDATE calls SET state='not_sent' WHERE id=?", (call_id,))
            db.execute(
                "UPDATE attempts SET future=future+? WHERE id=? AND state='active'",
                (call["upper_units"], call["attempt_id"]),
            )

    def finish_attempt(self, attempt_id: str) -> None:
        with self._transaction() as db:
            db.execute("UPDATE attempts SET state='finished', future=0 WHERE id=?", (attempt_id,))
            for row in db.execute(
                "SELECT id FROM calls WHERE attempt_id=? AND state='prepared'",
                (attempt_id,),
            ).fetchall():
                self._release_prepared(db, row["id"])

    def revoke_attempt(self, attempt_id: str) -> None:
        with self._transaction() as db:
            db.execute(
                "UPDATE attempts SET state='revoked', fence=fence+1, future=0 WHERE id=?",
                (attempt_id,),
            )
            for row in db.execute(
                "SELECT id FROM calls WHERE attempt_id=? AND state='prepared'",
                (attempt_id,),
            ).fetchall():
                self._release_prepared(db, row["id"])

    def _reject(
        self,
        db: sqlite3.Connection,
        receipt_id: str,
        attempt_id: str,
        reason: str,
    ) -> Receipt:
        db.execute("INSERT INTO receipts VALUES (?, NULL, ?, ?)", (receipt_id, attempt_id, reason))
        return Receipt(receipt_id, None, "rejected", reason)

    def _held(self, db: sqlite3.Connection, currency: str) -> int:
        parents = sum(
            row[0]
            for row in db.execute(
                "SELECT future FROM attempts WHERE currency=?",
                (currency,),
            )
        )
        calls = sum(
            row[0]
            for row in db.execute(
                "SELECT COALESCE(c.actual_units, c.upper_units) "
                "FROM calls c JOIN attempts a ON a.id=c.attempt_id "
                "WHERE a.currency=? AND c.state!='not_sent'",
                (currency,),
            )
        )
        return int(parents + calls)

    def snapshot(self) -> dict[str, Any]:
        with self._transaction() as db:
            budgets = {}
            for row in db.execute("SELECT * FROM budgets ORDER BY currency").fetchall():
                held = self._held(db, row["currency"])
                budgets[row["currency"]] = {
                    "limit": money(row["limit_units"]),
                    "held": money(held),
                    "available": money(row["limit_units"] - held),
                }
            attempts = [
                {
                    "id": row["id"],
                    "state": row["state"],
                    "future": money(row["future"]),
                    "reserved": money(row["reserved"]),
                    "currency": row["currency"],
                    "authorization_id": row["authorization_id"],
                    "fence": row["fence"],
                    "authorization_expires_at": row["authorization_expires_at"],
                    "profile": json.loads(row["profile"]),
                    "profile_digest": hashlib.sha256(row["profile"].encode()).hexdigest(),
                }
                for row in db.execute("SELECT * FROM attempts ORDER BY id")
            ]
            calls = [
                {
                    "id": row["id"],
                    "state": row["state"],
                    "upper": money(row["upper_units"]),
                    "attempt_id": row["attempt_id"],
                    "price_revision": row["price_revision"],
                    "provider_request_id": row["provider_request_id"],
                    "logical_call_id": row["logical_call_id"],
                    "request_digest": row["request_digest"],
                    "bound_exceeded": (
                        row["actual_units"] is not None and row["actual_units"] > row["upper_units"]
                    ),
                    "input_bytes": len(json.loads(row["request_json"])["prompt"].encode("utf-8")),
                    "max_output_tokens": json.loads(row["request_json"])["max_output_tokens"],
                    "actual_charge": None
                    if row["actual_units"] is None
                    else money(row["actual_units"]),
                }
                for row in db.execute("SELECT * FROM calls ORDER BY rowid")
            ]
            receipts = [dict(row) for row in db.execute("SELECT * FROM receipts ORDER BY rowid")]
            usage = [dict(row) for row in db.execute("SELECT * FROM usage ORDER BY rowid")]
            return {
                "schema_version": "karajan.resources.snapshot.v1",
                "money_decimal_places": 6,
                "qualification_scope": "offline_local_fake",
                "live_qualified": False,
                "cash_api_enabled": False,
                "budgets": budgets,
                "attempts": attempts,
                "calls": calls,
                "receipts": receipts,
                "usage": usage,
            }
