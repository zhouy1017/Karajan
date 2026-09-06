"""Owner-facing reservation commands; operation identity makes advance/cancel idempotent."""

from typing import Any

from fastapi import FastAPI, Request

from karajan.contracts.probe import Contract
from karajan.orchestration.admission import ApprovedTaskAdmission

from .projects import command_key


class AdmissionCommand(Contract):
    pass


def register_admission_routes(app: FastAPI, service: ApprovedTaskAdmission) -> None:
    @app.post("/v1/runs/{run_id}/tasks/{task_id}/admissions", status_code=201)
    def enqueue(
        run_id: str, task_id: str, request: Request, data: AdmissionCommand
    ) -> dict[str, Any]:
        return service.enqueue(run_id, task_id, principal="owner", command_key=command_key(request))

    @app.get("/v1/runs/{run_id}/task-admissions/{operation_id}")
    def get(run_id: str, operation_id: str) -> dict[str, Any]:
        return service.get(run_id, operation_id, principal="owner")

    @app.post("/v1/runs/{run_id}/task-admissions/{operation_id}/advance")
    def advance(run_id: str, operation_id: str, data: AdmissionCommand) -> dict[str, Any]:
        return service.advance(run_id, operation_id, principal="owner")

    @app.post("/v1/runs/{run_id}/task-admissions/{operation_id}/cancel")
    def cancel(run_id: str, operation_id: str, data: AdmissionCommand) -> dict[str, Any]:
        return service.cancel(run_id, operation_id, principal="owner")
