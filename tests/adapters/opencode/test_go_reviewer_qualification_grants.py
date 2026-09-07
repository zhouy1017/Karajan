"""Reviewer send grants through the public SQLite journal; no provider calls."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from karajan.adapters.opencode.go_journal import GoCallJournal, GoJournalError
from test_go_context import accounting, artifacts
from test_go_qualification_grants import measurement, qualification_binding

__all__ = ["accounting", "artifacts"]


def reviewer_binding(accounting, *, scenario="clean_review"):
    return {
        **qualification_binding(accounting),
        "schema_version": "karajan.go-reviewer-qualification-grant.v1",
        "scenario": scenario,
    }


@pytest.mark.parametrize("scenario", ["clean_review", "defect_review", "denied_read"])
def test_reviewer_first_send_requires_measurement_and_reopen_cannot_send_again(
    tmp_path, accounting, scenario
):
    bound = reviewer_binding(accounting, scenario=scenario)
    journal = GoCallJournal(tmp_path / "calls.sqlite", clock=lambda: 1000.0)
    grant = journal.create_grant(bound, grant_id="reviewer")
    assert grant["binding"] == bound
    with pytest.raises(GoJournalError, match="^QUALIFICATION_CONTEXT_REQUIRED$"):
        journal.begin_call("reviewer", "first", capability=grant["capability"], binding=bound)
    assert journal.snapshot("reviewer")["request_count"] == 0
    measured = measurement(accounting, bound)
    first = journal.begin_call(
        "reviewer",
        "first",
        capability=grant["capability"],
        binding=bound,
        request_context=measured,
    )
    assert first["send_allowed"] is True
    assert first["receipt"]["state"] == "send_unknown"
    reopened = GoCallJournal(journal.path, clock=lambda: 1001.0)
    assert reopened.begin_call(
        "reviewer",
        "first",
        capability=grant["capability"],
        binding=bound,
        request_context=measured,
    ) == {"send_allowed": False, "receipt": first["receipt"]}
    assert reopened.create_grant(bound, grant_id="reviewer")["capability"] is None
    assert reopened.snapshot("reviewer")["request_count"] == 1


@pytest.mark.parametrize(
    "field",
    [
        "source_sha256",
        "approved_input_tokens",
        "reserved_output_tokens",
        "operating_context_tokens",
        "fixed_margin",
        "ratio_margin_basis_points",
    ],
)
def test_reviewer_requires_each_original_context_limit_before_consumption(
    tmp_path, accounting, field
):
    bound = reviewer_binding(accounting)
    journal = GoCallJournal(tmp_path / "calls.sqlite", clock=lambda: 1000.0)
    grant = journal.create_grant(bound, grant_id="reviewer")
    if field == "source_sha256":
        changed = {**measurement(accounting, bound), field: "e" * 64}
    else:
        changed = measurement(accounting, bound, **{field: bound["context"][field] + 1})
    with pytest.raises(GoJournalError, match="^QUALIFICATION_CONTEXT_MISMATCH$"):
        journal.begin_call(
            "reviewer",
            "wrong",
            capability=grant["capability"],
            binding=bound,
            request_context=changed,
        )
    assert journal.snapshot("reviewer")["request_count"] == 0
    assert journal.call_receipt("reviewer", "wrong") is None


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": "karajan.go-reviewer-qualification-grant.v2"},
        {"scenario": "edit"},
        {"tools": ["read", "edit"]},
        {"subject": {"kind": "task_attempt", "project_id": "p", "run_id": "r", "task_id": "t"}},
        {"context": None},
        {"probe_spec_digest": "RAW_PRIVATE_VALUE"},
    ],
)
def test_unknown_mixed_or_unbound_reviewer_grants_do_not_create_an_identity(
    tmp_path, accounting, change
):
    journal = GoCallJournal(tmp_path / "calls.sqlite", clock=lambda: 1000.0)
    with pytest.raises(GoJournalError, match="^GO_JOURNAL_INPUT_INVALID$"):
        journal.create_grant({**reviewer_binding(accounting), **change}, grant_id="reviewer")
    with pytest.raises(GoJournalError, match="^GRANT_NOT_FOUND$"):
        journal.snapshot("reviewer")


@pytest.mark.parametrize(
    "change",
    [
        {"scenario": "defect_review"},
        {"probe_spec_digest": "e" * 64},
        {"schema_version": "karajan.go-qualification-grant.v2", "scenario": "denied_read"},
    ],
)
def test_existing_reviewer_identity_cannot_be_rebound_or_recast_as_worker(
    tmp_path, accounting, change
):
    journal = GoCallJournal(tmp_path / "calls.sqlite", clock=lambda: 1000.0)
    bound = reviewer_binding(accounting, scenario="denied_read")
    grant = journal.create_grant(bound, grant_id="reviewer")
    with pytest.raises(GoJournalError, match="^GRANT_CONFLICT$"):
        journal.create_grant({**bound, **change}, grant_id="reviewer")
    with pytest.raises(GoJournalError, match="^GRANT_BINDING_MISMATCH$"):
        journal.begin_call(
            "reviewer",
            "wrong",
            capability=grant["capability"],
            binding={**bound, **change},
            request_context=measurement(accounting, bound),
        )
    assert journal.snapshot("reviewer")["request_count"] == 0


def test_reviewer_concurrent_calls_share_six_unknown_slots_after_reopen(tmp_path, accounting):
    journal = GoCallJournal(tmp_path / "calls.sqlite", clock=lambda: 1000.0)
    bound = reviewer_binding(accounting)
    grant = journal.create_grant(bound, grant_id="reviewer")
    measured = measurement(accounting, bound)

    def begin(index):
        opened = GoCallJournal(journal.path, clock=lambda: 1001.0, existing_only=True)
        try:
            return opened.begin_call(
                "reviewer",
                f"call-{index}",
                capability=grant["capability"],
                binding=bound,
                request_context=measured,
            )["send_allowed"]
        except GoJournalError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(begin, range(8)))
    assert results.count(True) == 6
    assert results.count("REQUEST_LIMIT_REACHED") == 2
    calls = journal.snapshot("reviewer")["calls"]
    assert [call["sequence"] for call in calls] == [1, 2, 3, 4, 5, 6]
    assert all(call["state"] == "send_unknown" for call in calls)


@pytest.mark.parametrize("restriction", ["revoke", "expiry"])
def test_reviewer_restriction_keeps_lost_send_and_cannot_be_reversed_by_reopen(
    tmp_path, accounting, restriction
):
    bound = reviewer_binding(accounting)
    journal = GoCallJournal(tmp_path / "calls.sqlite", clock=lambda: 1000.0)
    grant = journal.create_grant(bound, grant_id="reviewer")
    measured = measurement(accounting, bound)
    journal.begin_call(
        "reviewer",
        "lost",
        capability=grant["capability"],
        binding=bound,
        request_context=measured,
    )
    if restriction == "revoke":
        journal.revoke_grant("reviewer")
    else:
        journal = GoCallJournal(journal.path, clock=lambda: bound["expires_at"])
    with pytest.raises(GoJournalError, match="^GRANT_(REVOKED|EXPIRED)$"):
        journal.begin_call(
            "reviewer",
            "second",
            capability=grant["capability"],
            binding=bound,
            request_context=measured,
        )
    reopened = GoCallJournal(journal.path, clock=lambda: 1001.0)
    with pytest.raises(GoJournalError, match="^GRANT_(REVOKED|EXPIRED)$"):
        reopened.begin_call(
            "reviewer",
            "second",
            capability=grant["capability"],
            binding=bound,
            request_context=measured,
        )
    assert (
        reopened.begin_call(
            "reviewer",
            "lost",
            capability=grant["capability"],
            binding=bound,
            request_context=measured,
        )["send_allowed"]
        is False
    )
    assert reopened.call_receipt("reviewer", "lost")["state"] == "send_unknown"
    assert reopened.snapshot("reviewer")["request_count"] == 1
