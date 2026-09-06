"""Authenticated, stateless simulation of supplied facts; never an admission."""

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from karajan.projects import ProjectRegistry
from karajan.routing import RoutingError, evaluate_route


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RoutingError("ROUTING_INPUT_INVALID")
        result[key] = value
    return result


def register_simulation_routes(app: FastAPI, registry: ProjectRegistry) -> None:
    @app.get("/v1/projects/{project_id}/rulebook/simulation-example")
    def simulation_example(project_id: str) -> Response:
        registry.get(project_id)
        example = Path(__file__).parents[1] / "routing" / "example.v1.json"
        return Response(example.read_bytes(), media_type="application/json")

    @app.post("/v1/projects/{project_id}/rulebook/simulate")
    async def simulate(project_id: str, request: Request) -> JSONResponse:
        registry.get(project_id)
        try:
            source: Any = json.loads(await request.body(), object_pairs_hook=_unique_pairs)
            if not isinstance(source, dict) or set(source) != {"task", "policy", "capacity"}:
                raise RoutingError("ROUTING_INPUT_INVALID")
            result = evaluate_route(source["task"], source["policy"], source["capacity"])
            return JSONResponse(
                {
                    "schema_version": "karajan.rulebook-simulation.v1",
                    "scope": "explicit_simulation",
                    "activation_allowed": False,
                    "model_calls": 0,
                    "result": result,
                }
            )
        except RoutingError as error:
            return JSONResponse(
                {
                    "reason_code": error.code,
                    "issues": error.issues,
                    "activation_allowed": False,
                    "model_calls": 0,
                },
                status_code=422,
            )
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
            return JSONResponse(
                {
                    "reason_code": "ROUTING_INPUT_INVALID",
                    "issues": [],
                    "activation_allowed": False,
                    "model_calls": 0,
                },
                status_code=422,
            )
