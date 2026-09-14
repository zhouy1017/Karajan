"""Declared addresses are canonical, and declared parameters cannot be credentials."""

import pytest
from karajan.gateway import GatewayError
from karajan.gateway.models import (
    DeclaredIdentity,
    ProtocolIdentity,
    discovery_path,
    parameter_name_is_credential_shaped,
    registered_origin,
)
from pydantic import ValidationError

SENTINEL = "FAKE_SECRET_SENTINEL"


@pytest.mark.parametrize(
    ("base_url", "destination", "expected"),
    [
        ("http://127.0.0.1:8765", None, "http://127.0.0.1:8765"),
        ("http://127.0.0.1:8765/", None, "http://127.0.0.1:8765"),
        ("http://localhost:8080", None, "http://localhost:8080"),
        ("http://127.0.0.1:80", None, "http://127.0.0.1"),
        ("http://LOCALHOST:8080", None, "http://localhost:8080"),
        ("http://[::1]:45678", None, "http://[::1]:45678"),
        ("http://[::1]", None, "http://[::1]"),
        ("http://[::1]:80", None, "http://[::1]"),
        # One interface has one stored authority regardless of spelling.
        ("http://[0:0:0:0:0:0:0:1]:9000", None, "http://[::1]:9000"),
        ("http://[::0:1]:9000", None, "http://[::1]:9000"),
        ("http://[::FFFF:127.0.0.1]:9000", None, "http://[::ffff:127.0.0.1]:9000"),
        ("https://gw.example.test:8443", "vendor-eu", "https://gw.example.test:8443"),
        ("https://GW.Example.Test:4443", "vendor-eu", "https://gw.example.test:4443"),
        ("https://gw.example.test:443", "vendor-eu", "https://gw.example.test"),
        ("https://[2001:DB8::1]:8443", "vendor-eu", "https://[2001:db8::1]:8443"),
        ("https://[2001:db8:0:0:0:0:0:1]:8443", "vendor-eu", "https://[2001:db8::1]:8443"),
    ],
)
def test_registered_origin_canonicalizes_authority(
    base_url: str, destination: str | None, expected: str
) -> None:
    assert registered_origin(base_url, remote_data_destination=destination) == expected


def test_ipv6_loopback_authority_round_trips_into_a_connectable_probe_origin() -> None:
    """The stored authority must still parse back to the same host and port.

    Regression: an unbracketed ``::1:45678`` is a single address with no port,
    so the probe rejected the origin it had just been handed and never connected.
    """
    from urllib.parse import urlsplit

    stored = registered_origin("http://[0:0:0:0:0:0:0:1]:45678", remote_data_destination=None)
    parsed = urlsplit(stored)
    assert parsed.hostname == "::1"
    assert parsed.port == 45678


@pytest.mark.parametrize(
    "base_url",
    [
        "http://192.168.1.9:8080",
        "http://gateway.example.test",
        "http://127.0.0.1:0",
        "http://user:pass@127.0.0.1:8080",
        "http://127.0.0.1:8080/v1/models",
        "http://127.0.0.1:8080/anything",
        "http://127.0.0.1:8080?key=value",
        "http://127.0.0.1:8080#fragment",
        "http://[fe80::1%25eth0]:8080",
        "ftp://127.0.0.1:8080",
        "127.0.0.1:8080",
        "http://127.0.0.1:notaport",
        "http://[::1",
    ],
)
def test_registered_origin_rejects_nonregistrable_addresses(base_url: str) -> None:
    with pytest.raises(GatewayError) as error:
        registered_origin(base_url, remote_data_destination=None)
    assert error.value.code in {
        "GATEWAY_BASE_URL_INVALID",
        "GATEWAY_BASE_URL_NOT_LOOPBACK",
    }


def test_remote_plaintext_and_unattributed_tls_are_both_refused() -> None:
    """Loopback or explicit TLS-plus-destination; nothing in between."""
    with pytest.raises(GatewayError) as tls_missing_destination:
        registered_origin("https://gw.example.test", remote_data_destination=None)
    assert tls_missing_destination.value.code == "GATEWAY_DATA_DESTINATION_REQUIRED"
    with pytest.raises(GatewayError) as plaintext_remote:
        registered_origin("http://gw.example.test", remote_data_destination="vendor-eu")
    assert plaintext_remote.value.code == "GATEWAY_BASE_URL_NOT_LOOPBACK"
    with pytest.raises(GatewayError) as loopback_with_destination:
        registered_origin("http://127.0.0.1:8080", remote_data_destination="vendor-eu")
    assert loopback_with_destination.value.code == "GATEWAY_DATA_DESTINATION_UNEXPECTED"


@pytest.mark.parametrize(
    "name",
    [
        "api_key",
        "apiKey",
        "API_KEY",
        "apikey",
        "x-api-key",
        "X-Goog-Api-Key",
        "headers",
        "header",
        "Authorization",
        "authorization_header",
        "auth_token",
        "access_token",
        "bearer_token",
        "client_secret",
        "secret",
        "password",
        "passwd",
        "session_cookie",
        "set-cookie",
        "user_credential",
    ],
)
def test_credential_shaped_parameter_names_are_refused(name: str) -> None:
    assert parameter_name_is_credential_shaped(name) is True


@pytest.mark.parametrize(
    "name",
    [
        "author",
        "authors",
        "temperature",
        "top_p",
        "max_output_tokens",
        "api_version",
        "reasoning_effort",
        "response_format",
        "seed",
        "stop",
        "tokenizer",
        "tokens",
        "max_tokens",
        "secretary",
    ],
)
def test_ordinary_parameter_names_survive(name: str) -> None:
    """Names carrying no credential or header noun are unaffected."""
    assert parameter_name_is_credential_shaped(name) is False


@pytest.mark.parametrize("name", ["header_count", "credential_scope", "authorization_mode"])
def test_names_merely_mentioning_a_protected_noun_are_still_refused(name: str) -> None:
    """The boundary fails closed on ambiguous names.

    ``header_value`` can carry a credential while ``header_count`` cannot, and
    the name alone cannot separate them. Refusing both is a small, deliberate
    false-positive cost; a caller renames the parameter. Accepting the ambiguous
    case would reopen exactly the hole this validator exists to close.
    """
    assert parameter_name_is_credential_shaped(name) is True


@pytest.mark.parametrize(
    "parameters",
    [
        {"api_key": SENTINEL},
        {"apiKey": SENTINEL},
        {"headers": {"Authorization": "Bearer " + SENTINEL}},
        {"Authorization": "Bearer " + SENTINEL},
        {"client_secret": SENTINEL},
        {"request_headers": {"x-api-key": SENTINEL}},
        {"metadata": [{"api_key": SENTINEL}]},
        {"options": {"outer": {"inner": SENTINEL}}},
        {"parameters": [SENTINEL]},
        {"temperature": {"nested": 0.2}},
        {"temperature": [0.2, 0.7]},
        {"": "value"},
        {"api key": "value"},
    ],
)
def test_credential_or_container_parameters_never_validate(parameters: object) -> None:
    """Rejection happens before persistence, so nothing needs later redaction.

    The validation error is inspected via its machine-readable fields only:
    Pydantic's human-readable rendering echoes the rejected input, so the
    guarantee to assert is that no accepted object ever carries the sentinel.
    """
    with pytest.raises(ValidationError) as error:
        DeclaredIdentity(
            provider_id="provider",
            account_id="account",
            billing_path="api_cash",
            required_parameters=parameters,
        )
    rendered = [
        issue["loc"] for issue in error.value.errors(include_input=False, include_url=False)
    ]
    assert rendered
    assert all(
        isinstance(part, int | str) for location in rendered for part in location
    )


def test_noncredential_parameter_values_survive_round_trip() -> None:
    declared = DeclaredIdentity(
        provider_id="vendor",
        account_id="vendor-account",
        billing_path="subscription_only",
        required_parameters={"temperature": 0.2, "max_output_tokens": 2048, "stream": False},
    )
    assert declared.required_parameters == {
        "temperature": 0.2,
        "max_output_tokens": 2048,
        "stream": False,
    }
    assert declared.model_dump(mode="json")["required_parameters"]["temperature"] == 0.2


def test_protocol_identity_carries_no_caller_supplied_discovery_path() -> None:
    """A path is a property of the negotiated family, not a caller declaration."""
    with pytest.raises(ValidationError):
        ProtocolIdentity.model_validate(
            {"family": "openai_compatible", "protocol_version": "v1", "catalog_path": "/admin"}
        )
    assert discovery_path("openai_compatible") == "/v1/models"
    with pytest.raises(GatewayError) as unsupported:
        discovery_path("anthropic_compatible")
    assert unsupported.value.code == "GATEWAY_PROTOCOL_UNSUPPORTED"
