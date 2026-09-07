"""First controller initialization cannot resurrect an existing control."""

import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from karajan.execution import LaunchDenied, ProcessSpec, RunnerHost
from test_runnerhost import manifest


@pytest.fixture
def prepared_host(tmp_path):
    host = RunnerHost(tmp_path / "host")
    attempt = manifest()
    host.prepare(attempt, "original-start", ProcessSpec((sys.executable, "-c", "pass"), tmp_path))
    return host, {
        "attempt_id": attempt.id,
        "prepared_id": "original-start",
        "fence": attempt.fence,
        "authorization_ref": attempt.authorization_ref,
    }


def test_control_is_inserted_once_and_identical_replay_is_readonly(prepared_host):
    host, arguments = prepared_host
    first = host.initialize_control_once(**arguments)
    assert first["inserted"] is True
    assert first["dispatch_enabled"] is True
    assert first["activation_allowed"] is False
    before = host.database.read_bytes()
    replay = host.initialize_control_once(**arguments)
    assert replay == first | {"inserted": False}
    assert host.database.read_bytes() == before


def test_disabled_control_is_observed_without_reenabling(prepared_host):
    host, arguments = prepared_host
    host.initialize_control_once(**arguments)
    host.set_control(
        arguments["attempt_id"],
        fence=arguments["fence"],
        authorization_ref=arguments["authorization_ref"],
        dispatch_enabled=False,
    )
    before = host.database.read_bytes()
    assert host.initialize_control_once(**arguments)["dispatch_enabled"] is False
    assert host.database.read_bytes() == before


def test_cancellation_before_first_initialization_creates_no_control(prepared_host):
    host, arguments = prepared_host
    host.cancel(arguments["attempt_id"], "cancel-before-control")
    before = host.database.read_bytes()
    with pytest.raises(LaunchDenied):
        host.initialize_control_once(**arguments)
    assert host.database.read_bytes() == before
    with sqlite3.connect(host.database) as db:
        assert db.execute("SELECT COUNT(*) FROM controls").fetchone()[0] == 0


@pytest.mark.parametrize("change", ["prepared_id", "fence", "authorization_ref"])
def test_wrong_launch_identity_cannot_initialize_control(prepared_host, change):
    host, arguments = prepared_host
    arguments = arguments | {change: 2 if change == "fence" else "different"}
    before = host.database.read_bytes()
    with pytest.raises((LaunchDenied, ValueError)):
        host.initialize_control_once(**arguments)
    assert host.database.read_bytes() == before


def test_newer_control_is_never_replaced_by_initialization(prepared_host):
    host, arguments = prepared_host
    host.set_control(
        arguments["attempt_id"],
        fence=2,
        authorization_ref=arguments["authorization_ref"],
        dispatch_enabled=False,
    )
    before = host.database.read_bytes()
    with pytest.raises(LaunchDenied):
        host.initialize_control_once(**arguments)
    assert host.database.read_bytes() == before


def test_concurrent_initializers_create_one_control(prepared_host):
    host, arguments = prepared_host
    barrier = Barrier(2)

    def initialize():
        barrier.wait()
        return host.initialize_control_once(**arguments)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(initialize) for _ in range(2)]
        assert sorted(f.result()["inserted"] for f in futures) == [False, True]


def test_missing_existing_host_ledger_is_not_recreated(prepared_host):
    host, arguments = prepared_host
    host.database.rename(host.database.with_suffix(".saved"))
    with pytest.raises(sqlite3.Error):
        host.initialize_control_once(**arguments)
    assert not host.database.exists()
