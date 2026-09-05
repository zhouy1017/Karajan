"""Single-use permission decisions over exact, controller-authorized requests."""

import hashlib
import json
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from .models import AttemptContext, Authorization, NativeCommandRequest, PermissionDecision


def request_digest(message: dict[str, Any]) -> str:
    """Digest the complete native request, including callback identity and scope."""
    return hashlib.sha256(
        json.dumps(message, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


class PermissionGate:
    """An in-memory gate for one bound Attempt; it never transmits responses."""

    def __init__(self, attempt: AttemptContext, authorization: Authorization) -> None:
        self.attempt = attempt.model_copy(deep=True)
        self.authorization = authorization.model_copy(deep=True)
        self.pending: dict[str, dict[str, Any]] = {}
        self.active = True
        self.seen: set[str] = set()

    def register(
        self, message: dict[str, Any], *, expires_at: datetime | None, now: datetime
    ) -> dict[str, Any]:
        if not self.active:
            return {"status": "rejected", "reason": "ATTEMPT_INACTIVE"}
        if type(message.get("id")) not in (str, int) or message.get("id") == "":
            return {"status": "rejected", "reason": "NATIVE_REQUEST_INVALID"}
        key = json.dumps(message["id"])
        if key in self.seen:
            return {"status": "rejected", "reason": "REQUEST_ALREADY_SEEN"}
        self.seen.add(key)
        method = message.get("method")
        if method != "item/commandExecution/requestApproval":
            if method == "item/permissions/requestApproval":
                response = {"id": message["id"], "result": {"permissions": {}, "scope": "turn"}}
            elif method == "item/fileChange/requestApproval":
                return self._cancel(message["id"], "NATIVE_METHOD_UNSUPPORTED", status="blocked")
            else:
                response = {
                    "id": message["id"],
                    "error": {"code": -32601, "message": "Unsupported native request"},
                }
            return {
                "status": "blocked",
                "reason": "NATIVE_METHOD_UNSUPPORTED",
                "response": response,
            }
        try:
            native = NativeCommandRequest.model_validate(message)
        except ValidationError:
            return self._cancel(message["id"], "NATIVE_REQUEST_INVALID", status="rejected")
        if (
            native.params.kind != "command"
            or native.params.environmentId is not None
            or native.params.networkApprovalContext is not None
        ):
            return self._cancel(message["id"], "NATIVE_SCOPE_UNSUPPORTED", status="blocked")
        params = message.get("params", {})
        if (
            params.get("threadId") != self.attempt.thread_id
            or params.get("turnId") != self.attempt.turn_id
        ):
            return self._cancel(message["id"], "NATIVE_BINDING_MISMATCH", status="rejected")
        try:
            digest = request_digest(message)
        except ValueError:
            return self._cancel(message["id"], "NATIVE_REQUEST_INVALID")
        if digest not in self.authorization.allowed_request_digests:
            return self._cancel(message["id"], "REQUEST_NOT_AUTHORIZED", status="rejected")
        self.pending[key] = {
            "message": json.loads(json.dumps(message)),
            "digest": digest,
            "expires_at": expires_at,
            "registered_at": now,
        }
        return {
            "status": "pending",
            "request_digest": digest,
            "ticket": {
                **self.attempt.model_dump(),
                "authorization_hash": self.authorization.hash,
                "request_id": message["id"],
                "request_digest": digest,
            },
            "expires_at": expires_at.isoformat() if expires_at is not None else None,
            "native_callback": {
                "item_id": native.params.itemId,
                "approval_id": native.params.approvalId,
            },
        }

    def decide(self, decision: PermissionDecision, *, now: datetime) -> dict[str, Any]:
        if not self.active:
            return {"status": "rejected", "reason": "ATTEMPT_INACTIVE"}
        record = self.pending.pop(json.dumps(decision.request_id), None)
        if record is None:
            return {"status": "rejected", "reason": "REQUEST_NOT_PENDING"}
        if now < record["registered_at"]:
            return self._cancel(decision.request_id, "EVENT_TIME_REVERSED")
        if decision.decision not in ("accept", "decline", "cancel"):
            return self._cancel(decision.request_id, "DECISION_SCOPE_UNSUPPORTED")
        if (
            record["expires_at"] is None
            or now >= record["expires_at"]
            or now >= self.authorization.expires_at
        ):
            return self._cancel(decision.request_id, "PERMISSION_EXPIRED")
        if (
            any(
                getattr(decision, field) != value
                for field, value in self.attempt.model_dump().items()
            )
            or decision.authorization_hash != self.authorization.hash
            or decision.request_digest != record["digest"]
        ):
            return self._cancel(decision.request_id, "DECISION_BINDING_MISMATCH")
        outcome: dict[str, Any] = {
            "status": "accepted" if decision.decision == "accept" else "denied",
            "response": {"id": decision.request_id, "result": {"decision": decision.decision}},
        }
        if decision.decision != "accept":
            outcome["reason"] = "PERMISSION_DECLINED"
        if decision.decision == "cancel":
            outcome["additional_responses"] = self.invalidate()
        return outcome

    def invalidate(self) -> list[dict[str, Any]]:
        """Fence/authorization changes and cancellation permanently close this gate."""
        self.active = False
        responses = [
            {"id": record["message"]["id"], "result": {"decision": "cancel"}}
            for record in self.pending.values()
        ]
        self.pending.clear()
        return responses

    def resolve(self, *, thread_id: str, request_id: str | int) -> dict[str, Any]:
        """Remove a callback cleared or answered by the server, without answering again."""
        if thread_id != self.attempt.thread_id:
            return {"status": "rejected", "reason": "NATIVE_BINDING_MISMATCH"}
        self.pending.pop(json.dumps(request_id), None)
        self.seen.add(json.dumps(request_id))
        return {"status": "resolved", "request_id": request_id}

    def _cancel(self, request_id: Any, reason: str, *, status: str = "rejected") -> dict[str, Any]:
        return {
            "status": status,
            "reason": reason,
            "response": {"id": request_id, "result": {"decision": "cancel"}},
            "additional_responses": self.invalidate(),
        }

    @property
    def pending_count(self) -> int:
        return len(self.pending)
