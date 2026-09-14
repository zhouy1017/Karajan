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
import re
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .errors import GatewayError

Identifier = Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[^\s\x00-\x1f\x7f]+$")]
Positive = Annotated[int, Field(gt=0, le=1_000_000)]

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
#: Compressed IPv6 loopback plus the IPv4-mapped spelling of it.
LOOPBACK_LITERALS = frozenset({"::1", "::ffff:127.0.0.1"})
DEFAULT_PORTS = {"http": 80, "https": 443}

#: Read-only discovery surfaces, one per supported protocol family. A probe may
#: read one of these and nothing else: no management, reload or generation path.
CATALOG_PATHS = {"openai_compatible": "/v1/models"}

PARAMETER_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
NON_ALPHANUMERIC = re.compile(r"[^a-z0-9]+")

#: Whole-name matches in joined-token form. A name is refused when any
#: contiguous join of its tokens appears here, so ``api_key``, ``apiKey`` and
#: ``x-api-key`` all normalize to ``apikey``. Single-token names are listed in
#: the same set; ordinary qualifiers stay accepted, because ``token_limit`` is a
#: numeric bound and ``header_count`` is a count, not a header.
FORBIDDEN_PARAMETER_NAMES = frozenset(
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
        "bearertoken",
        "sessionkey",
        "sessiontoken",
        "password",
        "passwd",
        "passphrase",
        "credential",
        "credentials",
        "secret",
        "authorization",
        "authheader",
        "proxyauthorization",
        "cookie",
        "cookies",
        "header",
        "headers",
    }
)

ParameterScalar = str | bool | int | float | None


def _parameter_tokens(name: str) -> list[str]:
    """Split a parameter name into comparable lowercase tokens."""
    spaced = CAMEL_BOUNDARY.sub("_", name)
    return [token for token in NON_ALPHANUMERIC.split(spaced.lower()) if token]


def parameter_name_is_credential_shaped(name: str) -> bool:
    """Whether a declared parameter name can carry a credential or a header.

    Every contiguous join of the name's tokens is compared against
    :data:`FORBIDDEN_PARAMETER_NAMES`, so a credential qualifier cannot be moved
    to another position (``auth_token``/``token_auth``) or re-cased to slip
    through, while ordinary parameters such as ``author``, ``max_tokens`` or
    ``api_version`` are unaffected.
    """
    tokens = _parameter_tokens(name)
    if not tokens:
        return True
    for start in range(len(tokens)):
        joined = ""
        for end in range(start, len(tokens)):
            joined += tokens[end]
            if joined in FORBIDDEN_PARAMETER_NAMES:
                return True
    return False


def _scalar_only(value: object, *, depth: int = 0) -> ParameterScalar:
    """Reject containers outright: a nested payload cannot hide a credential.

    Recursion is still explicit so a future nested type cannot silently reopen
    the boundary; today any mapping or sequence is refused at the first level.
    """
    if depth > 8:
        raise ValueError("depth")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, str):
        if not 1 <= len(value) <= 256 or any(character < " " for character in value):
            raise ValueError("string")
        return value
    raise ValueError("container")


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
    """A caller uploads declarations only; a credential value is never accepted."""

    connection_id: Identifier
    base_url: Annotated[str, Field(min_length=1, max_length=2048)]
    protocol: ProtocolIdentity
    secret_ref: Identifier | None = None
    request_transformation: TransformationPolicy
    remote_data_destination: Identifier | None = None
    capability_evidence_refs: Annotated[list[Identifier], Field(max_length=64)] = Field(
        default_factory=list
    )


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
    binding_id: Identifier
    connection_id: Identifier
    connection_revision: Positive
    model_alias: Identifier
    declared: DeclaredIdentity
    request_transformation: TransformationPolicy

    @model_validator(mode="after")
    def connection_identities_are_plain(self) -> "BindingCreate":
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
        or "%" in hostname
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
