"""Content-free failures for the role-directed scheduling control plane.

Every rejection carries a stable reason code and, when it is attached to a place
in a submitted command, a located :class:`Diagnostic`. A location is always a
control-plane pointer such as ``scheduling#/actions/0/tasks/id=repair-a``; a host
path, a credential or a host user name is never part of an error, so a refused
decision cannot be used to probe the local filesystem or the credential store.
"""

from collections.abc import Iterable, Mapping
from typing import Any

from karajan.workflows.errors import Diagnostic

__all__ = ["CONFLICT_CODES", "Diagnostic", "SchedulingError", "at", "located"]

#: Locations this module will echo. The pointer is built from fixed words plus a
#: validated identity, so a caller cannot smuggle a host path into a diagnostic.
_LOCATION_PREFIXES = ("scheduling#", "run#", "grant#", "claim#", "task#")


def at(pointer: object) -> str:
    """Coerce any value into a control-plane pointer."""
    if isinstance(pointer, str) and pointer.startswith(_LOCATION_PREFIXES):
        return pointer[:200]
    return "scheduling#/"


def located(code: str, pointer: object, detail: str) -> Diagnostic:
    """One located, machine-readable rejection reason."""
    safe = at(pointer)
    return Diagnostic(code=code, location=safe, detail=detail)


class SchedulingError(ValueError):
    """A scheduling rejection with a stable code and optional diagnostics."""

    def __init__(
        self,
        code: str,
        *,
        current_revision: int | None = None,
        diagnostics: Iterable[Diagnostic] = (),
        fields: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.current_revision = current_revision
        self.diagnostics = tuple(diagnostics)
        self.fields = dict(fields or {})

    def document(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"reason_code": self.code}
        payload.update(self.fields)
        if self.current_revision is not None:
            payload["current_revision"] = self.current_revision
        if self.diagnostics:
            payload["diagnostics"] = [item.as_document() for item in self.diagnostics]
        return payload


#: Failures that describe a durable-state disagreement the caller resolves by
#: re-reading, rather than a malformed request. The HTTP boundary maps these to
#: 409 so a caller cannot mistake a lost race for a bad payload.
CONFLICT_CODES = frozenset(
    {
        "SCHEDULING_IDEMPOTENCY_CONFLICT",
        "SCHEDULING_GRAPH_REVISION_CONFLICT",
        "SCHEDULING_SOURCE_REPLACED",
        "SCHEDULING_SOURCE_STALE",
        "SCHEDULING_RUN_SOURCE_STALE",
        "SCHEDULING_GRANT_REVOKED",
        "SCHEDULING_GRANT_EXPIRED",
        "SCHEDULING_GRANT_TERM_STALE",
        "SCHEDULING_CREDENTIAL_REVOKED",
        "SCHEDULING_CREDENTIAL_EXPIRED",
        "SCHEDULING_WRITE_ZONE_BUSY",
        "SCHEDULING_RUN_ALREADY_EXISTS",
        "SCHEDULING_EXPANSION_MEMBER_POLICY_EXCEEDED",
        "SCHEDULING_TASK_ALREADY_EXISTS",
        "SCHEDULING_CLAIM_SUPERSEDED",
        "SCHEDULING_TASK_FROZEN",
        "SCHEDULING_TASK_NOT_READY",
        "SCHEDULING_CAPACITY_BUSY",
        "SCHEDULING_CAPACITY_UNKNOWN",
        "SCHEDULING_EXPANSION_SEALED",
        "SCHEDULING_EXPANSION_NOT_SEALED",
        "SCHEDULING_RUN_RECORD_CHANGED",
        "SCHEDULING_TASK_RECORD_CHANGED",
        "SCHEDULING_GRAPH_RECORD_CHANGED",
        "SCHEDULING_GRANT_RECORD_CHANGED",
        "SCHEDULING_DEPENDENCY_UNSATISFIED",
        "SCHEDULING_DEPENDENCY_FAILED",
    }
)
