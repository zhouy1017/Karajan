"""Independent public Host checks; real SQLite and local Python subprocesses."""

import hashlib
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest
from karajan.execution import Activation, LaunchDenied, ProbeCrash, ProcessSpec, RunnerHost
from test_runnerhost import manifest


def control(host, *, fence=1, authorization=None, enabled=True):
    host.set_control(
        manifest().id,
        fence=fence,
        authorization_ref=authorization or manifest().authorization_ref,
        dispatch_enabled=enabled,
    )


def activation():
    spec = manifest()
    return Activation(
        "independent-activation",
        spec.id,
        spec.fence,
        spec.authorization_ref,
        spec.budget_ref,
        time.time() + 60,
    )


def guard(host, *, fence=1, authorization=None):
    return host.current_fence_guard(
        manifest().id,
        fence=fence,
        authorization_ref=authorization or manifest().authorization_ref,
    )


def ledger_hash(host):
    # Byte-level no-write observation at the database file, not SQL fabrication.
    return hashlib.sha256(host.database.read_bytes()).hexdigest()


def wait_finished(host):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        snapshot = host.inspect(manifest().id)
        if snapshot.state == "exited" and snapshot.exit_code == 0:
            return snapshot
        time.sleep(0.02)
    pytest.fail(f"real local command did not finish: {snapshot}")


@contextmanager
def finished_host(tmp_path):
    host = RunnerHost(tmp_path / "host")
    host.prepare(
        manifest(), "independent-start", ProcessSpec((sys.executable, "-c", "pass"), tmp_path)
    )
    control(host)
    host.start("independent-start", activation())
    wait_finished(host)
    try:
        yield host
    finally:
        host.cancel(manifest().id, "independent-cleanup")


@pytest.mark.parametrize("stage", ["prepared", "after_accept", "before_spawn"])
def test_prepared_or_unresolved_start_does_not_authorize_capture(tmp_path, stage):
    host = RunnerHost(tmp_path / "host")
    marker = tmp_path / "effect.txt"
    command = f"from pathlib import Path; Path({str(marker)!r}).write_text('started')"
    host.prepare(
        manifest(), "independent-start", ProcessSpec((sys.executable, "-c", command), tmp_path)
    )
    control(host)
    if stage != "prepared":
        with pytest.raises(ProbeCrash):
            host.start("independent-start", activation(), crash_at=stage)
    reopened = RunnerHost(host.directory)
    before = ledger_hash(reopened)
    with pytest.raises(LaunchDenied, match="^CAPTURE_START_REQUIRED$"):
        with guard(reopened):
            pytest.fail("an unlaunched attempt received capture identity")
    assert ledger_hash(reopened) == before
    assert not marker.exists()


def test_accepted_finished_identity_is_read_only_and_not_a_new_activation(tmp_path, monkeypatch):
    with finished_host(tmp_path) as host:
        before = ledger_hash(host)
        snapshot = host.inspect(manifest().id)
        # Start expiry is not a collection deadline, and this guard needs no clock.
        with monkeypatch.context() as patch:

            def forbidden_clock():
                raise AssertionError("capture must not create a fresh time-based activation")

            patch.setattr("karajan.execution.host.time.time", forbidden_clock)
            with guard(host) as receipt:
                assert receipt == {
                    "scope": "current_host_fence",
                    "attempt_id": manifest().id,
                    "prepared_id": "independent-start",
                    "fence": 1,
                    "authorization_ref": manifest().authorization_ref,
                    "profile": {"id": manifest().profile_id, "revision": 1},
                    "activation_id": "independent-activation",
                    "activation_allowed": False,
                }
                receipt["profile"]["revision"] = 999
        assert host.inspect(manifest().id) == snapshot
        with guard(host) as fresh:
            assert fresh["profile"]["revision"] == 1
        assert ledger_hash(host) == before


@pytest.mark.parametrize("change", ["old-fence", "wrong-auth", "disabled", "cancelled"])
def test_current_controls_override_old_accepted_result(tmp_path, change):
    with finished_host(tmp_path) as host:
        original = host.receive_result(manifest().id, 1, "finished-result", {"candidate": "old"})
        assert original.accepted
        if change == "old-fence":
            control(host, fence=2)
        elif change == "wrong-auth":
            control(host, authorization="new-authorization")
        elif change == "disabled":
            control(host, enabled=False)
        else:
            host.cancel(manifest().id, "explicit-cancel")
            # Even an explicit coordinator re-enable cannot erase cancellation.
            control(host)
        assert (
            host.receive_result(manifest().id, 1, "finished-result", {"candidate": "old"})
            == original
        )
        before = ledger_hash(host)
        with pytest.raises(LaunchDenied, match="^CAPTURE_FENCE_NOT_CURRENT$"):
            with guard(host):
                pytest.fail("historical result restored capture authority")
        assert ledger_hash(host) == before


@pytest.mark.parametrize("argument", [{"fence": 2}, {"authorization": "other"}, {"fence": True}])
def test_caller_identity_must_match_manifest_and_activation(tmp_path, argument):
    with finished_host(tmp_path) as host:
        before = ledger_hash(host)
        with pytest.raises(ValueError):
            with guard(host, **argument):
                pytest.fail("wrong caller identity was accepted")
        assert ledger_hash(host) == before
        with guard(host) as valid:
            assert valid["fence"] == 1


@pytest.mark.parametrize("operation,body_failure", [("control", False), ("cancel", True)])
def test_real_control_and_cancellation_writers_wait_through_guard(
    tmp_path, operation, body_failure
):
    with finished_host(tmp_path) as host:
        writer_started = threading.Event()

        def writer():
            other = RunnerHost(host.directory)
            writer_started.set()
            if operation == "control":
                control(other, fence=2)
            else:
                other.cancel(manifest().id, "concurrent-cancel")

        class CaptureAbort(Exception):
            pass

        with ThreadPoolExecutor(max_workers=1) as executor:
            try:
                with guard(host) as current:
                    future = executor.submit(writer)
                    assert writer_started.wait(2)
                    time.sleep(0.15)
                    assert not future.done()
                    assert current["fence"] == 1
                    if body_failure:
                        raise CaptureAbort
            except CaptureAbort:
                assert body_failure
            future.result(timeout=4)
        with pytest.raises(LaunchDenied, match="^CAPTURE_FENCE_NOT_CURRENT$"):
            with guard(host):
                pytest.fail("a concurrent revocation was ignored")


def test_running_supervisor_is_allowed_but_guard_does_not_claim_termination(tmp_path):
    host = RunnerHost(tmp_path / "host")
    host.prepare(
        manifest(),
        "independent-start",
        ProcessSpec((sys.executable, "-c", "import time; time.sleep(10)"), tmp_path, 12),
    )
    control(host)
    host.start("independent-start", activation())
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and host.inspect(manifest().id).state != "running":
            time.sleep(0.02)
        assert host.inspect(manifest().id).state == "running"
        with guard(host) as current:
            assert current["scope"] == "current_host_fence"
            assert "stopped" not in current
            assert "candidate_accepted" not in current
            assert host.inspect(manifest().id).processes
    finally:
        assert host.cancel(manifest().id, "independent-stop").status == "confirmed"


def test_missing_ledger_guard_does_not_create_a_replacement_database(tmp_path):
    host = RunnerHost(tmp_path / "host")
    original = host.database.with_name("retained-ledger.sqlite3")
    host.database.rename(original)
    with pytest.raises((sqlite3.Error, OSError, KeyError)):
        with guard(host):
            pytest.fail("missing ledger authorized capture")
    assert original.is_file()
    assert not host.database.exists(), "a read-only guard created a new empty ledger"
