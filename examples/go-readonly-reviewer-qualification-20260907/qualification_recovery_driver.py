# ruff: noqa: E501
"""Persistent, no-new-start recovery driver for Issue 107 qualification facts.

This operator boundary deliberately has no qualification or Go-suite import.  It
can only observe an existing command/start/record and drive the already prepared
membership-only consumer while the original record remains current.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol, cast

ROOT = Path(__file__).resolve().parents[2]
_SQLITE_CONNECT = sqlite3.connect
COMMAND = "issue107-official-go-reviewer-20260907-ordered-attempt4"
PRINCIPAL = "issue107-controller"
SAFE_CODES = {
    "QUALIFICATION_EXPIRED",
    "QUALIFICATION_REVOKED",
    "QUALIFICATION_START_NOT_FOUND",
    "QUALIFICATION_UNKNOWN",
    "RUNTIME_TOOLS_NOT_QUALIFIED",
    "RESUME_RECEIPT_CONFLICT",
    "MEMBERSHIP_HISTORY_UNAVAILABLE",
    "UNCLASSIFIED",
}
_LOCAL_LOCKS: dict[str, threading.RLock] = {}
_LOCAL_LOCKS_GUARD = threading.Lock()


def _posix_lock(descriptor: int, mode: str) -> None:
    import fcntl

    flag = fcntl.LOCK_EX if mode == "exclusive" else fcntl.LOCK_UN  # type: ignore[attr-defined]
    fcntl.flock(descriptor, flag)  # type: ignore[attr-defined]


class RecoveryError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class QualificationReader(Protocol):
    def get_command_start(
        self, project_id: str, command_key: str, *, principal: str
    ) -> dict[str, Any]: ...

    def get(self, project_id: str, observation_id: str, *, principal: str) -> dict[str, Any]: ...

    def revoke(
        self, project_id: str, observation_id: str, *, principal: str, reason: str
    ) -> dict[str, Any]: ...


class QualificationWriter(QualificationReader, Protocol):
    def qualify_runtime_tools(
        self,
        project_id: str,
        profile_ref: dict[str, Any],
        *,
        principal: str,
        command_key: str,
        suite_ref: dict[str, Any],
        validity_seconds: int,
    ) -> dict[str, Any]: ...


def _sha(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _safe_error(error: BaseException) -> str:
    code = getattr(error, "code", None)
    return code if code in SAFE_CODES else "UNCLASSIFIED"


def _summary(value: dict[str, Any]) -> dict[str, Any]:
    """Export only stable identifiers, status, reasons and digests."""
    return {
        "sha256": _sha(value),
        "id_sha256": hashlib.sha256(str(value.get("id", "")).encode()).hexdigest(),
        "status": value.get("status"),
        "reason_codes": value.get("reason_codes", []),
        "expires_at": value.get("expires_at"),
        "revoked": bool(value.get("revocation")),
    }


def _expiry(start: dict[str, Any], record: dict[str, Any]) -> float:
    """A completed qualification uses its validity, not its suite deadline."""
    if isinstance(record.get("valid_until"), (int, float)):
        return float(record["valid_until"])
    execution_start = start.get("binding", {}).get("execution_start", {})
    value = execution_start.get("expires_at", start.get("expires_at", record.get("valid_until", 0)))
    return float(value)


def _qualification_ref(record: dict[str, Any]) -> str:
    prefix = "fixed-go-qualification"
    if record.get("qualification_scope") == "readonly_reviewer_tools":
        prefix = "readonly-go-reviewer-qualification"
    return prefix + ":" + str(record["id"])


def _identity(start: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    """The immutable qualification identity which owns this consumer effect."""
    execution = start.get("binding", {}).get("execution_start", {})
    immutable_record = {key: value for key, value in record.items() if key != "revocation"}
    return {
        "command": COMMAND,
        "record_id": record.get("id"),
        "qualification_ref": _qualification_ref(record),
        "start_sha256": _sha(start),
        "record_sha256": _sha(immutable_record),
        "execution_start_sha256": _sha(execution) if isinstance(execution, dict) else None,
    }


def _matching_history(
    value: dict[str, Any] | None,
    record: dict[str, Any],
    identity: dict[str, Any],
    *,
    strict: bool,
) -> bool:
    """Accept only a receipt explicitly bound to this original command identity."""
    if not isinstance(value, dict):
        return False
    if not strict:
        return value.get("membership_only") is True or value.get("state") == "ready"
    if value.get("qualification_ref") != _qualification_ref(record):
        return False
    observed = value.get("qualification_identity")
    return isinstance(observed, dict) and observed == identity and value.get("membership_only") is True


def _record_view(value: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Preserve the Store's separate, immutable record and revocation facts."""
    record = value.get("record")
    if not isinstance(record, dict):
        raise RecoveryError("QUALIFICATION_UNKNOWN")
    revocation = value.get("revocation", record.get("revocation"))
    return record, revocation if isinstance(revocation, dict) else None


def _qualification_id(start: dict[str, Any]) -> str:
    """Read the original durable identity from either public Store view."""
    execution_start = start.get("binding", {}).get("execution_start", {})
    value = start.get("qualification_id", start.get("id", execution_start.get("qualification_id")))
    if not isinstance(value, str) or not value:
        raise RecoveryError("QUALIFICATION_UNKNOWN")
    return value


class ReceiptLedger:
    """Append-only atomic stage receipts; a stage can only be replayed exactly."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    @property
    def database(self) -> Path:
        return self.directory / "receipts.sqlite"

    def _connection(self) -> sqlite3.Connection:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        database = _SQLITE_CONNECT(self.database, isolation_level=None)
        database.execute("PRAGMA synchronous=FULL")
        database.execute(
            "CREATE TABLE IF NOT EXISTS stages (stage TEXT PRIMARY KEY, receipt TEXT NOT NULL)"
        )
        return database

    def _legacy(self, stage: str) -> dict[str, Any] | None:
        path = self.directory / f"{stage}.json"
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raise RecoveryError("RESUME_RECEIPT_CONFLICT") from None
        if not isinstance(value, dict) or value.get("stage") != stage:
            raise RecoveryError("RESUME_RECEIPT_CONFLICT")
        return value

    @staticmethod
    def _decoded(value: object) -> dict[str, Any]:
        try:
            decoded = json.loads(cast(str, value))
        except (TypeError, json.JSONDecodeError):
            raise RecoveryError("RESUME_RECEIPT_CONFLICT") from None
        if not isinstance(decoded, dict):
            raise RecoveryError("RESUME_RECEIPT_CONFLICT")
        return decoded

    def _import_legacy(self, database: sqlite3.Connection, stage: str) -> dict[str, Any] | None:
        """Make the original JSON receipt authoritative before any new SQLite fact."""
        legacy = self._legacy(stage)
        row = database.execute("SELECT receipt FROM stages WHERE stage=?", (stage,)).fetchone()
        current = None if row is None else self._decoded(row[0])
        if legacy is None:
            return current
        if current is None:
            database.execute(
                "INSERT INTO stages VALUES (?,?)",
                (stage, json.dumps(legacy, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),
            )
            return legacy
        if current != legacy:
            raise RecoveryError("RESUME_RECEIPT_CONFLICT")
        return legacy

    @contextmanager
    def recovery_lock(self) -> Any:
        """Serialize the complete decide/claim/effect/reconcile transaction across processes."""
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        key = str(self.directory.resolve())
        with _LOCAL_LOCKS_GUARD:
            thread_lock = _LOCAL_LOCKS.setdefault(key, threading.RLock())
        with thread_lock:
            lock_path = self.directory / ".recovery.lock"
            descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b"0")
                os.lseek(descriptor, 0, os.SEEK_SET)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
                else:
                    _posix_lock(descriptor, "exclusive")
                yield
            finally:
                try:
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                    else:
                        _posix_lock(descriptor, "unlock")
                finally:
                    os.close(descriptor)

    def read(self, stage: str) -> dict[str, Any] | None:
        database = self._connection()
        try:
            database.execute("BEGIN IMMEDIATE")
            value = self._import_legacy(database, stage)
            database.commit()
            return value
        except BaseException:
            database.rollback()
            raise
        finally:
            database.close()

    def _write(self, stage: str, receipt: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        value = {"schema_version": "karajan.issue107-recovery-stage.v1", "stage": stage, **receipt}
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        database = self._connection()
        try:
            database.execute("BEGIN IMMEDIATE")
            previous = self._import_legacy(database, stage)
            if previous is not None:
                if previous != value:
                    raise RecoveryError("RESUME_RECEIPT_CONFLICT")
                database.commit()
                return previous, False
            database.execute("INSERT INTO stages VALUES (?,?)", (stage, encoded))
            database.commit()
        except BaseException:
            database.rollback()
            raise
        finally:
            database.close()
        self._publish(stage, encoded)
        return value, True

    def _publish(self, stage: str, encoded: str) -> None:
        path = self.directory / f"{stage}.json"
        if path.exists():
            return
        descriptor, temporary = tempfile.mkstemp(prefix=f".{stage}.", dir=self.directory)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(encoded)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, path)
            os.unlink(temporary)
        except OSError:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise RecoveryError("UNCLASSIFIED") from None

    def write(self, stage: str, receipt: dict[str, Any]) -> dict[str, Any]:
        return self._write(stage, receipt)[0]

    def claim(self, stage: str, receipt: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        return self._write(stage, receipt)


class QualificationRecovery:
    def __init__(
        self,
        ledger: ReceiptLedger,
        store: QualificationReader,
        *,
        project_id: str,
        positive_history: Callable[..., dict[str, Any] | None],
        positive: Callable[..., dict[str, Any]],
        negative: Callable[[], dict[str, Any]],
        now: Callable[[], float] = time.time,
    ) -> None:
        self.ledger, self.store, self.project_id = ledger, store, project_id
        self.positive_history, self.positive, self.negative, self.now = (
            positive_history,
            positive,
            negative,
            now,
        )

    def preflight(self) -> dict[str, Any]:
        """Record interpreter/tree readiness before any future qualification effect."""
        try:
            import pydantic  # noqa: F401
        except ImportError:
            return self.ledger.write(
                "preflight", {"status": "failed", "reason_code": "UNCLASSIFIED"}
            )
        return self.ledger.write(
            "preflight",
            {
                "status": "passed",
                "python_sha256": hashlib.sha256(
                    str(Path(sys.executable).resolve()).encode()
                ).hexdigest(),
                "product_tree_sha256": hashlib.sha256(str(ROOT.resolve()).encode()).hexdigest(),
            },
        )

    def resume(self) -> dict[str, Any]:
        """Recover the original command only. This method never creates a start."""
        with self.ledger.recovery_lock():
            return self._resume_locked()

    def _history(self, identity: dict[str, Any]) -> tuple[dict[str, Any] | None, Exception | None]:
        try:
            if inspect.signature(self.positive_history).parameters:
                return self.positive_history(identity), None
            return self.positive_history(), None
        except Exception as error:
            return None, error

    def _resume_locked(self) -> dict[str, Any]:
        self.preflight()
        try:
            start = self.store.get_command_start(self.project_id, COMMAND, principal=PRINCIPAL)
            record_id = _qualification_id(start)
            record, revocation = _record_view(
                self.store.get(self.project_id, record_id, principal=PRINCIPAL)
            )
        except Exception as error:
            return self.ledger.write(
                "start_observed", {"status": "unknown", "reason_code": _safe_error(error)}
            )
        identity = _identity(start, record)
        self.ledger.write("start_observed", {"status": "observed", "start": _summary(start)})
        self.ledger.write(
            "qualification_observed", {"status": "observed", "record": _summary(record)}
        )
        positive_stage = self.ledger.read("positive_observed")
        positive_reconciled = self.ledger.read("positive_reconciled")
        claim = self.ledger.read("positive_claimed")
        if positive_stage is None or positive_stage.get("status") == "unknown":
            historical_positive, history_error = self._history(identity)
            strict_history = bool(inspect.signature(self.positive_history).parameters)
            if _matching_history(historical_positive, record, identity, strict=strict_history):
                target = "positive_observed" if positive_stage is None else "positive_reconciled"
                recovered = self.ledger.write(
                    target,
                    {
                        "status": "recovered",
                        "receipt_sha256": _sha(cast(dict[str, Any], historical_positive)),
                        "qualification_identity": identity,
                    },
                )
                if target == "positive_observed":
                    positive_stage = recovered
                else:
                    positive_reconciled = recovered
            elif history_error is not None:
                return self.ledger.write(
                    "positive_observed",
                    {"status": "unknown", "reason_code": _safe_error(history_error)},
                )
            elif claim is not None or (positive_stage is not None and positive_stage.get("status") == "unknown"):
                # A committed claim or an ambiguous reply is irrevocable: only original
                # membership history may resolve it. Never infer a missing effect.
                return positive_stage or self.ledger.write(
                    "positive_observed", {"status": "unknown", "reason_code": "UNCLASSIFIED"}
                )
        positive_ready = (
            positive_stage is not None and positive_stage.get("status") in {"passed", "recovered"}
        ) or (positive_reconciled is not None and positive_reconciled.get("status") == "recovered")
        if not positive_ready:
            if revocation:
                return self.ledger.write(
                    "complete", {"status": "revoked", "reason_code": "QUALIFICATION_REVOKED"}
                )
            if _expiry(start, record) <= self.now():
                return self.ledger.write(
                    "complete", {"status": "expired", "reason_code": "QUALIFICATION_EXPIRED"}
                )
            if record.get("status") != "passed":
                return self.ledger.write(
                    "complete", {"status": "unknown", "reason_code": "QUALIFICATION_UNKNOWN"}
                )
            _claim, new_claim = self.ledger.claim(
                "positive_claimed", {"status": "claimed", "qualification_identity": identity}
            )
            if not new_claim:
                return self.ledger.write(
                    "positive_observed", {"status": "unknown", "reason_code": "UNCLASSIFIED"}
                )
            try:
                positive = self.positive(identity) if inspect.signature(self.positive).parameters else self.positive()
            except Exception as error:
                return self.ledger.write(
                    "positive_observed", {"status": "unknown", "reason_code": _safe_error(error)}
                )
            self.ledger.write(
                "positive_observed",
                {
                    "status": "passed",
                    "receipt_sha256": _sha(positive),
                    "qualification_identity": identity,
                },
            )
        if _expiry(start, record) <= self.now() and not revocation:
            return self.ledger.write(
                "complete", {"status": "expired", "reason_code": "QUALIFICATION_EXPIRED"}
            )
        revoked_stage = self.ledger.read("revoked_observed")
        if revoked_stage is None:
            try:
                record, revocation = _record_view(
                    self.store.get(self.project_id, record_id, principal=PRINCIPAL)
                )
            except Exception as error:
                return self.ledger.write(
                    "revoked_observed", {"status": "unknown", "reason_code": _safe_error(error)}
                )
            if revocation:
                revoked_stage = self.ledger.write(
                    "revoked_observed", {"status": "recovered", "receipt_sha256": _sha(revocation)}
                )
        if revoked_stage is None:
            try:
                revoked = self.store.revoke(
                    self.project_id, record_id, principal=PRINCIPAL, reason="issue107-driver-post-positive"
                )
            except Exception as error:
                try:
                    _record, recovered_revocation = _record_view(
                        self.store.get(self.project_id, record_id, principal=PRINCIPAL)
                    )
                except Exception:
                    recovered_revocation = None
                if recovered_revocation:
                    self.ledger.write(
                        "revoked_observed",
                        {"status": "recovered", "receipt_sha256": _sha(recovered_revocation)},
                    )
                else:
                    return self.ledger.write(
                        "revoked_observed", {"status": "unknown", "reason_code": _safe_error(error)}
                    )
            else:
                self.ledger.write(
                    "revoked_observed", {"status": "observed", "receipt_sha256": _sha(revoked)}
                )
        elif revoked_stage.get("status") not in {"observed", "recovered"}:
            return revoked_stage
        negative_stage = self.ledger.read("negative_history_observed")
        if negative_stage is None:
            try:
                negative = self.negative()
            except Exception as error:
                return self.ledger.write(
                    "negative_history_observed", {"status": "unknown", "reason_code": _safe_error(error)}
                )
            if not isinstance(negative, dict):
                return self.ledger.write(
                    "negative_history_observed", {"status": "unknown", "reason_code": "QUALIFICATION_REVOKED"}
                )
            result = negative.get("result")
            reasons = result.get("reason_codes") if isinstance(result, dict) else None
            issues = negative.get("qualification_issues")
            revoked_issue = isinstance(issues, list) and any(
                isinstance(row, dict) and row.get("reason_code") == "QUALIFICATION_REVOKED" for row in issues
            )
            if (
                negative.get("expected_revoke_reason_observed") is not True
                or not isinstance(result, dict)
                or result.get("state") not in {"blocked", "rejected"}
                or not isinstance(reasons, list)
                or "QUALIFICATION_REVOKED" not in reasons
                or not revoked_issue
            ):
                return self.ledger.write(
                    "negative_history_observed", {"status": "unknown", "reason_code": "QUALIFICATION_REVOKED"}
                )
            self.ledger.write(
                "negative_history_observed", {"status": "observed", "receipt_sha256": _sha(negative)}
            )
        elif negative_stage.get("status") != "observed":
            return negative_stage
        return self.ledger.write("complete", {"status": "completed", "reason_code": None})


def execute(
    recovery: QualificationRecovery,
    store: QualificationWriter,
    *,
    profile_ref: dict[str, Any],
    suite_ref: dict[str, Any],
    source: Callable[[], dict[str, Any]],
    prepare_fixture: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    """The sole future start path; each pre-effect and post-effect fact is durable."""
    with recovery.ledger.recovery_lock():
        return _execute_locked(
            recovery, store, profile_ref=profile_ref, suite_ref=suite_ref,
            source=source, prepare_fixture=prepare_fixture,
        )


def _execute_locked(
    recovery: QualificationRecovery,
    store: QualificationWriter,
    *,
    profile_ref: dict[str, Any],
    suite_ref: dict[str, Any],
    source: Callable[[], dict[str, Any]],
    prepare_fixture: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    recovery.preflight()
    try:
        fixture = prepare_fixture()
        observed_source = source()
    except Exception as error:
        return recovery.ledger.write(
            "execute_preflight", {"status": "failed", "reason_code": _safe_error(error)}
        )
    recovery.ledger.write(
        "execute_preflight",
        {
            "status": "prepared",
            "fixture_sha256": _sha(fixture),
            "source_sha256": _sha(observed_source),
            "command": COMMAND,
        },
    )
    try:
        store.get_command_start(recovery.project_id, COMMAND, principal=PRINCIPAL)
    except Exception as error:
        if _safe_error(error) != "QUALIFICATION_START_NOT_FOUND":
            return recovery.ledger.write(
                "execute_start", {"status": "unknown", "reason_code": _safe_error(error)}
            )
    else:
        return recovery.ledger.write(
            "execute_start", {"status": "refused", "reason_code": "RESUME_RECEIPT_CONFLICT"}
        )
    recovery.ledger.write("execute_start", {"status": "prepared", "command": COMMAND})
    recovery.ledger.write("qualification_claimed", {"status": "claimed", "command": COMMAND})
    try:
        record = store.qualify_runtime_tools(
            recovery.project_id,
            profile_ref,
            principal=PRINCIPAL,
            command_key=COMMAND,
            suite_ref=suite_ref,
            validity_seconds=600,
        )
    except Exception:
        # The Store persists start intent before resolving credentials or doing
        # the suite. A lost reply must be recovered only through that original
        # command; resume never calls the qualification effect.
        return recovery._resume_locked()
    recovery.ledger.write(
        "qualification_observed", {"status": "observed", "record": _summary(record)}
    )
    return recovery._resume_locked()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("preflight", "execute", "resume"))
    parser.add_argument("--private-root", type=Path, required=True)
    parser.add_argument("--receipts", type=Path, required=True)
    args = parser.parse_args()
    # Importing this legacy controller does not call its Go suite. execute is
    # deliberately explicit because it is a future separately-authorized effect.
    sys.path.insert(0, str(ROOT / "backend"))
    sys.path.insert(0, str(Path(__file__).parent))
    import prepare_issue107_consumer as consumer
    import run_official_issue107 as controller

    if args.mode == "resume":
        store, project_id = controller.open_existing_controller(args.private_root)
        reviewer: dict[str, Any] | None = None
    else:
        store, project_id, reviewer, _journal = controller.open_controller(args.private_root)

    def negative() -> dict[str, Any]:
        report = args.receipts / "consumer-negative-private.json"
        consumer.negative(args.private_root, report)
        value = json.loads(report.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise RecoveryError("UNCLASSIFIED")
        return value

    recovery = QualificationRecovery(
        ReceiptLedger(args.receipts),
        store,
        project_id=project_id,
        positive_history=lambda identity: consumer.positive_history(args.private_root, identity),
        positive=lambda identity: consumer.positive_result(args.private_root, identity),
        negative=negative,
    )
    try:
        if args.mode == "preflight":
            result = recovery.preflight()
        elif args.mode == "resume":
            result = recovery.resume()
        else:
            if reviewer is None:
                raise RecoveryError("QUALIFICATION_START_NOT_FOUND")
            result = execute(
                recovery,
                store,
                profile_ref={"id": reviewer["id"], "revision": reviewer["revision"]},
                suite_ref={"id": "opencode-go-readonly-review-linux", "revision": 1},
                source=lambda: store.reviewer_suite.source(),
                prepare_fixture=lambda: consumer.ensure_fixture(args.private_root)[2],
            )
    except Exception as error:
        result = {"stage": "driver", "status": "unknown", "reason_code": _safe_error(error)}
    print(json.dumps({"stage": result["stage"], "status": result["status"]}, sort_keys=True))


if __name__ == "__main__":
    main()
