"""Trusted identities: the user session, the role protocol, the execution consumer.

Authority in this control plane is never taken from a request body. It is taken
from a *credential the service itself issued*, resolved here into an immutable
:class:`Principal` that the stores then check against their own durable rows.

Three credential kinds exist, and they are deliberately different objects with
different scopes rather than one token with a role field:

``user_session``
    Issued by the authenticated workbench session. It may create Runs, issue and
    revoke grants, delegate, and publish trusted resource observations. It may
    **not** claim work; claiming is an execution fact, not a user action.

``role_protocol``
    Issued by a management command, bound to one exact ``run`` + ``grant`` +
    ``role_instance`` + ``term``. It may submit a scheduling decision inside that
    grant's scope and nothing else. It cannot authenticate to any management
    route, and it cannot claim work.

``execution_consumer``
    Issued by a management command, bound to one exact run. It may read the queue
    and claim one ready task, and it may report a resource observation. It cannot
    submit a scheduling decision, and it cannot publish a *trusted* observation.

Two properties are structural here rather than documented:

* **Only a digest is stored.** The raw token exists once, in the response to the
  issuing command, and is never written to any table, any log or any receipt.
  What the store keeps is a sha256 of the token, so a leaked database does not
  yield a usable credential.
* **A request cannot describe itself.** ``user=true``, ``ExecutionRef``, a role
  name or a run identity in a body are inputs to no decision: the identity used
  by every store call is the resolved :class:`Principal`, and a body field that
  claims an identity is refused rather than believed.
"""

import hashlib
import hmac
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from .errors import SchedulingError, located
from .values import bounded_identifier, content_digest, non_negative_count, number

CREDENTIAL_SCHEMA_VERSION = "karajan.scheduling-credential.v1"

#: The three kinds, and the only route families each one may use.
KINDS: tuple[str, ...] = ("user_session", "role_protocol", "execution_consumer")

CredentialKind = Literal["user_session", "role_protocol", "execution_consumer"]

#: What a ``role_protocol`` credential may do, and nothing more.
ROLE_PROTOCOL_ROUTES = frozenset({"decision"})

#: What an ``execution_consumer`` credential may do. ``observe_task`` is an
#: *untrusted* observation about a task this consumer is running; ``resource``
#: is the trusted reading that decides admission, and it is deliberately not
#: granted here.
EXECUTION_CONSUMER_ROUTES = frozenset({"queue", "claim", "task", "observe_task"})

#: What a ``user_session`` credential may do. It cannot claim or observe work.
#: ``reconcile`` is the one capability that may turn a reported observation
#: into control-plane truth. It belongs to the user session alone: a scheduling
#: role and an execution consumer may describe what they believe happened, and
#: neither may declare it verified.
USER_SESSION_ROUTES = frozenset(
    {
        "run",
        "authorization",
        "grant",
        "delegation",
        "revocation",
        "resource",
        "consumer",
        "reconcile",
    }
)

ROUTES_BY_KIND: dict[str, frozenset[str]] = {
    "user_session": USER_SESSION_ROUTES,
    "role_protocol": ROLE_PROTOCOL_ROUTES,
    "execution_consumer": EXECUTION_CONSUMER_ROUTES,
}


def token_digest(token: str) -> str:
    """The sha256 of one issued token; the only form that is ever stored."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token() -> str:
    """One 256-bit URL-safe token, from the operating system's entropy source."""
    return secrets.token_urlsafe(32)


@dataclass(frozen=True, slots=True)
class IssuedCredential:
    """The result of issuing a credential. The raw token is returned exactly once.

    ``as_document`` deliberately omits the token: a receipt is stored and read
    back, and a stored token would be a credential at rest. The caller that
    issued it receives the token separately, in the command response only.
    """

    credential_id: str
    kind: CredentialKind
    run_id: str
    grant_id: str | None
    role_instance: str | None
    term: int
    issued_at: float
    expires_at: float | None
    digest: str

    def as_document(self) -> dict[str, object]:
        return {
            "schema_version": CREDENTIAL_SCHEMA_VERSION,
            "credential_id": self.credential_id,
            "kind": self.kind,
            "run_id": self.run_id,
            "grant_id": self.grant_id,
            "role_instance": self.role_instance,
            "term": self.term,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "token_digest": self.digest,
            "routes": sorted(ROUTES_BY_KIND[self.kind]),
            "stores_raw_token": False,
        }


@dataclass(frozen=True, slots=True)
class Principal:
    """The resolved, trusted identity one request acts as.

    Everything a store needs to make an authority decision is here, and it was
    read from the durable credential row rather than from the request. The
    ``token`` field is the raw presented token, kept only so a revocation can
    name the exact credential that was used; it is never stored or echoed.
    """

    credential_id: str
    kind: CredentialKind
    runs: frozenset[str]
    grant_id: str | None
    role_instance: str | None
    term: int
    routes: frozenset[str]
    token: str = ""

    @property
    def is_user(self) -> bool:
        return self.kind == "user_session"

    def require_route(self, route: str) -> None:
        """Refuse a credential whose kind does not carry this capability.

        This is the whole separation between the three interfaces. A protocol
        credential that reaches a management route is refused *here*, before any
        store is consulted, so a role identity cannot even attempt a management
        command under its own legitimate token.
        """
        if route not in self.routes:
            raise SchedulingError(
                "SCHEDULING_CREDENTIAL_SCOPE_INSUFFICIENT",
                fields={"kind": self.kind, "required_route": route},
                diagnostics=[
                    located(
                        "SCHEDULING_CREDENTIAL_SCOPE_INSUFFICIENT",
                        f"grant#/{route}",
                        "this credential kind does not carry that capability",
                    )
                ],
            )

    def require_run(self, run_id: str) -> None:
        """Refuse a run this credential was not issued for."""
        if run_id not in self.runs:
            raise SchedulingError(
                "SCHEDULING_RUN_NOT_FOUND",
                diagnostics=[
                    located(
                        "SCHEDULING_RUN_NOT_FOUND",
                        "run#/id",
                        "this credential is not issued for that run",
                    )
                ],
            )

    def require_grant(self, grant_id: str) -> None:
        """Refuse a grant this credential was not issued for.

        The comparison is on encoded bytes rather than on two ``str`` objects:
        ``hmac.compare_digest`` refuses a non-ASCII ``str``, and an identity this
        module accepts (``grant-授权``) is still a value a caller may submit, so a
        comparison on ``str`` would raise instead of answering.
        """
        if self.grant_id is not None and not hmac.compare_digest(
            self.grant_id.encode("utf-8"), grant_id.encode("utf-8")
        ):
            raise SchedulingError(
                "SCHEDULING_GRANT_NOT_FOUND",
                diagnostics=[
                    located(
                        "SCHEDULING_GRANT_NOT_FOUND",
                        "grant#/id",
                        "this credential is bound to a different grant",
                    )
                ],
            )


@dataclass(frozen=True, slots=True)
class CredentialRecord:
    """One durable credential row, as read back from the store."""

    credential_id: str
    kind: CredentialKind
    run_id: str
    grant_id: str | None
    role_instance: str | None
    term: int
    digest: str
    issued_at: float
    expires_at: float | None
    revoked_at: float | None
    revoked_reason: str | None
    delegated_from: str | None
    document: dict[str, object]

    @property
    def live(self) -> bool:
        return self.revoked_at is None

    def as_document(self) -> dict[str, object]:
        return dict(self.document)


def parse_issued(document: dict[str, object]) -> IssuedCredential:
    """Rebuild the immutable identity of one issued credential from its record."""
    return IssuedCredential(
        credential_id=str(document["credential_id"]),
        kind=validate_kind(document.get("kind")),
        run_id=str(document["run_id"]),
        grant_id=None if document.get("grant_id") is None else str(document["grant_id"]),
        role_instance=(
            None if document.get("role_instance") is None else str(document["role_instance"])
        ),
        term=int(str(document["term"])),
        issued_at=float(str(document["issued_at"])),
        expires_at=(
            None
            if document.get("expires_at") is None
            else float(str(document["expires_at"]))
        ),
        digest=str(document["token_digest"]),
    )


def validate_kind(value: object) -> CredentialKind:
    if not isinstance(value, str) or value not in KINDS:
        raise SchedulingError(
            "SCHEDULING_CREDENTIAL_KIND_INVALID",
            diagnostics=[
                located(
                    "SCHEDULING_CREDENTIAL_KIND_INVALID",
                    "grant#/credential/kind",
                    "a credential kind is one of the three issued kinds",
                )
            ],
        )
    return value  # type: ignore[return-value]


def validate_issue_request(value: object, *, clock: Callable[[], float]) -> dict[str, object]:
    """Validate one management credential-issuing command.

    ``authorized_by`` is the *scope* the grant named for this consumer, not the
    acting user: the user identity comes from the session, never from the body.
    """
    if not isinstance(value, dict):
        raise SchedulingError("SCHEDULING_INPUT_INVALID")
    unknown = sorted(set(value) - {"kind", "run_id", "grant_id", "role_instance", "expires_in"})
    if unknown:
        raise SchedulingError(
            "SCHEDULING_INPUT_INVALID",
            diagnostics=[
                located("SCHEDULING_INPUT_INVALID", "grant#/credential", "unknown field")
                for _ in unknown[:8]
            ],
        )
    kind = validate_kind(value.get("kind"))
    run_id = bounded_identifier(value.get("run_id"), "SCHEDULING_RUN_INVALID", "run#/id")
    grant_id = (
        None
        if value.get("grant_id") is None
        else bounded_identifier(value.get("grant_id"), "SCHEDULING_GRANT_INVALID", "grant#/id")
    )
    role_instance = (
        None
        if value.get("role_instance") is None
        else bounded_identifier(
            value.get("role_instance"), "SCHEDULING_ROLE_INVALID", "grant#/role_instance"
        )
    )
    if kind == "role_protocol" and (grant_id is None or role_instance is None):
        raise SchedulingError(
            "SCHEDULING_CREDENTIAL_SUBJECT_REQUIRED",
            diagnostics=[
                located(
                    "SCHEDULING_CREDENTIAL_SUBJECT_REQUIRED",
                    "grant#/credential",
                    "a role protocol credential is bound to a grant and a role instance",
                )
            ],
        )
    expires_at: float | None = None
    seconds: float | None = None
    if value.get("expires_in") is not None:
        seconds = number(value.get("expires_in"), "SCHEDULING_INPUT_INVALID", "grant#/expires_in")
        if seconds <= 0:
            raise SchedulingError("SCHEDULING_INPUT_INVALID")
        expires_at = clock() + seconds
    return {
        "kind": kind,
        "run_id": run_id,
        "grant_id": grant_id,
        "role_instance": role_instance,
        "expires_at": expires_at,
        # The duration exactly as the caller stated it, kept separate from the
        # absolute expiry so the issuing command can be identified by what was
        # requested rather than by when it ran.
        "requested_seconds": None if value.get("expires_in") is None else seconds,
    }


def credential_identity(
    *,
    kind: str,
    run_id: str,
    grant_id: str | None,
    role_instance: str | None,
    term: int,
    expires_at: float | None,
) -> str:
    """The digest that identifies one issued capability.

    It covers the exact binding the credential was issued for - kind, run, grant,
    role instance, term and expiry - so a credential issued for one grant cannot
    be replayed as another, and the identity is stable on a replay of the issuing
    command rather than depending on when it was issued.
    """
    return content_digest(
        {
            "kind": kind,
            "run_id": run_id,
            "grant_id": grant_id,
            "role_instance": role_instance,
            "term": non_negative_count(term, "SCHEDULING_INPUT_INVALID", "grant#/term"),
            "expires_at": expires_at,
        }
    )
