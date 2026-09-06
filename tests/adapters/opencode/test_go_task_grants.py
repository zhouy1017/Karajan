"""Task grants share the durable send protocol without claiming qualification."""

import copy
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from karajan.adapters.opencode.go_journal import GoCallJournal, GoJournalError


def task_binding() -> dict[str, Any]:
    return {
        "subject": {
            "kind": "task_attempt",
            "project_id": "project-1",
            "run_id": "run-1",
            "task_id": "feature/修复",
        },
        "attempt_id": "attempt-1",
        "fence": 1,
        "approval_digest": "c" * 64,
        "execution_policy_digest": "d" * 64,
        "workspace_digest": "e" * 64,
        "authentication_source_digest": "f" * 64,
        "profile_digest": "a" * 64,
        "runtime_digest": "b" * 64,
        "channel": "opencode-go-account-1",
        "model": "glm-5.3-flash",
        "auth_generation": "generation-1",
        "expires_at": 2000.0,
        "max_requests": 6,
    }


def test_task_send_and_completion_survive_reopen_without_new_send_authority(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite3"
    journal = GoCallJournal(path, clock=lambda: 1000.0)
    binding = task_binding()
    grant = journal.create_grant(binding, grant_id="grant-task-1")
    assert grant["binding"] == binding
    assert "qualification_id" not in grant["binding"]
    intent = journal.begin_call(
        "grant-task-1", "call-1", binding=binding, capability=grant["capability"]
    )
    assert intent["send_allowed"] is True
    assert intent["receipt"]["state"] == "send_unknown"

    reopened = GoCallJournal(path, clock=lambda: 1001.0)
    assert reopened.snapshot("grant-task-1")["binding"] == binding
    assert reopened.create_grant(binding, grant_id="grant-task-1")["capability"] is None
    assert reopened.begin_call(
        "grant-task-1", "call-1", binding=binding, capability=grant["capability"]
    ) == {"send_allowed": False, "receipt": intent["receipt"]}
    outcome = {
        "state": "response_received",
        "upstream_status": 200,
        "response_bytes": 71,
        "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
        "protocol_passed": True,
        "reason_codes": [],
    }
    completion = reopened.complete_call(
        "grant-task-1", "call-1", binding=binding, capability=grant["capability"], outcome=outcome
    )
    again = GoCallJournal(path, clock=lambda: 1002.0)
    assert again.call_receipt("grant-task-1", "call-1") == completion
    assert again.snapshot("grant-task-1")["request_count"] == 1
    assert (
        again.complete_call(
            "grant-task-1",
            "call-1",
            binding=binding,
            capability=grant["capability"],
            outcome=outcome,
        )
        == completion
    )


@pytest.mark.parametrize(
    "field",
    [
        "project_id",
        "run_id",
        "task_id",
        "attempt_id",
        "fence",
        "approval_digest",
        "execution_policy_digest",
        "workspace_digest",
        "authentication_source_digest",
        "profile_digest",
        "runtime_digest",
        "channel",
        "auth_generation",
        "expires_at",
        "max_requests",
    ],
)
def test_task_binding_changes_cannot_recreate_replay_or_complete_an_existing_grant(
    tmp_path: Path, field: str
) -> None:
    journal = GoCallJournal(tmp_path / "journal.sqlite3", clock=lambda: 1000.0)
    binding = task_binding()
    grant = journal.create_grant(binding, grant_id="task-grant")
    journal.begin_call("task-grant", "sent-call", binding=binding, capability=grant["capability"])
    before = journal.snapshot("task-grant")
    changed = copy.deepcopy(binding)
    if field in {"project_id", "run_id", "task_id"}:
        changed["subject"][field] = "different-identity"
    else:
        changed[field] = (
            "0" * 64
            if field.endswith("digest")
            else 2
            if field == "fence"
            else 1900.0
            if field == "expires_at"
            else 5
            if field == "max_requests"
            else "different-identity"
        )
    with pytest.raises(GoJournalError, match="^GRANT_CONFLICT$"):
        journal.create_grant(changed, grant_id="task-grant")
    with pytest.raises(GoJournalError, match="^GRANT_BINDING_MISMATCH$"):
        journal.begin_call(
            "task-grant", "sent-call", binding=changed, capability=grant["capability"]
        )
    with pytest.raises(GoJournalError, match="^GRANT_BINDING_MISMATCH$"):
        journal.complete_call(
            "task-grant",
            "sent-call",
            binding=changed,
            capability=grant["capability"],
            outcome={"state": "send_unknown", "reason_codes": ["RELAY_TRANSPORT_ERROR"]},
        )
    assert journal.snapshot("task-grant") == before


@pytest.mark.parametrize(
    "change",
    [
        "missing_subject",
        "unknown_subject",
        "mixed_subject",
        "subject_extra",
        "invalid_task_id",
        "missing_approval_digest",
        "missing_execution_policy_digest",
        "missing_workspace_digest",
        "missing_authentication_source_digest",
        "invalid_digest",
        "another_model",
        "too_many_requests",
        "boolean_fence",
        "raw_secret",
    ],
)
def test_ambiguous_or_incomplete_task_authority_is_rejected_without_creating_a_grant(
    tmp_path: Path, change: str
) -> None:
    journal = GoCallJournal(tmp_path / "journal.sqlite3", clock=lambda: 1000.0)
    binding = task_binding()
    if change.startswith("missing_"):
        binding.pop(change.removeprefix("missing_"))
    elif change == "unknown_subject":
        binding["subject"]["kind"] = "qualification"
    elif change == "mixed_subject":
        binding["qualification_id"] = "pretended-qualification"
    elif change == "subject_extra":
        binding["subject"]["approval"] = "caller-controlled"
    elif change == "invalid_task_id":
        binding["subject"]["task_id"] = "invalid task"
    elif change == "invalid_digest":
        binding["workspace_digest"] = "owner-supplied-name"
    elif change == "another_model":
        binding["model"] = "another-model"
    elif change == "too_many_requests":
        binding["max_requests"] = 7
    elif change == "boolean_fence":
        binding["fence"] = True
    else:
        binding["secret"] = "synthetic-unaccepted-key"
    with pytest.raises(GoJournalError, match="^GO_JOURNAL_INPUT_INVALID$"):
        journal.create_grant(binding, grant_id="invalid-grant")
    with pytest.raises(GoJournalError, match="^GRANT_NOT_FOUND$"):
        journal.snapshot("invalid-grant")


@pytest.mark.parametrize("restriction", ["revocation", "expiry"])
def test_task_restriction_preserves_unknown_send_and_cannot_refund_count(
    tmp_path: Path, restriction: str
) -> None:
    path = tmp_path / "journal.sqlite3"
    journal = GoCallJournal(path, clock=lambda: 1000.0)
    binding = task_binding()
    grant = journal.create_grant(binding, grant_id="task-grant")
    intent = journal.begin_call(
        "task-grant", "first-call", binding=binding, capability=grant["capability"]
    )
    if restriction == "revocation":
        journal.revoke_grant("task-grant")
    reopened = GoCallJournal(path, clock=lambda: 2000.0 if restriction == "expiry" else 1001.0)
    expected = "GRANT_EXPIRED" if restriction == "expiry" else "GRANT_REVOKED"
    with pytest.raises(GoJournalError, match=f"^{expected}$"):
        reopened.begin_call(
            "task-grant", "new-call", binding=binding, capability=grant["capability"]
        )
    assert reopened.begin_call(
        "task-grant", "first-call", binding=binding, capability=grant["capability"]
    ) == {"send_allowed": False, "receipt": intent["receipt"]}
    assert reopened.snapshot("task-grant")["request_count"] == 1
    if restriction == "expiry":
        rolled_back = GoCallJournal(path, clock=lambda: 1002.0)
        with pytest.raises(GoJournalError, match="^GRANT_EXPIRED$"):
            rolled_back.begin_call(
                "task-grant", "new-call", binding=binding, capability=grant["capability"]
            )


@pytest.mark.parametrize("shared_call", [False, True])
def test_concurrent_task_grant_calls_keep_the_same_six_slot_limit(
    tmp_path: Path, shared_call: bool
) -> None:
    path = tmp_path / "journal.sqlite3"
    journal = GoCallJournal(path, clock=lambda: 1000.0)
    binding = task_binding()
    grant = journal.create_grant(binding, grant_id="task-grant")
    barrier = threading.Barrier(12)

    def begin(index: int) -> bool | str:
        store = GoCallJournal(path, clock=lambda: 1001.0)
        barrier.wait(timeout=10)
        try:
            result = store.begin_call(
                "task-grant",
                "same-call" if shared_call else f"call-{index}",
                binding=binding,
                capability=grant["capability"],
            )
            return bool(result["send_allowed"])
        except GoJournalError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=12) as workers:
        results = list(workers.map(begin, range(12)))
    assert results.count(True) == (1 if shared_call else 6)
    assert results.count(False) == (11 if shared_call else 0)
    assert results.count("REQUEST_LIMIT_REACHED") == (0 if shared_call else 6)
    reopened = GoCallJournal(path, clock=lambda: 1002.0)
    assert reopened.snapshot("task-grant")["request_count"] == (1 if shared_call else 6)


def test_preexisting_qualification_json_and_capability_remain_usable_without_migration(
    tmp_path: Path,
) -> None:
    # Frozen v1 storage fixture. All behavior checks below use the public API;
    # the setup represents a database written before Task bindings existed.
    binding = {
        "qualification_id": "qualification-1",
        "attempt_id": "attempt-1",
        "fence": 1,
        "profile_digest": "a" * 64,
        "runtime_digest": "b" * 64,
        "channel": "opencode-go-account-1",
        "model": "glm-5.3-flash",
        "auth_generation": "generation-1",
        "expires_at": 2000.0,
        "max_requests": 6,
    }
    path = tmp_path / "prior-version.sqlite3"
    with sqlite3.connect(path) as database:
        database.execute(
            "CREATE TABLE go_grants (id TEXT PRIMARY KEY, binding TEXT NOT NULL, "
            "capability_digest TEXT NOT NULL, created_at REAL NOT NULL, revoked_at REAL)"
        )
        database.execute(
            "INSERT INTO go_grants VALUES (?, ?, ?, ?, NULL)",
            (
                "legacy-grant",
                json.dumps(binding, sort_keys=True, separators=(",", ":")),
                "b59504219b78de9b9b62c7998c0d818f5e45201c75d5d1bbf8924e575b7f3708",
                999.0,
            ),
        )
    journal = GoCallJournal(path, clock=lambda: 1000.0)
    before = journal.snapshot("legacy-grant")
    assert before["binding"] == binding
    replay = journal.create_grant(binding, grant_id="legacy-grant")
    assert replay == {"grant_id": "legacy-grant", "binding": binding, "capability": None}
    assert list(replay["binding"]) == list(binding)
    sent = journal.begin_call("legacy-grant", "legacy-call", binding=binding, capability="L" * 43)
    assert sent["send_allowed"] is True
    reopened = GoCallJournal(path, clock=lambda: 1001.0)
    assert reopened.begin_call(
        "legacy-grant", "legacy-call", binding=binding, capability="L" * 43
    ) == {"send_allowed": False, "receipt": sent["receipt"]}
    assert reopened.snapshot("legacy-grant")["binding"] == before["binding"]
    assert "subject" not in reopened.snapshot("legacy-grant")["binding"]


@pytest.mark.parametrize("create_task", [False, True])
def test_qualification_and_task_subjects_cannot_share_send_or_completion_authority(
    tmp_path: Path, create_task: bool
) -> None:
    task = task_binding()
    legacy = {
        "qualification_id": "qualification-1",
        **{
            key: value
            for key, value in task.items()
            if key
            not in {
                "subject",
                "approval_digest",
                "execution_policy_digest",
                "workspace_digest",
                "authentication_source_digest",
            }
        },
    }
    actual, other = (task, legacy) if create_task else (legacy, task)
    journal = GoCallJournal(tmp_path / "journal.sqlite3", clock=lambda: 1000.0)
    grant = journal.create_grant(actual, grant_id="one-grant")
    journal.begin_call("one-grant", "sent-call", binding=actual, capability=grant["capability"])
    before = journal.snapshot("one-grant")
    with pytest.raises(GoJournalError, match="^GRANT_CONFLICT$"):
        journal.create_grant(other, grant_id="one-grant")
    with pytest.raises(GoJournalError, match="^GRANT_BINDING_MISMATCH$"):
        journal.begin_call("one-grant", "new-call", binding=other, capability=grant["capability"])
    with pytest.raises(GoJournalError, match="^GRANT_BINDING_MISMATCH$"):
        journal.complete_call(
            "one-grant",
            "sent-call",
            binding=other,
            capability=grant["capability"],
            outcome={"state": "send_unknown"},
        )
    assert journal.snapshot("one-grant") == before
