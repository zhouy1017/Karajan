"""Owner predictions for a complete Attempt, bound by the controller to an approved Run."""

import json
import math
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import Field, FiniteFloat, TypeAdapter, ValidationError, model_validator

from karajan.contracts.probe import Contract
from karajan.resources.broker import units
from karajan.routing.models import Estimate, Quantity

from .models import Identifier, ProfileRef
from .publication import digest, effective_catalog
from .registry import encoded, identifier

if TYPE_CHECKING:
    from karajan.runs import RunPlanner


class DemandError(ValueError):
    @property
    def code(self) -> str:
        return str(self)


Revision = Annotated[int, Field(gt=0, le=1_000_000)]


class PredictedPool(Contract):
    pool_id: Identifier
    unit: Literal["requests", "tokens", "percent"]
    window_kind: Literal["fixed", "rolling", "balance", "unknown"]
    amount: Quantity

    @model_validator(mode="after")
    def positive_discrete(self) -> "PredictedPool":
        count = units(self.amount)
        if count <= 0 or self.unit in {"requests", "tokens"} and count % 1_000_000:
            raise ValueError("Positive discrete demand required")
        return self


class Prediction(Contract):
    id: Identifier
    revision: Revision
    source_kind: Literal["owner_conservative_estimate"]
    validity_seconds: Annotated[int, Field(gt=0, le=86400)]
    measurement_semantics: Literal["window_independent_attempt"]
    demand: Annotated[list[PredictedPool], Field(min_length=1, max_length=32)]
    completion_seconds: Annotated[FiniteFloat, Field(ge=0, le=1_000_000)] | None
    basis: Annotated[str, Field(min_length=1, max_length=2000, pattern=r"\S")]


class PoolWindow(Contract):
    pool_id: Identifier
    account_id: Identifier
    kind: Literal["service", "platform_allowance"]
    unit: Literal["requests", "tokens", "percent"]
    window_kind: Literal["fixed", "rolling", "balance", "unknown"]
    window_id: Identifier


def _record(row: sqlite3.Row) -> dict[str, Any]:
    result: dict[str, Any] = json.loads(row["record"])
    if digest({key: value for key, value in result.items() if key != "digest"}) != row[
        "digest"
    ] or (result["digest"] != row["digest"]):
        raise DemandError("RESOURCE_ESTIMATE_RECORD_CHANGED")
    return result


def _binding(
    run: dict[str, Any], task_id: str, ref: dict[str, Any], catalog: dict[str, Any]
) -> dict[str, Any]:
    try:
        ref = ProfileRef.model_validate(ref).model_dump()
    except ValidationError:
        raise DemandError("PROFILE_REFERENCE_INVALID") from None
    if run.get("schema_version") != "karajan.run-planning.v2":
        raise DemandError("APPROVED_ROUTING_REQUIRED")
    plan = next(
        (p for p in run["plans"] if p["plan_revision"] == run["active_plan_revision"]), None
    )
    if plan is None or run["state"] != "executing":
        raise DemandError("TASK_SCOPE_NOT_APPROVED")
    approval = next(
        (a for a in reversed(run["approvals"]) if a["plan_revision"] == plan["plan_revision"]), None
    )
    if (
        approval is None
        or any(
            approval[key] != plan[key]
            for key in (
                "term",
                "plan_digest",
                "authorization_digest",
                "configuration_digest",
                "routing_digest",
            )
        )
        or approval["approved_by"] != run["owner"]
    ):
        raise DemandError("TASK_SCOPE_NOT_APPROVED")
    task = next((t for t in plan["plan"]["tasks"] if t["id"] == task_id), None)
    if task is None or task["readiness"] != "ready":
        raise DemandError("TASK_SCOPE_NOT_APPROVED")
    if task["duration_seconds"] > 1_000_000 or ref["revision"] > 1_000_000:
        raise DemandError("ADMISSION_BOUND_NOT_REPRESENTABLE")
    if ref not in plan["plan"]["authorization"]["profile_refs"]:
        raise DemandError("PROFILE_NOT_APPROVED")
    frozen = run["configuration_snapshot"]["configuration"]["resources"]
    registrations = [
        p for p in frozen["profiles"] if {"id": p["id"], "revision": p["revision"]} == ref
    ]
    if len(registrations) != 1 or registrations[0]["profile"] is None:
        raise DemandError("PROFILE_IDENTITY_MISSING")
    registration = registrations[0]
    profile = registration["profile"]
    binding = profile["binding"]
    if {"id": profile["id"], "revision": profile["revision"]} != ref:
        raise DemandError("PROFILE_IDENTITY_MISMATCH")
    if catalog["project_id"] != run["project_id"] or ref not in catalog["approved_profile_refs"]:
        raise DemandError("PROFILE_NOT_APPROVED")
    resources = catalog.get("resources")
    if resources is None:
        raise DemandError("PROFILE_IDENTITY_MISSING")
    current = [
        p for p in resources["profiles"] if {"id": p["id"], "revision": p["revision"]} == ref
    ]
    if current != [registration] or not registration["enabled"]:
        raise DemandError("PROFILE_IDENTITY_MISMATCH")
    references = registration["quota_pool_refs"]
    if not 1 <= len(references) <= 32 or len(set(references)) != len(references):
        raise DemandError("PROFILE_POOL_VECTOR_INVALID")
    associated: dict[str, Any] = {}
    for kind, identity in (
        ("accounts", binding["account_id"]),
        ("channels", binding["channel_id"]),
    ):
        rows = [r for r in resources[kind] if r["id"] == identity]
        original = [r for r in frozen[kind] if r["id"] == identity]
        if len(rows) != 1 or rows != original:
            raise DemandError("PROFILE_IDENTITY_MISMATCH")
        associated[kind] = rows[0]
    if associated["accounts"]["secret_ref"] != profile["auth_ref"] or (
        associated["channels"]["account_id"] != binding["account_id"]
        or associated["channels"]["billing_path"] != binding["billing_path"]
        or not associated["channels"]["approved_data_destination"]
    ):
        raise DemandError("PROFILE_IDENTITY_MISMATCH")
    pools = []
    for pool_id in sorted(references):
        rows = [p for p in resources["quota_pools"] if p["id"] == pool_id]
        original = [p for p in frozen["quota_pools"] if p["id"] == pool_id]
        if len(rows) != 1 or rows != original or rows[0]["account_id"] != binding["account_id"]:
            raise DemandError("PROFILE_POOL_VECTOR_INVALID")
        pools.append(rows[0])
    requirements = plan["routing_binding"]["task_requirements"][task_id]
    if any(task[key] != value for key, value in requirements.items()):
        raise DemandError("APPROVED_REQUIREMENTS_MISMATCH")
    context = run["execution_policy_snapshot"]["context_policy"]
    return {
        "project_id": run["project_id"],
        "owner": run["owner"],
        "run_id": run["id"],
        "plan_revision": plan["plan_revision"],
        "plan_digest": plan["plan_digest"],
        "approval_id": approval["id"],
        "authorization_digest": plan["authorization_digest"],
        "routing_digest": plan["routing_digest"],
        "task_id": task_id,
        "task_requirements": requirements,
        "task_requirements_digest": digest(requirements),
        "context_policy": context,
        "context_policy_digest": digest(context),
        "execution_policy_digest": run["execution_policy_snapshot"]["digest"],
        "profile": ref,
        "profile_digest": digest(profile),
        "registration": registration,
        "runtime_version": binding["runtime_version"],
        "associated": associated,
        "pools": pools,
    }


class AttemptEstimateStore:
    """Local controller service. There is no arbitrary snapshot HTTP input.

    Registration takes Run -> project locks. estimate_locked is only consumed
    under an existing Run.activation_guard and project qualification guard.
    Its independent read-only connection cannot nest BEGIN IMMEDIATE.
    """

    def __init__(self, planner: "RunPlanner", *, clock: Callable[[], float] = time.time) -> None:
        self.planner = planner
        self.projects = planner.projects
        self.clock = clock
        with self.projects._transaction() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS attempt_estimates (sequence INTEGER PRIMARY KEY, "
                "project_id TEXT NOT NULL, id TEXT NOT NULL, revision INTEGER NOT NULL, "
                "run_id TEXT NOT NULL, task_id TEXT NOT NULL, profile_id TEXT NOT NULL, "
                "profile_revision INTEGER NOT NULL, record TEXT NOT NULL, digest TEXT NOT NULL, "
                "UNIQUE(project_id,id,revision))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS attempt_estimate_commands ("
                "principal TEXT NOT NULL, key TEXT NOT NULL, digest TEXT NOT NULL, "
                "result TEXT NOT NULL, PRIMARY KEY(principal,key))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS attempt_estimate_revocations ("
                "project_id TEXT NOT NULL, id TEXT NOT NULL, revision INTEGER NOT NULL, "
                "record TEXT NOT NULL, PRIMARY KEY(project_id,id,revision))"
            )

    def _now(self) -> float:
        value = self.clock()
        if type(value) not in (int, float) or not math.isfinite(value):
            raise DemandError("ESTIMATE_CLOCK_INVALID")
        return float(value)

    @contextmanager
    def _readonly(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(
            self.projects.database.resolve().as_uri() + "?mode=ro",
            uri=True,
            timeout=10,
            isolation_level=None,
        )
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            yield db
        finally:
            db.close()

    def register(
        self,
        run_id: str,
        task_id: str,
        profile_ref: dict[str, Any],
        request: dict[str, Any],
        *,
        principal: str,
        command_key: str,
    ) -> dict[str, Any]:
        identifier(principal)
        identifier(command_key)
        identifier(task_id)
        try:
            request = Prediction.model_validate(request).model_dump()
        except (ValidationError, ValueError, ArithmeticError):
            raise DemandError("RESOURCE_ESTIMATE_INPUT_INVALID") from None
        identity = digest([run_id, task_id, profile_ref, request])
        with self.planner.activation_guard(run_id) as run:
            if principal != run["owner"]:
                raise DemandError("ESTIMATE_OWNER_REQUIRED")
            with self.projects._transaction() as db:
                self.projects._require_owner(db, run["project_id"], principal)
                replay = db.execute(
                    "SELECT * FROM attempt_estimate_commands WHERE principal=? AND key=?",
                    (principal, command_key),
                ).fetchone()
                if replay is not None:
                    if replay["digest"] != identity:
                        raise DemandError("IDEMPOTENCY_CONFLICT")
                    return dict(json.loads(replay["result"]))
                bound = _binding(
                    run, task_id, profile_ref, effective_catalog(db, run["project_id"])
                )
                predictions = request["demand"]
                if len({p["pool_id"] for p in predictions}) != len(predictions) or (
                    {p["pool_id"] for p in predictions} != {p["id"] for p in bound["pools"]}
                ):
                    raise DemandError("PROFILE_POOL_VECTOR_INVALID")
                for prediction in predictions:
                    pool = next(p for p in bound["pools"] if p["id"] == prediction["pool_id"])
                    if prediction["unit"] != pool["unit"]:
                        raise DemandError("PROFILE_POOL_UNIT_MISMATCH")
                previous = db.execute(
                    "SELECT revision FROM attempt_estimates WHERE project_id=? AND id=? "
                    "ORDER BY revision DESC LIMIT 1",
                    (run["project_id"], request["id"]),
                ).fetchone()
                if previous is not None and request["revision"] <= previous["revision"]:
                    raise DemandError("ESTIMATE_REVISION_CONFLICT")
                now = self._now()
                record = {
                    "schema_version": "karajan.attempt-estimate.v1",
                    **request,
                    "binding": bound,
                    "created_at": now,
                    "valid_until": now + request["validity_seconds"],
                    "confidence": "unknown",
                    "price": None,
                    "completion_basis": "owner_prediction"
                    if request["completion_seconds"] is not None
                    else None,
                }
                record["digest"] = digest(record)
                db.execute(
                    "INSERT INTO attempt_estimates VALUES (NULL,?,?,?,?,?,?,?,?,?)",
                    (
                        run["project_id"],
                        request["id"],
                        request["revision"],
                        run_id,
                        task_id,
                        profile_ref["id"],
                        profile_ref["revision"],
                        encoded(record),
                        record["digest"],
                    ),
                )
                db.execute(
                    "INSERT INTO attempt_estimate_commands VALUES (?,?,?,?)",
                    (principal, command_key, identity, encoded(record)),
                )
                return record

    def revoke(
        self, project_id: str, estimate_id: str, revision: int, *, principal: str, reason: str
    ) -> dict[str, Any]:
        for value in (project_id, estimate_id, principal, reason):
            identifier(value)
        try:
            TypeAdapter(Revision).validate_python(revision, strict=True)
        except ValidationError:
            raise DemandError("ESTIMATE_REVISION_INVALID") from None
        with self.projects._transaction() as db:
            self.projects._require_owner(db, project_id, principal)
            found = db.execute(
                "SELECT 1 FROM attempt_estimates WHERE project_id=? AND id=? AND revision=?",
                (project_id, estimate_id, revision),
            ).fetchone()
            if found is None:
                raise DemandError("RESOURCE_ESTIMATE_MISSING")
            record = {"principal": principal, "reason": reason, "revoked_at": self._now()}
            db.execute(
                "INSERT OR IGNORE INTO attempt_estimate_revocations VALUES (?,?,?,?)",
                (project_id, estimate_id, revision, encoded(record)),
            )
            return dict(
                json.loads(
                    db.execute(
                        "SELECT record FROM attempt_estimate_revocations "
                        "WHERE project_id=? AND id=? AND revision=?",
                        (project_id, estimate_id, revision),
                    ).fetchone()["record"]
                )
            )

    def get(
        self, project_id: str, estimate_id: str, revision: int, *, principal: str
    ) -> dict[str, Any]:
        for value in (project_id, estimate_id, principal):
            identifier(value)
        try:
            TypeAdapter(Revision).validate_python(revision, strict=True)
        except ValidationError:
            raise DemandError("ESTIMATE_REVISION_INVALID") from None
        with self.projects._transaction() as db:
            self.projects._require_owner(db, project_id, principal)
            row = db.execute(
                "SELECT record,digest FROM attempt_estimates "
                "WHERE project_id=? AND id=? AND revision=?",
                (project_id, estimate_id, revision),
            ).fetchone()
            if row is None:
                raise DemandError("RESOURCE_ESTIMATE_MISSING")
            revoked = db.execute(
                "SELECT record FROM attempt_estimate_revocations "
                "WHERE project_id=? AND id=? AND revision=?",
                (project_id, estimate_id, revision),
            ).fetchone()
            return {
                "record": _record(row),
                "revocation": json.loads(revoked["record"]) if revoked else None,
            }

    def estimate(
        self,
        run_id: str,
        task_id: str,
        profile_ref: dict[str, Any],
        *,
        principal: str,
        pool_windows: list[dict[str, Any]],
        as_of: float,
    ) -> dict[str, Any]:
        with self.planner.activation_guard(run_id) as run:
            if principal != run["owner"]:
                raise DemandError("ESTIMATE_OWNER_REQUIRED")
            with self.projects._transaction() as db:
                self.projects._require_owner(db, run["project_id"], principal)
                return self.estimate_locked(
                    run,
                    task_id,
                    profile_ref,
                    current_catalog=effective_catalog(db, run["project_id"]),
                    pool_windows=pool_windows,
                    as_of=as_of,
                )

    def estimate_locked(
        self,
        approved_run: dict[str, Any],
        task_id: str,
        profile_ref: dict[str, Any],
        *,
        current_catalog: dict[str, Any],
        pool_windows: list[dict[str, Any]],
        as_of: float,
    ) -> dict[str, Any]:
        """Internal controller port: both Run and project guards must already be held."""
        try:
            with self._readonly() as db:
                self.projects._require_owner(db, approved_run["project_id"], approved_run["owner"])
                if effective_catalog(db, approved_run["project_id"]) != current_catalog:
                    raise DemandError("ESTIMATE_CATALOG_CHANGED")
                bound = _binding(approved_run, task_id, profile_ref, current_catalog)
                row = db.execute(
                    "SELECT record,digest FROM attempt_estimates WHERE project_id=? "
                    "AND run_id=? AND task_id=? AND profile_id=? AND profile_revision=? "
                    "ORDER BY sequence DESC LIMIT 1",
                    (
                        approved_run["project_id"],
                        approved_run["id"],
                        task_id,
                        profile_ref["id"],
                        profile_ref["revision"],
                    ),
                ).fetchone()
                if row is None:
                    raise DemandError("RESOURCE_ESTIMATE_MISSING")
                record = _record(row)
                if record["binding"] != bound:
                    raise DemandError("RESOURCE_ESTIMATE_BINDING_CHANGED")
                if db.execute(
                    "SELECT 1 FROM attempt_estimate_revocations "
                    "WHERE project_id=? AND id=? AND revision=?",
                    (approved_run["project_id"], record["id"], record["revision"]),
                ).fetchone():
                    raise DemandError("RESOURCE_ESTIMATE_REVOKED")
                if (
                    type(as_of) not in (int, float)
                    or not math.isfinite(as_of)
                    or not (
                        record["created_at"] <= as_of < record["valid_until"]
                        and record["created_at"] <= self._now() < record["valid_until"]
                    )
                ):
                    raise DemandError("RESOURCE_ESTIMATE_EXPIRED")
                windows = [PoolWindow.model_validate(p).model_dump() for p in pool_windows]
                if len({p["pool_id"] for p in windows}) != len(windows) or (
                    {p["pool_id"] for p in windows} != {p["id"] for p in bound["pools"]}
                ):
                    raise DemandError("PROFILE_POOL_VECTOR_INVALID")
                demands = []
                for pool in bound["pools"]:
                    window = next(w for w in windows if w["pool_id"] == pool["id"])
                    prediction = next(p for p in record["demand"] if p["pool_id"] == pool["id"])
                    if any(window[key] != pool[key] for key in ("account_id", "kind", "unit")) or (
                        window["window_kind"] != prediction["window_kind"]
                    ):
                        raise DemandError("RESOURCE_ESTIMATE_WINDOW_BINDING_CHANGED")
                    demands.append(
                        {
                            "pool_id": pool["id"],
                            "unit": pool["unit"],
                            "window_id": window["window_id"],
                            "amount": prediction["amount"],
                        }
                    )
                estimate = Estimate.model_validate(
                    {
                        "profile": profile_ref,
                        "demand": demands,
                        "confidence": "unknown",
                        "completion_seconds": record["completion_seconds"],
                        "price": None,
                        "evidence_ref": "owner-estimate:" + record["digest"],
                    }
                ).model_dump()
                return {"estimate": estimate, "source_binding": record, "reason_codes": []}
        except DemandError as error:
            return {"estimate": None, "source_binding": None, "reason_codes": [error.code]}
        except (ValidationError, ValueError, ArithmeticError):
            return {
                "estimate": None,
                "source_binding": None,
                "reason_codes": ["RESOURCE_ESTIMATE_INPUT_INVALID"],
            }
