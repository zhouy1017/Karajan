"""Business Relay grants stay distinct from legacy and qualification records."""

import os
from pathlib import Path

import pytest
from karajan.adapters.opencode.go_context import GoRequestAccounting
from karajan.adapters.opencode.go_journal import GoCallJournal, GoJournalError
from karajan.routing.compiler import digest
from test_go_context import measure, payload


@pytest.fixture(scope="module")
def accounting() -> GoRequestAccounting:
    return GoRequestAccounting(Path(os.environ["KARAJAN_GO_TOKENIZER_DIRECTORY"]))


def _common() -> dict[str, object]:
    return {
        "attempt_id": "attempt",
        "fence": 1,
        "profile_digest": "a" * 64,
        "runtime_digest": "b" * 64,
        "channel": "opencode-go",
        "model": "glm-5.3-flash",
        "auth_generation": "generation",
        "expires_at": 2000.0,
        "max_requests": 2,
    }


def _context(source: str = "c" * 64) -> dict[str, object]:
    return {
        "source_sha256": source,
        "approved_input_tokens": 4000,
        "reserved_output_tokens": 4096,
        "operating_context_tokens": 8192,
        "fixed_margin": 100,
        "ratio_margin_basis_points": 1000,
    }


def _planning(source: str = "c" * 64) -> dict[str, object]:
    return {
        **_common(),
        "schema_version": "karajan.go-planning-native-grant.v1",
        "subject": {
            "kind": "planning_execution",
            "project_id": "project",
            "run_id": "run",
            "intent_id": "intent",
            "execution_id": "execution",
        },
        "planning_binding_sha256": "d" * 64,
        "admission_sha256": "e" * 64,
        "input_sha256": "f" * 64,
        "authentication_source_digest": "0" * 64,
        "context": _context(source),
        "tool_policy": "none",
    }


def _reviewer(source: str = "c" * 64) -> dict[str, object]:
    return {
        **_common(),
        "schema_version": "karajan.go-reviewer-native-grant.v1",
        "subject": {
            "kind": "reviewer_execution",
            "project_id": "project",
            "run_id": "run",
            "reviewer_operation_id": "review-op",
            "worker_operation_id": "worker-op",
            "reviewer_task_id": "review",
            "execution_id": "execution",
        },
        "review_binding_sha256": "d" * 64,
        "reviewer_input_sha256": "e" * 64,
        "candidate_checks_sha256": "f" * 64,
        "authentication_source_digest": "0" * 64,
        "context": _context(source),
        "tool_policy": "read",
    }


def _measurement(accounting, binding):
    limits = dict(binding["context"])
    del limits["source_sha256"]
    return measure(accounting, payload(), **limits)


@pytest.mark.parametrize("factory", [_planning, _reviewer])
def test_business_bindings_are_durable_and_context_bound(tmp_path, accounting, factory):
    source = digest(accounting.source())
    binding = factory(source)
    journal = GoCallJournal(tmp_path / "journal.sqlite", clock=lambda: 1000.0)
    created = journal.create_grant(binding, grant_id="grant")
    call = journal.begin_call(
        "grant", "relay-owned-call", capability=created["capability"], binding=binding,
        request_context=_measurement(accounting, binding),
    )
    assert call["send_allowed"] is True
    assert journal.snapshot("grant")["calls"][0]["state"] == "send_unknown"
    assert journal.begin_call(
        "grant", "relay-owned-call", capability=created["capability"], binding=binding,
        request_context=_measurement(accounting, binding),
    )["send_allowed"] is False


def test_business_context_tamper_is_pre_send_and_legacy_is_readable(tmp_path, accounting):
    journal = GoCallJournal(tmp_path / "journal.sqlite", clock=lambda: 1000.0)
    binding = _planning(digest(accounting.source()))
    created = journal.create_grant(binding, grant_id="new")
    bad = _measurement(accounting, binding)
    bad["approved_input_tokens"] = 1
    with pytest.raises(GoJournalError, match="GO_JOURNAL_INPUT_INVALID"):
        journal.begin_call(
            "new", "call", capability=created["capability"], binding=binding, request_context=bad
        )
    assert journal.snapshot("new")["request_count"] == 0
    legacy = {**_common(), "qualification_id": "historical"}
    old = journal.create_grant(legacy, grant_id="old")
    result = journal.authenticate_grant("old", capability=old["capability"], binding=legacy)
    assert result["binding"] == legacy
