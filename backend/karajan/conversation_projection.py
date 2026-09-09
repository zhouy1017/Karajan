"""Shared durable initialization for the legacy Conversation projection."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Callable
from typing import Any, cast

LEGACY_CONVERSATION_NAMESPACE = uuid.UUID("e9e9a0cf-f970-5b45-9aa0-0a1ea3374b4b")


class ConversationProjectionError(ValueError):
    """The shared Run/Conversation transaction could not persist its projection."""


def encoded(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def legacy_conversation_id(project_id: str) -> str:
    return str(uuid.uuid5(LEGACY_CONVERSATION_NAMESPACE, project_id))


def ensure_legacy_conversation(
    db: sqlite3.Connection,
    project_id: str,
    *,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Create exactly the documented omitted-identity compatibility projection.

    The caller owns validation of the Project and passes its encompassing
    transaction.  This helper intentionally never accepts an arbitrary
    conversation ID, so an explicitly supplied bad reference cannot be
    converted into a legacy conversation.
    """
    conversation_id = legacy_conversation_id(project_id)
    row = db.execute(
        "SELECT snapshot FROM commander_conversations WHERE id=?", (conversation_id,)
    ).fetchone()
    if row is not None:
        conversation = cast(dict[str, Any], json.loads(row["snapshot"]))
        if conversation.get("project_id") != project_id:
            raise ConversationProjectionError("CROSS_PROJECT_REFERENCE")
        return conversation
    conversation = {
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
        (conversation_id, project_id, encoded(conversation)),
    )
    # Draft revision is a public concurrency value.  Create it together with
    # every compatibility conversation just as explicit creation does.
    db.execute(
        "INSERT INTO conversation_drafts VALUES (?, ?)",
        (
            conversation_id,
            encoded(
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
    cursor = db.execute(
        "INSERT INTO conversation_events("
        "project_id, conversation_id, event_type, object_revision, payload, at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            project_id,
            conversation_id,
            "conversation_created",
            1,
            encoded({"id": conversation_id, "revision": 1}),
            clock(),
        ),
    )
    if cursor.lastrowid is None:
        raise ConversationProjectionError("CONVERSATION_EVENT_NOT_PERSISTED")
    conversation["last_event_seq"] = cursor.lastrowid
    db.execute(
        "UPDATE commander_conversations SET snapshot=? WHERE id=?",
        (encoded(conversation), conversation_id),
    )
    return conversation
