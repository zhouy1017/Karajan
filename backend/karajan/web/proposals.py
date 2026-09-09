"""HTTP adapter for immutable owner-intent plan proposals."""

from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from karajan.proposals import ProposalError, ProposalStore

from .projects import command_key


def register_proposal_routes(app: FastAPI, store: ProposalStore) -> None:
    @app.exception_handler(ProposalError)
    async def proposal_error(request: Request, error: ProposalError) -> JSONResponse:
        status = (
            404
            if error.code.endswith("NOT_FOUND")
            else 409
            if any(
                part in error.code
                for part in (
                    "STALE",
                    "MISMATCH",
                    "CONFLICT",
                    "REUSED",
                    "CROSS_PROJECT",
                    "ALREADY",
                )
            )
            else 422
        )
        return JSONResponse({"reason_code": error.code}, status_code=status)

    @app.post("/v1/conversations/{conversation_id}/proposals", status_code=201)
    def create_proposal(
        conversation_id: str, request: Request, data: dict[str, Any]
    ) -> JSONResponse:
        result = store.create(
            conversation_id, data, principal="owner", key=command_key(request)
        )
        return JSONResponse(
            result, status_code=201, headers={"ETag": f'"{result["proposal_revision"]}"'}
        )

    @app.get("/v1/conversations/{conversation_id}/proposals")
    def list_proposals(
        conversation_id: str, proposal_revision: int | None = None
    ) -> dict[str, Any]:
        return store.read(conversation_id, revision=proposal_revision)

    @app.post("/v1/conversations/{conversation_id}/proposals/{proposal_revision}/accept")
    def accept_proposal(
        conversation_id: str,
        proposal_revision: int,
        request: Request,
        data: dict[str, Any],
    ) -> JSONResponse:
        if set(data) - {"run_revision"} or type(data.get("run_revision")) is not int:
            raise HTTPException(422, {"reason_code": "PROPOSAL_INPUT_INVALID"})
        result = store.accept(
            conversation_id,
            proposal_revision,
            run_revision=data["run_revision"],
            principal="owner",
            key=command_key(request),
        )
        return JSONResponse(result, headers={"ETag": f'"{result["proposal_revision"]}"'})
