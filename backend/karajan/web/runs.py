"""Owner-facing Run commands; model submissions remain on the trusted side."""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from karajan.conversations import ConversationError, ConversationStore
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
    def list_runs(
        project_id: str | None = None, conversation_id: str | None = None
    ) -> dict[str, Any]:
        if conversation_id is not None:
            conversation_project = conversations.conversation_project(conversation_id)
            if project_id is not None and project_id != conversation_project:
                raise ConversationError("CROSS_PROJECT_REFERENCE")
            project_id = conversation_project
        return {
            "items": [
                _run_response(item, conversations)
                for item in planner.list(principal="owner", project_id=project_id)
                if conversation_id is None
                or conversations.run_binding(item["id"], item["project_id"])["conversation_id"]
                == conversation_id
            ]
        }

    @app.get("/v1/conversations/{conversation_id}/runs")
    def list_conversation_runs(conversation_id: str) -> dict[str, Any]:
        project_id = conversations.conversation_project(conversation_id)
        return {
            "items": [
                _run_response(item, conversations)
                for item in planner.list(principal="owner", project_id=project_id)
                if conversations.run_binding(item["id"], item["project_id"])["conversation_id"]
                == conversation_id
            ]
        }

    @app.get("/v1/runs/{run_id}")
    def get_run(run_id: str) -> JSONResponse:
        result = planner.get(run_id, principal="owner")
        result = _run_response(result, conversations)
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


def _run_response(item: dict[str, Any], conversations: ConversationStore) -> dict[str, Any]:
    """Overlay the normalized migration identity without rewriting Run receipts."""
    binding = conversations.run_binding(item["id"], item["project_id"])
    snapshot_conversation = item.get("conversation_id")
    if (
        snapshot_conversation is not None
        and binding["conversation_id"] is not None
        and snapshot_conversation != binding["conversation_id"]
    ):
        raise ConversationError("CROSS_PROJECT_REFERENCE")
    return {
        **item,
        "conversation_id": binding["conversation_id"],
        **(
            {"conversation_recovery_blocker": binding["recovery_blocker"]}
            if binding["recovery_blocker"] is not None
            else {}
        ),
    }
