"""Content-free failures for the workflow bundle control plane.

Every failure carries a stable reason code and, when the rejection is attached to
a specific place in the bundle, a located :class:`Diagnostic`. A location is
always a bundle-relative pointer such as ``workflow.yaml#/steps/id=comparison``;
an absolute host path, a credential or a host user name is never part of an
error, so a rejected upload cannot be used to probe the local filesystem.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

#: Bundle-relative pointer syntax; deliberately narrow so a diagnostic can never
#: be mistaken for a filesystem path.
_LOCATION_PREFIXES = ("manifest.json", "workflow.yaml", "roles/", "templates/")


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """One located, machine-readable rejection reason."""

    code: str
    location: str
    detail: str
    step_id: str | None = None
    file: str | None = None

    def as_document(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "code": self.code,
            "location": self.location,
            "detail": self.detail,
        }
        if self.step_id is not None:
            document["step_id"] = self.step_id
        if self.file is not None:
            document["file"] = self.file
        return document


def safe_location(location: object) -> str:
    """Coerce any value into a bundle-relative pointer.

    A diagnostic is built on paths the caller submitted, so a value that is not a
    bundle-relative pointer must still produce a usable location rather than an
    exception: a rejection has to reach the client as a located error, and a
    raised ``ValueError`` here would turn a refused path into an internal error.
    The submitted text is kept out of the location and truncated into the detail
    instead, so it cannot be mistaken for a host path.
    """
    if isinstance(location, str) and location.startswith(_LOCATION_PREFIXES):
        return location
    return "workflow.yaml#/"


def located(
    code: str, location: object, detail: str, *, step_id: str | None = None
) -> Diagnostic:
    """Build a diagnostic whose location is always bundle-relative."""
    safe = safe_location(location)
    return Diagnostic(
        code=code,
        location=safe,
        detail=detail,
        step_id=step_id,
        file=safe.split("#", 1)[0],
    )


class WorkflowError(ValueError):
    """A control-plane rejection with a stable code and optional diagnostics."""

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
