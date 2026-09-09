"""HTTP adapter for the Commander conversation domain."""

import json
from collections.abc import Iterator
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from karajan.conversations import ConversationError, ConversationStore

from .projects import command_key, expected_revision


def register_conversation_routes(app: FastAPI, store: ConversationStore) -> None:
    @app.exception_handler(ConversationError)
    async def conversation_error(request: Request, error: ConversationError) -> JSONResponse:
        status = (
            404
            if error.code.endswith("NOT_FOUND")
            else 409
            if any(marker in error.code for marker in ("CONFLICT", "CROSS_PROJECT", "REUSED"))
            else 422
        )
        payload: dict[str, Any] = {"reason_code": error.code}
        if error.revision is not None:
            payload["current_revision"] = error.revision
        return JSONResponse(payload, status_code=status)

    @app.get("/v1/projects/{project_id}/conversations")
    def list_conversations(project_id: str) -> dict[str, Any]:
        return {"items": store.list(project_id)}

    @app.get("/v1/projects/{project_id}/commander-options")
    def get_commander_options(project_id: str) -> dict[str, Any]:
        return {"items": store.commander_options(project_id)}

    @app.post("/v1/projects/{project_id}/conversations", status_code=201)
    def create_conversation(
        project_id: str, request: Request, data: dict[str, Any]
    ) -> JSONResponse:
        result = store.create(project_id, data, principal="owner", key=command_key(request))
        return JSONResponse(result, status_code=201, headers={"ETag": f'"{result["revision"]}"'})

    @app.get("/v1/conversations/{conversation_id}/snapshot")
    @app.get("/v1/conversations/{conversation_id}/hub")
    def get_snapshot(conversation_id: str) -> dict[str, Any]:
        return store.snapshot(conversation_id)

    @app.put("/v1/conversations/{conversation_id}/settings")
    def save_settings(conversation_id: str, request: Request, data: dict[str, Any]) -> JSONResponse:
        result = store.settings(
            conversation_id,
            data,
            principal="owner",
            key=command_key(request),
            revision=expected_revision(request),
        )
        return JSONResponse(result, headers={"ETag": f'"{result["revision"]}"'})

    @app.post("/v1/conversations/{conversation_id}/messages", status_code=201)
    def create_message(
        conversation_id: str, request: Request, data: dict[str, Any]
    ) -> JSONResponse:
        result = store.message(conversation_id, data, principal="owner", key=command_key(request))
        return JSONResponse(result, status_code=201, headers={"ETag": '"1"'})

    @app.put("/v1/conversations/{conversation_id}/draft")
    def save_draft(conversation_id: str, request: Request, data: dict[str, Any]) -> JSONResponse:
        result = store.draft(
            conversation_id,
            data,
            principal="owner",
            key=command_key(request),
            revision=expected_revision(request),
        )
        return JSONResponse(result, headers={"ETag": f'"{result["revision"]}"'})

    @app.post("/v1/conversations/{conversation_id}/task-drafts", status_code=201)
    def create_task_draft(
        conversation_id: str, request: Request, data: dict[str, Any]
    ) -> JSONResponse:
        result = store.task_draft(
            conversation_id, data, principal="owner", key=command_key(request)
        )
        return JSONResponse(result, status_code=201, headers={"ETag": '"1"'})

    @app.get("/v1/conversations/{conversation_id}/events")
    def get_events(conversation_id: str, after_seq: int = 0) -> StreamingResponse:
        events = store.events(conversation_id, after_seq)

        def body() -> Iterator[str]:
            for event in events:
                if (event_id := event.get("sequence")) is not None:
                    yield f"id: {event_id}\n"
                yield f"event: {event['event_type']}\n"
                yield f"data: {json.dumps(event, sort_keys=True, separators=(',', ':'))}\n\n"

        return StreamingResponse(
            body(),
            media_type="text/event-stream",
            headers={
                "X-Snapshot-Watermark": str(store.snapshot(conversation_id)["snapshot_event_seq"]),
                "X-Accel-Buffering": "no",
            },
        )
