"""Independent journal boundary review against real SQLite and local processes."""

import json
import math
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from karajan.adapters.opencode.go_journal import GoCallJournal, GoJournalError

WORKTREE = Path(__file__).resolve().parents[2]
CHILD = Path(__file__).with_name("child.py")


def specification():
    return {
        "qualification_id": "independent-qualified-case",
        "attempt_id": "independent-attempt",
        "fence": 2,
        "profile_digest": "1" * 64,
        "runtime_digest": "2" * 64,
        "channel": "go-scoped-channel",
        "model": "glm-5.3-flash",
        "auth_generation": "opaque-generation",
        "expires_at": 110.0,
        "max_requests": 6,
    }


def setup(tmp_path):
    path = tmp_path / "independent.sqlite"
    journal = GoCallJournal(path, clock=lambda: 100.0)
    spec = specification()
    capability = journal.create_grant(spec, grant_id="grant")["capability"]
    return journal, path, spec, capability


def child_env():
    return {**os.environ, "PYTHONPATH": str(WORKTREE / "backend")}


def test_process_dies_after_committed_intent_without_returning_permission(tmp_path):
    journal, path, spec, capability = setup(tmp_path)
    child = subprocess.run(
        [sys.executable, str(CHILD)],
        input=json.dumps(
            {
                "path": str(path),
                "binding": spec,
                "capability": capability,
                "calls": ["lost-return"],
                "crash_after_commit": True,
            }
        ),
        text=True,
        capture_output=True,
        timeout=15,
        cwd=WORKTREE,
        env=child_env(),
    )
    assert child.returncode == 23
    assert child.stdout == ""
    reopened = GoCallJournal(path, clock=lambda: 101.0)
    replay = reopened.begin_call("grant", "lost-return", binding=spec, capability=capability)
    assert replay["send_allowed"] is False
    assert replay["receipt"]["state"] == "send_unknown"
    assert journal.snapshot("grant")["request_count"] == 1


def test_four_processes_share_one_durable_limit_and_logical_call_identity(tmp_path):
    journal, path, spec, capability = setup(tmp_path)
    gate = tmp_path / "start"
    processes = []
    try:
        for index in range(4):
            process = subprocess.Popen(
                [sys.executable, str(CHILD)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=WORKTREE,
                env=child_env(),
            )
            process.stdin.write(
                json.dumps(
                    {
                        "path": str(path),
                        "binding": spec,
                        "capability": capability,
                        "gate": str(gate),
                        "calls": ["shared", *[f"p{index}-{j}" for j in range(6)]],
                    }
                )
            )
            process.stdin.close()
            process.stdin = None
            processes.append(process)
        gate.write_text("start", encoding="utf-8")
        results = []
        for process in processes:
            output, error = process.communicate(timeout=15)
            assert process.returncode == 0, error
            results.extend(json.loads(output))
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
    assert sum(row.get("send_allowed", False) for row in results) == 6
    assert sum(row.get("send_allowed", False) for row in results if row["call"] == "shared") == 1
    assert all("send_allowed" in row for row in results if row["call"] == "shared")
    snapshot = journal.snapshot("grant")
    assert snapshot["request_count"] == 6
    assert [call["sequence"] for call in snapshot["calls"]] == list(range(1, 7))


def test_revoked_unknown_can_finish_but_neither_replay_nor_completion_refunds_a_send(tmp_path):
    journal, path, spec, capability = setup(tmp_path)
    journal.begin_call("grant", "sent", binding=spec, capability=capability)
    journal.revoke_grant("grant")
    reopened = GoCallJournal(path, clock=lambda: 111.0)
    receipt = reopened.complete_call(
        "grant",
        "sent",
        binding=spec,
        capability=capability,
        outcome={"state": "send_unknown", "reason_codes": ["RELAY_TRANSPORT_ERROR"]},
    )
    assert receipt["state"] == "send_unknown"
    assert reopened.begin_call("grant", "sent", binding=spec, capability=capability)[
        "send_allowed"
    ] is False
    with pytest.raises(GoJournalError, match="^GRANT_REVOKED$"):
        reopened.begin_call("grant", "new", binding=spec, capability=capability)
    assert reopened.snapshot("grant")["request_count"] == 1
    assert reopened.create_grant(spec, grant_id="grant")["capability"] is None


def test_wrong_binding_on_historical_receipt_does_not_bypass_authentication(tmp_path):
    journal, path, spec, capability = setup(tmp_path)
    journal.begin_call("grant", "old", binding=spec, capability=capability)
    reopened = GoCallJournal(path, clock=lambda: 101.0)
    before = reopened.snapshot("grant")
    with pytest.raises(GoJournalError, match="^GRANT_BINDING_MISMATCH$"):
        reopened.begin_call(
            "grant", "old", binding={**spec, "auth_generation": "rotated"}, capability=capability
        )
    assert reopened.snapshot("grant") == before


def test_rejected_raw_fields_and_capability_never_reach_database_or_snapshot(tmp_path):
    journal, path, spec, capability = setup(tmp_path)
    journal.begin_call("grant", "sent", binding=spec, capability=capability)
    synthetic = "fixture-raw-secret-for-independent-scan"
    with pytest.raises(GoJournalError) as failure:
        journal.complete_call(
            "grant",
            "sent",
            binding=spec,
            capability=capability,
            outcome={"state": "send_unknown", "headers": {"Authorization": synthetic}},
        )
    assert str(failure.value) == "GO_JOURNAL_INPUT_INVALID"
    for secret in (synthetic, capability):
        assert secret not in json.dumps(journal.snapshot("grant"))
        assert all(secret.encode() not in item.read_bytes() for item in tmp_path.iterdir())
    assert journal.call_receipt("grant", "sent")["outcome"] is None


def test_historical_reads_do_not_recreate_a_missing_database(tmp_path):
    journal, path, _, _ = setup(tmp_path)
    archived = tmp_path / "archived.sqlite"
    path.rename(archived)
    for read in (lambda: journal.snapshot("grant"), lambda: journal.call_receipt("grant", "x")):
        with pytest.raises(sqlite3.OperationalError):
            read()
        assert not path.exists()
    assert archived.exists()


@pytest.mark.parametrize("bad_time", [math.inf, math.nan, True, -1.0])
def test_invalid_clock_never_persists_new_intent(tmp_path, bad_time):
    journal, path, spec, capability = setup(tmp_path)
    bad = GoCallJournal(path, clock=lambda: bad_time)
    with pytest.raises(GoJournalError, match="^GO_JOURNAL_CLOCK_INVALID$"):
        bad.begin_call("grant", "new", binding=spec, capability=capability)
    assert journal.snapshot("grant")["request_count"] == 0


def test_expiry_rejection_cannot_be_undone_by_wall_clock_rollback_after_reopen(tmp_path):
    _, path, spec, capability = setup(tmp_path)
    expired = GoCallJournal(path, clock=lambda: 110.0)
    with pytest.raises(GoJournalError, match="^GRANT_EXPIRED$"):
        expired.begin_call("grant", "first", binding=spec, capability=capability)
    reopened = GoCallJournal(path, clock=lambda: 105.0)
    with pytest.raises(GoJournalError):
        reopened.begin_call("grant", "second", binding=spec, capability=capability)
    assert reopened.call_receipt("grant", "second") is None
