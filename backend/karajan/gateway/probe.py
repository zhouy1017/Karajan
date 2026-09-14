"""Read one registered model catalog without inference, fallback or redirection.

The probe connects only to the host parsed from the persisted connection
revision and reads only the path that revision registered. It never accepts a
URL, path, header or parameter from the caller, never follows a redirect, never
retries another source and never performs a generation request. A visible model
is catalog data only: no observation from this module grants execution
eligibility (docs/architecture/08-provider-gateway.md §2, GW-AC01).
"""

import http.client
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from .errors import GatewayError

MAXIMUM_BODY = 1_048_576
MAXIMUM_MODELS = 1000
MAXIMUM_SECRET = 4096
TIMEOUT_SECONDS = 5.0
REDACTION = "[redacted]"

CATALOG_STATES = (
    "ok",
    "redirect_rejected",
    "unauthorized",
    "upstream_error",
    "network_error",
    "invalid_response",
    "credential_unavailable",
    # A probe was started under this command key but has not finished. The
    # outcome is unknown and no second request is sent under the same key.
    "probe_in_progress",
    "outcome_unknown",
)


class SessionSecretResolver(Protocol):
    """Trusted controller port; only the probe call site ever sees the value."""

    def resolve(self, project_id: str, secret_ref: str) -> str: ...


@dataclass(frozen=True, slots=True)
class LocalFileSecretResolver:
    """Controller-configured files; a caller can never select or read one.

    Values live in memory for the duration of one request only. They are never
    persisted, returned or included in a reason code.
    """

    sources: Mapping[tuple[str, str], Path]

    def resolve(self, project_id: str, secret_ref: str) -> str:
        path = self.sources.get((project_id, secret_ref))
        if path is None:
            raise GatewayError("GATEWAY_SECRET_REF_UNCONFIGURED")
        try:
            raw = path.read_bytes()
        except OSError:
            raise GatewayError("GATEWAY_SECRET_MATERIAL_UNAVAILABLE") from None
        try:
            secret = raw.decode("utf-8-sig").strip()
        except UnicodeError:
            raise GatewayError("GATEWAY_SECRET_MATERIAL_INVALID") from None
        if (
            not 1 <= len(secret) <= MAXIMUM_SECRET
            or not secret.isascii()
            or not secret.isprintable()
            or any(character.isspace() for character in secret)
        ):
            raise GatewayError("GATEWAY_SECRET_MATERIAL_INVALID")
        return secret


@dataclass(frozen=True, slots=True)
class CatalogObservation:
    """A structured, content-free probe result; secrets are already redacted."""

    status: str
    reason_codes: list[str] = field(default_factory=list)
    http_status: int | None = None
    model_ids: list[str] = field(default_factory=list)
    model_count: int = 0
    requested_origin: str = ""
    requested_path: str = ""
    credential_supplied: bool = False
    secret_echo_detected: bool = False
    redirected: bool = False
    follow_ups_sent: int = 0
    #: ``None`` means genuinely unknown: an interrupted probe may or may not have
    #: sent its request, so zero would be a false claim.
    requests_sent: int | None = 0
    inference_requests_sent: int = 0


def _redact(values: Sequence[str], secrets: Sequence[str]) -> tuple[list[str], bool]:
    """Drop any catalog entry that would echo a credential back to the client."""
    leaked = False
    kept: list[str] = []
    for value in values:
        cleaned = value
        for secret in secrets:
            if secret and secret in cleaned:
                leaked = True
                cleaned = cleaned.replace(secret, REDACTION)
        if not 1 <= len(cleaned) <= 256 or any(
            character.isspace() for character in cleaned
        ):
            leaked = True
            continue
        if cleaned not in kept:
            kept.append(cleaned)
    return kept, leaked


def _read_catalog(response: http.client.HTTPResponse) -> tuple[list[str], str | None]:
    body = response.read(MAXIMUM_BODY + 1)
    if len(body) > MAXIMUM_BODY:
        return [], "CATALOG_BODY_TOO_LARGE"
    try:
        document = json.loads(body.decode("utf-8-sig"))
    except (UnicodeError, ValueError, RecursionError):
        return [], "CATALOG_BODY_INVALID"
    if not isinstance(document, dict) or not isinstance(document.get("data"), list):
        return [], "CATALOG_SHAPE_INVALID"
    if len(document["data"]) > MAXIMUM_MODELS:
        return [], "CATALOG_TOO_MANY_MODELS"
    identifiers: list[str] = []
    for entry in document["data"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            return [], "CATALOG_ENTRY_INVALID"
        identifiers.append(entry["id"])
    return identifiers, None


def probe_catalog(
    *,
    origin: str,
    catalog_path: str,
    secret: str | None,
) -> CatalogObservation:
    """Perform exactly one bounded read of the registered catalog path.

    A missing secret is resolved before any connection attempt, so an
    unconfigured ``secret_ref`` is an explicit gap rather than an unauthorized
    upstream call.
    """
    parsed = urlsplit(origin)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not catalog_path.startswith("/")
        or catalog_path.startswith("//")
        or "?" in catalog_path
        or "#" in catalog_path
        or any(character.isspace() for character in catalog_path)
    ):
        return CatalogObservation(
            status="invalid_response",
            reason_codes=["GATEWAY_ORIGIN_INVALID"],
            requested_origin=origin,
            requested_path=catalog_path,
        )
    if secret is not None and not secret.isascii():
        raise GatewayError("GATEWAY_SECRET_MATERIAL_INVALID")
    connection_type = (
        http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    )
    headers = {
        "Accept": "application/json",
        "User-Agent": "karajan-gateway-catalog-probe/1",
    }
    if secret is not None:
        headers["Authorization"] = "Bearer " + secret
    try:
        # A malformed authority raises InvalidURL from the constructor, before
        # any socket exists. It is a structured observation here, not a 500.
        connection = connection_type(parsed.hostname, parsed.port, timeout=TIMEOUT_SECONDS)
    except (OSError, ValueError, http.client.HTTPException):
        return CatalogObservation(
            status="invalid_response",
            reason_codes=["GATEWAY_ORIGIN_NOT_CONNECTABLE"],
            requested_origin=origin,
            requested_path=catalog_path,
            credential_supplied=secret is not None,
        )
    try:
        connection.request("GET", catalog_path, headers=headers)
        response = connection.getresponse()
        status = response.status
        redirect = response.getheader("Location")
        if 300 <= status < 400:
            response.read(4096)
            return CatalogObservation(
                status="redirect_rejected",
                reason_codes=["CATALOG_REDIRECT_REJECTED"],
                http_status=status,
                requested_origin=origin,
                requested_path=catalog_path,
                credential_supplied=secret is not None,
                redirected=True,
                requests_sent=1,
            )
        if status in {401, 403}:
            response.read(4096)
            return CatalogObservation(
                status="unauthorized",
                reason_codes=["CATALOG_UNAUTHORIZED"],
                http_status=status,
                requested_origin=origin,
                requested_path=catalog_path,
                credential_supplied=secret is not None,
                requests_sent=1,
            )
        if status != 200:
            response.read(4096)
            return CatalogObservation(
                status="upstream_error",
                reason_codes=["CATALOG_UPSTREAM_ERROR"],
                http_status=status,
                requested_origin=origin,
                requested_path=catalog_path,
                credential_supplied=secret is not None,
                requests_sent=1,
            )
        identifiers, problem = _read_catalog(response)
        observation = CatalogObservation(
            status="ok" if problem is None else "invalid_response",
            reason_codes=[] if problem is None else [problem],
            http_status=status,
            model_ids=identifiers,
            model_count=len(identifiers),
            requested_origin=origin,
            requested_path=catalog_path,
            credential_supplied=secret is not None,
            redirected=redirect is not None,
            requests_sent=1,
        )
        return observation
    except (OSError, http.client.HTTPException, ValueError):
        return CatalogObservation(
            status="network_error",
            reason_codes=["CATALOG_NETWORK_ERROR"],
            requested_origin=origin,
            requested_path=catalog_path,
            credential_supplied=secret is not None,
            requests_sent=1,
        )
    finally:
        connection.close()


def observed_catalog(
    *,
    origin: str,
    catalog_path: str,
    project_id: str,
    secret_ref: str | None,
    resolver: SessionSecretResolver | None,
) -> CatalogObservation:
    """Resolve ``secret_ref`` through the trusted port, then probe once."""
    if secret_ref is None:
        return probe_catalog(origin=origin, catalog_path=catalog_path, secret=None)
    if resolver is None:
        return CatalogObservation(
            status="credential_unavailable",
            reason_codes=["GATEWAY_SECRET_REF_UNCONFIGURED"],
            requested_origin=origin,
            requested_path=catalog_path,
        )
    try:
        secret = resolver.resolve(project_id, secret_ref)
    except GatewayError as error:
        return CatalogObservation(
            status="credential_unavailable",
            reason_codes=[error.code],
            requested_origin=origin,
            requested_path=catalog_path,
        )
    observation = probe_catalog(origin=origin, catalog_path=catalog_path, secret=secret)
    model_ids, leaked = _redact(observation.model_ids, [secret])
    if not leaked:
        return observation
    return replace(
        observation,
        status="invalid_response",
        reason_codes=["CATALOG_CREDENTIAL_ECHO_REJECTED"],
        model_ids=model_ids,
        model_count=len(model_ids),
        secret_echo_detected=True,
    )


def public_observation(observation: CatalogObservation) -> dict[str, Any]:
    """Project one observation; a visible model is never execution eligibility."""
    return {
        "status": observation.status,
        "reason_codes": list(observation.reason_codes),
        "http_status": observation.http_status,
        "model_ids": list(observation.model_ids),
        "model_count": observation.model_count,
        "requested_origin": observation.requested_origin,
        "requested_path": observation.requested_path,
        "credential_supplied": observation.credential_supplied,
        "secret_echo_detected": observation.secret_echo_detected,
        "redirect_followed": False,
        "redirect_observed": observation.redirected,
        "requests_sent": observation.requests_sent,
        "inference_requests_sent": observation.inference_requests_sent,
        "catalog_visible": observation.status == "ok" and observation.model_count > 0,
        "execution_eligible": False,
        "dispatch_eligible": False,
        "live_qualified": False,
        "qualification_scope": "declared_catalog_only",
    }
