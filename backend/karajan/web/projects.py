"""Translate authenticated HTTP commands to the trusted project registry."""

import re
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from karajan.projects import ProjectError, ProjectRegistry


def command_key(request: Request) -> str:
    keys = request.headers.getlist("idempotency-key")
    if len(keys) != 1 or re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", keys[0]) is None:
        raise HTTPException(400, {"reason_code": "COMMAND_KEY_REQUIRED"})
    return keys[0]


def expected_revision(request: Request) -> int:
    values = request.headers.getlist("if-match")
    match = re.fullmatch(r'"([1-9][0-9]{0,11})"', values[0]) if len(values) == 1 else None
    if match is None:
        raise HTTPException(428, {"reason_code": "REVISION_REQUIRED"})
    return int(match.group(1))


class ApplyInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    preview_id: str


def register_project_routes(app: FastAPI, registry: ProjectRegistry) -> None:
    @app.exception_handler(ProjectError)
    async def project_error(request: Request, error: ProjectError) -> JSONResponse:
        status = 409 if "CONFLICT" in error.code else 404 if "NOT_FOUND" in error.code else 422
        payload: dict[str, Any] = {"reason_code": error.code}
        if error.current_revision is not None:
            status = 409
            payload["current_revision"] = error.current_revision
        return JSONResponse(payload, status_code=status)

    @app.post("/v1/projects", status_code=201)
    def create_project(request: Request, data: dict[str, Any]) -> JSONResponse:
        result = registry.create(data, command_key=command_key(request), principal="owner")
        return JSONResponse(result, status_code=201, headers={"ETag": f'"{result["revision"]}"'})

    @app.get("/v1/projects")
    def list_projects() -> dict[str, Any]:
        return {"items": registry.list()}

    @app.get("/v1/projects/{project_id}")
    def get_project(project_id: str) -> JSONResponse:
        result = registry.get(project_id)
        return JSONResponse(result, headers={"ETag": f'"{result["revision"]}"'})

    @app.get("/v1/projects/{project_id}/configuration")
    def get_configuration(project_id: str) -> dict[str, Any]:
        return registry.get_configuration(project_id)

    @app.patch("/v1/projects/{project_id}")
    def update_project(project_id: str, request: Request, data: dict[str, Any]) -> JSONResponse:
        result = registry.update(
            project_id,
            data,
            expected_revision=expected_revision(request),
            command_key=command_key(request),
            principal="owner",
        )
        return JSONResponse(result, headers={"ETag": f'"{result["revision"]}"'})

    @app.post("/v1/projects/{project_id}/configuration/preview")
    def preview_configuration(
        project_id: str, request: Request, data: dict[str, Any]
    ) -> dict[str, Any]:
        return registry.preview_configuration(
            project_id, data, command_key=command_key(request), principal="owner"
        )

    @app.post("/v1/projects/{project_id}/configuration/apply")
    def apply_configuration(project_id: str, request: Request, data: ApplyInput) -> JSONResponse:
        result = registry.apply_configuration(
            project_id,
            data.preview_id,
            expected_revision=expected_revision(request),
            command_key=command_key(request),
            principal="owner",
        )
        return JSONResponse(result, headers={"ETag": f'"{result["revision"]}"'})

    @app.get("/v1/projects/{project_id}/rulebook/versions")
    def rulebook_versions(project_id: str) -> dict[str, Any]:
        registry.get(project_id)
        return {"items": registry.list_rulebook_versions(project_id)}

    @app.get("/v1/projects/{project_id}/rulebook/publications")
    def rulebook_publications(project_id: str) -> dict[str, Any]:
        registry.get(project_id)
        return {"items": registry.list_rulebook_publications(project_id)}

    @app.post("/v1/projects/{project_id}/rulebook/preview")
    def preview_rulebook(
        project_id: str,
        request: Request,
        data: dict[str, Any],
    ) -> JSONResponse:
        result = registry.preview_rulebook(
            project_id,
            data,
            expected_revision=expected_revision(request),
            command_key=command_key(request),
            principal="owner",
        )
        return JSONResponse(result, headers={"ETag": f'"{result["project_revision"]}"'})

    @app.post("/v1/projects/{project_id}/rulebook/publish")
    def publish_rulebook(
        project_id: str,
        request: Request,
        data: ApplyInput,
    ) -> JSONResponse:
        result = registry.publish_rulebook(
            project_id,
            data.preview_id,
            expected_revision=expected_revision(request),
            command_key=command_key(request),
            principal="owner",
        )
        return JSONResponse(result, headers={"ETag": f'"{result["project_revision"]}"'})
