import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from karajan.adapters.opencode.go_journal import GoCallJournal, GoJournalError


def binding(**changes: Any) -> dict[str, Any]:
    return {
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
        **changes,
    }


def test_committed_send_intent_is_history_after_lost_return_and_database_reopen(
    tmp_path: Path,
) -> None:
    path = tmp_path / "journal.sqlite3"
    journal = GoCallJournal(path, clock=lambda: 1000.0)
    grant = journal.create_grant(binding(), grant_id="grant-1")
    first = journal.begin_call(
        "grant-1", "call-1", capability=grant["capability"], binding=binding()
    )
    assert first["send_allowed"] is True
    assert first["receipt"]["state"] == "send_unknown"

    # Losing this return cannot authorize a retry, even in a new process/store.
    reopened = GoCallJournal(path, clock=lambda: 1001.0)
    replay = reopened.begin_call(
        "grant-1", "call-1", capability=grant["capability"], binding=binding()
    )
    assert replay == {"send_allowed": False, "receipt": first["receipt"]}
    assert reopened.call_receipt("grant-1", "call-1") == first["receipt"]
    assert reopened.snapshot("grant-1")["request_count"] == 1


def test_response_facts_are_durable_immutable_and_never_reissue_send_permission(
    tmp_path: Path,
) -> None:
    path = tmp_path / "journal.sqlite3"
    journal = GoCallJournal(path, clock=lambda: 1000.0)
    grant = journal.create_grant(binding(), grant_id="grant-1")
    journal.begin_call("grant-1", "call-1", capability=grant["capability"], binding=binding())
    outcome = {
        "state": "response_received",
        "upstream_status": 200,
        "response_bytes": 100,
        "usage": {"prompt_tokens": 21, "completion_tokens": 9, "total_tokens": 30},
        "protocol_passed": True,
        "reason_codes": [],
    }
    receipt = journal.complete_call(
        "grant-1", "call-1", capability=grant["capability"], binding=binding(), outcome=outcome
    )
    assert receipt["state"] == "response_received"
    assert receipt["outcome"]["usage"] == {
        "prompt_tokens": 21,
        "completion_tokens": 9,
        "total_tokens": 30,
    }
    reopened = GoCallJournal(path, clock=lambda: 1001.0)
    assert reopened.call_receipt("grant-1", "call-1") == receipt
    assert (
        reopened.complete_call(
            "grant-1", "call-1", capability=grant["capability"], binding=binding(), outcome=outcome
        )
        == receipt
    )
    assert reopened.begin_call(
        "grant-1", "call-1", capability=grant["capability"], binding=binding()
    ) == {"send_allowed": False, "receipt": receipt}
    with pytest.raises(GoJournalError, match="^CALL_COMPLETION_CONFLICT$"):
        reopened.complete_call(
            "grant-1",
            "call-1",
            capability=grant["capability"],
            binding=binding(),
            outcome={**outcome, "response_bytes": 101},
        )


@pytest.mark.parametrize("restriction", ["revoke", "expire"])
def test_revocation_or_expiry_blocks_new_calls_but_preserves_unknown_send_and_completion(
    tmp_path: Path, restriction: str
) -> None:
    path = tmp_path / "journal.sqlite3"
    journal = GoCallJournal(path, clock=lambda: 1000.0)
    grant = journal.create_grant(binding(), grant_id="grant-1")
    sent = journal.begin_call(
        "grant-1", "call-1", capability=grant["capability"], binding=binding()
    )
    if restriction == "revoke":
        revoked = journal.revoke_grant("grant-1")
        assert revoked == {"grant_id": "grant-1", "revoked_at": 1000.0}
        assert journal.revoke_grant("grant-1") == revoked
    reopened = GoCallJournal(path, clock=lambda: 2000.0 if restriction == "expire" else 1001.0)
    expected = "GRANT_EXPIRED" if restriction == "expire" else "GRANT_REVOKED"
    assert reopened.snapshot("grant-1")["state"] == (
        "expired" if restriction == "expire" else "revoked"
    )
    with pytest.raises(GoJournalError, match=f"^{expected}$"):
        reopened.begin_call("grant-1", "call-2", capability=grant["capability"], binding=binding())
    assert reopened.begin_call(
        "grant-1", "call-1", capability=grant["capability"], binding=binding()
    ) == {"send_allowed": False, "receipt": sent["receipt"]}
    assert reopened.snapshot("grant-1")["request_count"] == 1
    receipt = reopened.complete_call(
        "grant-1",
        "call-1",
        capability=grant["capability"],
        binding=binding(),
        outcome={"state": "send_unknown", "reason_codes": ["RELAY_TRANSPORT_ERROR"]},
    )
    assert receipt["state"] == "send_unknown"
    assert receipt["outcome"]["usage"] == {}
    assert receipt["outcome"]["protocol_passed"] is False
    assert reopened.snapshot("grant-1")["request_count"] == 1


@pytest.mark.parametrize(
    "outcome",
    [
        {"state": "send_unknown", "protocol_passed": True},
        {
            "state": "rejected",
            "protocol_passed": True,
            "upstream_status": 200,
            "response_bytes": 100,
        },
        {"state": "response_received"},
        {
            "state": "response_received",
            "protocol_passed": True,
            "upstream_status": 500,
            "response_bytes": 100,
        },
        {"state": "response_received", "protocol_passed": True, "upstream_status": 200},
        {
            "state": "response_received",
            "protocol_passed": True,
            "upstream_status": 200,
            "response_bytes": 100,
            "reason_codes": ["INCOMPLETE_SSE"],
        },
    ],
)
def test_completion_cannot_turn_missing_or_contradictory_response_into_protocol_success(
    tmp_path: Path, outcome: dict[str, Any]
) -> None:
    journal = GoCallJournal(tmp_path / "journal.sqlite3", clock=lambda: 1000.0)
    grant = journal.create_grant(binding(), grant_id="grant-1")
    journal.begin_call("grant-1", "call-1", capability=grant["capability"], binding=binding())
    before = journal.snapshot("grant-1")
    with pytest.raises(GoJournalError, match="^GO_JOURNAL_INPUT_INVALID$"):
        journal.complete_call(
            "grant-1", "call-1", capability=grant["capability"], binding=binding(), outcome=outcome
        )
    assert journal.snapshot("grant-1") == before


@pytest.mark.parametrize("shared_call", [False, True])
def test_concurrent_connections_commit_only_six_new_calls_or_one_shared_logical_call(
    tmp_path: Path, shared_call: bool
) -> None:
    path = tmp_path / "journal.sqlite3"
    journal = GoCallJournal(path, clock=lambda: 1000.0)
    grant = journal.create_grant(binding(), grant_id="grant-1")
    barrier = threading.Barrier(12)

    def begin(index: int) -> dict[str, Any] | str:
        connection = GoCallJournal(path, clock=lambda: 1000.0)
        barrier.wait(timeout=10)
        try:
            return connection.begin_call(
                "grant-1",
                "shared-call" if shared_call else f"call-{index}",
                capability=grant["capability"],
                binding=binding(),
            )
        except GoJournalError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(begin, range(12)))
    count = 1 if shared_call else 6
    assert sum(isinstance(result, dict) and result["send_allowed"] for result in results) == count
    if shared_call:
        assert all(isinstance(result, dict) for result in results)
    else:
        assert results.count("REQUEST_LIMIT_REACHED") == 6
    reopened = GoCallJournal(path, clock=lambda: 1001.0)
    snapshot = reopened.snapshot("grant-1")
    assert snapshot["request_count"] == count
    assert [call["sequence"] for call in snapshot["calls"]] == list(range(1, count + 1))
    assert all(call["state"] == "send_unknown" for call in snapshot["calls"])
    assert reopened.create_grant(binding(), grant_id="grant-1")["capability"] is None
    assert reopened.snapshot("grant-1") == snapshot


@pytest.mark.parametrize(
    "change",
    [
        {"qualification_id": "qualification-2"},
        {"attempt_id": "attempt-2"},
        {"fence": 2},
        {"profile_digest": "c" * 64},
        {"runtime_digest": "c" * 64},
        {"channel": "other-go-account"},
        {"auth_generation": "generation-2"},
        {"expires_at": 3000.0},
        {"max_requests": 5},
    ],
)
def test_every_binding_field_is_checked_before_new_intent_history_and_completion(
    tmp_path: Path, change: dict[str, Any]
) -> None:
    journal = GoCallJournal(tmp_path / "journal.sqlite3", clock=lambda: 1000.0)
    grant = journal.create_grant(binding(), grant_id="grant-1")
    journal.begin_call("grant-1", "call-1", capability=grant["capability"], binding=binding())
    before = journal.snapshot("grant-1")
    for call_id in ("call-1", "call-2"):
        with pytest.raises(GoJournalError, match="^GRANT_BINDING_MISMATCH$"):
            journal.begin_call(
                "grant-1", call_id, capability=grant["capability"], binding=binding(**change)
            )
    with pytest.raises(GoJournalError, match="^GRANT_BINDING_MISMATCH$"):
        journal.complete_call(
            "grant-1",
            "call-1",
            capability=grant["capability"],
            binding=binding(**change),
            outcome={"state": "send_unknown"},
        )
    with pytest.raises(GoJournalError, match="^GRANT_CONFLICT$"):
        journal.create_grant(binding(**change), grant_id="grant-1")
    assert journal.snapshot("grant-1") == before


@pytest.mark.parametrize("wrong_capability", ["x" * 43, "", "secret\n" * 20, "汉" * 43])
def test_invalid_capability_never_returns_history_or_changes_a_call(
    tmp_path: Path, wrong_capability: str
) -> None:
    journal = GoCallJournal(tmp_path / "journal.sqlite3", clock=lambda: 1000.0)
    grant = journal.create_grant(binding(), grant_id="grant-1")
    journal.begin_call("grant-1", "call-1", capability=grant["capability"], binding=binding())
    before = journal.snapshot("grant-1")
    for call_id in ("call-1", "call-2"):
        with pytest.raises(GoJournalError, match="^INVALID_CAPABILITY$"):
            journal.begin_call("grant-1", call_id, capability=wrong_capability, binding=binding())
    with pytest.raises(GoJournalError, match="^INVALID_CAPABILITY$"):
        journal.complete_call(
            "grant-1",
            "call-1",
            capability=wrong_capability,
            binding=binding(),
            outcome={"state": "send_unknown"},
        )
    assert journal.snapshot("grant-1") == before


def test_capability_is_created_once_not_logged_and_snapshots_are_detached(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite3"
    journal = GoCallJournal(path, clock=lambda: 1000.0)
    grant = journal.create_grant(binding(), grant_id="grant-1")
    capability = grant["capability"]
    # Trusted controller repeats its fixed grant ID after losing the create return.
    reopened = GoCallJournal(path, clock=lambda: 1001.0)
    assert reopened.create_grant(binding(), grant_id="grant-1") == {
        "grant_id": "grant-1",
        "binding": binding(),
        "capability": None,
    }
    first = journal.begin_call("grant-1", "call-1", capability=capability, binding=binding())
    first["receipt"]["state"] = "invented"
    snapshot = reopened.snapshot("grant-1")
    assert snapshot["calls"][0]["state"] == "send_unknown"
    assert capability not in json.dumps(snapshot)
    assert "capability" not in json.dumps(snapshot)
    # Credential storage test scans file bytes; no SQL or private model assertions.
    assert all(capability.encode() not in file.read_bytes() for file in tmp_path.iterdir())
    snapshot["binding"]["max_requests"] = 100
    snapshot["calls"].clear()
    assert reopened.snapshot("grant-1")["request_count"] == 1
    assert reopened.snapshot("grant-1")["binding"]["max_requests"] == 6
    assert reopened.call_receipt("grant-1", "no-such-call") is None


@pytest.mark.parametrize(
    "change",
    [
        {"model": "other-model"},
        {"max_requests": 0},
        {"max_requests": 7},
        {"max_requests": True},
        {"fence": 0},
        {"profile_digest": "not-a-digest"},
        {"expires_at": float("inf")},
        {"expires_at": float("nan")},
        {"auth_generation": "generation with spaces"},
        {"headers": {"Authorization": "synthetic-secret"}},
    ],
)
def test_invalid_grant_inputs_are_rejected_without_echo_or_persistence(
    tmp_path: Path, change: dict[str, Any]
) -> None:
    journal = GoCallJournal(tmp_path / "journal.sqlite3", clock=lambda: 1000.0)
    with pytest.raises(GoJournalError, match="^GO_JOURNAL_INPUT_INVALID$"):
        journal.create_grant(binding(**change), grant_id="grant-1")
    with pytest.raises(GoJournalError, match="^GRANT_NOT_FOUND$"):
        journal.snapshot("grant-1")


@pytest.mark.parametrize(
    "change",
    [
        {"request": "synthetic-secret"},
        {"text": "synthetic-secret"},
        {"headers": {"Authorization": "synthetic-secret"}},
        {"usage": {"prompt_tokens": "3"}},
        {"usage": {"prompt_tokens": True}},
        {"usage": {"prompt_tokens": -1}},
        {"usage": {"total_tokens": 2**63}},
        {"usage": {"completion_tokens": 1.5}},
        {"usage": {"prompt_tokens_details": {"cached_tokens": -1}}},
        {"usage": {"cost": "0.0001"}},
        {"response_bytes": -1},
        {"upstream_status": 600},
        {"reason_codes": ["synthetic-secret"]},
        {"protocol_passed": "true"},
        {"state": "remote_stopped"},
    ],
)
def test_only_strict_redacted_completion_facts_are_accepted(
    tmp_path: Path, change: dict[str, Any]
) -> None:
    journal = GoCallJournal(tmp_path / "journal.sqlite3", clock=lambda: 1000.0)
    grant = journal.create_grant(binding(), grant_id="grant-1")
    journal.begin_call("grant-1", "call-1", capability=grant["capability"], binding=binding())
    before = journal.snapshot("grant-1")
    with pytest.raises(GoJournalError, match="^GO_JOURNAL_INPUT_INVALID$"):
        journal.complete_call(
            "grant-1",
            "call-1",
            capability=grant["capability"],
            binding=binding(),
            outcome={"state": "send_unknown", **change},
        )
    assert journal.snapshot("grant-1") == before
    assert all(b"synthetic-secret" not in file.read_bytes() for file in tmp_path.iterdir())


def test_observed_expiry_survives_clock_rollback_without_losing_old_call_history(
    tmp_path: Path,
) -> None:
    path = tmp_path / "journal.sqlite3"
    journal = GoCallJournal(path, clock=lambda: 1000.0)
    grant = journal.create_grant(binding(), grant_id="grant-1")
    old = journal.begin_call(
        "grant-1", "old-call", capability=grant["capability"], binding=binding()
    )
    expired = GoCallJournal(path, clock=lambda: 2000.0)
    with pytest.raises(GoJournalError, match="^GRANT_EXPIRED$"):
        expired.begin_call("grant-1", "new-call", capability=grant["capability"], binding=binding())
    reopened = GoCallJournal(path, clock=lambda: 1500.0)
    assert reopened.snapshot("grant-1")["state"] == "expired"
    with pytest.raises(GoJournalError, match="^GRANT_EXPIRED$"):
        reopened.begin_call(
            "grant-1", "another-call", capability=grant["capability"], binding=binding()
        )
    assert reopened.call_receipt("grant-1", "new-call") is None
    assert reopened.call_receipt("grant-1", "another-call") is None
    assert reopened.begin_call(
        "grant-1", "old-call", capability=grant["capability"], binding=binding()
    ) == {"send_allowed": False, "receipt": old["receipt"]}
    receipt = reopened.complete_call(
        "grant-1",
        "old-call",
        capability=grant["capability"],
        binding=binding(),
        outcome={"state": "send_unknown", "reason_codes": ["RELAY_TRANSPORT_ERROR"]},
    )
    assert receipt["state"] == "send_unknown"
    assert reopened.create_grant(binding(), grant_id="grant-1")["capability"] is None
    assert reopened.snapshot("grant-1")["state"] == "expired"
    assert reopened.snapshot("grant-1")["request_count"] == 1


def test_expiry_preview_and_call_receipt_remain_read_only(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite3"
    journal = GoCallJournal(path, clock=lambda: 1000.0)
    journal.create_grant(binding(), grant_id="grant-1")
    journal.clock = lambda: 2000.0
    before = path.read_bytes()
    assert journal.snapshot("grant-1")["state"] == "expired"
    assert journal.call_receipt("grant-1", "absent") is None
    assert path.read_bytes() == before
