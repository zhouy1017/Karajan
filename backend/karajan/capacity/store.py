"""Shared quota transactions. Admission is not permission to launch a real model."""

import hashlib
import json
import math
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from karajan.resources.broker import money, units

from .models import (
    AdmissionRef,
    AdmissionRequest,
    Failure,
    Observation,
    Policy,
    Pool,
    Profile,
    Reconciliation,
    UsageReceipt,
)


class CapacityError(ValueError):
    """Stable boundary failure; no caller contents are echoed."""


def encoded(value: object) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError):
        raise CapacityError("CAPACITY_INPUT_INVALID") from None


def validate(model: type[BaseModel], value: object) -> dict[str, Any]:
    try:
        return model.model_validate(value).model_dump()
    except (ValidationError, ValueError, TypeError):
        raise CapacityError("CAPACITY_INPUT_INVALID") from None


class CapacityStore:
    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time) -> None:
        self.path, self.clock = Path(path), clock
        with self._transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS pools (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS profiles (id TEXT NOT NULL, revision INTEGER "
                "NOT NULL, data TEXT NOT NULL, PRIMARY KEY(id, revision))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS observations (sequence INTEGER PRIMARY KEY, "
                "pool_id TEXT NOT NULL, data TEXT NOT NULL, received_at REAL NOT NULL, "
                "applied INTEGER NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS policies (account_id TEXT NOT NULL, "
                "revision INTEGER NOT NULL, data TEXT NOT NULL, "
                "PRIMARY KEY(account_id, revision))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS reservations (id TEXT PRIMARY KEY, "
                "attempt_id TEXT NOT NULL UNIQUE, account_id TEXT NOT NULL, data TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS commands (key TEXT PRIMARY KEY, digest TEXT "
                "NOT NULL, result TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS lifecycle (sequence INTEGER PRIMARY KEY, "
                "admission_id TEXT NOT NULL, data TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS usage (id TEXT PRIMARY KEY, admission_id TEXT "
                "NOT NULL, account_id TEXT NOT NULL, data TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS failures (sequence INTEGER PRIMARY KEY, "
                "account_id TEXT NOT NULL, data TEXT NOT NULL)"
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

    def _command(
        self,
        kind: str,
        payload: object,
        command_key: str,
        action: Callable[[sqlite3.Connection], dict[str, Any]],
    ) -> dict[str, Any]:
        if (
            not isinstance(command_key, str)
            or not 0 < len(command_key) <= 256
            or not command_key.isprintable()
        ):
            raise CapacityError("CAPACITY_INPUT_INVALID")
        try:
            command_key.encode("utf-8")
        except UnicodeError:
            raise CapacityError("CAPACITY_INPUT_INVALID") from None
        digest = hashlib.sha256(encoded([kind, payload]).encode()).hexdigest()
        with self._transaction() as db:
            original = db.execute("SELECT * FROM commands WHERE key=?", (command_key,)).fetchone()
            if original is not None:
                if original["digest"] != digest:
                    raise CapacityError("IDEMPOTENCY_CONFLICT")
                return dict(json.loads(original["result"]))
            result = action(db)
            db.execute(
                "INSERT INTO commands VALUES (?, ?, ?)", (command_key, digest, encoded(result))
            )
            return result

    def register_pool(self, pool: dict[str, Any], *, command_key: str) -> dict[str, Any]:
        value = validate(Pool, pool)

        def apply(db: sqlite3.Connection) -> dict[str, Any]:
            current = db.execute("SELECT data FROM pools WHERE id=?", (value["id"],)).fetchone()
            if current is not None:
                if current[0] != encoded(value):
                    raise CapacityError("POOL_IDENTITY_CONFLICT")
            else:
                db.execute("INSERT INTO pools VALUES (?, ?)", (value["id"], encoded(value)))
            return value

        return self._command("register_pool", value, command_key, apply)

    def register_profile(self, profile: dict[str, Any], *, command_key: str) -> dict[str, Any]:
        value = validate(Profile, profile)

        def apply(db: sqlite3.Connection) -> dict[str, Any]:
            pools = [self._pool(db, identity) for identity in value["pool_ids"]]
            if any(p["account_id"] != value["account_id"] for p in pools) or not any(
                p["kind"] == "service" for p in pools
            ):
                raise CapacityError("PROFILE_POOL_MISMATCH")
            current = db.execute(
                "SELECT data FROM profiles WHERE id=? AND revision=?",
                (value["id"], value["revision"]),
            ).fetchone()
            if current is not None:
                if current[0] != encoded(value):
                    raise CapacityError("PROFILE_IDENTITY_CONFLICT")
            else:
                db.execute(
                    "INSERT INTO profiles VALUES (?, ?, ?)",
                    (value["id"], value["revision"], encoded(value)),
                )
            return value

        return self._command("register_profile", value, command_key, apply)

    @staticmethod
    def _pool(db: sqlite3.Connection, identity: str) -> dict[str, Any]:
        row = db.execute("SELECT data FROM pools WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise CapacityError("POOL_UNKNOWN")
        return dict(json.loads(row[0]))

    def _now(self) -> float:
        value = self.clock()
        if type(value) not in (int, float):
            raise CapacityError("CLOCK_UNAVAILABLE")
        try:
            if not math.isfinite(value):
                raise CapacityError("CLOCK_UNAVAILABLE")
            return float(value)
        except OverflowError:
            raise CapacityError("CLOCK_UNAVAILABLE") from None

    def observe(self, observation: dict[str, Any], *, command_key: str) -> dict[str, Any]:
        value = validate(Observation, observation)

        def apply(db: sqlite3.Connection) -> dict[str, Any]:
            pool = self._pool(db, value["pool_id"])
            now = self._now()
            if value["observed_at"] > now or (
                value["reset_at"] is not None and value["reset_at"] <= value["observed_at"]
            ):
                raise CapacityError("OBSERVATION_TIME_INVALID")
            if value["source"] == "manual" and not value["adjustment_reason"]:
                raise CapacityError("ADJUSTMENT_REASON_REQUIRED")
            if pool["kind"] == "platform_allowance" and value["source"] == "official":
                raise CapacityError("ALLOWANCE_REQUIRES_LOCAL_OBSERVATION")
            if (
                (value["metric"] == "unknown" and value["amount"] is not None)
                or (value["metric"] != "unknown" and value["amount"] is None)
                or (
                    value["metric"] == "remaining"
                    and value["limit"] is not None
                    and units(value["amount"]) > units(value["limit"])
                )
            ):
                raise CapacityError("OBSERVATION_QUANTITY_INVALID")
            if value["covered_usage_ids"]:
                if not value["coverage_ref"] or len(set(value["covered_usage_ids"])) != len(
                    value["covered_usage_ids"]
                ):
                    raise CapacityError("COVERAGE_EVIDENCE_REQUIRED")
                for identity in value["covered_usage_ids"]:
                    row = db.execute("SELECT data FROM usage WHERE id=?", (identity,)).fetchone()
                    if (
                        row is None
                        or value["pool_id"] not in json.loads(row[0])["receipt"]["amounts"]
                    ):
                        raise CapacityError("COVERAGE_USAGE_UNKNOWN")
            if pool["window_kind"] == "fixed":
                for old_row in db.execute(
                    "SELECT data FROM observations WHERE pool_id=? AND applied=1",
                    (value["pool_id"],),
                ):
                    old = json.loads(old_row[0])
                    if (
                        old["window_id"] == value["window_id"]
                        and old["reset_at"] is not None
                        and old["reset_at"] != value["reset_at"]
                    ):
                        raise CapacityError("WINDOW_IDENTITY_CONFLICT")
            previous = self._observation(db, value["pool_id"])
            applied = previous is None or value["observed_at"] > previous["observed_at"]
            if (
                previous is not None
                and previous["window_id"] != value["window_id"]
                and (previous["reset_at"] is None or value["observed_at"] < previous["reset_at"])
            ):
                applied = False
            cursor = db.execute(
                "INSERT INTO observations (pool_id,data,received_at,applied) VALUES (?, ?, ?, ?)",
                (value["pool_id"], encoded(value), now, int(applied)),
            )
            return {
                "sequence": cursor.lastrowid,
                "applied": applied,
                "observation": value,
                "received_at": now,
                "previous": previous,
                "current": value if applied else previous,
            }

        return self._command("observe", value, command_key, apply)

    @staticmethod
    def _observation(db: sqlite3.Connection, pool_id: str) -> dict[str, Any] | None:
        row = db.execute(
            "SELECT data FROM observations WHERE pool_id=? AND applied=1 "
            "ORDER BY sequence DESC LIMIT 1",
            (pool_id,),
        ).fetchone()
        return dict(json.loads(row[0])) if row is not None else None

    def activate_policy(
        self, policy: dict[str, Any], *, expected_revision: int, command_key: str
    ) -> dict[str, Any]:
        value = validate(Policy, policy)
        if type(expected_revision) is not int or expected_revision < 0:
            raise CapacityError("CAPACITY_INPUT_INVALID")

        def apply(db: sqlite3.Connection) -> dict[str, Any]:
            current = db.execute(
                "SELECT COALESCE(MAX(revision),0) FROM policies WHERE account_id=?",
                (value["account_id"],),
            ).fetchone()[0]
            if current != expected_revision:
                raise CapacityError("CAPACITY_POLICY_STALE")
            if value["lead_reserved_slots"] > value["max_active_attempts"]:
                raise CapacityError("CAPACITY_POLICY_INVALID")
            for identity in set(value["safety_margin"]) | set(value["lead_reserve"]):
                if self._pool(db, identity)["account_id"] != value["account_id"]:
                    raise CapacityError("CAPACITY_POLICY_INVALID")
            revision = current + 1
            db.execute(
                "INSERT INTO policies VALUES (?, ?, ?)",
                (value["account_id"], revision, encoded(value)),
            )
            return {"revision": revision, "policy": value}

        return self._command("activate_policy", [value, expected_revision], command_key, apply)

    def admit(self, request: dict[str, Any], *, command_key: str) -> dict[str, Any]:
        value = validate(AdmissionRequest, request)

        def apply(db: sqlite3.Connection) -> dict[str, Any]:
            if (
                db.execute(
                    "SELECT 1 FROM reservations WHERE attempt_id=?", (value["attempt_id"],)
                ).fetchone()
                is not None
            ):
                raise CapacityError("ATTEMPT_ALREADY_RESERVED")
            profile_row = db.execute(
                "SELECT data FROM profiles WHERE id=? AND revision=?",
                (value["profile_id"], value["profile_revision"]),
            ).fetchone()
            if profile_row is None:
                raise CapacityError("PROFILE_UNKNOWN")
            profile = json.loads(profile_row[0])
            policy_row = db.execute(
                "SELECT revision,data FROM policies WHERE account_id=? "
                "ORDER BY revision DESC LIMIT 1",
                (profile["account_id"],),
            ).fetchone()
            if policy_row is None:
                raise CapacityError("CAPACITY_POLICY_REQUIRED")
            policy = json.loads(policy_row["data"])
            now = self._now()
            held = self._held(db, profile["account_id"], now)
            reasons, observations, availability = self._evaluate(
                db, value, profile, policy, held, now
            )
            decision: dict[str, Any] = {
                "decision": "rejected" if reasons else "admitted",
                "reason_codes": reasons,
                "policy_revision": policy_row["revision"],
                "request": value,
                "observations": observations,
                "available_before": availability,
                "admission_id": None,
                "profile_enabled": False,
                "activation_allowed": False,
                "live_qualification": "not_run",
            }
            if not reasons:
                identity = str(uuid4())
                reservation = {
                    "id": identity,
                    "request": value,
                    "state": "reserved",
                    "account_id": profile["account_id"],
                    "created_at": now,
                    "expires_at": now + value["duration_seconds"],
                    "policy_revision": policy_row["revision"],
                    "observations": observations,
                }
                db.execute(
                    "INSERT INTO reservations VALUES (?, ?, ?, ?)",
                    (identity, value["attempt_id"], profile["account_id"], encoded(reservation)),
                )
                decision["admission_id"] = identity
            return decision

        return self._command("admit", value, command_key, apply)

    @staticmethod
    def _reservation(db: sqlite3.Connection, identity: str) -> dict[str, Any]:
        row = db.execute("SELECT data FROM reservations WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise CapacityError("ADMISSION_UNKNOWN")
        return dict(json.loads(row[0]))

    @staticmethod
    def _save(db: sqlite3.Connection, reservation: dict[str, Any], event: dict[str, Any]) -> None:
        db.execute(
            "UPDATE reservations SET data=? WHERE id=?", (encoded(reservation), reservation["id"])
        )
        db.execute(
            "INSERT INTO lifecycle (admission_id,data) VALUES (?,?)",
            (reservation["id"], encoded(event)),
        )

    def _held(self, db: sqlite3.Connection, account_id: str, now: float) -> list[dict[str, Any]]:
        reservations = [
            json.loads(row[0])
            for row in db.execute("SELECT data FROM reservations WHERE account_id=?", (account_id,))
        ]
        held: list[dict[str, Any]] = []
        for item in reservations:
            if item["state"] == "reserved" and item["expires_at"] <= now:
                item["state"] = "expired"
                self._save(db, item, {"kind": "unsent_reservation_expired", "at": now})
            if item["state"] in ("reserved", "active", "unknown"):
                held.append(item)
        return held

    def activate(self, admission_id: str, *, command_key: str) -> dict[str, Any]:
        """Persist pre-effect intent and recheck capacity; grants no F05 execution permission."""
        value = validate(AdmissionRef, {"admission_id": admission_id})

        def apply(db: sqlite3.Connection) -> dict[str, Any]:
            item = self._reservation(db, admission_id)
            now = self._now()
            held = self._held(db, item["account_id"], now)
            item = self._reservation(db, admission_id)
            if item["state"] == "expired":
                return {
                    "decision": "rejected",
                    "reason_codes": ["RESERVATION_EXPIRED"],
                    "admission_id": admission_id,
                    "activation_allowed": False,
                }
            if item["state"] != "reserved":
                raise CapacityError("ACTIVATION_ALREADY_RECORDED")
            row = db.execute(
                "SELECT revision,data FROM policies WHERE account_id=? "
                "ORDER BY revision DESC LIMIT 1",
                (item["account_id"],),
            ).fetchone()
            profile = json.loads(
                db.execute(
                    "SELECT data FROM profiles WHERE id=? AND revision=?",
                    (item["request"]["profile_id"], item["request"]["profile_revision"]),
                ).fetchone()[0]
            )
            reasons, observations, availability = self._evaluate(
                db,
                item["request"],
                profile,
                json.loads(row["data"]),
                [other for other in held if other["id"] != admission_id],
                now,
            )
            result = {
                "decision": "rejected" if reasons else "capacity_revalidated",
                "reason_codes": reasons,
                "policy_revision": row["revision"],
                "admission_id": admission_id,
                "expires_at": item["expires_at"],
                "observations": observations,
                "available_before": availability,
                "activation_allowed": False,
                "live_qualification": "not_run",
            }
            if not reasons:
                item.update(
                    state="active", activation_at=now, activation_policy_revision=row["revision"]
                )
                self._save(db, item, {"kind": "activation_intent", "at": now, "result": result})
            return result

        return self._command("activate", value, command_key, apply)

    def reconcile(
        self,
        admission_id: str,
        *,
        local_ended: bool,
        remote_ended: bool,
        usage_complete: bool,
        not_sent: bool,
        evidence_ref: str,
        command_key: str,
    ) -> dict[str, Any]:
        value = validate(
            Reconciliation,
            {
                "admission_id": admission_id,
                "local_ended": local_ended,
                "remote_ended": remote_ended,
                "usage_complete": usage_complete,
                "not_sent": not_sent,
                "evidence_ref": evidence_ref,
            },
        )

        def apply(db: sqlite3.Connection) -> dict[str, Any]:
            item = self._reservation(db, admission_id)
            if item["state"] in ("ended", "released", "expired"):
                raise CapacityError("ADMISSION_ALREADY_RECONCILED")
            if not_sent:
                if db.execute(
                    "SELECT 1 FROM usage WHERE admission_id=?", (admission_id,)
                ).fetchone():
                    raise CapacityError("UNSENT_CONFLICTS_WITH_USAGE")
                if not (local_ended and remote_ended):
                    raise CapacityError("RECONCILIATION_INCOMPLETE")
                state = "released"
            else:
                if item["state"] == "reserved":
                    raise CapacityError("ACTIVATION_REQUIRED")
                if (
                    usage_complete
                    and not db.execute(
                        "SELECT 1 FROM usage WHERE admission_id=?", (admission_id,)
                    ).fetchone()
                ):
                    raise CapacityError("FINAL_USAGE_REQUIRED")
                state = "ended" if local_ended and remote_ended and usage_complete else "unknown"
            item.update(state=state, reconciliation=value)
            self._save(db, item, {"kind": "reconciled", "at": self._now(), "evidence": value})
            return {"admission_id": admission_id, "state": state, "activation_allowed": False}

        return self._command("reconcile", value, command_key, apply)

    def record_usage(self, receipt: dict[str, Any], *, command_key: str) -> dict[str, Any]:
        """Append a uniquely identified delta, including actual values above an estimate."""
        value = validate(UsageReceipt, receipt)

        def apply(db: sqlite3.Connection) -> dict[str, Any]:
            old = db.execute("SELECT data FROM usage WHERE id=?", (value["id"],)).fetchone()
            if old is not None:
                saved = dict(json.loads(old[0]))
                if saved["receipt"] != value:
                    raise CapacityError("USAGE_IDENTITY_CONFLICT")
                return saved
            item = self._reservation(db, value["admission_id"])
            if item["state"] not in ("active", "unknown", "ended"):
                raise CapacityError("ACTIVATION_REQUIRED")
            pools = set(item["request"]["demand"])
            if set(value["amounts"]) != pools or set(value["window_ids"]) != pools:
                raise CapacityError("POOL_VECTOR_MISMATCH")
            if any(value["window_ids"].values()) and not value["attribution_ref"]:
                raise CapacityError("WINDOW_ATTRIBUTION_REQUIRED")
            resets: dict[str, float | None] = {}
            for pool_id, window_id in value["window_ids"].items():
                if window_id is None:
                    resets[pool_id] = None
                    continue
                matches = [
                    json.loads(row[0])
                    for row in db.execute(
                        "SELECT data FROM observations WHERE pool_id=? AND applied=1 "
                        "ORDER BY sequence",
                        (pool_id,),
                    )
                    if json.loads(row[0])["window_id"] == window_id
                ]
                if not matches:
                    raise CapacityError("WINDOW_UNKNOWN")
                resets[pool_id] = matches[-1]["reset_at"]
            all_usage = self._usage(db, item["account_id"])
            over = [
                p
                for p in sorted(pools)
                if units(value["amounts"][p])
                + sum(
                    units(u["receipt"]["amounts"][p])
                    for u in all_usage
                    if u["receipt"]["admission_id"] == item["id"]
                )
                > units(item["request"]["demand"][p])
            ]
            result = {
                "receipt": value,
                "recorded_at": self._now(),
                "window_resets": resets,
                "over_estimate_pools": over,
            }
            db.execute(
                "INSERT INTO usage VALUES (?,?,?,?)",
                (value["id"], item["id"], item["account_id"], encoded(result)),
            )
            return result

        return self._command("record_usage", value, command_key, apply)

    @staticmethod
    def _usage(db: sqlite3.Connection, account_id: str) -> list[dict[str, Any]]:
        return [
            json.loads(row[0])
            for row in db.execute(
                "SELECT data FROM usage WHERE account_id=? ORDER BY id", (account_id,)
            )
        ]

    def _pool_usage(
        self,
        db: sqlite3.Connection,
        pool_id: str,
        observed: dict[str, Any],
        held: list[dict[str, Any]],
    ) -> tuple[int, int]:
        pool = self._pool(db, pool_id)
        all_usage = self._usage(db, pool["account_id"])
        uncovered = 0
        for item in all_usage:
            receipt = item["receipt"]
            if pool_id not in receipt["amounts"] or receipt["id"] in observed["covered_usage_ids"]:
                continue
            window = receipt["window_ids"][pool_id]
            reset = item["window_resets"][pool_id]
            if (
                pool["window_kind"] == "fixed"
                and window is not None
                and window != observed["window_id"]
                and reset is not None
                and reset <= observed["observed_at"]
            ):
                continue
            uncovered += units(receipt["amounts"][pool_id])
        future = sum(
            max(
                0,
                units(item["request"]["demand"].get(pool_id, "0"))
                - sum(
                    units(u["receipt"]["amounts"].get(pool_id, "0"))
                    for u in all_usage
                    if u["receipt"]["admission_id"] == item["id"]
                ),
            )
            for item in held
        )
        return uncovered, future

    def record_failure(
        self,
        account_id: str,
        *,
        reason: str,
        retry_after_seconds: int,
        evidence_ref: str,
        command_key: str,
    ) -> dict[str, Any]:
        value = validate(
            Failure,
            {
                "account_id": account_id,
                "reason": reason,
                "retry_after_seconds": retry_after_seconds,
                "evidence_ref": evidence_ref,
            },
        )

        def apply(db: sqlite3.Connection) -> dict[str, Any]:
            row = db.execute(
                "SELECT data FROM policies WHERE account_id=? ORDER BY revision DESC LIMIT 1",
                (account_id,),
            ).fetchone()
            if row is None:
                raise CapacityError("CAPACITY_POLICY_REQUIRED")
            mode = json.loads(row[0])["conservative_mode"] or {}
            cooldown = mode.get("cooldown_seconds") or 0
            now = self._now()
            result = {
                "failure": value,
                "at": now,
                "until": now + max(cooldown, retry_after_seconds),
            }
            db.execute(
                "INSERT INTO failures (account_id,data) VALUES (?,?)", (account_id, encoded(result))
            )
            return result

        return self._command("record_failure", value, command_key, apply)

    @staticmethod
    def _conservative(
        request: dict[str, Any],
        policy: dict[str, Any],
        observed: dict[str, Any],
        held: list[dict[str, Any]],
        now: float,
        lead: bool,
    ) -> list[str]:
        if policy["require_official_observation"]:
            return ["OFFICIAL_QUOTA_REQUIRED"]
        mode = policy["conservative_mode"]
        if not mode or not mode["enabled"]:
            return ["QUOTA_UNKNOWN"]
        if any(
            mode[key] is None
            for key in (
                "max_local_active_attempts",
                "max_attempt_duration_seconds",
                "observation_max_age_seconds",
                "cooldown_seconds",
            )
        ):
            return ["CONSERVATIVE_MODE_INCOMPLETE"]
        reasons = []
        slots = mode["max_local_active_attempts"] - (0 if lead else policy["lead_reserved_slots"])
        if len(held) >= slots:
            reasons.append("CONSERVATIVE_CONCURRENCY_EXHAUSTED")
        if request["duration_seconds"] > mode["max_attempt_duration_seconds"]:
            reasons.append("CONSERVATIVE_DURATION_EXCEEDED")
        if now - observed["observed_at"] > mode["observation_max_age_seconds"]:
            reasons.append("CONSERVATIVE_OBSERVATION_STALE")
        return reasons

    @staticmethod
    def _numeric_remaining(observed: dict[str, Any]) -> int | None:
        if observed["metric"] == "remaining":
            return units(observed["amount"])
        if observed["metric"] == "used" and observed["limit"] is not None:
            return units(observed["limit"]) - units(observed["amount"])
        return None

    def _exhausted(
        self,
        db: sqlite3.Connection,
        pool: dict[str, Any],
        observed: dict[str, Any],
        failures: list[dict[str, Any]],
    ) -> bool:
        numeric = [
            json.loads(row[0])
            for row in db.execute(
                "SELECT data FROM observations WHERE pool_id=? AND applied=1 "
                "ORDER BY sequence DESC",
                (pool["id"],),
            )
            if self._numeric_remaining(json.loads(row[0])) is not None
        ]
        latest = numeric[0] if numeric else None
        remaining = self._numeric_remaining(latest) if latest is not None else None
        for failure in failures:
            if pool["kind"] == "service" and failure["failure"]["reason"] == "QUOTA_EXHAUSTED":
                if (
                    latest is None
                    or latest["observed_at"] <= failure["at"]
                    or remaining is None
                    or remaining <= 0
                ):
                    return True
        if latest is not None and remaining is not None and remaining <= 0:
            reset_proved = (
                pool["window_kind"] == "fixed"
                and observed["window_id"] != latest["window_id"]
                and latest["reset_at"] is not None
                and observed["observed_at"] >= latest["reset_at"]
            )
            return not reset_proved
        return False

    def _evaluate(
        self,
        db: sqlite3.Connection,
        request: dict[str, Any],
        profile: dict[str, Any],
        policy: dict[str, Any],
        held: list[dict[str, Any]],
        now: float,
    ) -> tuple[list[str], dict[str, Any], dict[str, str]]:
        if set(request["demand"]) != set(profile["pool_ids"]):
            return ["POOL_VECTOR_MISMATCH"], {}, {}
        reasons: list[str] = []
        observations: dict[str, Any] = {}
        available: dict[str, str] = {}
        lead = request["role"] == "commander" and request["purpose"] == "lead"
        slots = policy["max_active_attempts"] - (0 if lead else policy["lead_reserved_slots"])
        if len(held) >= slots:
            reasons.append("ACCOUNT_CONCURRENCY_EXHAUSTED")
        if request["duration_seconds"] > policy["max_attempt_duration_seconds"]:
            reasons.append("ATTEMPT_DURATION_UNBOUNDED")
        failures = [
            json.loads(row[0])
            for row in db.execute(
                "SELECT data FROM failures WHERE account_id=?", (profile["account_id"],)
            )
        ]
        if any(item["until"] > now for item in failures):
            reasons.append("ACCOUNT_COOLDOWN")
        for pool_id in sorted(profile["pool_ids"]):
            pool = self._pool(db, pool_id)
            observed = self._observation(db, pool_id)
            observations[pool_id] = observed
            if (
                observed is None
                or observed["observed_at"] > now
                or now - observed["observed_at"] > policy["observation_max_age_seconds"]
                or (observed["reset_at"] is not None and observed["reset_at"] <= now)
            ):
                reasons.append("OBSERVATION_STALE:" + pool_id)
                continue
            if (
                pool["kind"] == "service"
                and policy["require_official_observation"]
                and observed["source"] != "official"
            ):
                reasons.append("OFFICIAL_OBSERVATION_REQUIRED:" + pool_id)
                continue
            if (
                self._numeric_remaining(observed) is None
                and self._exhausted(db, pool, observed, failures)
            ) or any(
                pool["kind"] == "service"
                and failure["failure"]["reason"] == "QUOTA_EXHAUSTED"
                and failure["at"] >= observed["observed_at"]
                for failure in failures
            ):
                reasons.append("EXHAUSTION_REQUIRES_NEW_OBSERVATION:" + pool_id)
                continue
            if (
                observed["metric"] == "unknown"
                or observed["amount"] is None
                or (observed["metric"] == "used" and observed["limit"] is None)
            ):
                reasons.extend(
                    reason + ":" + pool_id
                    for reason in self._conservative(request, policy, observed, held, now, lead)
                )
                available[pool_id] = "unknown"
                continue
            amount = units(observed["amount"])
            if observed["metric"] == "used":
                if observed["limit"] is None:
                    reasons.append("QUOTA_UNKNOWN:" + pool_id)
                    continue
                amount = units(observed["limit"]) - amount
            uncovered, future = self._pool_usage(db, pool_id, observed, held)
            quantity = amount - uncovered - future
            quantity -= units(policy["safety_margin"].get(pool_id, "0"))
            if not lead:
                quantity -= units(policy["lead_reserve"].get(pool_id, "0"))
            available[pool_id] = money(quantity)
            if units(request["demand"][pool_id]) > quantity:
                reasons.append("QUOTA_INSUFFICIENT:" + pool_id)
        return reasons, observations, available

    def snapshot(self) -> dict[str, Any]:
        with self._transaction() as db:
            return {
                "pools": [
                    json.loads(row[0]) for row in db.execute("SELECT data FROM pools ORDER BY id")
                ],
                "profiles": [
                    json.loads(row[0])
                    for row in db.execute("SELECT data FROM profiles ORDER BY id,revision")
                ],
                "reservations": [
                    json.loads(row[0])
                    for row in db.execute("SELECT data FROM reservations ORDER BY id")
                ],
                "policies": [
                    {"revision": row[0], "policy": json.loads(row[1])}
                    for row in db.execute(
                        "SELECT revision,data FROM policies ORDER BY account_id,revision"
                    )
                ],
                "observations": [
                    {
                        "sequence": row[0],
                        "observation": json.loads(row[1]),
                        "received_at": row[2],
                        "applied": bool(row[3]),
                    }
                    for row in db.execute(
                        "SELECT sequence,data,received_at,applied "
                        "FROM observations ORDER BY sequence"
                    )
                ],
                "lifecycle": [
                    json.loads(row[0])
                    for row in db.execute("SELECT data FROM lifecycle ORDER BY sequence")
                ],
                "usage": [
                    json.loads(row[0]) for row in db.execute("SELECT data FROM usage ORDER BY id")
                ],
                "failures": [
                    json.loads(row[0])
                    for row in db.execute("SELECT data FROM failures ORDER BY sequence")
                ],
                "live_qualification": "not_run",
                "activation_allowed": False,
            }
