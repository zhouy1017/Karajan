"""Authenticated owner views and revision-bound edits of existing shared policies."""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from karajan.capacity import CapacityError, CapacityStore
from karajan.capacity.models import Contract, Policy

from .projects import command_key, expected_revision


class PolicyUpdate(Contract):
    policy: Policy


def register_resource_routes(app: FastAPI, capacity: CapacityStore) -> None:
    @app.exception_handler(CapacityError)
    async def capacity_error(request: Request, error: CapacityError) -> JSONResponse:
        reason = str(error)
        status = 409 if "STALE" in reason or "CONFLICT" in reason else 422
        return JSONResponse({"reason_code": reason}, status_code=status)

    @app.get("/v1/resources")
    def resource_view() -> dict[str, Any]:
        return capacity.resource_view()

    @app.post("/v1/resources/policy")
    def update_policy(account_id: str, data: PolicyUpdate, request: Request) -> JSONResponse:
        if len(request.query_params.getlist("account_id")) != 1:
            raise CapacityError("POLICY_ACCOUNT_INVALID")
        if data.policy.account_id != account_id:
            raise CapacityError("POLICY_ACCOUNT_MISMATCH")
        revision = expected_revision(request)
        result = capacity.update_protection(
            data.policy.model_dump(),
            expected_revision=revision,
            command_key="web-policy-" + command_key(request),
        )
        return JSONResponse(result, headers={"ETag": f'"{result["revision"]}"'})
