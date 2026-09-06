"""Durable send intents for the fixed Go qualification relay.

This is an internal controller/relay port, not model qualification, cash budgeting
or proof of remote cancellation. The controller supplies grant identity; only the
trusted relay supplies logical call IDs. Native request IDs are not authoritative.
No request, response text, header or credential belongs in this journal.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import secrets
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from karajan.contracts.probe import Contract
from pydantic import BaseModel, Field, ValidationError, model_validator

_Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.:-]{1,160}$")]
_Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
_Positive = Annotated[int, Field(gt=0, le=2**63 - 1)]
_Count = Annotated[int, Field(ge=0, le=2**63 - 1)]
_Reason = Literal[
    "ACTIVE_HANDLER_REMAINS",
    "CHOICE_AFTER_FINISH",
    "CLIENT_CLOSE_FAILED",
    "DATA_AFTER_TERMINATOR",
    "DENIED_CANARY_IN_REQUEST",
    "DUPLICATE_JSON_KEY",
    "INCOMPLETE_REQUEST",
    "INCOMPLETE_SSE",
    "INCOMPLETE_TOOL_CALL",
    "INVALID_BODY_LENGTH",
    "INVALID_CAPABILITY",
    "INVALID_CHOICES",
    "INVALID_DELTA",
    "INVALID_GO_COST_TRAILER",
    "INVALID_JSON",
    "INVALID_MAX_TOKENS",
    "INVALID_MESSAGES",
    "INVALID_MODEL",
    "INVALID_PATH",
    "INVALID_REQUEST_OBJECT",
    "INVALID_SESSION_HEADER",
    "INVALID_SSE_JSON",
    "INVALID_SSE",
    "INVALID_TOOL_CALLS",
    "INVALID_TOOL_NAME",
    "INVALID_UPSTREAM_CONTENT_TYPE",
    "INVALID_USAGE",
    "METHOD_NOT_ALLOWED",
    "MISSING_MODEL",
    "MODEL_MISMATCH",
    "NONFINITE_JSON_NUMBER",
    "RELAY_CLOSING",
    "RELAY_TRANSPORT_ERROR",
    "REQUEST_LIMIT_REACHED",
    "REQUEST_TOO_LARGE",
    "SERVER_THREAD_REMAINS",
    "STREAM_REQUIRED",
    "UNAPPROVED_TOOL",
    "UNEXPECTED_CONTENT_ENCODING",
    "UNEXPECTED_SSE_FIELD",
    "UNSUCCESSFUL_FINISH",
    "UPSTREAM_CREDENTIAL_ECHO",
    "UPSTREAM_ERROR_EVENT",
    "UPSTREAM_HTTP_ERROR",
    "UPSTREAM_RESPONSE_TOO_LARGE",
]


class GoJournalError(ValueError):
    """Stable failure without echoing caller input."""


class _GrantBinding(Contract):
    qualification_id: _Identifier
    attempt_id: _Identifier
    fence: _Positive
    profile_digest: _Digest
    runtime_digest: _Digest
    channel: _Identifier
    model: Literal["glm-5.3-flash"]
    auth_generation: _Identifier
    expires_at: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    max_requests: Annotated[int, Field(ge=1, le=6)]


class _GrantRef(Contract):
    grant_id: _Identifier


class _CallRef(_GrantRef):
    call_id: _Identifier


class _PromptDetails(Contract):
    cached_tokens: _Count | None = None


class _CompletionDetails(Contract):
    reasoning_tokens: _Count | None = None


class _Usage(Contract):
    prompt_tokens: _Count | None = None
    completion_tokens: _Count | None = None
    total_tokens: _Count | None = None
    prompt_tokens_details: _PromptDetails | None = None
    completion_tokens_details: _CompletionDetails | None = None


class _Outcome(Contract):
    state: Literal["response_received", "send_unknown", "rejected"]
    upstream_status: Annotated[int, Field(ge=100, le=599)] | None = None
    response_bytes: _Count = 0
    usage: _Usage = Field(default_factory=_Usage)
    protocol_passed: bool = False
    reason_codes: Annotated[list[_Reason], Field(max_length=8)] = Field(default_factory=list)

    @model_validator(mode="after")
    def consistent_response(self) -> Self:
        if self.state == "response_received" and self.upstream_status is None:
            raise ValueError("RESPONSE_STATUS_REQUIRED")
        if self.protocol_passed and (
            self.state != "response_received"
            or self.upstream_status != 200
            or self.response_bytes == 0
            or self.reason_codes
        ):
            raise ValueError("PROTOCOL_FACTS_INCONSISTENT")
        return self


def _validated(model: type[BaseModel], value: object) -> dict[str, Any]:
    try:
        return model.model_validate(value).model_dump(exclude_none=True)
    except (ValidationError, TypeError, ValueError):
        raise GoJournalError("GO_JOURNAL_INPUT_INVALID") from None


def _encoded(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class GoCallJournal:
    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time) -> None:
        self.path, self.clock = Path(path), clock
        with self._transaction() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS go_grants (id TEXT PRIMARY KEY, "
                "binding TEXT NOT NULL, capability_digest TEXT NOT NULL, "
                "created_at REAL NOT NULL, revoked_at REAL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS go_calls (grant_id TEXT NOT NULL, "
                "call_id TEXT NOT NULL, receipt TEXT NOT NULL, "
                "PRIMARY KEY (grant_id, call_id))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS go_expired_grants ("
                "grant_id TEXT PRIMARY KEY, observed_at REAL NOT NULL)"
            )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def _reader(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN")
            yield db
        finally:
            db.close()

    def _now(self) -> float:
        now = self.clock()
        if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
            raise GoJournalError("GO_JOURNAL_CLOCK_INVALID")
        return float(now)

    @staticmethod
    def _grant(db: sqlite3.Connection, grant_id: str) -> sqlite3.Row:
        row: sqlite3.Row | None = db.execute(
            "SELECT * FROM go_grants WHERE id=?", (grant_id,)
        ).fetchone()
        if row is None:
            raise GoJournalError("GRANT_NOT_FOUND")
        return row

    @staticmethod
    def _call(db: sqlite3.Connection, grant_id: str, call_id: str) -> dict[str, Any] | None:
        row = db.execute(
            "SELECT receipt FROM go_calls WHERE grant_id=? AND call_id=?", (grant_id, call_id)
        ).fetchone()
        return dict(json.loads(row["receipt"])) if row is not None else None

    @staticmethod
    def _authenticate(row: sqlite3.Row, binding: dict[str, Any], capability: str) -> None:
        if not isinstance(capability, str) or not capability.isascii() or len(capability) != 43:
            raise GoJournalError("INVALID_CAPABILITY")
        digest = hashlib.sha256(capability.encode()).hexdigest()
        if not hmac.compare_digest(row["capability_digest"], digest):
            raise GoJournalError("INVALID_CAPABILITY")
        if row["binding"] != _encoded(binding):
            raise GoJournalError("GRANT_BINDING_MISMATCH")

    def create_grant(self, binding: object, *, grant_id: str) -> dict[str, Any]:
        """Controller-only creation; a lost capability is deliberately unrecoverable.

        Repeating creation returns no capability and cannot reset request count.
        Controllers must not mint a replacement grant after a lost create return.
        """
        _validated(_GrantRef, {"grant_id": grant_id})
        value = _validated(_GrantBinding, binding)
        encoded = _encoded(value)
        with self._transaction() as db:
            old = db.execute("SELECT * FROM go_grants WHERE id=?", (grant_id,)).fetchone()
            if old is not None:
                if old["binding"] != encoded:
                    raise GoJournalError("GRANT_CONFLICT")
                return {"grant_id": grant_id, "binding": value, "capability": None}
            now = self._now()
            if value["expires_at"] <= now:
                raise GoJournalError("GRANT_EXPIRED")
            capability = secrets.token_urlsafe(32)
            db.execute(
                "INSERT INTO go_grants VALUES (?, ?, ?, ?, NULL)",
                (grant_id, encoded, hashlib.sha256(capability.encode()).hexdigest(), now),
            )
        return {"grant_id": grant_id, "binding": value, "capability": capability}

    def revoke_grant(self, grant_id: str) -> dict[str, Any]:
        """Controller-only revocation; existing unknown sends are never refunded."""
        _validated(_GrantRef, {"grant_id": grant_id})
        with self._transaction() as db:
            row = self._grant(db, grant_id)
            revoked_at = row["revoked_at"]
            if revoked_at is None:
                revoked_at = self._now()
                db.execute("UPDATE go_grants SET revoked_at=? WHERE id=?", (revoked_at, grant_id))
        return {"grant_id": grant_id, "revoked_at": revoked_at}

    def begin_call(
        self, grant_id: str, call_id: str, *, capability: str, binding: object
    ) -> dict[str, Any]:
        """Commit an unknown send, then grant send permission once, only in this return.

        A repeated logical call returns history and ``send_allowed=False``, even
        if the first return was lost. No new request may be sent from that replay.
        ``call_id`` must be assigned by the trusted relay, not by native input.
        An authenticated expiry rejection is committed before raising: observed
        expiry cannot be undone by a later clock rollback or process restart.
        """
        _validated(_CallRef, {"grant_id": grant_id, "call_id": call_id})
        value = _validated(_GrantBinding, binding)
        receipt = None
        with self._transaction() as db:
            grant = self._grant(db, grant_id)
            self._authenticate(grant, value, capability)
            old = self._call(db, grant_id, call_id)
            if old is not None:
                return {"send_allowed": False, "receipt": old}
            now = self._now()
            if grant["revoked_at"] is not None:
                raise GoJournalError("GRANT_REVOKED")
            expired = db.execute(
                "SELECT 1 FROM go_expired_grants WHERE grant_id=?", (grant_id,)
            ).fetchone()
            if expired is not None or value["expires_at"] <= now:
                db.execute("INSERT OR IGNORE INTO go_expired_grants VALUES (?, ?)", (grant_id, now))
            else:
                count = db.execute(
                    "SELECT COUNT(*) FROM go_calls WHERE grant_id=?", (grant_id,)
                ).fetchone()[0]
                if count >= value["max_requests"]:
                    raise GoJournalError("REQUEST_LIMIT_REACHED")
                receipt = {
                    "grant_id": grant_id,
                    "call_id": call_id,
                    "sequence": count + 1,
                    "send_intent_at": now,
                    "state": "send_unknown",
                    "completed_at": None,
                    "outcome": None,
                }
                db.execute(
                    "INSERT INTO go_calls VALUES (?, ?, ?)", (grant_id, call_id, _encoded(receipt))
                )
        if receipt is None:
            raise GoJournalError("GRANT_EXPIRED")
        return {"send_allowed": True, "receipt": receipt}

    def complete_call(
        self,
        grant_id: str,
        call_id: str,
        *,
        capability: str,
        binding: object,
        outcome: object,
    ) -> dict[str, Any]:
        """Record only relay facts; local completion never proves remote stopping.

        Existing calls can complete after grant expiry/revocation. This cannot
        refund a request or authorize any new send, including ``rejected`` facts.
        A completed observation is immutable; identical completion is a replay.
        """
        _validated(_CallRef, {"grant_id": grant_id, "call_id": call_id})
        bound = _validated(_GrantBinding, binding)
        value = _validated(_Outcome, outcome)
        value.setdefault("upstream_status", None)
        with self._transaction() as db:
            self._authenticate(self._grant(db, grant_id), bound, capability)
            receipt = self._call(db, grant_id, call_id)
            if receipt is None:
                raise GoJournalError("CALL_NOT_FOUND")
            if receipt["outcome"] is not None:
                if receipt["outcome"] != value:
                    raise GoJournalError("CALL_COMPLETION_CONFLICT")
                return receipt
            receipt.update(state=value["state"], completed_at=self._now(), outcome=value)
            db.execute(
                "UPDATE go_calls SET receipt=? WHERE grant_id=? AND call_id=?",
                (_encoded(receipt), grant_id, call_id),
            )
        return receipt

    def call_receipt(self, grant_id: str, call_id: str) -> dict[str, Any] | None:
        """Controller-only historical read, with no send permission or mutations."""
        _validated(_CallRef, {"grant_id": grant_id, "call_id": call_id})
        with self._reader() as db:
            self._grant(db, grant_id)
            return self._call(db, grant_id, call_id)

    def snapshot(self, grant_id: str) -> dict[str, Any]:
        """Controller-only coherent view, omitting even the capability digest."""
        _validated(_GrantRef, {"grant_id": grant_id})
        with self._reader() as db:
            row = self._grant(db, grant_id)
            value = json.loads(row["binding"])
            expired = db.execute(
                "SELECT 1 FROM go_expired_grants WHERE grant_id=?", (grant_id,)
            ).fetchone()
            calls = [
                json.loads(item["receipt"])
                for item in db.execute("SELECT receipt FROM go_calls WHERE grant_id=?", (grant_id,))
            ]
            return {
                "grant_id": grant_id,
                "binding": value,
                "created_at": row["created_at"],
                "revoked_at": row["revoked_at"],
                "state": (
                    "revoked"
                    if row["revoked_at"] is not None
                    else "expired"
                    if expired is not None or value["expires_at"] <= self._now()
                    else "active"
                ),
                "request_count": len(calls),
                "calls": sorted(calls, key=lambda call: call["sequence"]),
            }
