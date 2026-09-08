"""The planning output ledger makes a lost dispatch fail closed."""

from pathlib import Path

import pytest
from karajan.orchestration.planning_transport import PlanningOutputStore
from karajan.runs import RunError


def _binding() -> dict[str, object]:
    return {"execution_id": "execution"}


def test_dispatch_claim_allows_one_attempt_and_completion_is_recoverable(tmp_path: Path) -> None:
    store = PlanningOutputStore(tmp_path / "outputs.sqlite", authority_kind="fixture")
    binding = _binding()
    store.arm(binding, {"producer": "fixture"})

    assert store.claim_dispatch(binding) == "claimed"
    # A retry cannot manufacture another model/provider send while the first
    # send outcome is unknown.
    assert store.claim_dispatch(binding) == "pending"

    store.publish(binding, b'{"summary":"fixture"}')
    assert store.claim_dispatch(binding) == "completed"


def test_existing_output_store_never_creates_a_missing_ledger(tmp_path: Path) -> None:
    missing = tmp_path / "missing.sqlite"
    try:
        PlanningOutputStore(missing, authority_kind="production", existing_only=True)
    except Exception as error:
        assert str(error) == "PLANNING_OUTPUT_AUTHORITY_UNAVAILABLE"
    else:
        raise AssertionError("existing-only factory must not provision an output ledger")
    assert not missing.exists()


def test_execute_key_is_bound_to_one_execution_before_any_dispatch(tmp_path: Path) -> None:
    store = PlanningOutputStore(tmp_path / "outputs.sqlite", authority_kind="fixture")
    store.claim_execute_command(
        {"execution_id": "first"}, principal="owner", command_key="execute"
    )

    with pytest.raises(RunError, match="^IDEMPOTENCY_CONFLICT$"):
        store.claim_execute_command(
            {"execution_id": "second"}, principal="owner", command_key="execute"
        )
