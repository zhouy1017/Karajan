"""Delivery state owns activation ordering; adapters own remote observations."""

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import uuid4

from pydantic import ValidationError

from .errors import DeliveryError, RemoteUnknown
from .git import LocalGitRemote
from .models import DeliveryRequest, PullRequestObservation, VerificationReceipt


class PullRequests(Protocol):
    execution_scope: str

    def lookup(self, binding: dict[str, Any]) -> list[dict[str, Any]]: ...
    def publish(self, binding: dict[str, Any], existing_id: str | None) -> dict[str, Any]: ...


def encoded(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError):
        raise DeliveryError("DELIVERY_INPUT_INVALID") from None


def identifier(value: object) -> str:
    if not isinstance(value, str) or not 0 < len(value) <= 256 or not value.isprintable():
        raise DeliveryError("DELIVERY_INPUT_INVALID")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise DeliveryError("DELIVERY_INPUT_INVALID") from None
    return value


class DeliveryCoordinator:
    def __init__(
        self,
        database: Path,
        *,
        git_remote: LocalGitRemote | None = None,
        pr_service: PullRequests | None = None,
        verification_reader: Callable[[str], dict[str, Any]] | None = None,
        mode: Literal["production", "offline_fixture"] = "production",
    ) -> None:
        self.database = Path(database)
        self.git_remote = git_remote
        self.pr_service = pr_service
        self.verification_reader = verification_reader
        self.mode = mode
        if mode not in {"production", "offline_fixture"}:
            raise DeliveryError("DELIVERY_MODE_INVALID")
        with self._transaction() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS deliveries (id TEXT PRIMARY KEY, run_id TEXT NOT NULL, "
                "revision INTEGER NOT NULL, binding TEXT NOT NULL, snapshot TEXT NOT NULL, "
                "UNIQUE(run_id, revision))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS commands (principal TEXT NOT NULL, key TEXT NOT NULL, "
                "digest TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(principal, key))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS run_targets (run_id TEXT PRIMARY KEY, "
                "repository_id TEXT NOT NULL, branch TEXT NOT NULL, base_branch TEXT NOT NULL, "
                "UNIQUE(repository_id, branch))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS run_controls "
                "(run_id TEXT PRIMARY KEY, state TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS run_prs (run_id TEXT PRIMARY KEY, "
                "repository_id TEXT NOT NULL, pr_id TEXT NOT NULL, "
                "UNIQUE(repository_id, pr_id))"
            )
            # Preserve already-confirmed identities when opening an older local
            # fixture database; conflicting history cannot select a replacement.
            for (serialized,) in db.execute("SELECT snapshot FROM deliveries").fetchall():
                old = json.loads(serialized)
                if old["pr"] is not None and not self._claim_run_pr(
                    db, old["request"], old["pr"]["id"]
                ):
                    raise DeliveryError("PR_IDENTITY_CONFLICT")

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.database, timeout=10)
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            with db:
                yield db
        finally:
            db.close()

    def plan(self, request: dict[str, Any], *, command_key: str, principal: str) -> dict[str, Any]:
        if principal != "controller":
            raise DeliveryError("DELIVERY_ACTOR_FORBIDDEN")
        command_key = identifier(command_key)
        try:
            validated = DeliveryRequest.model_validate(request).model_dump()
        except ValidationError:
            raise DeliveryError("DELIVERY_INPUT_INVALID") from None
        snapshot: dict[str, Any] = {
            "schema_version": "karajan.delivery.v1",
            "id": str(uuid4()),
            "request": validated,
            "binding_sha256": hashlib.sha256(encoded(validated).encode()).hexdigest(),
            "state": "planned",
            "control_state": "active",
            "reason": None,
            "operations": [],
            "production_qualified": False,
            "execution_scope": self.mode,
            "remote_observation": None,
            "pr": None,
            "ci": {"sha": None, "status": "unknown"},
            "merge": {"merged": False},
            "completion": {"requirements_satisfied": False, "scope": self.mode},
        }
        with self._transaction() as db:
            command = db.execute(
                "SELECT digest, result FROM commands WHERE principal=? AND key=?",
                (principal, command_key),
            ).fetchone()
            if command:
                if command[0] != snapshot["binding_sha256"]:
                    raise DeliveryError("IDEMPOTENCY_CONFLICT")
                return dict(json.loads(command[1]))
            target = db.execute(
                "SELECT repository_id, branch, base_branch FROM run_targets WHERE run_id=?",
                (validated["run_id"],),
            ).fetchone()
            binding = (
                validated["repository_id"],
                validated["managed_branch"],
                validated["base_branch"],
            )
            if target is not None and tuple(target) != binding:
                raise DeliveryError("RUN_DELIVERY_TARGET_CHANGED")
            if target is None:
                try:
                    db.execute(
                        "INSERT INTO run_targets VALUES (?, ?, ?, ?)",
                        (validated["run_id"], *binding),
                    )
                except sqlite3.IntegrityError:
                    raise DeliveryError("MANAGED_BRANCH_OWNED") from None
            db.execute(
                "INSERT OR IGNORE INTO run_controls VALUES (?, 'active')", (validated["run_id"],)
            )
            snapshot["control_state"] = db.execute(
                "SELECT state FROM run_controls WHERE run_id=?", (validated["run_id"],)
            ).fetchone()[0]
            previous = db.execute(
                "SELECT binding, snapshot FROM deliveries WHERE run_id=? AND revision=?",
                (validated["run_id"], validated["delivery_revision"]),
            ).fetchone()
            if previous:
                if previous[0] != snapshot["binding_sha256"]:
                    raise DeliveryError("DELIVERY_REVISION_CONFLICT")
                snapshot = json.loads(previous[1])
            else:
                db.execute(
                    "INSERT INTO deliveries VALUES (?, ?, ?, ?, ?)",
                    (
                        snapshot["id"],
                        validated["run_id"],
                        validated["delivery_revision"],
                        snapshot["binding_sha256"],
                        encoded(snapshot),
                    ),
                )
            db.execute(
                "INSERT INTO commands VALUES (?, ?, ?, ?)",
                (principal, command_key, snapshot["binding_sha256"], encoded(snapshot)),
            )
        return snapshot

    def set_control(
        self, run_id: str, state: str, *, command_key: str, principal: str
    ) -> dict[str, Any]:
        if principal != "controller":
            raise DeliveryError("DELIVERY_ACTOR_FORBIDDEN")
        run_id, command_key = identifier(run_id), identifier(command_key)
        if state not in {"active", "paused", "cancelled", "revoked"}:
            raise DeliveryError("DELIVERY_CONTROL_INVALID")
        digest = hashlib.sha256(encoded(["control", run_id, state]).encode()).hexdigest()
        with self._transaction() as db:
            previous = db.execute(
                "SELECT digest, result FROM commands WHERE principal=? AND key=?",
                (principal, command_key),
            ).fetchone()
            if previous is not None:
                if previous[0] != digest:
                    raise DeliveryError("IDEMPOTENCY_CONFLICT")
                return dict(json.loads(previous[1]))
            current = db.execute(
                "SELECT state FROM run_controls WHERE run_id=?", (run_id,)
            ).fetchone()
            if current is None:
                raise DeliveryError("DELIVERY_NOT_FOUND")
            if current[0] in {"cancelled", "revoked"} and state != current[0]:
                raise DeliveryError("DELIVERY_CONTROL_TERMINAL")
            db.execute("UPDATE run_controls SET state=? WHERE run_id=?", (state, run_id))
            for delivery_id, serialized in db.execute(
                "SELECT id, snapshot FROM deliveries WHERE run_id=?", (run_id,)
            ).fetchall():
                snapshot = json.loads(serialized)
                snapshot["control_state"] = state
                if state != "active":
                    snapshot["completion"]["requirements_satisfied"] = False
                db.execute(
                    "UPDATE deliveries SET snapshot=? WHERE id=?", (encoded(snapshot), delivery_id)
                )
            result = {"run_id": run_id, "state": state}
            db.execute(
                "INSERT INTO commands VALUES (?, ?, ?, ?)",
                (principal, command_key, digest, encoded(result)),
            )
        return result

    def _activation_reason(self, db: sqlite3.Connection, snapshot: dict[str, Any]) -> str | None:
        request = snapshot["request"]
        control = db.execute(
            "SELECT state FROM run_controls WHERE run_id=?", (request["run_id"],)
        ).fetchone()
        if control is None or control[0] != "active":
            return "DELIVERY_CONTROL_" + (control[0].upper() if control else "UNKNOWN")
        revisions = db.execute(
            "SELECT revision, snapshot FROM deliveries WHERE run_id=?", (request["run_id"],)
        ).fetchall()
        if max(row[0] for row in revisions) != request["delivery_revision"]:
            return "DELIVERY_REVISION_SUPERSEDED"
        if any(
            row[0] < request["delivery_revision"]
            and any(item["state"] == "send_unknown" for item in json.loads(row[1])["operations"])
            for row in revisions
        ):
            return "DELIVERY_PREVIOUS_UNRESOLVED"
        return None

    def get(self, delivery_id: str) -> dict[str, Any]:
        delivery_id = identifier(delivery_id)
        with self._transaction() as db:
            row = db.execute(
                "SELECT snapshot FROM deliveries WHERE id=?", (delivery_id,)
            ).fetchone()
        if row is None:
            raise DeliveryError("DELIVERY_NOT_FOUND")
        result: dict[str, Any] = json.loads(row[0])
        return result

    def _verification(self, snapshot: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
        if (
            self.mode != "offline_fixture"
            or snapshot["execution_scope"] != self.mode
            or not isinstance(self.git_remote, LocalGitRemote)
            or self.pr_service is None
            or self.pr_service.execution_scope != "offline_fixture"
            or self.verification_reader is None
        ):
            return None, "DELIVERY_QUALIFICATION_NOT_RUN"
        request = snapshot["request"]
        try:
            receipt = VerificationReceipt.model_validate(
                self.verification_reader(request["verification_ref"])
            ).model_dump()
        except (ValidationError, KeyError, ValueError, OSError, RemoteUnknown):
            return None, "VERIFICATION_RECEIPT_UNAVAILABLE"
        if (
            receipt["receipt_ref"] != request["verification_ref"]
            or receipt["binding_sha256"] != snapshot["binding_sha256"]
            or receipt["decision"] != "allow"
            or receipt["provenance"] != "fixture"
        ):
            return None, "VERIFICATION_BINDING_NOT_ALLOWED"
        return receipt, None

    def advance(self, delivery_id: str, *, principal: str) -> dict[str, Any]:
        if principal != "controller":
            raise DeliveryError("DELIVERY_ACTOR_FORBIDDEN")
        snapshot = self.get(delivery_id)
        pending = next(
            (item for item in snapshot["operations"] if item["state"] == "send_unknown"), None
        )
        if pending is not None and isinstance(self.git_remote, LocalGitRemote):
            if pending["step"] == "pr":
                return self._reconcile_pr(snapshot, pending)
            return self._reconcile_push(snapshot, pending)
        receipt, reason = self._verification(snapshot)
        if reason:
            return self._block(snapshot, reason)
        assert receipt is not None and self.git_remote is not None
        with self._transaction() as db:
            reason = self._activation_reason(db, snapshot)
            if reason:
                current = json.loads(
                    db.execute(
                        "SELECT snapshot FROM deliveries WHERE id=?", (delivery_id,)
                    ).fetchone()[0]
                )
                return self._block_in_transaction(db, current, reason)
        request = snapshot["request"]
        if any(
            item["step"] == "push" and item["state"] == "confirmed"
            for item in snapshot["operations"]
        ):
            return self._advance_pr(snapshot, receipt)
        try:
            self.git_remote.validate(request)
        except DeliveryError as error:
            return self._block(snapshot, error.code)
        except RemoteUnknown:
            return self._block(snapshot, "REMOTE_OBSERVATION_UNKNOWN")
        operation = {
            "id": str(uuid4()),
            "step": "push",
            "state": "send_unknown",
            "activation": {
                "id": str(uuid4()),
                "binding_sha256": snapshot["binding_sha256"],
                "verification_receipt": receipt,
                "at": datetime.now(UTC).isoformat(),
            },
            "observation": None,
        }
        with self._transaction() as db:
            current = json.loads(
                db.execute("SELECT snapshot FROM deliveries WHERE id=?", (delivery_id,)).fetchone()[
                    0
                ]
            )
            reason = self._activation_reason(db, current)
            if reason:
                return self._block_in_transaction(db, current, reason)
            if current != snapshot:
                return dict(current)
            current["operations"].append(operation)
            current["state"] = "pushing"
            db.execute(
                "UPDATE deliveries SET snapshot=? WHERE id=?", (encoded(current), delivery_id)
            )
        try:
            observation = self.git_remote.push(request)
        except RemoteUnknown:
            observation = None
        with self._transaction() as db:
            current = json.loads(
                db.execute("SELECT snapshot FROM deliveries WHERE id=?", (delivery_id,)).fetchone()[
                    0
                ]
            )
            stored = next(item for item in current["operations"] if item["id"] == operation["id"])
            if stored["state"] != "confirmed":
                stored["state"] = "confirmed" if observation else "send_unknown"
                stored["observation"] = observation
                current["remote_observation"] = observation
                current["state"] = "pushed" if observation else "reconciling"
                current["reason"] = None if observation else "REMOTE_RESULT_UNKNOWN"
            db.execute(
                "UPDATE deliveries SET snapshot=? WHERE id=?", (encoded(current), delivery_id)
            )
        return dict(current)

    def _advance_pr(self, snapshot: dict[str, Any], receipt: dict[str, Any]) -> dict[str, Any]:
        assert self.git_remote is not None and self.pr_service is not None
        request = snapshot["request"]
        try:
            observation = self.git_remote.inspect(request)
            matches = self.pr_service.lookup(request)
        except RemoteUnknown:
            return self._block(snapshot, "REMOTE_OBSERVATION_UNKNOWN")
        if observation["head_sha"] != request["commit_sha"]:
            return self._block(snapshot, "REMOTE_HEAD_CHANGED")
        if observation["base_sha"] != request["tested_base_sha"]:
            return self._block(snapshot, "TESTED_BASE_CHANGED")
        if (
            not isinstance(matches, list)
            or len(matches) > 1
            or any(not self._pr_identity(request, item) for item in matches)
        ):
            return self._block(snapshot, "PR_IDENTITY_CONFLICT")
        with self._transaction() as db:
            bound_id = self._run_pr_id(db, request["run_id"])
        if bound_id is not None and (len(matches) != 1 or matches[0]["id"] != bound_id):
            return self._block(snapshot, "PR_IDENTITY_CONFLICT")
        if snapshot["pr"] is not None:
            if len(matches) != 1 or matches[0]["id"] != snapshot["pr"]["id"]:
                return self._block(snapshot, "PR_IDENTITY_CONFLICT")
            previous = next(item for item in snapshot["operations"] if item["step"] == "pr")
            return self._confirm_pr(snapshot, previous, matches[0])
        existing_id = matches[0]["id"] if matches else None
        operation = {
            "id": str(uuid4()),
            "step": "pr",
            "state": "send_unknown",
            "activation": {
                "id": str(uuid4()),
                "binding_sha256": snapshot["binding_sha256"],
                "verification_receipt": receipt,
                "at": datetime.now(UTC).isoformat(),
            },
            "observation": None,
            "expected_pr_id": existing_id,
        }
        with self._transaction() as db:
            current = json.loads(
                db.execute(
                    "SELECT snapshot FROM deliveries WHERE id=?", (snapshot["id"],)
                ).fetchone()[0]
            )
            reason = self._activation_reason(db, current)
            if reason:
                return self._block_in_transaction(db, current, reason)
            if current != snapshot:
                return dict(current)
            bound_id = self._run_pr_id(db, request["run_id"])
            if (bound_id is not None and bound_id != existing_id) or (
                existing_id is not None and not self._claim_run_pr(db, request, existing_id)
            ):
                return self._block_in_transaction(db, current, "PR_IDENTITY_CONFLICT")
            current["operations"].append(operation)
            current["state"] = "publishing"
            db.execute(
                "UPDATE deliveries SET snapshot=? WHERE id=?", (encoded(current), snapshot["id"])
            )
        try:
            pr = self.pr_service.publish(request, existing_id)
        except RemoteUnknown:
            return self._unknown(snapshot["id"])
        return self._confirm_pr(snapshot, operation, pr)

    def _unknown(self, delivery_id: str) -> dict[str, Any]:
        with self._transaction() as db:
            current = json.loads(
                db.execute("SELECT snapshot FROM deliveries WHERE id=?", (delivery_id,)).fetchone()[
                    0
                ]
            )
            if not any(item["state"] == "send_unknown" for item in current["operations"]):
                return dict(current)
            current["state"] = "reconciling"
            current["reason"] = "REMOTE_RESULT_UNKNOWN"
            current["completion"]["requirements_satisfied"] = False
            db.execute(
                "UPDATE deliveries SET snapshot=? WHERE id=?", (encoded(current), delivery_id)
            )
        return dict(current)

    def _reconcile_pr(self, snapshot: dict[str, Any], operation: dict[str, Any]) -> dict[str, Any]:
        if self.pr_service is None or self.git_remote is None:
            return snapshot
        try:
            observation = self.git_remote.inspect(snapshot["request"])
            matches = self.pr_service.lookup(snapshot["request"])
        except RemoteUnknown:
            return snapshot
        if observation["head_sha"] != snapshot["request"]["commit_sha"]:
            return self._block(snapshot, "REMOTE_HEAD_CHANGED")
        if observation["base_sha"] != snapshot["request"]["tested_base_sha"]:
            return self._block(snapshot, "TESTED_BASE_CHANGED")
        if not isinstance(matches, list) or len(matches) != 1:
            return self._unknown(snapshot["id"])
        return self._confirm_pr(snapshot, operation, matches[0])

    def _confirm_pr(
        self, snapshot: dict[str, Any], operation: dict[str, Any], pr: dict[str, Any]
    ) -> dict[str, Any]:
        request = snapshot["request"]
        if not self._pr_identity(request, pr) or pr.get("head_sha") != request["commit_sha"]:
            if operation["state"] == "confirmed":
                return self._block(snapshot, "PR_OBSERVATION_CHANGED")
            return self._unknown(snapshot["id"])
        _, verification_reason = self._verification(snapshot)
        assert self.git_remote is not None
        observation = None
        remote_reason = None
        try:
            observation = self.git_remote.inspect(request)
            if observation["head_sha"] != request["commit_sha"]:
                remote_reason = "REMOTE_HEAD_CHANGED"
            elif observation["base_sha"] != request["tested_base_sha"]:
                remote_reason = "TESTED_BASE_CHANGED"
        except (RemoteUnknown, DeliveryError):
            remote_reason = "REMOTE_OBSERVATION_UNKNOWN"
        with self._transaction() as db:
            current = json.loads(
                db.execute(
                    "SELECT snapshot FROM deliveries WHERE id=?", (snapshot["id"],)
                ).fetchone()[0]
            )
            stored = next(item for item in current["operations"] if item["id"] == operation["id"])
            if not self._claim_run_pr(db, request, pr["id"]):
                return self._block_in_transaction(db, current, "PR_IDENTITY_CONFLICT")
            stored["state"] = "confirmed"
            stored["observation"] = pr
            current["pr"] = pr
            current["ci"] = {"sha": pr["ci_sha"], "status": pr["ci_status"]}
            current["merge"] = {"merged": pr["merged"]}
            current["remote_observation"] = observation
            completed = not request["require_ci"] or (
                pr["ci_sha"] == request["commit_sha"] and pr["ci_status"] == "success"
            )
            reason = self._activation_reason(db, current) or verification_reason or remote_reason
            current["completion"]["requirements_satisfied"] = completed and reason is None
            current["state"] = "blocked" if reason else "delivered" if completed else "awaiting_ci"
            current["reason"] = reason
            db.execute(
                "UPDATE deliveries SET snapshot=? WHERE id=?", (encoded(current), snapshot["id"])
            )
        return dict(current)

    @staticmethod
    def _run_pr_id(db: sqlite3.Connection, run_id: str) -> str | None:
        row = db.execute("SELECT pr_id FROM run_prs WHERE run_id=?", (run_id,)).fetchone()
        return str(row[0]) if row is not None else None

    @staticmethod
    def _claim_run_pr(db: sqlite3.Connection, request: dict[str, Any], pr_id: str) -> bool:
        row = db.execute(
            "SELECT repository_id, pr_id FROM run_prs WHERE run_id=?", (request["run_id"],)
        ).fetchone()
        if row is not None:
            return tuple(row) == (request["repository_id"], pr_id)
        try:
            db.execute(
                "INSERT INTO run_prs VALUES (?, ?, ?)",
                (request["run_id"], request["repository_id"], pr_id),
            )
        except sqlite3.IntegrityError:
            return False
        return True

    @staticmethod
    def _pr_identity(request: dict[str, Any], pr: dict[str, Any]) -> bool:
        try:
            observed = PullRequestObservation.model_validate(pr).model_dump()
        except ValidationError:
            return False
        return all(
            observed[key] == request[key]
            for key in ("repository_id", "managed_branch", "base_branch", "run_id")
        )

    @staticmethod
    def _block_in_transaction(
        db: sqlite3.Connection, snapshot: dict[str, Any], reason: str
    ) -> dict[str, Any]:
        snapshot["state"] = "blocked"
        snapshot["reason"] = reason
        snapshot["completion"]["requirements_satisfied"] = False
        db.execute(
            "UPDATE deliveries SET snapshot=? WHERE id=?", (encoded(snapshot), snapshot["id"])
        )
        return snapshot

    def _block(self, snapshot: dict[str, Any], reason: str) -> dict[str, Any]:
        previous = encoded(snapshot)
        snapshot["state"] = "blocked"
        snapshot["reason"] = reason
        snapshot["completion"]["requirements_satisfied"] = False
        with self._transaction() as db:
            db.execute(
                "UPDATE deliveries SET snapshot=? WHERE id=? AND snapshot=?",
                (encoded(snapshot), snapshot["id"], previous),
            )
            current = db.execute(
                "SELECT snapshot FROM deliveries WHERE id=?", (snapshot["id"],)
            ).fetchone()[0]
        return dict(json.loads(current))

    def _reconcile_push(
        self, snapshot: dict[str, Any], operation: dict[str, Any]
    ) -> dict[str, Any]:
        assert self.git_remote is not None
        try:
            observation = self.git_remote.inspect(snapshot["request"])
        except RemoteUnknown:
            return snapshot
        with self._transaction() as db:
            current = json.loads(
                db.execute(
                    "SELECT snapshot FROM deliveries WHERE id=?", (snapshot["id"],)
                ).fetchone()[0]
            )
            stored = next(item for item in current["operations"] if item["id"] == operation["id"])
            if stored["state"] == "send_unknown":
                stored["observation"] = observation
                current["remote_observation"] = observation
                if observation["head_sha"] == current["request"]["commit_sha"]:
                    stored["state"] = "confirmed"
                    stored["reconciled"] = True
                    current["state"] = "pushed"
                    current["reason"] = None
                else:
                    current["state"] = "reconciling"
                    current["reason"] = "REMOTE_RESULT_UNKNOWN"
                db.execute(
                    "UPDATE deliveries SET snapshot=? WHERE id=?",
                    (encoded(current), snapshot["id"]),
                )
        return dict(current)
