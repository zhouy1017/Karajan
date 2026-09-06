"""Assess a persisted approval; clients cannot upload routing authority or facts."""

from typing import Any

from fastapi import FastAPI, Request

from karajan.contracts.probe import Contract
from karajan.orchestration.routing import ApprovedRunRouting

from .projects import command_key


class AssessApprovedTask(Contract):
    pass


def register_approved_routing_routes(app: FastAPI, routing: ApprovedRunRouting) -> None:
    @app.post("/v1/runs/{run_id}/tasks/{task_id}/routing-assessments", status_code=201)
    def assess(
        run_id: str, task_id: str, request: Request, data: AssessApprovedTask
    ) -> dict[str, Any]:
        return routing.assess(run_id, task_id, principal="owner", command_key=command_key(request))

    @app.get("/v1/runs/{run_id}/routing-assessments/{assessment_id}")
    def get_assessment(run_id: str, assessment_id: str) -> dict[str, Any]:
        return routing.get(run_id, assessment_id, principal="owner")
