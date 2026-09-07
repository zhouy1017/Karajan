"""Planning grant bindings use an explicit schema and durable send journal."""

import copy
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from karajan.adapters.opencode.go_journal import GoCallJournal, GoJournalError


def planning_binding(**changes: Any) -> dict[str, Any]:
    return {
        "schema_version": "karajan.go-planning-grant.v1",
        "subject": {
            "kind": "planning_execution",
            "project_id": "project-1",
            "run_id": "run-1",
            "intent_id": "intent-1",
            "execution_id": "execution-1",
        },
        "attempt_id": "attempt-1",
        "fence": 1,
        "profile_digest": "a" * 64,
        "runtime_digest": "b" * 64,
        "channel": "opencode-go-account-1",
        "model": "glm-5.3-flash",
        "auth_generation": "generation-1",
        "expires_at": 2000.0,
        "max_requests": 6,
        "planning_binding_sha256": "c" * 64,
        "admission_sha256": "d" * 64,
        "input_sha256": "e" * 64,
        "authentication_source_digest": "f" * 64,
        **changes,
    }


def test_planning_send_is_recovered_exactly_after_sqlite_reopen(tmp_path: Path) -> None:
    path = tmp_path / "planning-journal.sqlite3"
    binding = planning_binding()
    journal = GoCallJournal(path, clock=lambda: 1000.0)
    grant = journal.create_grant(binding, grant_id="planning-grant")

    first = journal.begin_call(
        "planning-grant", "call-1", capability=grant["capability"], binding=binding
    )
    reopened = GoCallJournal(path, clock=lambda: 1001.0)
    assert reopened.snapshot("planning-grant")["binding"] == binding
    assert reopened.begin_call(
        "planning-grant", "call-1", capability=grant["capability"], binding=binding
    ) == {"send_allowed": False, "receipt": first["receipt"]}

    outcome = {
        "state": "response_received",
        "upstream_status": 200,
        "response_bytes": 17,
        "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
        "protocol_passed": True,
        "reason_codes": [],
    }
    receipt = reopened.complete_call(
        "planning-grant",
        "call-1",
        capability=grant["capability"],
        binding=binding,
        outcome=outcome,
    )
    assert GoCallJournal(path, clock=lambda: 1002.0).call_receipt(
        "planning-grant", "call-1"
    ) == receipt


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": "karajan.go-planning-grant.v2"},
        {"schema_version": "karajan.go-qualification-grant.v2"},
        {"schema_version": "karajan.go-planning-grant.v1", "qualification_id": "legacy"},
        {"schema_version": "karajan.go-planning-grant.v1", "subject": {"kind": "task_attempt"}},
        {"schema_version": "karajan.go-planning-grant.v1", "planning_binding_sha256": "C" * 64},
        {"schema_version": "karajan.go-planning-grant.v1", "input_sha256": "not-a-sha256"},
        {"schema_version": "karajan.go-planning-grant.v1", "max_requests": 7},
    ],
)
def test_planning_schema_subject_and_fields_are_strictly_dispatched(
    tmp_path: Path, change: dict[str, Any]
) -> None:
    journal = GoCallJournal(tmp_path / "planning-journal.sqlite3", clock=lambda: 1000.0)
    altered = planning_binding()
    altered.update(copy.deepcopy(change))
    if "subject" in change:
        altered["subject"] = change["subject"]
    with pytest.raises(GoJournalError, match="^GO_JOURNAL_INPUT_INVALID$"):
        journal.create_grant(altered, grant_id="invalid-planning-grant")
    with pytest.raises(GoJournalError, match="^GRANT_NOT_FOUND$"):
        journal.snapshot("invalid-planning-grant")


@pytest.mark.parametrize(
    "field",
    [
        "project_id",
        "run_id",
        "intent_id",
        "execution_id",
        "planning_binding_sha256",
        "admission_sha256",
        "input_sha256",
        "authentication_source_digest",
    ],
)
def test_planning_identity_cannot_be_rebound_after_creation(tmp_path: Path, field: str) -> None:
    path = tmp_path / "planning-journal.sqlite3"
    binding = planning_binding()
    journal = GoCallJournal(path, clock=lambda: 1000.0)
    grant = journal.create_grant(binding, grant_id="planning-grant")
    altered = copy.deepcopy(binding)
    if field in {"project_id", "run_id", "intent_id", "execution_id"}:
        altered["subject"][field] = "different-identity"
    else:
        altered[field] = "0" * 64
    with pytest.raises(GoJournalError, match="^GRANT_CONFLICT$"):
        journal.create_grant(altered, grant_id="planning-grant")
    with pytest.raises(GoJournalError, match="^GRANT_BINDING_MISMATCH$"):
        journal.begin_call(
            "planning-grant", "call-1", capability=grant["capability"], binding=altered
        )


def test_planning_concurrent_calls_use_one_bounded_send_slot_per_logical_call(
    tmp_path: Path,
) -> None:
    path = tmp_path / "planning-journal.sqlite3"
    binding = planning_binding()
    journal = GoCallJournal(path, clock=lambda: 1000.0)
    grant = journal.create_grant(binding, grant_id="planning-grant")
    barrier = threading.Barrier(8)

    def begin(index: int) -> bool:
        store = GoCallJournal(path, clock=lambda: 1001.0)
        barrier.wait(timeout=10)
        result = store.begin_call(
            "planning-grant",
            "shared-call",
            capability=grant["capability"],
            binding=binding,
        )
        return bool(result["send_allowed"])

    with ThreadPoolExecutor(max_workers=8) as workers:
        results = list(workers.map(begin, range(8)))
    assert results.count(True) == 1
    assert results.count(False) == 7
    assert GoCallJournal(path).snapshot("planning-grant")["request_count"] == 1
