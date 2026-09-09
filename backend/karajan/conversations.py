"""Durable, non-executing Commander conversations.

This store deliberately owns only the Project -> Conversation -> Run identity and
the browser's control-context data.  It never calls planning or execution code.
"""

from __future__ import annotations

import builtins
import hashlib
import json
import re
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, cast

from karajan.projects import ProjectError, ProjectRegistry
from karajan.runs import RunPlanner
from karajan.storage import open_database, require_schema


class ConversationError(ValueError):
    def __init__(self, code: str, *, revision: int | None = None) -> None:
        self.code, self.revision = code, revision
        super().__init__(code)


def _encoded(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: object) -> str:
    return hashlib.sha256(_encoded(value).encode()).hexdigest()


def _identifier(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) is None:
        raise ConversationError("INPUT_INVALID")
    return value


class ConversationStore:
    """A small SQLite projection with idempotent commands and replayable events."""

    _legacy_namespace = uuid.UUID("e9e9a0cf-f970-5b45-9aa0-0a1ea3374b4b")

    def __init__(
        self,
        projects: ProjectRegistry,
        planner: RunPlanner,
        *,
        planning_execution: object | None = None,
    ) -> None:
        # The conversation projection shares the Run ledger.  In particular,
        # conversation_run_bindings has real foreign keys to both aggregates,
        # so a Run command can establish identity in its own transaction.
        self.database, self.projects, self.planner = planner.database, projects, planner
        self.planning_execution = planning_execution
        if planner.existing_only:
            require_schema(
                self.database,
                {
                    "commander_conversations": ["id", "project_id", "snapshot"],
                    "conversation_run_bindings": ["run_id", "conversation_id", "project_id"],
                    "conversation_messages": [
                        "id",
                        "conversation_id",
                        "client_message_id",
                        "snapshot",
                    ],
                    "conversation_drafts": ["conversation_id", "snapshot"],
                    "conversation_task_drafts": ["id", "conversation_id", "snapshot"],
                    "conversation_commands": ["principal", "key", "digest", "result", "error"],
                    "conversation_events": [
                        "sequence",
                        "project_id",
                        "conversation_id",
                        "event_type",
                        "object_revision",
                        "payload",
                        "at",
                    ],
                },
            )
        else:
            self.migrate_legacy_runs()

    @contextmanager
    def _transaction(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        db = open_database(
            self.database, existing_only=self.planner.existing_only, isolation_level=None
        )
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE" if write or not self.planner.existing_only else "BEGIN")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _project(self, project_id: str) -> None:
        try:
            self.projects.get(project_id)
        except ProjectError:
            raise ConversationError("PROJECT_NOT_FOUND") from None

    def commander_options(self, project_id: str) -> builtins.list[dict[str, Any]]:
        self._project(project_id)
        configuration = self.projects.get_configuration(project_id)["configuration"]
        approved = {
            (item["id"], item["revision"]) for item in configuration["approved_profile_refs"]
        }
        return [
            {
                "profile_ref": item["profile"]["id"],
                "profile_revision": item["profile"]["revision"],
                "source_ref": item["profile"]["binding"]["channel_id"],
            }
            for item in configuration["resources"]["profiles"]
            if item["profile"] is not None
            and (item["profile"]["id"], item["profile"]["revision"]) in approved
        ]

    def _validate_selection(self, project_id: str, profile: object, source: object) -> None:
        if profile is None and source is None:
            return
        if (
            not isinstance(profile, str)
            or not isinstance(source, str)
            or not any(
                option["profile_ref"] == profile and option["source_ref"] == source
                for option in self.commander_options(project_id)
            )
        ):
            raise ConversationError("COMMANDER_SELECTION_NOT_CONFIGURED")

    def _event(self, db: sqlite3.Connection, item: dict[str, Any], event_type: str) -> int:
        payload = {"id": item.get("id", item.get("draft_id")), "revision": item["revision"]}
        conversation_id = item.get("conversation_id") or item.get("id")
        if not isinstance(conversation_id, str):
            raise ConversationError("INPUT_INVALID")
        cursor = db.execute(
            "INSERT INTO conversation_events("
            "project_id, conversation_id, event_type, object_revision, payload, at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                item["project_id"],
                conversation_id,
                event_type,
                item["revision"],
                _encoded(payload),
                time.time(),
            ),
        )
        if cursor.lastrowid is None:
            raise ConversationError("CONVERSATION_EVENT_NOT_PERSISTED")
        return cursor.lastrowid

    def _command(
        self,
        db: sqlite3.Connection,
        principal: str,
        key: str,
        request: object,
        apply: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        identity = _digest(request)
        previous = db.execute(
            "SELECT * FROM conversation_commands WHERE principal=? AND key=?", (principal, key)
        ).fetchone()
        if previous is not None:
            if previous["digest"] != identity:
                raise ConversationError("IDEMPOTENCY_KEY_REUSED")
            if previous["error"]:
                raise ConversationError(previous["error"])
            return cast(dict[str, Any], json.loads(previous["result"]))
        try:
            result = apply()
        except ConversationError as error:
            db.execute(
                "INSERT INTO conversation_commands VALUES (?, ?, ?, NULL, ?)",
                (principal, key, identity, error.code),
            )
            raise
        db.execute(
            "INSERT INTO conversation_commands VALUES (?, ?, ?, ?, NULL)",
            (principal, key, identity, _encoded(result)),
        )
        return result

    def _conversation(self, db: sqlite3.Connection, conversation_id: str) -> dict[str, Any]:
        row = db.execute(
            "SELECT snapshot FROM commander_conversations WHERE id=?", (conversation_id,)
        ).fetchone()
        if row is None:
            raise ConversationError("CONVERSATION_NOT_FOUND")
        return cast(dict[str, Any], json.loads(row["snapshot"]))

    def _save_conversation(self, db: sqlite3.Connection, item: dict[str, Any]) -> None:
        db.execute(
            "UPDATE commander_conversations SET snapshot=? WHERE id=?", (_encoded(item), item["id"])
        )

    def migrate_legacy_runs(self) -> None:
        """Bind historical runs in their existing order without changing Run IDs."""
        runs = self.planner.list(principal="owner")
        with self._transaction(write=True) as db:
            for run in runs:
                bound = db.execute(
                    "SELECT 1 FROM conversation_run_bindings WHERE run_id=?", (run["id"],)
                ).fetchone()
                if bound is not None:
                    continue
                project_id = run["project_id"]
                conversation_id = run.get("conversation_id") or str(
                    uuid.uuid5(self._legacy_namespace, project_id)
                )
                row = db.execute(
                    "SELECT 1 FROM commander_conversations WHERE id=?", (conversation_id,)
                ).fetchone()
                if row is None:
                    item = {
                        "id": conversation_id,
                        "project_id": project_id,
                        "title": "Legacy Commander conversation",
                        "state": "active",
                        "commander_profile_ref": None,
                        "commander_source_ref": None,
                        "revision": 1,
                        "last_event_seq": 0,
                        "legacy": True,
                    }
                    db.execute(
                        "INSERT INTO commander_conversations VALUES (?, ?, ?)",
                        (conversation_id, project_id, _encoded(item)),
                    )
                    db.execute(
                        "INSERT OR IGNORE INTO conversation_drafts VALUES (?, ?)",
                        (
                            conversation_id,
                            _encoded(
                                {
                                    "conversation_id": conversation_id,
                                    "project_id": project_id,
                                    "draft_id": str(uuid.uuid4()),
                                    "content": "",
                                    "selected_task_id": None,
                                    "base_plan_revision": None,
                                    "revision": 1,
                                }
                            ),
                        ),
                    )
                    item["last_event_seq"] = self._event(db, item, "conversation_created")
                    self._save_conversation(db, item)
                db.execute(
                    "INSERT INTO conversation_run_bindings VALUES (?, ?, ?)",
                    (run["id"], conversation_id, project_id),
                )

    def list(self, project_id: str) -> builtins.list[dict[str, Any]]:
        self._project(project_id)
        with self._transaction() as db:
            return [
                json.loads(row["snapshot"])
                for row in db.execute(
                    "SELECT snapshot FROM commander_conversations "
                    "WHERE project_id=? ORDER BY rowid",
                    (project_id,),
                )
            ]

    def create(
        self, project_id: str, request: dict[str, Any], *, principal: str, key: str
    ) -> dict[str, Any]:
        self._project(project_id)
        title = request.get("title", "New Commander conversation")
        if not isinstance(title, str) or not title.strip() or len(title) > 200:
            raise ConversationError("INPUT_INVALID")
        for field in ("commander_profile_ref", "commander_source_ref"):
            if request.get(field) is not None:
                _identifier(request[field])
        self._validate_selection(
            project_id, request.get("commander_profile_ref"), request.get("commander_source_ref")
        )
        if set(request) - {"title", "commander_profile_ref", "commander_source_ref"}:
            raise ConversationError("INPUT_INVALID")
        with self._transaction(write=True) as db:

            def apply() -> dict[str, Any]:
                item = {
                    "id": str(uuid.uuid4()),
                    "project_id": project_id,
                    "title": title.strip(),
                    "state": "active",
                    "commander_profile_ref": request.get("commander_profile_ref"),
                    "commander_source_ref": request.get("commander_source_ref"),
                    "revision": 1,
                    "last_event_seq": 0,
                    "legacy": False,
                }
                db.execute(
                    "INSERT INTO commander_conversations VALUES (?, ?, ?)",
                    (item["id"], project_id, _encoded(item)),
                )
                # Draft revision is a public optimistic-concurrency value.
                # Materialize it at creation so every new conversation starts
                # at revision 1 instead of giving an absent draft a hidden 0.
                draft = {
                    "conversation_id": item["id"],
                    "project_id": project_id,
                    "draft_id": str(uuid.uuid4()),
                    "content": "",
                    "selected_task_id": None,
                    "base_plan_revision": None,
                    "revision": 1,
                }
                db.execute(
                    "INSERT INTO conversation_drafts VALUES (?, ?)", (item["id"], _encoded(draft))
                )
                item["last_event_seq"] = self._event(db, item, "conversation_created")
                self._save_conversation(db, item)
                return item

            return self._command(db, principal, key, ["create", project_id, request], apply)

    def bind_run(self, project_id: str, run_id: str, conversation_id: str | None) -> str:
        with self._transaction(write=True) as db:
            if conversation_id is None:
                conversation_id = str(uuid.uuid5(self._legacy_namespace, project_id))
                if (
                    db.execute(
                        "SELECT 1 FROM commander_conversations WHERE id=?", (conversation_id,)
                    ).fetchone()
                    is None
                ):
                    item = {
                        "id": conversation_id,
                        "project_id": project_id,
                        "title": "Legacy Commander conversation",
                        "state": "active",
                        "commander_profile_ref": None,
                        "commander_source_ref": None,
                        "revision": 1,
                        "last_event_seq": 0,
                        "legacy": True,
                    }
                    db.execute(
                        "INSERT INTO commander_conversations VALUES (?, ?, ?)",
                        (item["id"], project_id, _encoded(item)),
                    )
                    item["last_event_seq"] = self._event(db, item, "conversation_created")
                    self._save_conversation(db, item)
            item = self._conversation(db, conversation_id)
            if item["project_id"] != project_id:
                raise ConversationError("CROSS_PROJECT_REFERENCE")
            previous = db.execute(
                "SELECT conversation_id, project_id FROM conversation_run_bindings WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if previous is not None:
                if (
                    previous["project_id"] != project_id
                    or previous["conversation_id"] != conversation_id
                ):
                    raise ConversationError("CROSS_PROJECT_REFERENCE")
                return str(previous["conversation_id"])
            db.execute(
                "INSERT INTO conversation_run_bindings VALUES (?, ?, ?)",
                (run_id, conversation_id, project_id),
            )
            return conversation_id

    def validate_conversation(self, project_id: str, conversation_id: str) -> None:
        with self._transaction() as db:
            item = self._conversation(db, conversation_id)
            if item["project_id"] != project_id:
                raise ConversationError("CROSS_PROJECT_REFERENCE")

    def run_conversation(self, run_id: str, project_id: str) -> str:
        with self._transaction() as db:
            row = db.execute(
                "SELECT conversation_id, project_id FROM conversation_run_bindings WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise ConversationError("RUN_CONVERSATION_UNBOUND")
            if row["project_id"] != project_id:
                raise ConversationError("CROSS_PROJECT_REFERENCE")
            return str(row["conversation_id"])

    def bound_conversation(self, run_id: str, project_id: str) -> str:
        """Read an already migrated binding without creating recovery state."""
        with self._transaction() as db:
            row = db.execute(
                "SELECT conversation_id, project_id FROM conversation_run_bindings WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if row is None or row["project_id"] != project_id:
                raise ConversationError("RUN_CONVERSATION_UNBOUND")
            return str(row["conversation_id"])

    @staticmethod
    def _execution_next_action(execution: dict[str, Any]) -> str:
        """Name only an operation already supported by the persisted state."""
        if execution.get("cancel_requested"):
            return "none"
        state = execution.get("state")
        return {
            "awaiting_admission": "admit",
            "admission_unknown": "reconcile_admission",
            "awaiting_output": "submit_planning_output",
            "output_captured": "submit_planning_output",
            "submit_claimed": "reconcile_submission",
            "submission_unknown": "reconcile_submission",
        }.get(state if isinstance(state, str) else "", "none")

    def _execution_attempts(
        self, run: dict[str, Any]
    ) -> tuple[builtins.list[dict[str, Any]], builtins.list[dict[str, Any]], set[str]]:
        """Project actual controller ledger state onto the Hub without effects."""
        reader = self.planning_execution
        read = None if reader is None else getattr(reader, "_list_for_trusted_hub_run", None)
        if not callable(read):
            return [], [], set()
        records = read(run)
        if not isinstance(records, list):
            raise ConversationError("PLANNING_EXECUTION_LEDGER_INVALID")
        attempts: builtins.list[dict[str, Any]] = []
        blockers: builtins.list[dict[str, Any]] = []
        covered_intents: set[str] = set()
        for execution in records:
            if not isinstance(execution, dict):
                raise ConversationError("PLANNING_EXECUTION_LEDGER_INVALID")
            binding = execution.get("binding")
            intent_id = execution.get("intent_id")
            if (
                not isinstance(binding, dict)
                or not isinstance(intent_id, str)
                or binding.get("run_id") != run["id"]
                or binding.get("intent_id") != intent_id
                or not all(
                    isinstance(binding.get(field), str)
                    for field in ("attempt_id", "principal", "execution_id")
                )
                or not isinstance(binding.get("term"), int)
                or not isinstance(binding.get("profile"), dict)
                or not isinstance(execution.get("state"), str)
            ):
                raise ConversationError("PLANNING_EXECUTION_LEDGER_INVALID")
            covered_intents.add(intent_id)
            reason_codes = execution.get("reason_codes", [])
            if not isinstance(reason_codes, list) or not all(
                isinstance(code, str) for code in reason_codes
            ):
                raise ConversationError("PLANNING_EXECUTION_LEDGER_INVALID")
            admission = execution.get("admission")
            attempts.append(
                {
                    "id": binding["attempt_id"],
                    "run_id": run["id"],
                    "kind": "planning",
                    "state": execution["state"],
                    "term": binding["term"],
                    "principal": binding["principal"],
                    "profile": binding["profile"],
                    "intent_id": intent_id,
                    "execution_id": binding["execution_id"],
                    "admission_state": admission.get("state")
                    if isinstance(admission, dict)
                    else None,
                    "reason_codes": reason_codes,
                    "next_action": self._execution_next_action(execution),
                }
            )
            blockers.extend(
                {
                    "run_id": run["id"],
                    "attempt_id": binding["attempt_id"],
                    "execution_id": binding["execution_id"],
                    "reason_code": reason,
                }
                for reason in reason_codes
            )
        return attempts, blockers, covered_intents

    def snapshot(self, conversation_id: str) -> dict[str, Any]:
        with self._transaction() as db:
            item = self._conversation(db, conversation_id)
            messages = [
                json.loads(row["snapshot"])
                for row in db.execute(
                    "SELECT snapshot FROM conversation_messages "
                    "WHERE conversation_id=? ORDER BY rowid",
                    (conversation_id,),
                )
            ]
            draft = db.execute(
                "SELECT snapshot FROM conversation_drafts WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
            task_drafts = [
                json.loads(row["snapshot"])
                for row in db.execute(
                    "SELECT snapshot FROM conversation_task_drafts "
                    "WHERE conversation_id=? ORDER BY rowid",
                    (conversation_id,),
                )
            ]
            runs = [
                row["run_id"]
                for row in db.execute(
                    "SELECT run_id FROM conversation_run_bindings WHERE conversation_id=?",
                    (conversation_id,),
                )
            ]
            run_summaries = []
            tasks: builtins.list[dict[str, Any]] = []
            attempts: builtins.list[dict[str, Any]] = []
            blockers: builtins.list[dict[str, Any]] = []
            agents: builtins.list[dict[str, Any]] = []
            for run_id in runs:
                row = db.execute("SELECT snapshot FROM runs WHERE id=?", (run_id,)).fetchone()
                if row is None:
                    raise ConversationError("RUN_CONVERSATION_UNBOUND")
                run: dict[str, Any] = json.loads(row["snapshot"])
                if run.get("owner") != "owner":
                    raise ConversationError("RUN_CONVERSATION_UNBOUND")
                if run["project_id"] != item["project_id"]:
                    raise ConversationError("CROSS_PROJECT_REFERENCE")
                active = next(
                    (
                        plan
                        for plan in run["plans"]
                        if plan["plan_revision"] == run["active_plan_revision"]
                    ),
                    None,
                )
                latest = run["plans"][-1] if run["plans"] else None
                plan = active or latest
                plan_state = "active" if active else "proposed" if latest else "not_planned"
                summary = {
                    "id": run["id"],
                    "project_id": run["project_id"],
                    "conversation_id": conversation_id,
                    "revision": run["revision"],
                    "state": run["state"],
                    "commander_term": run["commander"]["term"],
                    "active_plan_revision": run["active_plan_revision"],
                    "latest_plan_revision": run["latest_plan_revision"],
                    "dispatch_enabled": run["dispatch_enabled"],
                    "snapshot_event_seq": item["last_event_seq"],
                    "plan_state": plan_state,
                }
                run_summaries.append(summary)
                agents.extend(
                    {
                        "run_id": run["id"],
                        "principal": participant["principal"],
                        "role": participant["purpose"],
                        "profile": participant["profile"],
                    }
                    for participant in run["participants"]
                )
                execution_attempts, execution_blockers, covered_intents = self._execution_attempts(
                    run
                )
                attempts.extend(
                    {
                        "id": intent["id"],
                        "run_id": run["id"],
                        "kind": "planning",
                        "state": intent["state"],
                        "term": intent["term"],
                        "principal": intent["principal"],
                        "profile": intent["profile"],
                    }
                    for intent in run["planning_intents"]
                    if intent["id"] not in covered_intents
                )
                attempts.extend(execution_attempts)
                blockers.extend(execution_blockers)
                if plan is None:
                    blockers.append({"run_id": run["id"], "reason_code": "PLAN_NOT_AVAILABLE"})
                    continue
                for task in plan["plan"]["tasks"]:
                    task_state = (
                        "ready"
                        if active and task["readiness"] == "ready"
                        else "proposed"
                        if not active
                        else "blocked"
                    )
                    task_view = {
                        "id": task["id"],
                        "run_id": run["id"],
                        "plan_revision": plan["plan_revision"],
                        "revision": task["revision"],
                        "role": task["role"],
                        "state": task_state,
                        "readiness": task["readiness"],
                        "depends_on": task["depends_on"],
                        "checks": task.get("checks", []),
                    }
                    tasks.append(task_view)
                    if task_state == "blocked":
                        blockers.append(
                            {
                                "run_id": run["id"],
                                "task_id": task["id"],
                                "reason_code": "TASK_NOT_READY",
                            }
                        )
            return {
                "conversation": item,
                "messages": messages,
                "draft": json.loads(draft["snapshot"]) if draft else None,
                "task_drafts": task_drafts,
                "runs": runs,
                "run_summaries": run_summaries,
                "tasks": tasks,
                "attempts": attempts,
                "agents": agents,
                "blockers": blockers,
                "snapshot_event_seq": item["last_event_seq"],
            }

    def settings(
        self,
        conversation_id: str,
        request: dict[str, Any],
        *,
        principal: str,
        key: str,
        revision: int,
    ) -> dict[str, Any]:
        if set(request) - {"title", "commander_profile_ref", "commander_source_ref"} or not request:
            raise ConversationError("INPUT_INVALID")
        for field in ("commander_profile_ref", "commander_source_ref"):
            if field in request and request[field] is not None:
                _identifier(request[field])
        if "title" in request and (
            not isinstance(request["title"], str)
            or not request["title"].strip()
            or len(request["title"]) > 200
        ):
            raise ConversationError("INPUT_INVALID")
        with self._transaction(write=True) as db:
            item = self._conversation(db, conversation_id)
            self._validate_selection(
                item["project_id"],
                request.get("commander_profile_ref", item["commander_profile_ref"]),
                request.get("commander_source_ref", item["commander_source_ref"]),
            )

            def apply() -> dict[str, Any]:
                if item["revision"] != revision:
                    raise ConversationError(
                        "CONVERSATION_REVISION_CONFLICT", revision=item["revision"]
                    )
                item.update(
                    {
                        key: value.strip() if key == "title" else value
                        for key, value in request.items()
                    }
                )
                item["revision"] += 1
                item["last_event_seq"] = self._event(db, item, "conversation_settings_saved")
                self._save_conversation(db, item)
                return item

            return self._command(
                db, principal, key, ["settings", conversation_id, revision, request], apply
            )

    def message(
        self, conversation_id: str, request: dict[str, Any], *, principal: str, key: str
    ) -> dict[str, Any]:
        if set(request) != {"client_message_id", "content"}:
            raise ConversationError("INPUT_INVALID")
        client_id, content = _identifier(request["client_message_id"]), request["content"]
        if not isinstance(content, str) or not content.strip() or len(content) > 20_000:
            raise ConversationError("INPUT_INVALID")
        with self._transaction(write=True) as db:
            item = self._conversation(db, conversation_id)

            def apply() -> dict[str, Any]:
                existing = db.execute(
                    "SELECT snapshot FROM conversation_messages WHERE conversation_id=? "
                    "AND client_message_id=?",
                    (conversation_id, client_id),
                ).fetchone()
                if existing is not None:
                    return cast(dict[str, Any], json.loads(existing["snapshot"]))
                message = {
                    "id": str(uuid.uuid4()),
                    "conversation_id": conversation_id,
                    "project_id": item["project_id"],
                    "client_message_id": client_id,
                    "role": "user",
                    "content": content,
                    "created_at": time.time(),
                    "revision": 1,
                }
                db.execute(
                    "INSERT INTO conversation_messages VALUES (?, ?, ?, ?)",
                    (message["id"], conversation_id, client_id, _encoded(message)),
                )
                item["revision"] += 1
                item["last_event_seq"] = self._event(db, message, "message_created")
                self._save_conversation(db, item)
                return message

            return self._command(db, principal, key, ["message", conversation_id, request], apply)

    def draft(
        self,
        conversation_id: str,
        request: dict[str, Any],
        *,
        principal: str,
        key: str,
        revision: int,
    ) -> dict[str, Any]:
        if set(request) - {"content", "selected_task_id", "base_plan_revision"} or not isinstance(
            request.get("content"), str
        ):
            raise ConversationError("INPUT_INVALID")
        if (
            len(request["content"]) > 20_000
            or (
                request.get("selected_task_id") is not None
                and not isinstance(request["selected_task_id"], str)
            )
            or (
                request.get("base_plan_revision") is not None
                and (
                    type(request["base_plan_revision"]) is not int
                    or request["base_plan_revision"] < 0
                )
            )
        ):
            raise ConversationError("INPUT_INVALID")
        with self._transaction(write=True) as db:
            item = self._conversation(db, conversation_id)

            def apply() -> dict[str, Any]:
                selected = request.get("selected_task_id")
                if selected is not None and not self._selected_object_exists(
                    db, conversation_id, selected
                ):
                    raise ConversationError("TASK_REFERENCE_NOT_FOUND")
                old = db.execute(
                    "SELECT snapshot FROM conversation_drafts WHERE conversation_id=?",
                    (conversation_id,),
                ).fetchone()
                current = json.loads(old["snapshot"])["revision"] if old else 0
                if revision != current:
                    raise ConversationError("DRAFT_REVISION_CONFLICT", revision=current)
                value = {
                    "conversation_id": conversation_id,
                    "project_id": item["project_id"],
                    "draft_id": str(uuid.uuid4())
                    if old is None
                    else json.loads(old["snapshot"])["draft_id"],
                    "content": request["content"],
                    "selected_task_id": request.get("selected_task_id"),
                    "base_plan_revision": request.get("base_plan_revision"),
                    "revision": current + 1,
                }
                db.execute(
                    "INSERT OR REPLACE INTO conversation_drafts VALUES (?, ?)",
                    (conversation_id, _encoded(value)),
                )
                item["revision"] += 1
                item["last_event_seq"] = self._event(db, value, "draft_saved")
                self._save_conversation(db, item)
                return value

            return self._command(
                db, principal, key, ["draft", conversation_id, revision, request], apply
            )

    def _selected_object_exists(
        self, db: sqlite3.Connection, conversation_id: str, selected_id: str
    ) -> bool:
        """Accept draft, Task, or planning Attempt identities from this conversation only."""
        if (
            db.execute(
                "SELECT 1 FROM conversation_task_drafts WHERE id=? AND conversation_id=?",
                (selected_id, conversation_id),
            ).fetchone()
            is not None
        ):
            return True
        for row in db.execute(
            "SELECT r.snapshot FROM runs r JOIN conversation_run_bindings b ON b.run_id=r.id "
            "WHERE b.conversation_id=?",
            (conversation_id,),
        ):
            run: dict[str, Any] = json.loads(row["snapshot"])
            if any(intent["id"] == selected_id for intent in run["planning_intents"]):
                return True
            if any(
                task["id"] == selected_id for plan in run["plans"] for task in plan["plan"]["tasks"]
            ):
                return True
        return False

    def task_draft(
        self, conversation_id: str, request: dict[str, Any], *, principal: str, key: str
    ) -> dict[str, Any]:
        if (
            set(request) != {"requirement"}
            or not isinstance(request["requirement"], str)
            or not request["requirement"].strip()
            or len(request["requirement"]) > 20_000
        ):
            raise ConversationError("INPUT_INVALID")
        with self._transaction(write=True) as db:
            item = self._conversation(db, conversation_id)

            def apply() -> dict[str, Any]:
                value = {
                    "id": str(uuid.uuid4()),
                    "conversation_id": conversation_id,
                    "project_id": item["project_id"],
                    "requirement": request["requirement"],
                    "state": "draft",
                    "proposal_revision": None,
                    "revision": 1,
                }
                db.execute(
                    "INSERT INTO conversation_task_drafts VALUES (?, ?, ?)",
                    (value["id"], conversation_id, _encoded(value)),
                )
                item["revision"] += 1
                item["last_event_seq"] = self._event(db, value, "task_draft_created")
                self._save_conversation(db, item)
                return value

            return self._command(
                db, principal, key, ["task_draft", conversation_id, request], apply
            )

    def events(self, conversation_id: str, after_seq: int) -> builtins.list[dict[str, Any]]:
        with self._transaction() as db:
            item = self._conversation(db, conversation_id)
            known_cursor = (
                after_seq == 0
                or after_seq == item["last_event_seq"]
                or db.execute(
                    "SELECT 1 FROM conversation_events WHERE conversation_id=? AND sequence=?",
                    (conversation_id, after_seq),
                ).fetchone()
                is not None
            )
            if after_seq < 0 or after_seq > item["last_event_seq"] or not known_cursor:
                return [
                    {
                        "event_type": "event_gap",
                        "snapshot_required": True,
                        "snapshot_event_seq": item["last_event_seq"],
                    }
                ]
            return [
                {
                    "sequence": row["sequence"],
                    "project_id": row["project_id"],
                    "conversation_id": row["conversation_id"],
                    "event_type": row["event_type"],
                    "object_revision": row["object_revision"],
                    "payload": json.loads(row["payload"]),
                    "at": row["at"],
                }
                for row in db.execute(
                    "SELECT * FROM conversation_events WHERE conversation_id=? "
                    "AND sequence>? ORDER BY sequence",
                    (conversation_id, after_seq),
                )
            ]
