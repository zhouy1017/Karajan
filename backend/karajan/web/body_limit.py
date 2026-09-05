"""Bound JSON bodies before the application parses them."""

import asyncio
import json

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class BodyLimitMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] in {"GET", "HEAD", "OPTIONS"}:
            await self.app(scope, receive, send)
            return
        body = bytearray()
        try:
            async with asyncio.timeout(10):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    body.extend(message.get("body", b""))
                    if len(body) > 65536:
                        response = JSONResponse(
                            {"reason_code": "REQUEST_TOO_LARGE"},
                            status_code=413,
                            headers={"Cache-Control": "no-store"},
                        )
                        await response(scope, receive, send)
                        return
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            await JSONResponse({"reason_code": "REQUEST_TIMEOUT"}, status_code=408)(
                scope, receive, send
            )
            return
        content_type = Headers(scope=scope).get("content-type", "").split(";", 1)[0].strip()
        if body and (
            not content_type or content_type == "application/json" or content_type.endswith("+json")
        ):
            try:
                value: object = json.loads(body.decode("utf-8"), parse_constant=_reject_constant)
                pending = [value]
                while pending:
                    current = pending.pop()
                    if isinstance(current, str):
                        current.encode("utf-8", errors="strict")
                    elif isinstance(current, dict):
                        pending.extend(current.keys())
                        pending.extend(current.values())
                    elif isinstance(current, list):
                        pending.extend(current)
            except (ValueError, UnicodeError, RecursionError):
                await JSONResponse(
                    {"reason_code": "INPUT_INVALID"},
                    status_code=422,
                    headers={"Cache-Control": "no-store"},
                )(scope, receive, send)
                return
        delivered = False

        async def buffered_receive() -> Message:
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, buffered_receive, send)


def _reject_constant(value: str) -> object:
    raise ValueError("Non-finite JSON number")
