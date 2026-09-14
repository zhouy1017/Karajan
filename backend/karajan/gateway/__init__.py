"""Catalog of external gateway connections and strict, versioned model bindings.

This package holds declarations, durable revisions and a read-only catalog
probe. It performs no inference, resolves no account credential by itself,
follows no redirect, performs no automatic fallback and grants no dispatch
eligibility. See [外置网关](../../../docs/architecture/08-provider-gateway.md).
"""

from .catalog import GatewayCatalogStore, content_digest, sealed
from .errors import GatewayError
from .models import (
    BindingCreate,
    ConnectionCreate,
    DeclaredIdentity,
    ProtocolIdentity,
    TransformationPolicy,
    addressable,
    discovery_path,
    parameter_name_is_credential_shaped,
    registered_origin,
)
from .probe import (
    CatalogObservation,
    LocalFileSecretResolver,
    SessionSecretResolver,
    probe_catalog,
    public_observation,
)

__all__ = [
    "BindingCreate",
    "CatalogObservation",
    "ConnectionCreate",
    "DeclaredIdentity",
    "GatewayCatalogStore",
    "GatewayError",
    "LocalFileSecretResolver",
    "ProtocolIdentity",
    "SessionSecretResolver",
    "TransformationPolicy",
    "addressable",
    "content_digest",
    "discovery_path",
    "parameter_name_is_credential_shaped",
    "probe_catalog",
    "public_observation",
    "registered_origin",
    "sealed",
]
