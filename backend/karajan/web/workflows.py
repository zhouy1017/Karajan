"""Authenticated HTTP commands for workflow bundles and their preview.

The routes translate requests into trusted store commands. They accept no
credential, path, host location or principal from a caller: the acting principal
is the authenticated session, and every record the store returns is scoped to the
project that owns it. Existing Session, Origin and CSRF middleware in
``create_app`` already protects every ``/v1/`` route, so this module adds no
authentication of its own and weakens none.

A rejection is returned as a located document. A path the caller submitted is
never echoed as a location: it appears, truncated, only inside a detail string.
"""

from typing import Annotated, Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from karajan.workflows import DeploymentStore, WorkflowError, WorkflowStore

from .projects import command_key, expected_revision


class DeclaredFile(BaseModel):
    """One bundle file, exactly as the caller supplies the bytes as text."""

    model_config = ConfigDict(extra="forbid", strict=True)
    path: Annotated[str, Field(min_length=1, max_length=200)]
    content: Annotated[str, Field(max_length=1_048_576)]


class BundlePayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    files: Annotated[list[DeclaredFile], Field(min_length=1, max_length=256)]
    delivery_kind: Literal["report", "patch", "pr"]


class EditOperation(BaseModel):
    """One structural table edit; an unknown field is refused, not ignored."""

    model_config = ConfigDict(extra="forbid", strict=True)
    operation: Literal[
        "set_dependency",
        "remove_dependency",
        "set_role",
        "set_required",
        "add_step",
        "remove_step",
        "set_step_input",
    ]
    step_id: Annotated[str, Field(min_length=1, max_length=96)]
    depends_on: Annotated[str, Field(max_length=96)] | None = None
    role: Annotated[str, Field(min_length=1, max_length=96)] | None = None
    required: bool | None = None
    name: Annotated[str, Field(min_length=1, max_length=96)] | None = None
    value: str | None = None
    execution_kind: Annotated[str, Field(max_length=96)] | None = None
    output_contract: Annotated[str, Field(max_length=96)] | None = None
    inputs: dict[str, Any] | None = None
    condition: dict[str, Any] | None = None
    join: Annotated[str, Field(max_length=32)] | None = None


class EditPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    edits: Annotated[list[EditOperation], Field(min_length=1, max_length=256)]


class AuthoringPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    instruction: Annotated[str, Field(min_length=1, max_length=8_000)]
    base_revision: Annotated[int, Field(gt=0, le=1_000_000)] | None = None


class DeployPayload(BaseModel):
    """An explicit deploy_only confirmation, bound to the confirmed preview.

    There is no field for the bundle, the conversation or any digest: the route
    already names the bundle and the conversation, and every identity is derived
    server-side from the real published files. A caller therefore cannot state a
    template identity the stored bytes would not satisfy.
    """

    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["deploy_only", "deploy_and_run"]
    slot: Annotated[str, Field(min_length=1, max_length=96)] | None = None
    expected_active_revision: Annotated[int, Field(ge=0, le=1_000_000)]
    preview_id: Annotated[str, Field(min_length=64, max_length=64)]


class RollbackPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    target_deployment_id: Annotated[str, Field(min_length=1, max_length=96)]
    expected_active_revision: Annotated[int, Field(ge=0, le=1_000_000)]


def _segment(value: object, code: str) -> str:
    """Reject a path parameter that can never name a stored, addressable record."""
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 96
        or "/" in value
        or "\\" in value
        or value.startswith(".")
    ):
        raise HTTPException(404, {"reason_code": code})
    return value


def _revision(value: int) -> int:
    if not 1 <= value <= 1_000_000:
        raise HTTPException(404, {"reason_code": "WORKFLOW_BUNDLE_NOT_FOUND"})
    return value


def register_workflow_routes(app: FastAPI, store: WorkflowStore) -> None:
    @app.exception_handler(WorkflowError)
    async def workflow_error(request: Request, error: WorkflowError) -> JSONResponse:
        del request
        document = error.document()
        # A conflict is a durable-state disagreement the caller can resolve by
        # re-reading; a missing record is a 404; everything else the caller sent
        # or that the stored bytes no longer satisfy is a refused input.
        conflict = (
            error.code.endswith("CONFLICT")
            or "MISMATCH" in error.code
            or "CHANGED" in error.code
            or "STALE" in error.code
            or "ALREADY_MATERIALIZED" in error.code
            # A published revision that no longer matches what it was verified
            # against is stale state, not a bad request: the caller must re-read.
            or error.code
            in {
                "WORKFLOW_UNDECLARED_FILE",
                "WORKFLOW_FILE_DIGEST_MISMATCH",
                "WORKFLOW_BUNDLE_DIGEST_MISMATCH",
                "WORKFLOW_COMPILE_MISMATCH",
                "WORKFLOW_COMPILER_REVISION_MISMATCH",
                "WORKFLOW_RECORD_CHANGED",
                "WORKFLOW_FILE_INVENTORY_MISMATCH",
                # A slot or a package that no longer holds what it recorded is a
                # state disagreement the caller resolves by re-reading, not a
                # malformed request.
                "WORKFLOW_UNCOMMITTED_MATERIALIZATION_MISMATCH",
                "WORKFLOW_DEPLOYMENT_STATE_CHANGED",
                "WORKFLOW_DEPLOYMENT_RECORD_CHANGED",
                "WORKFLOW_PACKAGE_MISSING",
                # Nothing is consumable before a deployment is really active;
                # the caller resolves this by deploying, not by fixing a request.
                "WORKFLOW_SLOT_EMPTY",
            }
        )
        status = (
            409
            if conflict
            else 404
            if error.code.endswith("NOT_FOUND")
            else 501
            if error.code == "WORKFLOW_DEPLOY_ACTION_UNSUPPORTED"
            else 422
        )
        return JSONResponse(document, status_code=status)

    def published(record: dict[str, Any], was_created: bool) -> JSONResponse:
        return JSONResponse(
            record,
            status_code=201 if was_created else 200,
            headers={"ETag": f'"{record["revision"]}"'},
        )

    @app.get("/v1/projects/{project_id}/workflows")
    def list_bundles(project_id: str) -> dict[str, Any]:
        return {"items": store.list_bundles(project_id, principal="owner")}

    @app.get("/v1/projects/{project_id}/workflow-execution-kinds")
    def list_execution_kinds(project_id: str) -> dict[str, Any]:
        """The trusted registry: which revisions have a real adapter here."""
        return store.catalog(project_id, principal="owner")

    @app.post(
        "/v1/projects/{project_id}/conversations/{conversation_id}"
        "/workflows/{bundle_id}",
        status_code=201,
    )
    def create_bundle(
        project_id: str,
        conversation_id: str,
        bundle_id: str,
        request: Request,
        data: BundlePayload,
    ) -> JSONResponse:
        bundle_id = _segment(bundle_id, "WORKFLOW_BUNDLE_ID_INVALID")
        record, was_created = store.create_bundle(
            project_id,
            conversation_id,
            bundle_id,
            data.model_dump(mode="json"),
            principal="owner",
            command_key=command_key(request),
        )
        return published(record, was_created)

    @app.put(
        "/v1/projects/{project_id}/conversations/{conversation_id}"
        "/workflows/{bundle_id}"
    )
    def revise_bundle(
        project_id: str,
        conversation_id: str,
        bundle_id: str,
        request: Request,
        data: BundlePayload,
    ) -> JSONResponse:
        bundle_id = _segment(bundle_id, "WORKFLOW_BUNDLE_NOT_FOUND")
        record, was_created = store.revise_bundle(
            project_id,
            conversation_id,
            bundle_id,
            data.model_dump(mode="json"),
            expected_revision=expected_revision(request),
            principal="owner",
            command_key=command_key(request),
        )
        return published(record, was_created)

    @app.get(
        "/v1/projects/{project_id}/workflows/{bundle_id}/revisions/{revision}"
    )
    def get_bundle(project_id: str, bundle_id: str, revision: int) -> JSONResponse:
        bundle_id = _segment(bundle_id, "WORKFLOW_BUNDLE_NOT_FOUND")
        revision = _revision(revision)
        record = store.get_bundle(project_id, bundle_id, revision, principal="owner")
        return JSONResponse(record, headers={"ETag": f'"{record["revision"]}"'})

    @app.get(
        "/v1/projects/{project_id}/workflows/{bundle_id}/revisions/{revision}/preview"
    )
    def preview_bundle(
        project_id: str, bundle_id: str, revision: int, compare_to: int | None = None
    ) -> dict[str, Any]:
        bundle_id = _segment(bundle_id, "WORKFLOW_BUNDLE_NOT_FOUND")
        revision = _revision(revision)
        if compare_to is not None:
            compare_to = _revision(compare_to)
        return store.preview(
            project_id,
            bundle_id,
            revision,
            compare_to=compare_to,
            principal="owner",
        )

    @app.post(
        "/v1/projects/{project_id}/conversations/{conversation_id}"
        "/workflows/{bundle_id}/edits"
    )
    def edit_bundle(
        project_id: str,
        conversation_id: str,
        bundle_id: str,
        request: Request,
        data: EditPayload,
    ) -> JSONResponse:
        """A structural edit: deterministic, and provably free of model calls."""
        bundle_id = _segment(bundle_id, "WORKFLOW_BUNDLE_NOT_FOUND")
        record, was_created = store.edit(
            project_id,
            conversation_id,
            bundle_id,
            data.model_dump(mode="json", exclude_none=True),
            expected_revision=expected_revision(request),
            principal="owner",
            command_key=command_key(request),
        )
        return published(record, was_created)

    @app.post(
        "/v1/projects/{project_id}/conversations/{conversation_id}"
        "/workflows/{bundle_id}/authoring-inputs",
        status_code=201,
    )
    def create_authoring_input(
        project_id: str,
        conversation_id: str,
        bundle_id: str,
        request: Request,
        data: AuthoringPayload,
    ) -> JSONResponse:
        """Persist design text as pending input; no configuration is generated."""
        bundle_id = _segment(bundle_id, "WORKFLOW_BUNDLE_NOT_FOUND")
        record, was_created = store.authoring(
            project_id,
            conversation_id,
            bundle_id,
            data.model_dump(mode="json", exclude_none=True),
            principal="owner",
            command_key=command_key(request),
        )
        return JSONResponse(record, status_code=201 if was_created else 200)

    @app.get(
        "/v1/projects/{project_id}/workflows/{bundle_id}/authoring-inputs"
    )
    def list_authoring_inputs(project_id: str, bundle_id: str) -> dict[str, Any]:
        bundle_id = _segment(bundle_id, "WORKFLOW_BUNDLE_NOT_FOUND")
        return {"items": store.list_authoring_inputs(project_id, bundle_id, principal="owner")}


def register_deployment_routes(app: FastAPI, deployments: DeploymentStore) -> None:
    """The authenticated deployment, readback and rollback commands.

    Deployment is its own resource rather than a field on a bundle: a bundle is a
    saved definition, and a deployment is the fact that one exact revision was
    materialised, loaded and made active in a slot. The routes accept no
    credential, path, digests or principal; the acting principal is the
    authenticated session, and every derived identity comes from the real files.
    """

    def _segment_or(value: str, code: str) -> str:
        return _segment(value, code)

    @app.post(
        "/v1/projects/{project_id}/conversations/{conversation_id}"
        "/workflows/{bundle_id}/revisions/{revision}/deployments",
        status_code=201,
    )
    def deploy(
        project_id: str,
        conversation_id: str,
        bundle_id: str,
        revision: int,
        request: Request,
        data: DeployPayload,
    ) -> JSONResponse:
        bundle_id = _segment_or(bundle_id, "WORKFLOW_BUNDLE_NOT_FOUND")
        revision = _revision(revision)
        payload = data.model_dump(mode="json", exclude_none=True)
        result, was_created = deployments.deploy(
            project_id,
            conversation_id,
            bundle_id,
            payload,
            preview_revision=revision,
            principal="owner",
            command_key=command_key(request),
        )
        return JSONResponse(
            result,
            status_code=201 if was_created else 200,
            headers={"ETag": f'"{result["slot"]["slot_revision"]}"'},
        )

    @app.get("/v1/projects/{project_id}/workflow-deployments/{slot}")
    def deployment_status(project_id: str, slot: str) -> dict[str, Any]:
        """The slot's present state, re-loaded from the real files on each call."""
        return deployments.status(project_id, slot, principal="owner")

    @app.get("/v1/projects/{project_id}/workflow-deployments/{slot}/history")
    def deployment_history(project_id: str, slot: str) -> dict[str, Any]:
        return {
            "items": deployments.list_deployments(project_id, slot, principal="owner"),
            "intents": deployments.pending(project_id, slot, principal="owner"),
        }

    @app.get(
        "/v1/projects/{project_id}/workflow-deployments/{slot}/deployments/{deployment_id}"
    )
    def deployment_detail(project_id: str, slot: str, deployment_id: str) -> dict[str, Any]:
        return deployments.deployment(
            project_id, slot, _segment_or(deployment_id, "WORKFLOW_DEPLOYMENT_NOT_FOUND"),
            principal="owner",
        )

    @app.get("/v1/projects/{project_id}/workflow-deployments/{slot}/definition")
    def deployment_definition(project_id: str, slot: str) -> dict[str, Any]:
        """The trusted consumer handle for the loaded and active definition.

        Acquisition re-loads the active package in this process, so a handle is
        only ever obtained from a definition this process has really re-read. The
        handle is immutable: a later deployment or rollback changes the slot, not
        a handle a consumer already holds. It is a definition, not an execution.
        """
        return deployments.accept(project_id, slot, principal="owner").as_document()

    @app.post("/v1/projects/{project_id}/workflow-rollbacks", status_code=201)
    def rollback(project_id: str, request: Request, data: RollbackPayload) -> JSONResponse:
        """Re-activate one exact historical deployment through the same pipeline."""
        result, was_created = deployments.rollback(
            project_id,
            data.model_dump(mode="json"),
            principal="owner",
            command_key=command_key(request),
        )
        return JSONResponse(
            result,
            status_code=201 if was_created else 200,
            headers={"ETag": f'"{result["slot"]["slot_revision"]}"'},
        )


def _bundle_id(request: Request) -> str:
    """The bundle identity for a create: one addressable segment, never a path."""
    values = request.headers.getlist("x-workflow-bundle-id")
    if len(values) != 1:
        raise HTTPException(400, {"reason_code": "WORKFLOW_BUNDLE_ID_REQUIRED"})
    return _segment(values[0], "WORKFLOW_BUNDLE_ID_INVALID")
