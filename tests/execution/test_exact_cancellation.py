"""Exact historical cancellation against real Host persistence, without launching."""

import sys
from copy import deepcopy

import pytest
from karajan.execution import LaunchDenied, ProcessSpec, RunnerHost
from test_runnerhost import manifest


@pytest.fixture
def owned(tmp_path):
    host = RunnerHost(tmp_path / "host")
    cwd = tmp_path / "old-controller-directory"
    cwd.mkdir()
    attempt = manifest()
    spec = ProcessSpec((sys.executable, "-c", "pass"), cwd)
    binding = {
        "prepared_id": "original-start",
        "manifest": attempt.model_dump(),
        "process_spec": spec.document(),
    }
    host.prepare(attempt, binding["prepared_id"], spec)
    host.initialize_control_once(
        attempt.id,
        prepared_id=binding["prepared_id"],
        fence=attempt.fence,
        authorization_ref=attempt.authorization_ref,
    )
    return host, attempt, binding, cwd


@pytest.mark.parametrize(
    "field",
    ["prepared_id", "fence", "budget_ref", "profile_revision", "argv", "cwd", "timeout_seconds"],
)
def test_expected_full_original_launch_is_checked_before_any_cancellation_write(owned, field):
    host, attempt, binding, _ = owned
    different = deepcopy(binding)
    if field == "prepared_id":
        different[field] = "other-start"
    elif field in {"fence", "profile_revision"}:
        different["manifest"][field] += 1
    elif field == "budget_ref":
        different["manifest"][field] = "another-budget"
    elif field == "argv":
        different["process_spec"][field] = [sys.executable, "-c", "print('different')"]
    elif field == "cwd":
        different["process_spec"][field] += "-different"
    else:
        different["process_spec"][field] += 1
    before = host.database.read_bytes()
    with pytest.raises(LaunchDenied, match="CANCELLATION_BINDING_MISMATCH"):
        host.cancel(attempt.id, "cancel", expected_binding=different, timeout_seconds=0)
    assert host.database.read_bytes() == before
    assert host.inspect(attempt.id).business_status == "pending"


def test_exact_historical_cancel_does_not_require_current_fence_or_existing_cwd(owned):
    host, attempt, binding, cwd = owned
    host.set_control(
        attempt.id, fence=attempt.fence + 1, authorization_ref="withdrawn", dispatch_enabled=False
    )
    cwd.rmdir()
    restarted = RunnerHost(host.directory, existing_only=True)
    first = restarted.cancel(attempt.id, "cancel", expected_binding=binding, timeout_seconds=0)
    assert first.snapshot.state == "exited" and first.snapshot.business_status == "cancelled"
    assert first.snapshot.supervisor is None
    before = restarted.database.read_bytes()
    repeated = restarted.cancel(attempt.id, "cancel", expected_binding=binding, timeout_seconds=0)
    assert repeated == first
    assert restarted.database.read_bytes() == before
