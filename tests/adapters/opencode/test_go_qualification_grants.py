"""Versioned qualification send intents through the real public SQLite journal."""

import copy
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from karajan.adapters.opencode.go_journal import GoCallJournal, GoJournalError
from karajan.routing.compiler import digest
from test_go_context import accounting, artifacts
from test_go_journal import binding
from test_go_relay import payload

__all__ = ["accounting", "artifacts"]


def qualification_binding(accounting):
    return binding(
        schema_version="karajan.go-qualification-grant.v2",
        probe_spec_digest="c" * 64,
        scenario="edit",
        context={
            "source_sha256": digest(accounting.source()),
            "approved_input_tokens": 4000,
            "reserved_output_tokens": 4096,
            "operating_context_tokens": 8192,
            "fixed_margin": 100,
            "ratio_margin_basis_points": 1000,
        },
    )


def measurement(accounting, grant_binding, **changes):
    limits = {k: v for k, v in grant_binding["context"].items() if k != "source_sha256"}
    return accounting.measure(payload(), **{**limits, **changes})


def test_new_qualification_requires_its_bound_measurement_before_a_send_slot(tmp_path, accounting):
    journal = GoCallJournal(tmp_path / "calls.sqlite", clock=lambda: 1000.0)
    bound = qualification_binding(accounting)
    grant = journal.create_grant(bound, grant_id="qualification")
    assert grant["binding"] == bound
    with pytest.raises(GoJournalError, match="^QUALIFICATION_CONTEXT_REQUIRED$"):
        journal.begin_call("qualification", "first", capability=grant["capability"], binding=bound)
    assert journal.snapshot("qualification")["request_count"] == 0
    measured = measurement(accounting, bound)
    first = journal.begin_call(
        "qualification",
        "first",
        capability=grant["capability"],
        binding=bound,
        request_context=measured,
    )
    assert first["send_allowed"] is True
    assert first["receipt"]["request_context"] == measured
    assert journal.call_receipt("qualification", "first")["state"] == "send_unknown"


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
def test_every_bound_context_value_is_required_exactly_before_consumption(
    tmp_path, accounting, field
):
    bound = qualification_binding(accounting)
    journal = GoCallJournal(tmp_path / "calls.sqlite", clock=lambda: 1000.0)
    grant = journal.create_grant(bound, grant_id="qualification")
    if field == "source_sha256":
        measured = {**measurement(accounting, bound), field: "e" * 64}
    else:
        measured = measurement(accounting, bound, **{field: bound["context"][field] + 1})
    with pytest.raises(GoJournalError, match="^QUALIFICATION_CONTEXT_MISMATCH$"):
        journal.begin_call(
            "qualification",
            "wrong",
            capability=grant["capability"],
            binding=bound,
            request_context=measured,
        )
    assert journal.snapshot("qualification")["request_count"] == 0
    assert journal.call_receipt("qualification", "wrong") is None


@pytest.mark.parametrize(
    "change", ["unknown-version", "missing-version", "subject", "scenario", "raw", "limits"]
)
def test_unrecognized_or_mixed_qualification_grants_cannot_be_created(tmp_path, accounting, change):
    bound = qualification_binding(accounting)
    if change == "unknown-version":
        bound["schema_version"] = "karajan.go-qualification-grant.v3"
    elif change == "missing-version":
        del bound["schema_version"]
    elif change == "subject":
        bound["subject"] = {
            "kind": "task_attempt",
            "project_id": "p",
            "run_id": "r",
            "task_id": "t",
        }
    elif change == "scenario":
        bound["scenario"] = "arbitrary_task"
    elif change == "raw":
        bound["context"]["raw_prompt"] = "PRIVATE_INPUT_CANARY"
    else:
        bound["context"]["ratio_margin_basis_points"] = 10001
    journal = GoCallJournal(tmp_path / "calls.sqlite", clock=lambda: 1000.0)
    with pytest.raises(GoJournalError, match="^GO_JOURNAL_INPUT_INVALID$"):
        journal.create_grant(bound, grant_id="qualification")
    with pytest.raises(GoJournalError, match="^GRANT_NOT_FOUND$"):
        journal.snapshot("qualification")


def test_unknown_send_and_replay_keep_original_measurement_and_no_new_permission(
    tmp_path, accounting
):
    journal = GoCallJournal(tmp_path / "calls.sqlite", clock=lambda: 1000.0)
    bound = qualification_binding(accounting)
    grant = journal.create_grant(bound, grant_id="qualification")
    measured = measurement(accounting, bound)
    journal.begin_call(
        "qualification",
        "lost",
        capability=grant["capability"],
        binding=bound,
        request_context=measured,
    )  # The caller loses this return and reopens, rather than resending.
    reopened = GoCallJournal(journal.path, clock=lambda: 1001.0)
    history = reopened.call_receipt("qualification", "lost")
    assert history["state"] == "send_unknown"
    replay = reopened.begin_call(
        "qualification",
        "lost",
        capability=grant["capability"],
        binding=bound,
        request_context=measured,
    )
    assert replay == {"send_allowed": False, "receipt": history}
    assert reopened.create_grant(bound, grant_id="qualification")["capability"] is None
    for changed in (None, {**measured, "request_digest": "e" * 64}):
        with pytest.raises(GoJournalError, match="^CALL_CONTEXT_CONFLICT$"):
            reopened.begin_call(
                "qualification",
                "lost",
                capability=grant["capability"],
                binding=bound,
                request_context=changed,
            )
    assert reopened.snapshot("qualification")["request_count"] == 1
    assert reopened.call_receipt("qualification", "lost") == history


@pytest.mark.parametrize(
    "field,value", [("probe_spec_digest", "d" * 64), ("scenario", "denied_read")]
)
def test_same_grant_cannot_be_reinterpreted_for_other_fixed_inputs(
    tmp_path, accounting, field, value
):
    bound = qualification_binding(accounting)
    journal = GoCallJournal(tmp_path / "calls.sqlite", clock=lambda: 1000.0)
    grant = journal.create_grant(bound, grant_id="qualification")
    altered = {**bound, field: value}
    with pytest.raises(GoJournalError, match="^GRANT_CONFLICT$"):
        journal.create_grant(altered, grant_id="qualification")
    with pytest.raises(GoJournalError, match="^GRANT_BINDING_MISMATCH$"):
        journal.begin_call(
            "qualification",
            "other",
            capability=grant["capability"],
            binding=altered,
            request_context=measurement(accounting, bound),
        )
    assert journal.snapshot("qualification")["request_count"] == 0


def test_concurrent_new_qualification_calls_share_six_durable_slots(tmp_path, accounting):
    path = tmp_path / "calls.sqlite"
    journal = GoCallJournal(path, clock=lambda: 1000.0)
    bound = qualification_binding(accounting)
    grant = journal.create_grant(bound, grant_id="qualification")
    measured = measurement(accounting, bound)

    def attempt(index):
        reopened = GoCallJournal(path, clock=lambda: 1001.0)
        try:
            return reopened.begin_call(
                "qualification",
                f"call-{index}",
                capability=grant["capability"],
                binding=bound,
                request_context=measured,
            )["send_allowed"]
        except GoJournalError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(8)))
    assert results.count(True) == 6
    assert results.count("REQUEST_LIMIT_REACHED") == 2
    calls = journal.snapshot("qualification")["calls"]
    assert [c["sequence"] for c in calls] == [1, 2, 3, 4, 5, 6]
    assert all(c["state"] == "send_unknown" for c in calls)
    assert all(c["request_context"] == measured for c in calls)


@pytest.mark.parametrize("restriction", ["revoked", "expired"])
def test_new_qualification_restrictions_do_not_erase_unknown_history(
    tmp_path, accounting, restriction
):
    bound = qualification_binding(accounting)
    journal = GoCallJournal(tmp_path / "calls.sqlite", clock=lambda: 1000.0)
    grant = journal.create_grant(bound, grant_id="qualification")
    measured = measurement(accounting, bound)
    first = journal.begin_call(
        "qualification",
        "first",
        capability=grant["capability"],
        binding=bound,
        request_context=measured,
    )
    if restriction == "revoked":
        journal.revoke_grant("qualification")
    else:
        journal = GoCallJournal(journal.path, clock=lambda: 2000.0)
    with pytest.raises(GoJournalError, match="^GRANT_(REVOKED|EXPIRED)$"):
        journal.begin_call(
            "qualification",
            "second",
            capability=grant["capability"],
            binding=bound,
            request_context=measured,
        )
    rolled_back_clock = GoCallJournal(journal.path, clock=lambda: 1001.0)
    with pytest.raises(GoJournalError, match="^GRANT_(REVOKED|EXPIRED)$"):
        rolled_back_clock.begin_call(
            "qualification",
            "third",
            capability=grant["capability"],
            binding=bound,
            request_context=measured,
        )
    assert rolled_back_clock.begin_call(
        "qualification",
        "first",
        capability=grant["capability"],
        binding=bound,
        request_context=measured,
    ) == {"send_allowed": False, "receipt": first["receipt"]}
    assert rolled_back_clock.snapshot("qualification")["request_count"] == 1


def test_legacy_shape_key_order_and_unmetered_history_remain_unchanged(tmp_path):
    bound = binding()
    journal = GoCallJournal(tmp_path / "calls.sqlite", clock=lambda: 1000.0)
    grant = journal.create_grant(bound, grant_id="legacy")
    assert json.dumps(grant["binding"]) == json.dumps(bound)
    first = journal.begin_call("legacy", "first", capability=grant["capability"], binding=bound)
    expected = {
        "grant_id": "legacy",
        "call_id": "first",
        "sequence": 1,
        "send_intent_at": 1000.0,
        "state": "send_unknown",
        "completed_at": None,
        "outcome": None,
    }
    assert first == {"send_allowed": True, "receipt": expected}
    reopened = GoCallJournal(journal.path, clock=lambda: 1001.0)
    assert reopened.begin_call(
        "legacy", "first", capability=grant["capability"], binding=bound
    ) == {
        "send_allowed": False,
        "receipt": expected,
    }
    detached = copy.deepcopy(reopened.snapshot("legacy"))
    assert "request_context" not in detached["calls"][0]
