"""Declared external-gateway connections and strict model bindings.

Every field here is a declaration. Nothing in this module observes an upstream,
resolves a credential or grants execution eligibility; declared identity and
verification evidence are separate, and no alias or catalog entry is a source
identity (docs/architecture/08-provider-gateway.md).

Two boundaries are enforced at validation time rather than after acceptance:

* A binding declares noncredential model parameters only. Credential- or
  header-shaped keys are rejected recursively, so no credential value can be
  persisted and later "cleaned up" by redaction.
* The catalog path is derived from the negotiated protocol family. A caller
  cannot nominate an arbitrary path (management API, generation endpoint or a
  protocol-relative URL) and have the probe perform a GET on it.
"""

import ipaddress
import math
import re
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .errors import GatewayError

Identifier = Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[^\s\x00-\x1f\x7f]+$")]
Positive = Annotated[int, Field(gt=0, le=1_000_000)]

#: An identity that can be addressed as exactly one URL path segment. The dot
#: forms are rejected by :func:`addressable` rather than by this pattern, because
#: the regex engine used for schema validation has no look-around support.
PathSegment = Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9._~-]+$")]
PATH_SEGMENT = re.compile(r"^[A-Za-z0-9._~-]{1,256}$")
DOT_SEGMENTS = frozenset({".", ".."})

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
#: Compressed IPv6 loopback plus the IPv4-mapped spelling of it.
LOOPBACK_LITERALS = frozenset({"::1", "::ffff:127.0.0.1"})
DEFAULT_PORTS = {"http": 80, "https": 443}

#: A registrable authority: an IPv4 literal, an IPv6 literal (bracketed by the
#: caller) or a DNS name. Anything containing whitespace or a control character
#: is refused here rather than at connect time, where the HTTP client raises a
#: bare ``InvalidURL`` outside any structured failure path.
HOSTNAME = re.compile(
    r"^(?:"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*"
    r")$"
)
IPV6_LITERAL = re.compile(r"^[0-9A-Fa-f:.]{2,45}(?:%[0-9A-Za-z._~-]+)?$")

#: Read-only discovery surfaces, one per supported protocol family. A probe may
#: read one of these and nothing else: no management, reload or generation path.
CATALOG_PATHS = {"openai_compatible": "/v1/models"}

PARAMETER_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
NON_ALPHANUMERIC = re.compile(r"[^a-z0-9]+")

#: Tokens that name a credential, an authentication factor or a header, in any
#: position. A declared parameter is refused when *any* of its tokens is one of
#: these, so reordering cannot evade the check: ``token_auth`` is refused even
#: though ``tokenauth`` was never enumerated. The set is deliberately exhaustive
#: about credential nouns and deliberately excludes the plural ``tokens``, which
#: keeps the common numeric bound ``max_tokens`` usable.
CREDENTIAL_TOKENS = frozenset(
    {
        "apikey",
        "accesskey",
        "secretkey",
        "privatekey",
        "clientsecret",
        "authtoken",
        "accesstoken",
        "refreshtoken",
        "idtoken",
        "sessiontoken",
        "sessionkey",
        "proxyauthorization",
        "authheader",
        "api",
        "auth",
        "authorization",
        "authorizations",
        "bearer",
        "cert",
        "certificate",
        "cookie",
        "cookies",
        "cred",
        "credential",
        "credentials",
        "header",
        "headers",
        "jwt",
        "key",
        "keys",
        "oauth",
        "passphrase",
        "passwd",
        "password",
        "passwords",
        "private",
        "secret",
        "secrets",
        "session",
        "sig",
        "signature",
        "token",
        "tokenauth",
        "tokensauth",
    }
)

#: Compact spellings that survive tokenization as one token, so the per-token
#: rule alone would miss them (``accesstoken`` never splits into two tokens
#: because there is no separator or camel boundary).
CREDENTIAL_COMPOUNDS = frozenset(
    {
        "apikey",
        "apikeys",
        "accesskey",
        "secretkey",
        "privatekey",
        "publickey",
        "clientsecret",
        "authtoken",
        "accesstoken",
        "refreshtoken",
        "idtoken",
        "sessiontoken",
        "sessionkey",
        "bearertoken",
        "tokenauth",
        "authheader",
        "proxyauthorization",
        "oauthtoken",
        "jwttoken",
        "password",
        "passwd",
        "passphrase",
        "credential",
        "credentials",
    }
)

#: Named exceptions to :data:`CREDENTIAL_TOKENS`, matched on the exact token.
#: ``api_version`` is a protocol selector, not a key holder.
ALLOWED_PARAMETER_TOKENS = frozenset({"api"})

ParameterScalar = str | bool | int | float | None


def _parameter_tokens(name: str) -> list[str]:
    """Split a parameter name into comparable lowercase tokens."""
    spaced = CAMEL_BOUNDARY.sub("_", name)
    return [token for token in NON_ALPHANUMERIC.split(spaced.lower()) if token]


def parameter_name_is_credential_shaped(name: str) -> bool:
    """Whether a declared parameter name can carry a credential or a header.

    Three independent rules, all fail-closed:

    1. A compact spelling in :data:`CREDENTIAL_COMPOUNDS` (``accessToken``).
    2. Any token in :data:`CREDENTIAL_TOKENS`, regardless of position, so a
       credential qualifier cannot be reordered (``token_auth``), re-cased
       (``apiKey``) or separated (``x-api-key``) to evade the check.
    3. Every contiguous join of adjacent tokens, which catches a qualifier that
       is split only by a separator (``auth-token`` -> ``authtoken``).

    The check is intentionally fail-closed on ambiguous names: ``header_count``
    is refused because the name alone cannot separate it from ``header_value``,
    and a caller renames the parameter instead of loosening the rule.
    """
    tokens = _parameter_tokens(name)
    if not tokens:
        return True
    joined = "".join(tokens)
    if joined in CREDENTIAL_COMPOUNDS:
        return True
    for token in tokens:
        if token in ALLOWED_PARAMETER_TOKENS:
            continue
        if token in CREDENTIAL_COMPOUNDS or token in CREDENTIAL_TOKENS:
            return True
    for start in range(len(tokens)):
        compound = ""
        for end in range(start, len(tokens)):
            compound += tokens[end]
            if end > start and compound in CREDENTIAL_COMPOUNDS:
                return True
    return False


def _scalar_only(value: object, *, depth: int = 0) -> ParameterScalar:
    """Reject containers and non-finite numbers outright.

    A nested payload could hide a credential, and a non-finite float has no JSON
    representation, so persisting one would fail later inside the digest step
    rather than here. Both are refused at the same boundary.
    """
    if depth > 8:
        raise ValueError("depth")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("nonfinite")
        return value
    if isinstance(value, str):
        # A value is data, not an identity: control characters are legitimate
        # here because a stop sequence is commonly "\n". Only the length and
        # JSON-storability matter.
        if not 1 <= len(value) <= 256 or any(character == "\x00" for character in value):
            raise ValueError("string")
        return value
    raise ValueError("container")


def addressable(value: object) -> bool:
    """Whether an identity can be addressed and stored as one URL path segment."""
    return (
        isinstance(value, str)
        and value not in DOT_SEGMENTS
        and PATH_SEGMENT.fullmatch(value) is not None
    )


def canonical_host(hostname: str) -> str:
    """Return the comparable form of a host name, without brackets.

    ``urlsplit`` keeps an IPv6 literal's textual spelling, so ``[0:0:0:0:0:0:0:1]``
    and ``[::1]`` describe the same interface but compare unequal as strings.
    Parsing through ``ipaddress`` collapses those spellings, which is required
    before a loopback decision and before storing one authority for one address.
    A host name that is not a literal is returned lowercased.
    """
    try:
        return ipaddress.ip_address(hostname).compressed
    except ValueError:
        return hostname.lower()


def hostname_is_registrable(hostname: str) -> bool:
    """Whether a host name can actually be connected to by the HTTP client.

    A single label that is not an IP literal is also accepted (an internal
    short name such as ``gateway``), since it is a resolvable authority on a
    controlled network.
    """
    if not hostname or len(hostname) > 253:
        return False
    if any(character < "!" or character == "\x7f" for character in hostname):
        return False
    try:
        ipaddress.ip_address(hostname)
        return True
    except ValueError:
        pass
    if IPV6_LITERAL.fullmatch(hostname):
        # A zone-scoped literal never reaches here (``%`` is refused earlier),
        # but a colon-bearing host must still be a real IPv6 address.
        try:
            ipaddress.IPv6Address(hostname)
            return True
        except ValueError:
            return False
    return HOSTNAME.fullmatch(hostname) is not None


def discovery_path(family: str) -> str:
    """Return the single registered discovery path for a protocol family."""
    path = CATALOG_PATHS.get(family)
    if path is None:
        raise GatewayError("GATEWAY_PROTOCOL_UNSUPPORTED")
    return path


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ProtocolIdentity(Contract):
    """Negotiated client protocol.

    The catalog surface is deliberately absent: it is derived from ``family`` by
    :func:`discovery_path`, so a caller cannot assert that an arbitrary GET is
    safe model discovery.
    """

    family: Literal["openai_compatible"]
    protocol_version: Identifier


class TransformationPolicy(Contract):
    """Declared request-transformation identity; this slice never applies one."""

    policy_id: Identifier
    revision: Positive
    payload_override: bool = False
    prompt_rewrite: bool = False
    managed_tools_injected: bool = False


class ConnectionCreate(Contract):
    """A caller uploads declarations only; a credential value is never accepted.

    ``connection_id`` is addressable, so it is constrained to exactly one URL
    path segment before it can be persisted. ``secret_ref`` deliberately keeps
    the wider :data:`Identifier` shape: a reference may name a controller path
    namespace such as ``secret:team/gateway`` and is never a URL segment.
    """

    connection_id: PathSegment
    base_url: Annotated[str, Field(min_length=1, max_length=2048)]
    protocol: ProtocolIdentity
    secret_ref: Identifier | None = None
    request_transformation: TransformationPolicy
    remote_data_destination: Identifier | None = None
    capability_evidence_refs: Annotated[list[Identifier], Field(max_length=64)] = Field(
        default_factory=list
    )

    @field_validator("connection_id")
    @classmethod
    def connection_id_is_addressable(cls, value: str) -> str:
        if not addressable(value):
            raise ValueError("not addressable")
        return value


class DeclaredIdentity(Contract):
    """What the owner declares about the real source behind an alias.

    ``required_parameters`` carries noncredential model parameters only. The
    credential for a source stays behind ``secret_ref`` on the connection.
    """

    provider_id: Identifier
    account_id: Identifier
    billing_path: Literal["subscription_only", "api_cash"]
    shared_pool_ref: Identifier | None = None
    required_parameters: dict[str, ParameterScalar] = Field(default_factory=dict)

    @field_validator("required_parameters")
    @classmethod
    def noncredential_parameters(
        cls, value: dict[str, ParameterScalar]
    ) -> dict[str, ParameterScalar]:
        if len(value) > 32:
            raise ValueError("too many parameters")
        checked: dict[str, ParameterScalar] = {}
        for name, parameter in value.items():
            if not isinstance(name, str) or PARAMETER_NAME.fullmatch(name) is None:
                raise ValueError("name")
            if parameter_name_is_credential_shaped(name):
                raise ValueError("credential-shaped name")
            checked[name] = _scalar_only(parameter)
        return checked


class BindingCreate(Contract):
    """A strict binding pins one connection revision and one declared identity.

    ``binding_id`` and ``connection_id`` are addressable URL segments.
    ``model_alias`` keeps the wider :data:`Identifier` shape: a provider alias
    such as ``vendor/model-a`` is a legitimate model identifier and is never
    used as a path segment, so it must not inherit URL restrictions.
    """

    binding_id: PathSegment
    connection_id: PathSegment
    connection_revision: Positive
    model_alias: Identifier
    declared: DeclaredIdentity
    request_transformation: TransformationPolicy

    @field_validator("binding_id", "connection_id")
    @classmethod
    def identities_are_addressable(cls, value: str) -> str:
        if not addressable(value):
            raise ValueError("not addressable")
        return value

    @model_validator(mode="after")
    def connection_identities_are_storable(self) -> "BindingCreate":
        # These are re-serialized into SQL and into the stored record verbatim.
        for value in (self.binding_id, self.connection_id, self.model_alias):
            if (
                not 1 <= len(value) <= 256
                or any(character.isspace() for character in value)
                or any(character < " " or character == "\x7f" for character in value)
            ):
                raise ValueError("identifier")
        return self


def registered_origin(base_url: str, *, remote_data_destination: str | None) -> str:
    """Return the canonical ``scheme://host[:port]`` for a registrable address.

    A plain-HTTP gateway must be loopback. A remote gateway must be explicitly
    registered as TLS with a named data destination, because a bare host name is
    not an approved data path. Userinfo, query and fragment are rejected rather
    than silently dropped: a credential in the URL would become part of the
    persisted record.

    A scoped IPv6 address such as ``fe80::1%eth0`` is not stably connectable and
    its percent-encoding would not survive storage, so it is refused. Ports are
    normalized through ``urlsplit``, an explicit ``:0`` is refused, and a default
    port is omitted so one gateway has one stored authority.
    """
    try:
        parsed = urlsplit(base_url)
        port = parsed.port
        hostname = parsed.hostname
    except ValueError:
        raise GatewayError("GATEWAY_BASE_URL_INVALID") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or port == 0
        # A scoped address is not stably connectable and its percent-encoding
        # would not survive storage, so the whole URL is refused.
        or "%" in hostname
        or not hostname_is_registrable(hostname)
    ):
        raise GatewayError("GATEWAY_BASE_URL_INVALID")
    host = canonical_host(hostname)
    if parsed.scheme == "http" and host not in LOOPBACK_HOSTS | LOOPBACK_LITERALS:
        raise GatewayError("GATEWAY_BASE_URL_NOT_LOOPBACK")
    if parsed.scheme == "https":
        if remote_data_destination is None:
            raise GatewayError("GATEWAY_DATA_DESTINATION_REQUIRED")
    elif remote_data_destination is not None:
        raise GatewayError("GATEWAY_DATA_DESTINATION_UNEXPECTED")
    netloc = f"[{host}]" if ":" in host else host
    if port is None or port == DEFAULT_PORTS[parsed.scheme]:
        return f"{parsed.scheme}://{netloc}"
    return f"{parsed.scheme}://{netloc}:{port}"
