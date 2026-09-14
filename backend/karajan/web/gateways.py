"""Authenticated HTTP commands for the gateway catalog.

The routes translate requests into trusted store commands and never accept a
caller-supplied credential, URL, path or principal. Existing Session, Origin
and CSRF middleware in ``create_app`` already protects every ``/v1/`` route
except the bootstrap endpoint, so this module adds no authentication of its
own and weakens none.

An identity that is addressable is validated with the same contract the
persistence layer uses, so a stored record is always reachable at exactly one
URL. The route rejects an unaddressable segment as a missing record rather than
storing something it cannot serve back.
"""

from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from karajan.gateway import (
    BindingCreate,
    ConnectionCreate,
    GatewayCatalogStore,
    GatewayError,
    addressable,
)

from .projects import command_key, expected_revision


def _segment(value: object, code: str) -> str:
    """Reject a path parameter that can never name a stored, addressable record."""
    if not addressable(value):
        raise HTTPException(404, {"reason_code": code})
    return str(value)


def _revision(value: int) -> int:
    if not 1 <= value <= 1_000_000:
        raise HTTPException(404, {"reason_code": "GATEWAY_REVISION_NOT_FOUND"})
    return value


def register_gateway_routes(app: FastAPI, catalog: GatewayCatalogStore) -> None:
    @app.exception_handler(GatewayError)
    async def gateway_error(request: Request, error: GatewayError) -> JSONResponse:
        status = 409 if "CONFLICT" in error.code else 404 if "NOT_FOUND" in error.code else 422
        payload: dict[str, Any] = {"reason_code": error.code}
        if error.current_revision is not None:
            status = 409
            payload["current_revision"] = error.current_revision
        return JSONResponse(payload, status_code=status)

    def created(record: dict[str, Any], was_created: bool) -> JSONResponse:
        return JSONResponse(
            record,
            status_code=201 if was_created else 200,
            headers={"ETag": f'"{record["revision"]}"'},
        )

    @app.get("/v1/projects/{project_id}/gateway-connections")
    def list_connections(project_id: str) -> dict[str, Any]:
        return {"items": catalog.list_connections(project_id, principal="owner")}

    @app.get("/v1/projects/{project_id}/gateway-connections/{connection_id}/revisions/{revision}")
    def get_connection(project_id: str, connection_id: str, revision: int) -> JSONResponse:
        connection_id = _segment(connection_id, "GATEWAY_REVISION_NOT_FOUND")
        revision = _revision(revision)
        record = catalog.get_connection(project_id, connection_id, revision, principal="owner")
        return JSONResponse(record, headers={"ETag": f'"{record["revision"]}"'})

    @app.post("/v1/projects/{project_id}/gateway-connections", status_code=201)
    def create_connection(
        project_id: str, request: Request, data: ConnectionCreate
    ) -> JSONResponse:
        record, was_created = catalog.create_connection(
            project_id,
            data.model_dump(mode="json"),
            principal="owner",
            command_key=command_key(request),
        )
        return created(record, was_created)

    @app.put("/v1/projects/{project_id}/gateway-connections/{connection_id}")
    def revise_connection(
        project_id: str, connection_id: str, request: Request, data: ConnectionCreate
    ) -> JSONResponse:
        connection_id = _segment(connection_id, "GATEWAY_CONNECTION_NOT_FOUND")
        record, was_created = catalog.revise_connection(
            project_id,
            connection_id,
            data.model_dump(mode="json"),
            expected_revision=expected_revision(request),
            principal="owner",
            command_key=command_key(request),
        )
        return created(record, was_created)

    @app.get("/v1/projects/{project_id}/gateway-bindings")
    def list_bindings(project_id: str) -> dict[str, Any]:
        return {"items": catalog.list_bindings(project_id, principal="owner")}

    @app.get("/v1/projects/{project_id}/gateway-bindings/{binding_id}/revisions/{revision}")
    def get_binding(project_id: str, binding_id: str, revision: int) -> JSONResponse:
        binding_id = _segment(binding_id, "GATEWAY_REVISION_NOT_FOUND")
        revision = _revision(revision)
        record = catalog.get_binding(project_id, binding_id, revision, principal="owner")
        return JSONResponse(record, headers={"ETag": f'"{record["revision"]}"'})

    @app.post("/v1/projects/{project_id}/gateway-bindings", status_code=201)
    def create_binding(project_id: str, request: Request, data: BindingCreate) -> JSONResponse:
        record, was_created = catalog.create_binding(
            project_id,
            data.model_dump(mode="json"),
            principal="owner",
            command_key=command_key(request),
        )
        return created(record, was_created)

    @app.put("/v1/projects/{project_id}/gateway-bindings/{binding_id}")
    def revise_binding(
        project_id: str, binding_id: str, request: Request, data: BindingCreate
    ) -> JSONResponse:
        binding_id = _segment(binding_id, "GATEWAY_BINDING_NOT_FOUND")
        record, was_created = catalog.revise_binding(
            project_id,
            binding_id,
            data.model_dump(mode="json"),
            expected_revision=expected_revision(request),
            principal="owner",
            command_key=command_key(request),
        )
        return created(record, was_created)

    @app.get("/v1/projects/{project_id}/gateway-catalog-observations")
    def list_catalog_observations(project_id: str) -> dict[str, Any]:
        return {"items": catalog.list_catalog_observations(project_id, principal="owner")}

    @app.get("/v1/projects/{project_id}/gateway-probe-claims")
    def list_probe_claims(project_id: str) -> dict[str, Any]:
        """Expose started probes so an interrupted one stays reconcilable."""
        return {"items": catalog.list_probe_claims(project_id, principal="owner")}

    @app.post(
        "/v1/projects/{project_id}/gateway-connections/{connection_id}"
        "/revisions/{revision}/catalog-probes",
        status_code=201,
    )
    def probe_catalog(
        project_id: str, connection_id: str, revision: int, request: Request
    ) -> JSONResponse:
        connection_id = _segment(connection_id, "GATEWAY_REVISION_NOT_FOUND")
        revision = _revision(revision)
        result = catalog.probe_catalog(
            project_id,
            connection_id,
            revision,
            principal="owner",
            command_key=command_key(request),
        )
        return JSONResponse(result, status_code=201)
