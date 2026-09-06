"""Capture holds a live writer identity through public Host calls and real SQLite."""

import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import replace
from threading import Event

import pytest
from karajan.execution import LaunchDenied, ProbeCrash, ProcessSpec, RunnerHost
from test_runnerhost import authorize, manifest, wait_for_exit


@pytest.fixture
def writer(tmp_path, request):
    host = RunnerHost(tmp_path / "host")
    started = tmp_path / "writer-started"
    command = (
        "from pathlib import Path; import time; "
        f"Path({str(started)!r}).write_text('started'); time.sleep(15)"
    )
    attempt = manifest()
    host.prepare(
        attempt, "writer-start", ProcessSpec((sys.executable, "-c", command), tmp_path, 20)
    )
    activation = authorize(host)
    if getattr(request, "param", None) is not None:
        activation = replace(activation, expires_at=time.time() + request.param)
    host.start("writer-start", activation)
    try:
        deadline = time.monotonic() + 5
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert started.read_text() == "started"
        assert host.inspect(attempt.id).state == "running"
        yield host, attempt, activation
    finally:
        host.cancel(attempt.id, "writer-cleanup")


def test_capture_can_hold_current_identity_while_the_trusted_writer_is_still_running(writer):
    host, attempt, activation = writer
    before = host.inspect(attempt.id)
    with host.current_fence_guard(
        attempt.id, fence=attempt.fence, authorization_ref=attempt.authorization_ref
    ) as current:
        assert current == {
            "scope": "current_host_fence",
            "attempt_id": attempt.id,
            "prepared_id": "writer-start",
            "fence": attempt.fence,
            "authorization_ref": attempt.authorization_ref,
            "profile": {"id": attempt.profile_id, "revision": attempt.profile_revision},
            "activation_id": activation.id,
            "activation_allowed": False,
        }
    assert before.state == "running" and before.processes
    assert host.inspect(attempt.id) == before


def test_an_unknown_attempt_cannot_acquire_a_capture_fence(tmp_path):
    host = RunnerHost(tmp_path / "host")
    with pytest.raises(KeyError):
        with host.current_fence_guard("missing", fence=1, authorization_ref="approved"):
            pytest.fail("An unknown attempt entered capture")


@pytest.mark.parametrize("directory", ["host", "host # percent% \u6d4b\u8bd5"])
def test_missing_capture_ledger_is_not_recreated(tmp_path, directory):
    host = RunnerHost(tmp_path / directory)
    retained = host.database.with_name("retained-ledger.sqlite3")
    original = host.database.read_bytes()
    host.database.rename(retained)
    with pytest.raises(sqlite3.Error):
        with host.current_fence_guard("missing", fence=1, authorization_ref="approved"):
            pytest.fail("A missing ledger authorized capture")
    assert retained.read_bytes() == original
    assert not host.database.exists()


def test_capture_opens_an_existing_ledger_with_uri_reserved_characters(tmp_path):
    host = RunnerHost(tmp_path / "host # percent% \u6d4b\u8bd5")
    attempt = manifest()
    host.prepare(attempt, "start", ProcessSpec((sys.executable, "-c", "pass"), tmp_path))
    before = host.inspect(attempt.id)
    with pytest.raises(LaunchDenied, match="CAPTURE_START_REQUIRED"):
        with host.current_fence_guard(
            attempt.id, fence=attempt.fence, authorization_ref=attempt.authorization_ref
        ):
            pytest.fail("A prepared attempt entered capture")
    assert host.inspect(attempt.id) == before


@pytest.mark.parametrize("phase", ["prepared", "after_accept", "before_spawn"])
def test_preparation_or_unobserved_start_intent_does_not_authorize_capture(tmp_path, phase):
    host = RunnerHost(tmp_path / "host")
    attempt = manifest()
    host.prepare(attempt, "start", ProcessSpec((sys.executable, "-c", "pass"), tmp_path))
    activation = authorize(host)
    if phase != "prepared":
        with pytest.raises(ProbeCrash):
            host.start("start", activation, crash_at=phase)
    before = host.inspect(attempt.id)
    with pytest.raises(LaunchDenied, match="CAPTURE_START_REQUIRED"):
        with host.current_fence_guard(
            attempt.id, fence=attempt.fence, authorization_ref=attempt.authorization_ref
        ):
            pytest.fail("A start intent without accepted supervisor identity entered capture")
    assert host.inspect(attempt.id) == before


@pytest.mark.parametrize("changed", ["fence", "authorization"])
def test_caller_must_name_the_exact_persisted_writer_identity(writer, changed):
    host, attempt, _ = writer
    with pytest.raises(LaunchDenied, match="CAPTURE_FENCE_NOT_CURRENT"):
        with host.current_fence_guard(
            attempt.id,
            fence=2 if changed == "fence" else attempt.fence,
            authorization_ref="other-approval"
            if changed == "authorization"
            else attempt.authorization_ref,
        ):
            pytest.fail("A caller-supplied replacement identity entered capture")


@pytest.mark.parametrize("change", ["fence", "authorization", "disabled", "cancelled"])
def test_current_control_revocation_prevents_capture_of_the_original_writer(writer, change):
    host, attempt, _ = writer
    if change == "cancelled":
        host.cancel(attempt.id, "cancel-before-capture")
    else:
        host.set_control(
            attempt.id,
            fence=2 if change == "fence" else attempt.fence,
            authorization_ref="new-approval"
            if change == "authorization"
            else attempt.authorization_ref,
            dispatch_enabled=change != "disabled",
        )
    with pytest.raises(LaunchDenied, match="CAPTURE_FENCE_NOT_CURRENT"):
        with host.current_fence_guard(
            attempt.id, fence=attempt.fence, authorization_ref=attempt.authorization_ref
        ):
            pytest.fail("Revoked current control entered capture")


def test_a_historical_accepted_result_is_not_fresh_capture_authorization(writer):
    host, attempt, _ = writer
    previous = host.receive_result(attempt.id, attempt.fence, "historical", {"candidate": "prior"})
    assert previous.accepted is True
    host.set_control(
        attempt.id,
        fence=attempt.fence,
        authorization_ref=attempt.authorization_ref,
        dispatch_enabled=False,
    )
    assert (
        host.receive_result(attempt.id, attempt.fence, "historical", {"candidate": "prior"})
        == previous
    )
    with pytest.raises(LaunchDenied, match="CAPTURE_FENCE_NOT_CURRENT"):
        with host.current_fence_guard(
            attempt.id, fence=attempt.fence, authorization_ref=attempt.authorization_ref
        ):
            pytest.fail("Historical completion bypassed current withdrawal")


@pytest.mark.parametrize("change", ["control", "cancel"])
def test_control_and_cancellation_wait_until_capture_fence_is_released(writer, change):
    host, attempt, _ = writer
    other = RunnerHost(host.directory)
    entered = Event()
    completed = Event()

    def revoke():
        entered.set()
        if change == "control":
            other.set_control(
                attempt.id,
                fence=2,
                authorization_ref=attempt.authorization_ref,
                dispatch_enabled=True,
            )
        else:
            other.cancel(attempt.id, "concurrent-cancel")
        completed.set()

    with ThreadPoolExecutor(max_workers=1) as executor:
        with host.current_fence_guard(
            attempt.id, fence=attempt.fence, authorization_ref=attempt.authorization_ref
        ):
            future = executor.submit(revoke)
            assert entered.wait(timeout=2)
            with pytest.raises(TimeoutError):
                future.result(timeout=0.2)
            assert not completed.is_set()
        future.result(timeout=5)
    assert completed.is_set()
    with pytest.raises(LaunchDenied):
        with host.current_fence_guard(
            attempt.id, fence=attempt.fence, authorization_ref=attempt.authorization_ref
        ):
            pytest.fail("The next capture ignored the committed revocation")


def test_body_exception_releases_lock_without_changing_execution_or_usage(writer):
    host, attempt, _ = writer
    host.record_usage(attempt.id, attempt.fence, "observed-usage", {"tokens": 7})
    before = host.inspect(attempt.id)
    with pytest.raises(RuntimeError, match="collector-failed"):
        with host.current_fence_guard(
            attempt.id, fence=attempt.fence, authorization_ref=attempt.authorization_ref
        ) as current:
            current["profile"]["id"] = "caller-mutation"
            raise RuntimeError("collector-failed")
    assert host.inspect(attempt.id) == before
    reopened = RunnerHost(host.directory)
    with reopened.current_fence_guard(
        attempt.id, fence=attempt.fence, authorization_ref=attempt.authorization_ref
    ) as retained:
        assert retained["profile"] == {
            "id": attempt.profile_id,
            "revision": attempt.profile_revision,
        }
    reopened.set_control(
        attempt.id, fence=2, authorization_ref=attempt.authorization_ref, dispatch_enabled=True
    )
    assert reopened.inspect(attempt.id).usage_events == before.usage_events


def test_normal_finished_attempt_can_be_collected_without_creating_a_new_start(tmp_path):
    host = RunnerHost(tmp_path / "host")
    attempt = manifest()
    host.prepare(attempt, "finished", ProcessSpec((sys.executable, "-c", "pass"), tmp_path))
    activation = authorize(host)
    host.start("finished", activation)
    wait_for_exit(host)
    before = host.inspect(attempt.id)
    with host.current_fence_guard(
        attempt.id, fence=attempt.fence, authorization_ref=attempt.authorization_ref
    ) as current:
        assert current["activation_id"] == activation.id
    assert host.inspect(attempt.id) == before


def test_capture_fence_input_rejects_boolean_fence_before_reading_host_state(tmp_path):
    host = RunnerHost(tmp_path / "host")
    with pytest.raises(ValueError):
        with host.current_fence_guard("missing", fence=True, authorization_ref="approval"):
            pytest.fail("A boolean became an attempt fence")


@pytest.mark.parametrize("writer", [3.0], indirect=True)
def test_expired_start_permission_does_not_expire_an_already_running_capture(writer):
    host, attempt, activation = writer
    deadline = time.monotonic() + 8
    while time.time() <= activation.expires_at and time.monotonic() < deadline:
        time.sleep(0.02)
    assert time.time() > activation.expires_at
    with host.current_fence_guard(
        attempt.id, fence=attempt.fence, authorization_ref=attempt.authorization_ref
    ) as current:
        assert current["activation_id"] == activation.id
    assert host.inspect(attempt.id).state == "running"
