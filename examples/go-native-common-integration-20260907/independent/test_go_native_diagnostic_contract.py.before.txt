"""Public SQLite contract for native failure diagnostic claims."""

import pytest
from karajan.execution import ProcessIdentity
from karajan.runs import RunError
from test_go_execution_intent import (
    case,
    launched_intent,
    prepared,
    projected,
    ready,
    reservation,
)

__all__ = ["case", "launched_intent", "prepared", "projected", "ready", "reservation"]


def test_replacement_runner_cannot_attach_or_replace_original_failure(launched_intent):
    service, args, runner = launched_intent
    service.effect_start_claim(*args, principal="owner", runner=runner)
    original = service.read(*args, principal="owner")
    fields = {
        "reason_code": "UNIX_RELAY_PATH_TOO_LONG",
        "error_type": "RuntimeError",
        "native_stop": "not_started",
        "relay_status": "closed",
    }
    with pytest.raises(RunError, match="TASK_EXECUTION_CLAIM_NOT_CURRENT"):
        service.record_failure_diagnostic(
            *args,
            principal="owner",
            runner=ProcessIdentity(runner.pid, "a-different-process-birth"),
            **fields,
        )
    assert service.read(*args, principal="owner") == original

    accepted = service.record_failure_diagnostic(*args, principal="owner", runner=runner, **fields)
    diagnostic = accepted["execution"]["failure_diagnostic"]
    assert diagnostic["intent_digest"] == original["execution"]["intent_digest"]
    assert diagnostic["reason_code"] == fields["reason_code"]
    assert accepted["execution"]["effect_claim"] == original["execution"]["effect_claim"]
    assert accepted["activation_allowed"] is False
    assert (
        service.record_failure_diagnostic(*args, principal="owner", runner=runner, **fields)
        == accepted
    )
    with pytest.raises(RunError, match="TASK_DIAGNOSTIC_IDENTITY_CONFLICT"):
        service.record_failure_diagnostic(
            *args,
            principal="owner",
            runner=runner,
            **{**fields, "reason_code": "TASK_EXECUTION_TIMEOUT"},
        )
    assert service.read(*args, principal="owner") == accepted


class DiagnosticText(str):
    """String subclass used to prove cleanup states require exact strings."""


@pytest.mark.parametrize(
    "value",
    ["PRIVATE_DIAGNOSTIC_CONTENT_IS_NOT_A_CLEANUP_STATE", True, None, [], DiagnosticText("closed")],
)
@pytest.mark.parametrize("field", ["native_stop", "relay_status"])
def test_invalid_cleanup_fact_is_rejected_before_persistence(launched_intent, field, value):
    service, args, runner = launched_intent
    service.effect_start_claim(*args, principal="owner", runner=runner)
    before = service.read(*args, principal="owner")
    fields = {
        "reason_code": "UNIX_RELAY_PATH_TOO_LONG",
        "error_type": "RuntimeError",
        "native_stop": "not_started",
        "relay_status": "closed",
    }
    fields[field] = value
    with pytest.raises(RunError, match="TASK_DIAGNOSTIC_NOT_ALLOWLISTED"):
        service.record_failure_diagnostic(*args, principal="owner", runner=runner, **fields)
    after = service.read(*args, principal="owner")
    assert after == before
    assert after["execution"].get("failure_diagnostic") is None
