"""Persist scheduling decisions without promoting offline qualification to execution."""

import json
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter, ValidationError

from karajan.candidates import CandidateStore
from karajan.contracts.probe import AttemptManifest, Identifier
from karajan.execution import Activation, LaunchDenied, ProcessSpec, RunnerHost
from karajan.runs import RunPlanner

from .binding import digest, material
from .fixture import LocalFixtureRunner


class CoordinationError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def encoded(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise CoordinationError("COORDINATION_INPUT_INVALID") from None


def identifier(value: str) -> None:
    try:
        TypeAdapter(Identifier).validate_python(value, strict=True)
        if not value.isprintable():
            raise ValueError
    except (ValidationError, ValueError):
        raise CoordinationError("COORDINATION_IDENTITY_INVALID") from None


class SerialCoordinator:
    def __init__(
        self,
        state_directory: Path,
        planner: RunPlanner,
        host: RunnerHost,
        candidates: CandidateStore,
        *,
        fixture_runner: LocalFixtureRunner | None = None,
    ) -> None:
        self.directory = state_directory.resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.database = self.directory / "coordination.sqlite"
        self.planner = planner
        self.host = host
        self.candidates = candidates
        self.fixture_runner = fixture_runner
        with self._transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS commands (principal TEXT NOT NULL, key TEXT NOT NULL, "
                "payload TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(principal,key))"
            )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.database, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def enqueue(
        self,
        run_id: str,
        task_id: str,
        *,
        profile_ref: dict[str, Any],
        command_key: str,
        principal: str,
    ) -> dict[str, Any]:
        for value in (run_id, task_id, command_key, principal):
            identifier(value)
        run = self.planner.get(run_id, principal=principal)
        payload = encoded([run_id, task_id, profile_ref])
        with self._transaction() as db:
            prior = db.execute(
                "SELECT payload,result FROM commands WHERE principal=? AND key=?",
                (principal, command_key),
            ).fetchone()
            if prior:
                if prior["payload"] != payload:
                    raise CoordinationError("IDEMPOTENCY_CONFLICT")
                return dict(json.loads(prior["result"]))
            binding, reason = material(self.planner, run, task_id, profile_ref)
            existing = db.execute("SELECT data FROM runs WHERE id=?", (run_id,)).fetchone()
            result: dict[str, Any] = (
                json.loads(existing["data"])
                if existing
                else {
                    "schema_version": "karajan.serial-run.v1",
                    "run_id": run_id,
                    "state": "blocked",
                    "reason_codes": [],
                    "attempts": [],
                    "tasks": {},
                    "outbox": [],
                    "inbox": [],
                    "dispatch_eligible": False,
                    "delivery_eligible": False,
                    "live_qualification": "not_run",
                    "paused": False,
                    "cancelled": False,
                }
            )
            if reason is None and binding is not None:
                if self.fixture_runner is None:
                    reason = "LIVE_QUALIFICATION_NOT_RUN"
                elif not self.fixture_runner.accepts(
                    binding["profile"], Path(binding["repository"]["root"])
                ):
                    reason = "FIXTURE_BINDING_UNSUPPORTED"
            if reason is None and binding is not None:
                if any(
                    dependency not in result["tasks"]
                    or result["tasks"][dependency]["state"] not in {"awaiting_review", "completed"}
                    for dependency in binding["task"]["depends_on"]
                ):
                    reason = "DEPENDENCIES_NOT_READY"
            if reason is None and binding is not None:
                if (
                    binding["task"]["role"] == "worker"
                    and "execution_worker_tasks" in result
                    and task_id not in result["execution_worker_tasks"]
                ):
                    reason = "TASK_LINEAGE_REQUIRED"
                elif binding["task"]["role"] == "reviewer":
                    dependencies = binding["task"]["depends_on"]
                    if len(dependencies) != 1:
                        reason = "REVIEW_SUBJECT_AMBIGUOUS"
                    else:
                        subject = result["tasks"][dependencies[0]]
                        if (
                            subject["binding"]["task"]["role"] != "worker"
                            or not subject.get("check_evidence")
                            or set(subject["binding"]["task"]["paths"])
                            != set(binding["task"]["paths"])
                        ):
                            reason = "REVIEW_SCOPE_UNSUPPORTED"
                        elif not any(
                            row["profile_id"] == profile_ref["id"]
                            and row["profile_revision"] == profile_ref["revision"]
                            for row in subject["policy"]["review"]["approved_reviewers"]
                        ):
                            reason = "REVIEW_PROFILE_NOT_APPROVED"
            if reason is None and binding is not None:
                self._queue(result, run, task_id, binding)
            else:
                result["state"] = "blocked"
                result["reason_codes"] = [reason]
            # A rejected command has its own durable receipt. It must not replace
            # an existing Run's execution facts or hide its pending work.
            if reason is None or existing is None:
                db.execute(
                    "INSERT INTO runs VALUES (?,?) ON CONFLICT(id) "
                    "DO UPDATE SET data=excluded.data",
                    (run_id, encoded(result)),
                )
            db.execute(
                "INSERT INTO commands VALUES (?,?,?,?)",
                (principal, command_key, payload, encoded(result)),
            )
            return result

    def snapshot(self, run_id: str, *, principal: str | None = None) -> dict[str, Any]:
        self.planner.get(run_id, principal=principal)
        with self._transaction() as db:
            row = db.execute("SELECT data FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise CoordinationError("COORDINATION_NOT_FOUND")
            return dict(json.loads(row["data"]))

    def retry(
        self, run_id: str, task_id: str, *, command_key: str, principal: str
    ) -> dict[str, Any]:
        for value in (run_id, task_id, command_key, principal):
            identifier(value)
        run = self.planner.get(run_id, principal=principal)
        payload = encoded([run_id, "infrastructure_retry", task_id])
        with self._transaction() as db:
            prior = db.execute(
                "SELECT payload,result FROM commands WHERE principal=? AND key=?",
                (principal, command_key),
            ).fetchone()
            if prior:
                if prior["payload"] != payload:
                    raise CoordinationError("IDEMPOTENCY_CONFLICT")
                return dict(json.loads(prior["result"]))
            row = db.execute("SELECT data FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise CoordinationError("COORDINATION_NOT_FOUND")
            state: dict[str, Any] = json.loads(row["data"])
            task = state["tasks"].get(task_id)
            if state["paused"] or state["cancelled"]:
                raise CoordinationError("RUN_NOT_ACTIVE")
            if task is None or task["state"] != "failed":
                raise CoordinationError("CONFIRMED_INFRASTRUCTURE_FAILURE_REQUIRED")
            source = next(
                item for item in reversed(state["attempts"]) if item["task_id"] == task_id
            )
            physical = self.host.inspect(source["id"])
            if physical.state != "exited" or physical.exit_code != 75 or source["stage"] != "write":
                raise CoordinationError("CONFIRMED_INFRASTRUCTURE_FAILURE_REQUIRED")
            if (
                self.fixture_runner is None
                or self.fixture_runner.identity() != task["fixture_recipe"]
            ):
                raise CoordinationError("FIXTURE_BINDING_UNSUPPORTED")
            current, error = material(self.planner, run, task_id, task["profile_ref"])
            if (
                error
                or current is None
                or current["input_sha256"] != task["binding"]["input_sha256"]
            ):
                raise CoordinationError(error or "APPROVED_INPUT_CHANGED")
            counters = state["counters"]
            root = counters["roots"][source["root_task_id"]]
            if (
                root["infrastructure_retries"]
                >= counters["max_infrastructure_retries_per_root_task"]
            ):
                raise CoordinationError("ROOT_RETRY_LIMIT")
            if counters["total_attempts"] >= counters["max_total_attempts"]:
                raise CoordinationError("RUN_ATTEMPT_LIMIT")
            now = time.time()
            if now >= counters["started_at"] + counters["max_duration_seconds"]:
                raise CoordinationError("RUN_DURATION_LIMIT")
            attempt = json.loads(encoded(source))
            attempt_id = str(uuid.uuid4())
            attempt.update(
                id=attempt_id,
                fence=1,
                state="queued",
                physical=None,
                start_key="serial:" + attempt_id,
                workspace=str(self.fixture_runner.workspace(attempt_id)),
            )
            attempt["manifest"].update(id=attempt_id, fence=1)
            attempt["activation"].update(
                id="activation:" + attempt_id,
                attempt_id=attempt_id,
                fence=1,
                expires_at=min(now + 30, counters["started_at"] + counters["max_duration_seconds"]),
            )
            source["fence"] += 1
            self.host.set_control(
                source["id"],
                fence=source["fence"],
                authorization_ref=source["manifest"]["authorization_ref"],
                dispatch_enabled=False,
            )
            root["infrastructure_retries"] += 1
            counters["total_attempts"] += 1
            state["attempts"].append(attempt)
            state["outbox"].append(
                {
                    "id": attempt["start_key"],
                    "kind": "start",
                    "state": "pending",
                    "attempt_id": attempt_id,
                }
            )
            task["state"] = "queued"
            state.update(state="queued", reason_codes=[])
            db.execute("UPDATE runs SET data=? WHERE id=?", (encoded(state), run_id))
            db.execute(
                "INSERT INTO commands VALUES (?,?,?,?)",
                (principal, command_key, payload, encoded(state)),
            )
            return state

    def advance(self, run_id: str, *, crash_at: str | None = None) -> dict[str, Any]:
        if crash_at not in {None, "before_spawn", "after_spawn", "after_ack"} or (
            crash_at is not None and self.fixture_runner is None
        ):
            raise CoordinationError("FIXTURE_CRASH_POINT_INVALID")
        run = self.planner.get(run_id)
        with self._transaction() as db:
            row = db.execute("SELECT data FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise CoordinationError("COORDINATION_NOT_FOUND")
            state: dict[str, Any] = json.loads(row["data"])
            if state["cancelled"]:
                self._cancel_observe(state)
                db.execute("UPDATE runs SET data=? WHERE id=?", (encoded(state), run_id))
                return state
            invalidating = [
                task for task in state["tasks"].values() if task["state"] == "invalidating"
            ]
            if invalidating:
                self._observe_invalidations(state, invalidating)
                db.execute("UPDATE runs SET data=? WHERE id=?", (encoded(state), run_id))
                return state
            if state["tasks"] and (
                self.fixture_runner is None
                or any(
                    task["fixture_recipe"] != self.fixture_runner.identity()
                    for task in state["tasks"].values()
                )
            ):
                has_execution = False
                for attempt in state["attempts"]:
                    try:
                        self.host.inspect(attempt["id"])
                        has_execution = True
                    except KeyError:
                        pass
                state.update(
                    state="invalidating" if has_execution else "blocked",
                    reason_codes=[
                        "LIVE_QUALIFICATION_NOT_RUN"
                        if self.fixture_runner is None
                        else "FIXTURE_RECIPE_CHANGED"
                    ],
                )
                if has_execution:
                    for task in state["tasks"].values():
                        if task["state"] == "invalidated":
                            continue
                        task.update(
                            state="invalidating", invalidation_reason=state["reason_codes"][0]
                        )
                        for attempt in state["attempts"]:
                            if attempt["task_id"] == task["id"]:
                                attempt["fence"] += 1
                                state["outbox"].append(
                                    {
                                        "id": "invalidate:" + attempt["id"],
                                        "kind": "invalidate",
                                        "attempt_id": attempt["id"],
                                        "state": "pending",
                                    }
                                )
                    if all(task["state"] == "invalidated" for task in state["tasks"].values()):
                        state["state"] = "blocked"
                db.execute("UPDATE runs SET data=? WHERE id=?", (encoded(state), run_id))
                return state
            invalidated = False
            approved_revisions = {item["plan_revision"] for item in run["approvals"]}
            for task in state["tasks"].values():
                if task["state"] == "invalidated":
                    continue
                original = task["binding"]
                current, error = material(self.planner, run, task["id"], task["profile_ref"])
                counters = state["counters"]
                if time.time() >= counters["started_at"] + counters["max_duration_seconds"]:
                    error = "RUN_DURATION_LIMIT"
                impacted = any(
                    row["plan_revision"] > original["plan_revision"]
                    and row["plan_revision"] in approved_revisions
                    and task["id"] in row["impact"]["affected"]
                    for row in run["plans"]
                )
                if (
                    error
                    or current is None
                    or current["input_sha256"] != original["input_sha256"]
                    or impacted
                ):
                    task.update(
                        state="invalidating", invalidation_reason=error or "APPROVED_INPUT_CHANGED"
                    )
                    for attempt in state["attempts"]:
                        if attempt["task_id"] == task["id"]:
                            attempt["fence"] += 1
                            state["outbox"].append(
                                {
                                    "id": "invalidate:" + attempt["id"],
                                    "kind": "invalidate",
                                    "attempt_id": attempt["id"],
                                    "state": "pending",
                                }
                            )
                    state.update(state="invalidating", reason_codes=[task["invalidation_reason"]])
                    invalidated = True
            if invalidated:
                db.execute("UPDATE runs SET data=? WHERE id=?", (encoded(state), run_id))
                return state
            for task in state["tasks"].values():
                if task["state"] != "completed" or task["binding"]["task"]["role"] != "worker":
                    continue
                gate = self.candidates.gate(
                    task["candidate_id"],
                    current={
                        "repository_identity": task["baseline"]["repository_identity"],
                        "base_sha": task["baseline"]["base_sha"],
                        "input_sha256": task["binding"]["input_sha256"],
                        "policy_sha256": digest(task["policy"]),
                    },
                )
                if not gate["local_gate_passed"]:
                    task["state"] = "blocked"
                    state.update(state="blocked", reason_codes=gate["reasons"])
                    db.execute("UPDATE runs SET data=? WHERE id=?", (encoded(state), run_id))
                    return state
            if state["paused"]:
                for attempt in state["attempts"]:
                    try:
                        attempt["physical"] = asdict(self.host.inspect(attempt["id"]))
                    except KeyError:
                        pass
                db.execute("UPDATE runs SET data=? WHERE id=?", (encoded(state), run_id))
                return state
            pending = next(
                (task for task in state["tasks"].values() if task["state"] == "candidate_ready"),
                None,
            )
            if pending is not None:
                self._queue_check(state, pending)
            for attempt in state["attempts"]:
                if attempt["state"] not in {"queued", "running", "unknown"}:
                    continue
                task = state["tasks"][attempt["task_id"]]
                if self.fixture_runner is None:
                    state.update(state="blocked", reason_codes=["LIVE_QUALIFICATION_NOT_RUN"])
                    break
                if attempt["state"] == "queued":
                    self._launch(state, attempt, task, crash_at=crash_at)
                else:
                    self._observe(state, attempt, task)
                break
            db.execute("UPDATE runs SET data=? WHERE id=?", (encoded(state), run_id))
            return state

    def _observe_invalidations(self, state: dict[str, Any], tasks: list[dict[str, Any]]) -> None:
        for task in tasks:
            stopped = True
            for attempt in state["attempts"]:
                if attempt["task_id"] != task["id"]:
                    continue
                outbox = next(
                    row for row in state["outbox"] if row["id"] == "invalidate:" + attempt["id"]
                )
                try:
                    self.host.inspect(attempt["id"])
                except KeyError:
                    outbox["state"] = "confirmed_not_started"
                    attempt["physical"] = {"state": "not_started", "remote_stop": "unknown"}
                    attempt["state"] = "invalidated"
                else:
                    self.host.set_control(
                        attempt["id"],
                        fence=attempt["fence"],
                        authorization_ref=attempt["manifest"]["authorization_ref"],
                        dispatch_enabled=False,
                    )
                    cancellation = self.host.cancel(
                        attempt["id"], outbox["id"], timeout_seconds=0.2
                    )
                    attempt["physical"] = asdict(cancellation.snapshot)
                    outbox["state"] = cancellation.status
                    attempt["state"] = (
                        "invalidated" if cancellation.status == "confirmed" else "unknown"
                    )
                    stopped &= cancellation.status == "confirmed"
            if stopped:
                task["state"] = "invalidated"
        pending = any(task["state"] == "invalidating" for task in tasks)
        state.update(
            state="invalidating" if pending else "blocked",
            reason_codes=["STOP_UNKNOWN"]
            if pending
            else sorted({task["invalidation_reason"] for task in tasks}),
        )

    def control(
        self, run_id: str, action: str, *, command_key: str, principal: str
    ) -> dict[str, Any]:
        self.planner.get(run_id, principal=principal)
        for value in (command_key, principal):
            identifier(value)
        if action not in {"pause", "resume", "cancel"}:
            raise CoordinationError("CONTROL_ACTION_INVALID")
        payload = encoded([run_id, "control", action])
        with self._transaction() as db:
            previous = db.execute(
                "SELECT payload,result FROM commands WHERE principal=? AND key=?",
                (principal, command_key),
            ).fetchone()
            if previous:
                if previous["payload"] != payload:
                    raise CoordinationError("IDEMPOTENCY_CONFLICT")
                return dict(json.loads(previous["result"]))
            row = db.execute("SELECT data FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise CoordinationError("COORDINATION_NOT_FOUND")
            state: dict[str, Any] = json.loads(row["data"])
            if state["cancelled"] and action != "cancel":
                raise CoordinationError("RUN_CANCELLED")
            if action == "cancel" and not state["cancelled"]:
                state.update(cancelled=True, paused=False, state="cancelling")
                for attempt in state["attempts"]:
                    attempt["fence"] += 1
                    key = "cancel:" + digest([command_key, attempt["id"]])
                    state["outbox"].append(
                        {
                            "id": key,
                            "kind": "cancel",
                            "state": "pending",
                            "attempt_id": attempt["id"],
                        }
                    )
            elif action == "pause" and not state["paused"]:
                state["before_pause_state"] = state["state"]
                state.update(paused=True, state="paused")
            elif action == "resume" and state["paused"]:
                state["paused"] = False
                if state["state"] == "paused":
                    state["state"] = state["before_pause_state"]
            db.execute("UPDATE runs SET data=? WHERE id=?", (encoded(state), run_id))
            db.execute(
                "INSERT INTO commands VALUES (?,?,?,?)",
                (principal, command_key, payload, encoded(state)),
            )
            return state

    def _cancel_observe(self, state: dict[str, Any]) -> None:
        for attempt in state["attempts"]:
            outbox = next(
                row
                for row in state["outbox"]
                if row["kind"] == "cancel" and row["attempt_id"] == attempt["id"]
            )
            try:
                observed = self.host.inspect(attempt["id"])
            except KeyError:
                attempt["physical"] = {"state": "not_started", "remote_stop": "unknown"}
                attempt["state"] = "cancelled"
                outbox["state"] = "confirmed_not_started"
            else:
                if attempt["state"] != "cancelled":
                    self.host.set_control(
                        attempt["id"],
                        fence=attempt["fence"],
                        authorization_ref=attempt["manifest"]["authorization_ref"],
                        dispatch_enabled=False,
                    )
                    cancellation = self.host.cancel(
                        attempt["id"], outbox["id"], timeout_seconds=0.2
                    )
                    observed = cancellation.snapshot
                    outbox["state"] = cancellation.status
                    attempt["state"] = (
                        "cancelled" if cancellation.status == "confirmed" else "unknown"
                    )
                attempt["physical"] = asdict(observed)
                event_id = digest([attempt["id"], attempt["physical"]])
                if not any(row["id"] == event_id for row in state["inbox"]):
                    state["inbox"].append(
                        {
                            "id": event_id,
                            "attempt_id": attempt["id"],
                            "physical": attempt["physical"],
                            "result_accepted": False,
                        }
                    )
            state["tasks"][attempt["task_id"]]["state"] = "cancelled"
        confirmed = all(attempt["state"] == "cancelled" for attempt in state["attempts"])
        state.update(
            state="cancelled" if confirmed else "cancelling_unknown",
            reason_codes=[] if confirmed else ["STOP_UNKNOWN"],
        )

    def _queue(
        self, state: dict[str, Any], run: dict[str, Any], task_id: str, binding: dict[str, Any]
    ) -> None:
        assert self.fixture_runner is not None
        if state["paused"] or state["cancelled"]:
            raise CoordinationError("RUN_NOT_ACTIVE")
        if task_id in state["tasks"]:
            raise CoordinationError("TASK_ALREADY_ENQUEUED")
        if "execution_worker_tasks" not in state:
            plan = next(
                row for row in run["plans"] if row["plan_revision"] == binding["plan_revision"]
            )
            state["execution_worker_tasks"] = [
                task["id"] for task in plan["plan"]["tasks"] if task["role"] == "worker"
            ]
            state["execution_plan_revision"] = binding["plan_revision"]
            state["quality_repair_enabled"] = False
        now = time.time()
        counters = state.setdefault(
            "counters",
            {
                "total_attempts": 0,
                "started_at": now,
                "max_total_attempts": binding["budget"]["max_total_attempts"],
                "max_duration_seconds": binding["budget"]["max_duration_seconds"],
                "quality_repair_rounds": 0,
                "roots": {},
                "max_quality_repair_rounds": binding["limits"]["max_quality_repair_rounds"],
                "max_infrastructure_retries_per_root_task": binding["limits"][
                    "max_infrastructure_retries_per_root_task"
                ],
            },
        )
        if counters["total_attempts"] >= counters["max_total_attempts"]:
            raise CoordinationError("RUN_ATTEMPT_LIMIT")
        if now >= counters["started_at"] + counters["max_duration_seconds"]:
            raise CoordinationError("RUN_DURATION_LIMIT")
        baseline = self.candidates.register_baseline(
            Path(binding["repository"]["root"]),
            repository_identity=run["project_id"],
            base_sha=binding["repository"]["base_sha"],
        )
        attempt_id = str(uuid.uuid4())
        start_key = "serial:" + attempt_id
        root_id = digest([run["id"], task_id])
        counters["roots"].setdefault(root_id, {"infrastructure_retries": 0})
        counters["total_attempts"] += 1
        expires = min(now + 30, counters["started_at"] + counters["max_duration_seconds"])
        authorization = "approval:" + binding["authorization_digest"]
        manifest = AttemptManifest(
            id=attempt_id,
            fence=1,
            role=binding["task"]["role"],
            profile_id=binding["profile"]["id"],
            profile_revision=binding["profile"]["revision"],
            authorization_ref=authorization,
            budget_ref=binding["budget"]["id"],
            permissions=binding["profile"]["required_permissions"],
            requested_binding=binding["profile"]["binding"],
        )
        attempt = {
            "id": attempt_id,
            "task_id": task_id,
            "root_task_id": root_id,
            "fence": 1,
            "start_key": start_key,
            "state": "queued",
            "workspace": str(self.fixture_runner.workspace(attempt_id)),
            "manifest": manifest.model_dump(),
            "activation": asdict(
                Activation(
                    "activation:" + attempt_id,
                    attempt_id,
                    1,
                    authorization,
                    binding["budget"]["id"],
                    expires,
                )
            ),
            "physical": None,
            "stage": "write" if binding["task"]["role"] == "worker" else "review",
        }
        policy = self.fixture_runner.policy(
            binding["task"]["paths"], binding["authorization"]["checks"], binding["reviewers"]
        )
        subject = None
        if binding["task"]["role"] == "reviewer":
            dependencies = binding["task"]["depends_on"]
            if len(dependencies) != 1:
                raise CoordinationError("REVIEW_SUBJECT_AMBIGUOUS")
            subject = state["tasks"][dependencies[0]]
            policy = subject["policy"]
        state["tasks"][task_id] = {
            "id": task_id,
            "state": "queued",
            "binding": binding,
            "baseline": baseline,
            "profile_ref": {
                "id": binding["profile"]["id"],
                "revision": binding["profile"]["revision"],
            },
            "policy": policy,
            "candidate_id": subject["candidate_id"] if subject else None,
            "subject_task_id": subject["id"] if subject else None,
            "fixture_recipe": self.fixture_runner.identity(),
        }
        state["attempts"].append(attempt)
        state["outbox"].append(
            {"id": start_key, "kind": "start", "state": "pending", "attempt_id": attempt_id}
        )
        state.update(state="queued", reason_codes=[])

    def _queue_check(self, state: dict[str, Any], task: dict[str, Any]) -> None:
        assert self.fixture_runner is not None
        counters = state["counters"]
        if counters["total_attempts"] >= counters["max_total_attempts"]:
            state.update(state="blocked", reason_codes=["RUN_ATTEMPT_LIMIT"])
            return
        original = next(row for row in state["attempts"] if row["task_id"] == task["id"])
        attempt = json.loads(encoded(original))
        attempt_id = str(uuid.uuid4())
        attempt.update(
            id=attempt_id,
            stage="check",
            state="queued",
            physical=None,
            start_key="serial:" + attempt_id,
            workspace=str(self.fixture_runner.workspace(attempt_id)),
        )
        attempt["manifest"]["id"] = attempt_id
        attempt["activation"].update(
            id="activation:" + attempt_id,
            attempt_id=attempt_id,
            expires_at=min(
                time.time() + 30, counters["started_at"] + counters["max_duration_seconds"]
            ),
        )
        counters["total_attempts"] += 1
        state["attempts"].append(attempt)
        state["outbox"].append(
            {
                "id": attempt["start_key"],
                "kind": "start",
                "state": "pending",
                "attempt_id": attempt_id,
            }
        )
        task["state"] = "checking"
        state["state"] = "queued"

    def _launch(
        self,
        state: dict[str, Any],
        attempt: dict[str, Any],
        task: dict[str, Any],
        *,
        crash_at: str | None = None,
    ) -> None:
        assert self.fixture_runner is not None
        if not self.fixture_runner.safe_path(
            Path(attempt["workspace"])
        ) or not self.fixture_runner.safe_path(self.fixture_runner.log_path(attempt["id"])):
            state.update(state="blocked", reason_codes=["FIXTURE_PATH_UNSAFE"])
            return
        try:
            observed = self.host.inspect(attempt["id"])
        except KeyError:
            if attempt["activation"]["expires_at"] <= time.time():
                attempt["state"] = "blocked"
                state.update(state="blocked", reason_codes=["ACTIVATION_NOT_CURRENT"])
                return
            workspace = Path(attempt["workspace"])
            if workspace.exists() or workspace.is_symlink():
                state.update(state="blocked", reason_codes=["WORKSPACE_NOT_NEW"])
                return
            if attempt["stage"] == "write":
                self.fixture_runner.materialize(task["baseline"], workspace)
            else:
                self.candidates.materialize(task["candidate_id"], workspace)
            self.fixture_runner.log_path(attempt["id"]).parent.mkdir(exist_ok=True)
            self.host.prepare(
                AttemptManifest.model_validate(attempt["manifest"]),
                attempt["start_key"],
                ProcessSpec(
                    self.fixture_runner.argv(
                        attempt["stage"], task["binding"]["task"]["paths"], attempt["id"]
                    ),
                    workspace,
                    max(0.01, attempt["activation"]["expires_at"] - time.time()),
                ),
            )
            observed = self.host.inspect(attempt["id"])
        try:
            self.host.set_control(
                attempt["id"],
                fence=attempt["fence"],
                authorization_ref=attempt["manifest"]["authorization_ref"],
                dispatch_enabled=True,
            )
            observed = self.host.start(
                attempt["start_key"], Activation(**attempt["activation"]), crash_at=crash_at
            )
        except LaunchDenied as error:
            attempt["state"] = "blocked"
            attempt["physical"] = asdict(self.host.inspect(attempt["id"]))
            state.update(state="blocked", reason_codes=[str(error)])
            return
        attempt["state"] = "running" if observed.state == "running" else "unknown"
        attempt["physical"] = asdict(observed)
        for row in state["outbox"]:
            if row["id"] == attempt["start_key"]:
                row["state"] = "acknowledged"
        state["state"] = attempt["state"]

    def _observe(
        self, state: dict[str, Any], attempt: dict[str, Any], task: dict[str, Any]
    ) -> None:
        observed = self.host.inspect(attempt["id"])
        attempt["physical"] = asdict(observed)
        event_id = digest([attempt["id"], attempt["physical"]])
        if not any(row["id"] == event_id for row in state["inbox"]):
            state["inbox"].append(
                {"id": event_id, "attempt_id": attempt["id"], "physical": attempt["physical"]}
            )
        if observed.state != "exited" or observed.exit_code is None:
            attempt["state"] = "running" if observed.state == "running" else "unknown"
            state["state"] = attempt["state"]
            return
        if attempt["stage"] in {"check", "review"}:
            self._record_validation(state, attempt, task, observed.exit_code, event_id)
            return
        if observed.exit_code != 0:
            attempt["state"] = "failed"
            task["state"] = "failed"
            state.update(state="blocked", reason_codes=["WORKER_FAILED"])
            return
        binding = task["binding"]
        registration = binding["registration"]
        candidate = self.candidates.freeze(
            Path(attempt["workspace"]),
            {
                "series_id": state["run_id"] + "/" + attempt["task_id"],
                "baseline_id": task["baseline"]["id"],
                "input_sha256": binding["input_sha256"],
                "allowed_paths": binding["task"]["paths"],
                "task_class": binding["effective_class"],
                "writer": {
                    "attempt_id": attempt["id"],
                    "fence": attempt["fence"],
                    "stopped": True,
                    "observation_ref": "runnerhost:" + event_id,
                },
                "authors": [
                    {
                        "attempt_id": attempt["id"],
                        "fence": attempt["fence"],
                        "profile_id": registration["id"],
                        "profile_revision": registration["revision"],
                        "model_family": registration["model_family"],
                        "context_id": attempt["id"],
                        "provenance_ref": "fixture:fixed-process",
                    }
                ],
                "policy": task["policy"],
            },
        )
        attempt["state"] = "completed"
        task.update(state="candidate_ready", candidate_id=candidate["id"])
        state["state"] = "candidate_ready"

    def _record_validation(
        self,
        state: dict[str, Any],
        attempt: dict[str, Any],
        task: dict[str, Any],
        exit_code: int,
        event_id: str,
    ) -> None:
        assert self.fixture_runner is not None
        candidate = self.candidates.get(task["candidate_id"])
        common = {
            "evidence_key": "evidence:" + attempt["id"],
            "candidate_id": candidate["id"],
            "policy_sha256": candidate["policy_sha256"],
            "input_sha256": candidate["input_sha256"],
            "observation_ref": "runnerhost:" + event_id,
            "provenance": "fixture",
        }
        log = self.fixture_runner.read_log(attempt["id"])
        if attempt["stage"] == "check":
            policy = task["policy"]["checks"][0]
            evidence = self.candidates.record_check(
                common
                | {
                    "environment_sha256": policy["environment_sha256"],
                    "check_id": policy["id"],
                    "check_revision": policy["revision"],
                    "executor_ref": "runnerhost:" + attempt["id"],
                    "exit_code": exit_code,
                    "outcome": "completed",
                },
                log=log,
            )
            task["check_evidence"] = evidence
            task["state"] = "awaiting_review" if evidence["status"] == "passed" else "blocked"
            state["state"] = task["state"]
            state["reason_codes"] = [] if evidence["status"] == "passed" else ["CHECK_NOT_PASSED"]
        else:
            subject = state["tasks"][task["subject_task_id"]]
            registration = task["binding"]["registration"]
            policy = task["policy"]["review"]
            verdict = "failed" if exit_code != 0 else "inconclusive"
            try:
                record = json.loads(log) if log else None
                if (
                    exit_code == 0
                    and isinstance(record, dict)
                    and record.get("operation") == "review"
                    and record.get("synthetic") is True
                    and record.get("files") == task["binding"]["task"]["paths"]
                    and record.get("author_reasoning_included") is False
                    and record.get("verdict") in {"passed", "failed", "inconclusive"}
                ):
                    verdict = record["verdict"]
            except (ValueError, TypeError):
                pass
            evidence = self.candidates.record_review(
                common
                | {
                    "environment_sha256": policy["environment_sha256"],
                    "review_revision": policy["revision"],
                    "check_evidence_ids": [subject["check_evidence"]["id"]],
                    "actor": {
                        "attempt_id": attempt["id"],
                        "fence": attempt["fence"],
                        "profile_id": registration["id"],
                        "profile_revision": registration["revision"],
                        "model_family": registration["model_family"],
                        "context_id": attempt["id"],
                        "provenance_ref": "fixture:independent-process",
                    },
                    "author_reasoning_included": False,
                    "verdict": verdict,
                    "findings": [],
                },
                log=log,
            )
            task["review_evidence"] = evidence
            task["state"] = "completed" if evidence["status"] == "passed" else "blocked"
            gate = self.candidates.gate(
                candidate["id"],
                current={
                    "repository_identity": subject["baseline"]["repository_identity"],
                    "base_sha": subject["baseline"]["base_sha"],
                    "input_sha256": subject["binding"]["input_sha256"],
                    "policy_sha256": digest(subject["policy"]),
                },
            )
            state.update(
                state="local_gate_passed" if gate["local_gate_passed"] else "blocked",
                reason_codes=gate["reasons"],
            )
            if gate["local_gate_passed"]:
                subject["state"] = "completed"
                run = self.planner.get(state["run_id"])
                plan = next(
                    row
                    for row in run["plans"]
                    if row["plan_revision"] == run["active_plan_revision"]
                )
                state["remaining_required_task_ids"] = sorted(
                    row["id"]
                    for row in plan["plan"]["tasks"]
                    if row["required"]
                    and state["tasks"].get(row["id"], {}).get("state") != "completed"
                )
                if state["remaining_required_task_ids"]:
                    state["state"] = "awaiting_tasks"
                elif sum(row["role"] == "worker" for row in plan["plan"]["tasks"]) > 1:
                    state.update(
                        state="integration_required",
                        reason_codes=["INTEGRATED_CANDIDATE_NOT_IMPLEMENTED"],
                    )
        attempt["state"] = "completed"
