"""Owner-facing Run commands; model submissions remain on the trusted side."""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from karajan.conversations import ConversationStore
from karajan.runs import RunError, RunPlanner

from .projects import command_key


def register_run_routes(
    app: FastAPI, planner: RunPlanner, conversations: ConversationStore
) -> None:
    @app.exception_handler(RunError)
    async def run_error(request: Request, error: RunError) -> JSONResponse:
        if "NOT_FOUND" in error.code:
            status = 404
        elif any(
            word in error.code
            for word in ("STALE", "MISMATCH", "CONFLICT", "CHANGED", "ALREADY", "CROSS_PROJECT")
        ):
            status = 409
        else:
            status = 422
        return JSONResponse({"reason_code": error.code}, status_code=status)

    @app.post("/v1/runs", status_code=201)
    def create_run(request: Request, data: dict[str, Any]) -> JSONResponse:
        # RunPlanner owns the conversation validation and binding in the same
        # transaction as the Run insert.  Keeping this boundary thin prevents
        # an HTTP-only preflight from leaving an orphaned Run on a crash.
        result = planner.create(data, command_key=command_key(request), principal="owner")
        return JSONResponse(result, status_code=201, headers={"ETag": f'"{result["revision"]}"'})

    @app.get("/v1/runs")
    def list_runs(project_id: str | None = None) -> dict[str, Any]:
        return {
            "items": [
                {
                    **item,
                    "conversation_id": item.get("conversation_id")
                    or conversations.bound_conversation(item["id"], item["project_id"]),
                }
                for item in planner.list(principal="owner", project_id=project_id)
            ]
        }

    @app.get("/v1/runs/{run_id}")
    def get_run(run_id: str) -> JSONResponse:
        result = planner.get(run_id, principal="owner")
        result = {
            **result,
            "conversation_id": result.get("conversation_id")
            or conversations.bound_conversation(result["id"], result["project_id"]),
        }
        return JSONResponse(result, headers={"ETag": f'"{result["revision"]}"'})

    @app.post("/v1/runs/{run_id}/plan-approval")
    def approve_plan(run_id: str, request: Request, data: dict[str, Any]) -> dict[str, Any]:
        return planner.approve_plan(
            run_id, data, command_key=command_key(request), principal="owner"
        )

    @app.post("/v1/runs/{run_id}/handoff-decision")
    def decide_handoff(run_id: str, request: Request, data: dict[str, Any]) -> dict[str, Any]:
        return planner.decide_handoff(
            run_id, data, command_key=command_key(request), principal="owner"
        )
