"""Authenticated HTTP boundaries for the role-directed scheduling control plane.

There are two families of route here, and they authenticate differently *by
construction* rather than by a flag:

**Management routes** live under ``/v1/projects/...`` (and the two
``/v1/scheduling/resource-*`` commands) and use the workbench session boundary
already registered in ``create_app``: session cookie, Origin and CSRF token. The
acting identity is a ``user_session`` credential, and only that kind may create a
Run, issue a grant, delegate, revoke, issue a credential, or publish a trusted
resource observation.

**Protocol routes** live under ``/v1/scheduling/protocol/...``. They carry a
bearer token issued by a management command. Each one resolves that token into a
credential whose *kind* is then checked against the capability the route needs,
so a role-protocol credential can submit a decision and can do nothing else, and
an execution-consumer credential can read the queue and claim work but cannot
submit a decision.

Both families refuse a body that tries to describe its own authority. A
``user``, ``role``, ``run_id``, ``grant_id``, ``ExecutionRef`` or ``principal``
field is not read as an identity anywhere: the identity is the resolved
credential, and a body field claiming one is an unknown field on the model, so it
is refused with ``422`` before any store method runs.
"""

from typing import Annotated, Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from karajan.scheduling.credentials import USER_SESSION_ROUTES, Principal
from karajan.scheduling.errors import CONFLICT_CODES, SchedulingError
from karajan.scheduling.store import SchedulingStore

from .projects import command_key

#: The one path prefix that does not use the workbench session boundary. Every
#: route below it authenticates its own issued capability instead.
PROTOCOL_PREFIX = "/v1/scheduling/protocol/"


class RunPayload(BaseModel):
    """One Run creation. The deployment identity is confirmed, never supplied."""

    model_config = ConfigDict(extra="forbid", strict=True)
    conversation_id: Annotated[str, Field(min_length=1, max_length=128)]
    slot: Annotated[str, Field(min_length=1, max_length=128)]
    expected_active_revision: Annotated[int, Field(ge=0, le=1_000_000)]
    deployment_id: Annotated[str, Field(min_length=1, max_length=128)]
    inputs: dict[str, Any]
    requirement: dict[str, Any]
    required_outcomes: Annotated[list[str], Field(min_length=1, max_length=256)]
    delivery_artifact: Annotated[str, Field(max_length=256)] | None = None
    title: Annotated[str, Field(max_length=200)] = ""


class GrantPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    grant_id: Annotated[str, Field(min_length=1, max_length=128)]
    subject_ref: Annotated[str, Field(min_length=1, max_length=128)]
    role_instance: Annotated[str, Field(min_length=1, max_length=128)]
    allowed_actions: Annotated[list[str], Field(min_length=1, max_length=16)]
    inputs: Annotated[list[str], Field(max_length=256)]
    scope: Annotated[list[str], Field(min_length=1, max_length=256)]
    write_scope: Annotated[list[str], Field(max_length=256)]
    required_outcomes: Annotated[list[str], Field(max_length=256)]
    role_refs: dict[str, str]
    model_refs: Annotated[list[str], Field(max_length=256)] = []
    execution_kind_refs: Annotated[list[str], Field(max_length=256)] = []
    tool_refs: Annotated[list[str], Field(max_length=256)] = []
    data_destinations: Annotated[list[str], Field(max_length=256)] = []
    artifact: Annotated[str, Field(max_length=256)] | None = None
    resource_policy: dict[str, Any]
    delegation: dict[str, Any] | None = None
    expires_at: Annotated[float, Field(gt=0, allow_inf_nan=False)] | None = None


class DelegationPayload(GrantPayload):
    parent_grant_id: Annotated[str, Field(min_length=1, max_length=128)]


class CredentialPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["user_session", "role_protocol", "execution_consumer"]
    run_id: Annotated[str, Field(min_length=1, max_length=128)]
    grant_id: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    role_instance: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    expires_in: Annotated[float, Field(gt=0, allow_inf_nan=False)] | None = None


class RevocationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    reason: Annotated[str, Field(min_length=1, max_length=512)]


class ExpansionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expansion_id: Annotated[str, Field(min_length=1, max_length=128)]
    goal: Annotated[str, Field(min_length=1, max_length=2000)]
    required_outcomes: Annotated[list[str], Field(max_length=256)]
    member_policy: Annotated[int, Field(gt=0, le=1_000_000)] | None = None
    parent_task_id: Annotated[str, Field(max_length=128)] | None = None


class ObservationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    pool_id: Annotated[str, Field(min_length=1, max_length=128)]
    window_id: Annotated[str, Field(min_length=1, max_length=128)]
    metric: Literal["remaining", "used", "unknown"]
    amount: Annotated[str, Field(max_length=18)] | None = None
    limit: Annotated[str, Field(max_length=18)] | None = None
    source: Literal["local_ledger", "official", "manual"]
    source_ref: Annotated[str, Field(min_length=1, max_length=128)]
    reset_at: Annotated[float, Field(allow_inf_nan=False)] | None = None
    adjustment_reason: Annotated[str, Field(max_length=1000)] | None = None


class ResourcePolicyPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    pool_id: Annotated[str, Field(min_length=1, max_length=128)]
    max_concurrent_claims: Annotated[int, Field(ge=0, le=1_000_000)]
    safety_margin: Annotated[str, Field(max_length=18)] | None = None
    require_observation: bool = True


class ClaimPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    claim_key: Annotated[str, Field(min_length=1, max_length=128)]
    task_id: Annotated[str, Field(min_length=1, max_length=128)]


class ReportPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    outcome: Literal["started", "completed", "failed", "unknown", "cancelled"]
    evidence_ref: Annotated[str, Field(min_length=1, max_length=256)]
    attempt_ref: Annotated[str, Field(max_length=128)] | None = None
    note: Annotated[str, Field(max_length=8000)] = ""


def refusal_status(code: str) -> int:
    """The HTTP status one refusal reason code is reported with.

    It is decided in exactly one place so that the response to a first refusal
    and the response to a replay of it are the same status as well as the same
    body: a caller that lost the first response must not be told something
    different when it asks again.
    """
    if code in CONFLICT_CODES or code.endswith("CONFLICT"):
        # A refusal the caller resolves by re-reading the current state.
        return 409
    if code.endswith("NOT_FOUND"):
        return 404
    if "SCOPE_INSUFFICIENT" in code or "NOT_PERMITTED" in code or "NOT_OWNED" in code:
        # A capability this credential kind does not carry, or authority over
        # something the caller does not own.
        return 403
    # Anything else is a payload the engine considered and refused.
    return 422


def bearer(request: Request) -> str:
    """The one credential a protocol request may present.

    A session cookie is deliberately not accepted here. These endpoints are the
    control-protocol surface, and accepting a browser session would make a role's
    narrow capability interchangeable with the user's full authority.
    """
    values = request.headers.getlist("authorization")
    if len(values) != 1 or not values[0].lower().startswith("bearer "):
        raise HTTPException(401, {"reason_code": "PROTOCOL_CREDENTIAL_REQUIRED"})
    token = values[0][7:].strip()
    if not token:
        raise HTTPException(401, {"reason_code": "PROTOCOL_CREDENTIAL_REQUIRED"})
    return token


def management_principal(request: Request) -> Principal:
    """The user-session credential behind an already-authenticated route.

    The session middleware has already run, so the caller is a real authenticated
    user. The credential is derived from *that* fact rather than looked up in the
    request, which is why no body can claim to be the user: the identity exists
    only because the session boundary admitted the call.
    """
    if getattr(request.state, "session", None) is None:
        raise HTTPException(401, {"reason_code": "AUTHENTICATION_REQUIRED"})
    return Principal(
        credential_id="session:owner",
        kind="user_session",
        runs=frozenset(),
        grant_id=None,
        role_instance=None,
        term=0,
        routes=USER_SESSION_ROUTES,
    )


def register_scheduling_routes(app: FastAPI, store: SchedulingStore) -> None:
    app.state.scheduling_store = store

    @app.exception_handler(SchedulingError)
    async def scheduling_error(request: Request, error: SchedulingError) -> JSONResponse:
        del request
        # A replayed refusal answers with the record that was stored once, so the
        # caller receives exactly the reason, revision and command identity the
        # first attempt received rather than a freshly derived document.
        from karajan.scheduling.store import _RejectedDecision

        if isinstance(error, _RejectedDecision):
            # The status follows the reason code the record carries, so a replay
            # answers exactly as the original refusal did.
            return JSONResponse(
                error.record, status_code=refusal_status(str(error.record["reason_code"]))
            )
        document = error.document()
        return JSONResponse(document, status_code=refusal_status(error.code))

    def protocol(request: Request, capability: str) -> Principal:
        """Resolve the bearer token and require one capability of its kind.

        The capability is fixed per route, so a credential kind's capability set
        decides which protocol endpoints it can reach and no request field
        selects one.
        """
        principal = store.resolve_credential(bearer(request))
        principal.require_route(capability)
        return principal

    # ------------------------------------------------------------- management

    @app.post("/v1/projects/{project_id}/workflow-runs", status_code=201)
    def create_run(
        project_id: str,
        request: Request,
        data: RunPayload,
    ) -> JSONResponse:
        principal = management_principal(request)
        principal.require_route("run")
        record, created = store.create_run(
            project_id,
            data.model_dump(mode="json", exclude_none=True),
            principal="owner",
            command_key=command_key(request),
        )
        return JSONResponse(record, status_code=201 if created else 200)

    @app.get("/v1/projects/{project_id}/workflow-runs")
    def list_runs(project_id: str, request: Request) -> dict[str, Any]:
        principal = management_principal(request)
        principal.require_route("run")
        return {"items": store.list_runs(project_id)}

    @app.get("/v1/projects/{project_id}/workflow-runs/{run_id}")
    def get_run(
        project_id: str,
        run_id: str,
        request: Request,
    ) -> dict[str, Any]:
        principal = management_principal(request)
        principal.require_route("run")
        return store.get_run(project_id, run_id)

    @app.get("/v1/projects/{project_id}/workflow-runs/{run_id}/authorization")
    def get_authorization(
        project_id: str,
        run_id: str,
        request: Request,
    ) -> dict[str, Any]:
        principal = management_principal(request)
        principal.require_route("authorization")
        run = store.get_run(project_id, run_id)
        return {
            "run_id": run_id,
            "initial_authorization": run["initial_authorization"],
            "source": run["source"],
            "graph_revision": run["graph_revision"],
            "graph_digest": run["graph_digest"],
        }

    @app.post(
        "/v1/projects/{project_id}/workflow-runs/{run_id}/grants", status_code=201
    )
    def issue_grant(
        project_id: str,
        run_id: str,
        request: Request,
        data: GrantPayload,
    ) -> JSONResponse:
        principal = management_principal(request)
        principal.require_route("grant")
        record, created = store.issue_grant(
            project_id,
            run_id,
            data.model_dump(mode="json", exclude_none=True),
            command_key=command_key(request),
        )
        return JSONResponse(record, status_code=201 if created else 200)

    @app.post(
        "/v1/projects/{project_id}/workflow-runs/{run_id}/grants/delegations",
        status_code=201,
    )
    def delegate_grant(
        project_id: str,
        run_id: str,
        request: Request,
        data: DelegationPayload,
    ) -> JSONResponse:
        principal = management_principal(request)
        principal.require_route("delegation")
        record, created = store.delegate_grant(
            project_id,
            run_id,
            data.model_dump(mode="json", exclude_none=True),
            command_key=command_key(request),
        )
        return JSONResponse(record, status_code=201 if created else 200)

    @app.get("/v1/projects/{project_id}/workflow-runs/{run_id}/grants")
    def list_grants(
        project_id: str,
        run_id: str,
        request: Request,
    ) -> dict[str, Any]:
        principal = management_principal(request)
        principal.require_route("grant")
        return {"items": store.list_grants(project_id, run_id)}

    @app.post(
        "/v1/projects/{project_id}/workflow-runs/{run_id}/grants/{grant_id}/revocation"
    )
    def revoke_grant(
        project_id: str,
        run_id: str,
        grant_id: str,
        request: Request,
        data: RevocationPayload,
    ) -> dict[str, Any]:
        principal = management_principal(request)
        principal.require_route("revocation")
        return store.revoke_grant(project_id, run_id, grant_id, reason=data.reason)

    @app.post(
        "/v1/projects/{project_id}/workflow-runs/{run_id}/credentials", status_code=201
    )
    def issue_credential(
        project_id: str,
        run_id: str,
        request: Request,
        data: CredentialPayload,
    ) -> JSONResponse:
        """Issue one narrow credential; the raw token is returned exactly once."""
        principal = management_principal(request)
        principal.require_route("consumer")
        record, created = store.issue_credential(
            project_id,
            run_id,
            data.model_dump(mode="json", exclude_none=True),
            command_key=command_key(request),
        )
        return JSONResponse(record, status_code=201 if created else 200)

    @app.get("/v1/projects/{project_id}/workflow-runs/{run_id}/credentials")
    def list_credentials(
        project_id: str,
        run_id: str,
        request: Request,
    ) -> dict[str, Any]:
        principal = management_principal(request)
        principal.require_route("consumer")
        return {"items": store.list_credentials(project_id, run_id)}

    @app.post("/v1/projects/{project_id}/workflow-runs/{run_id}/expansions", status_code=201)
    def create_expansion(
        project_id: str,
        run_id: str,
        request: Request,
        payload: dict[str, Any],
    ) -> JSONResponse:
        principal = management_principal(request)
        principal.require_route("grant")
        grant_id = payload.get("grant_id")
        if not isinstance(grant_id, str) or not grant_id:
            raise HTTPException(422, {"reason_code": "SCHEDULING_GRANT_INVALID"})
        data = ExpansionPayload.model_validate(
            {key: value for key, value in payload.items() if key != "grant_id"}
        )
        return JSONResponse(
            store.create_expansion(
                project_id, run_id, grant_id, data.model_dump(mode="json", exclude_none=True)
            ),
            status_code=201,
        )

    @app.get("/v1/projects/{project_id}/workflow-runs/{run_id}/tasks/{task_id}")
    def get_task(
        project_id: str, run_id: str, task_id: str, request: Request
    ) -> dict[str, Any]:
        """One exact task, by identity, whatever page its siblings fall on."""
        principal = management_principal(request)
        principal.require_route("run")
        return store.task(project_id, run_id, task_id)

    @app.get(
        "/v1/projects/{project_id}/workflow-runs/{run_id}/tasks/{task_id}"
        "/versions/{revision}"
    )
    def task_version(
        project_id: str,
        run_id: str,
        task_id: str,
        revision: int,
        digest: str,
        request: Request,
    ) -> dict[str, Any]:
        """One immutable task version, by the identity a seal pinned it with.

        A sealed member names a revision and a digest; both are required here, so
        the stored version cannot answer for a different one.
        """
        principal = management_principal(request)
        principal.require_route("run")
        return store.task_version(project_id, run_id, task_id, revision, digest)

    @app.get(
        "/v1/projects/{project_id}/workflow-runs/{run_id}/tasks/{task_id}/versions"
    )
    def task_versions(
        project_id: str, run_id: str, task_id: str, request: Request
    ) -> dict[str, Any]:
        principal = management_principal(request)
        principal.require_route("run")
        return {"items": store.task_versions(project_id, run_id, task_id)}

    @app.get("/v1/projects/{project_id}/workflow-runs/{run_id}/decision-rejections")
    def list_rejections(
        project_id: str,
        run_id: str,
        request: Request,
    ) -> dict[str, Any]:
        """Every refused decision of this run, with the reason it was refused.

        A conflict is durable evidence, not only the losing client's memory of a
        409: the command identity, the reason and the revision that was current
        are all readable here.
        """
        principal = management_principal(request)
        principal.require_route("run")
        return {"items": store.rejections(project_id, run_id)}

    @app.get(
        "/v1/projects/{project_id}/workflow-runs/{run_id}"
        "/decision-rejections/{command_key}"
    )
    def get_rejection(
        project_id: str,
        run_id: str,
        command_key: str,
        request: Request,
    ) -> dict[str, Any]:
        principal = management_principal(request)
        principal.require_route("run")
        return store.decision_rejection(project_id, run_id, command_key)

    @app.get("/v1/projects/{project_id}/workflow-runs/{run_id}/graph")
    def get_graph(
        project_id: str,
        run_id: str,
        request: Request,
    ) -> dict[str, Any]:
        principal = management_principal(request)
        principal.require_route("run")
        return store.graph(project_id, run_id)

    @app.get("/v1/projects/{project_id}/workflow-runs/{run_id}/tasks")
    def list_tasks(
        project_id: str,
        run_id: str,
        request: Request,
        cursor: str | None = None,
        limit: int | None = None,
        state: str | None = None,
    ) -> dict[str, Any]:
        principal = management_principal(request)
        principal.require_route("run")
        return store.list_tasks(project_id, run_id, cursor=cursor, limit=limit, state=state)

    @app.get("/v1/projects/{project_id}/workflow-runs/{run_id}/resources")
    def resource_state(
        project_id: str,
        run_id: str,
        request: Request,
    ) -> dict[str, Any]:
        principal = management_principal(request)
        principal.require_route("resource")
        return store.resource_state(project_id, run_id)

    @app.post("/v1/scheduling/resource-observations", status_code=201)
    def observe_resource(
        request: Request,
        data: ObservationPayload,
    ) -> JSONResponse:
        principal = management_principal(request)
        principal.require_route("resource")
        record, created = store.observe_resource(
            data.model_dump(mode="json", exclude_none=True),
            principal=principal,
            command_key=command_key(request),
        )
        return JSONResponse(record, status_code=201 if created else 200)

    @app.post("/v1/scheduling/resource-policies", status_code=201)
    def activate_resource_policy(
        request: Request,
        data: ResourcePolicyPayload,
    ) -> JSONResponse:
        principal = management_principal(request)
        principal.require_route("resource")
        record, created = store.activate_resource_policy(
            data.model_dump(mode="json", exclude_none=True),
            principal=principal,
            command_key=command_key(request),
        )
        return JSONResponse(record, status_code=201 if created else 200)

    @app.post(
        "/v1/projects/{project_id}/workflow-runs/{run_id}/tasks/{task_id}/reconciliation"
    )
    def reconcile_task(
        project_id: str,
        run_id: str,
        task_id: str,
        request: Request,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Reconcile one reported execution, as an explicit trusted act.

        This is the *only* path that turns a consumer's observation into
        control-plane truth: it terminalizes the task and releases the capacity
        its claim holds. It is a user-session capability, so a consumer cannot
        promote its own report, and the observation the consumer recorded stays
        in the task's history unchanged.
        """
        principal = management_principal(request)
        principal.require_route("reconcile")
        data = ReportPayload.model_validate(payload)
        record, _ = store.report_execution(
            project_id,
            run_id,
            task_id,
            data.model_dump(mode="json", exclude_none=True),
            principal=principal,
            command_key=command_key(request),
            trusted=True,
        )
        return dict(record)

    # --------------------------------------------------------------- protocol
    #
    # Each handler below resolves its own capability as the first statement of
    # its body. The capability is fixed per route, so a credential kind's
    # capability set decides which of these endpoints it can reach; no request
    # field selects one, and no other kind can reach them at all.

    @app.post(
        "/v1/scheduling/protocol/projects/{project_id}/runs/{run_id}"
        "/grants/{grant_id}/decisions"
    )
    def submit_decision(
        project_id: str,
        run_id: str,
        grant_id: str,
        request: Request,
        payload: dict[str, Any],
    ) -> JSONResponse:
        principal = protocol(request, "decision")
        record, created = store.submit_decision(
            project_id,
            run_id,
            grant_id,
            payload,
            principal=principal,
            command_key=command_key(request),
        )
        return JSONResponse(record, status_code=201 if created else 200)

    @app.get("/v1/scheduling/protocol/projects/{project_id}/runs/{run_id}/graph")
    def protocol_graph(
        project_id: str,
        run_id: str,
        request: Request,
    ) -> dict[str, Any]:
        principal = protocol(request, "decision")
        principal.require_run(run_id)
        return store.graph(project_id, run_id)

    @app.get("/v1/scheduling/protocol/projects/{project_id}/runs/{run_id}/queue")
    def protocol_queue(
        project_id: str,
        run_id: str,
        request: Request,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        principal = protocol(request, "queue")
        principal.require_run(run_id)
        return store.queue(project_id, run_id, cursor=cursor, limit=limit)

    @app.post(
        "/v1/scheduling/protocol/projects/{project_id}/runs/{run_id}/claims",
        status_code=201,
    )
    def protocol_claim(
        project_id: str,
        run_id: str,
        request: Request,
        data: ClaimPayload,
    ) -> JSONResponse:
        principal = protocol(request, "claim")
        record, created = store.claim(
            project_id,
            run_id,
            data.model_dump(mode="json", exclude_none=True),
            principal=principal,
            command_key=command_key(request),
        )
        return JSONResponse(record, status_code=201 if created else 200)

    @app.get(
        "/v1/scheduling/protocol/projects/{project_id}/runs/{run_id}/claims/{claim_key}"
    )
    def protocol_claim_readback(
        project_id: str,
        run_id: str,
        claim_key: str,
        request: Request,
    ) -> dict[str, Any]:
        principal = protocol(request, "claim")
        principal.require_run(run_id)
        return store.claim_of(project_id, run_id, claim_key)

    @app.post(
        "/v1/scheduling/protocol/projects/{project_id}/runs/{run_id}"
        "/tasks/{task_id}/reports"
    )
    def protocol_report(
        project_id: str,
        run_id: str,
        task_id: str,
        request: Request,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        principal = protocol(request, "observe_task")
        data = ReportPayload.model_validate(payload)
        record, _ = store.report_execution(
            project_id,
            run_id,
            task_id,
            data.model_dump(mode="json", exclude_none=True),
            principal=principal,
            command_key=command_key(request),
        )
        return dict(record)

    @app.get("/v1/scheduling/protocol/projects/{project_id}/runs/{run_id}/tasks/{task_id}")
    def protocol_task(
        project_id: str,
        run_id: str,
        task_id: str,
        request: Request,
    ) -> dict[str, Any]:
        principal = protocol(request, "task")
        principal.require_run(run_id)
        # An exact lookup rather than a scan of the first page: every task of the
        # run is addressable, however many there are.
        return store.task(project_id, run_id, task_id)


__all__ = [
    "PROTOCOL_PREFIX",
    "bearer",
    "management_principal",
    "refusal_status",
    "register_scheduling_routes",
]
