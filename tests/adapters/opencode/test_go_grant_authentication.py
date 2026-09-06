"""Authentication observes durable identity without allocating another send."""

import sqlite3

import pytest
from karajan.adapters.opencode.go_journal import GoCallJournal, GoJournalError
from test_go_journal import binding


@pytest.mark.parametrize("state", ["active", "expired", "revoked", "unknown"])
def test_authentication_is_detached_read_only_history(tmp_path, state):
    now = [1000.0]
    journal = GoCallJournal(tmp_path / "calls #%.sqlite", clock=lambda: now[0])
    grant = journal.create_grant(binding(), grant_id="own")
    if state == "expired":
        now[0] = 2000.0
    elif state == "revoked":
        journal.revoke_grant("own")
    elif state == "unknown":
        journal.begin_call("own", "lost", capability=grant["capability"], binding=binding())
    before, raw = journal.snapshot("own"), journal.path.read_bytes()
    observed = journal.authenticate_grant("own", capability=grant["capability"], binding=binding())
    assert observed == before
    assert "send_allowed" not in observed
    assert grant["capability"] not in str(observed)
    observed["binding"]["fence"] = 50
    assert journal.snapshot("own") == before
    assert journal.path.read_bytes() == raw
    if state == "unknown":
        assert before["calls"][0]["state"] == "send_unknown"


@pytest.mark.parametrize(
    "change,code",
    [
        ("cap", "INVALID_CAPABILITY"),
        ("binding", "GRANT_BINDING_MISMATCH"),
        ("missing", "GRANT_NOT_FOUND"),
    ],
)
def test_failed_authentication_has_no_effect(tmp_path, change, code):
    journal = GoCallJournal(tmp_path / "calls.sqlite", clock=lambda: 1000.0)
    grant = journal.create_grant(binding(), grant_id="own")
    raw = journal.path.read_bytes()
    with pytest.raises(GoJournalError, match="^" + code + "$"):
        journal.authenticate_grant(
            "missing" if change == "missing" else "own",
            capability="x" * 43 if change == "cap" else grant["capability"],
            binding=binding(fence=2) if change == "binding" else binding(),
        )
    assert journal.snapshot("own")["request_count"] == 0
    assert journal.path.read_bytes() == raw


def test_missing_ledger_is_not_created_by_authentication(tmp_path):
    journal = GoCallJournal(tmp_path / "calls.sqlite", clock=lambda: 1000.0)
    grant = journal.create_grant(binding(), grant_id="own")
    journal.path.rename(tmp_path / "saved.sqlite")
    with pytest.raises(sqlite3.OperationalError):
        journal.authenticate_grant("own", capability=grant["capability"], binding=binding())
    assert not journal.path.exists()
