"""The independent reviewer's original fixed inputs, through public APIs only."""

import json
from pathlib import Path

import pytest
from karajan.capacity import CapacityStore

CASES = Path(__file__).resolve().parents[2] / "examples/capacity/review-fixes"


@pytest.mark.parametrize("name", ["exhaustion-unknown", "zero-unknown"])
def test_unknown_cannot_reopen_a_known_exhausted_window(tmp_path: Path, name: str) -> None:
    case = json.loads((CASES / (name + ".input.json")).read_bytes())
    clock = [case["initial_time"]]
    store = CapacityStore(tmp_path / "capacity.sqlite", clock=lambda: clock[0])
    store.register_pool(case["pool"], command_key="pool")
    store.register_profile(case["profile"], command_key="profile")
    store.activate_policy(case["policy"], expected_revision=0, command_key="policy")
    store.observe(case["initial_observation"], command_key="initial")
    if case["failure"]:
        store.record_failure(**case["failure"], command_key="failure")
    clock[0] = case["later_time"]
    store.observe(case["later_observation"], command_key="unknown")
    decision = store.admit(case["request"], command_key="after-exhaustion")
    assert decision["decision"] == case["expected"]["decision"]
    assert decision["reason_codes"] == ["EXHAUSTION_REQUIRES_NEW_OBSERVATION:weekly"]
    assert store.snapshot()["reservations"] == []
    # A newer quantified recovery, after cooldown, can restore admission.
    clock[0] += 1
    observed = dict(
        case["later_observation"], observed_at=clock[0], metric="remaining", amount="10"
    )
    store.observe(observed, command_key="numeric-recovery")
    assert store.admit(case["request"], command_key="recovered")["decision"] == "admitted"
