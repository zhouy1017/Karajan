"""Independent public Host checks with real command children and SQLite migration.

The embedded Python runner is a controlled process fixture. No namespace, model,
provider or credential is used. Only legacy-schema tests deliberately edit SQL.
"""

import json
import os
import signal
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from contextlib import contextmanager
from dataclasses import asdict
from threading import Event

import pytest
from karajan.execution import (
    LaunchDenied,
    ProcessIdentity,
    ProcessSpec,
    RunnerHost,
    observe_process,
)
from test_runnerhost import authorize, manifest

_PROGRAM = r"""
import json, os, subprocess, sys, time
from dataclasses import asdict
from pathlib import Path
from karajan.execution import RunnerHost, LaunchDenied

directory, output, role = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
host = RunnerHost(directory)
kwargs = dict(fence=1, authorization_ref='authorization-test-1')

def publish(name, value):
    path = output / name
    temporary = path.with_suffix('.writing')
    temporary.write_text(json.dumps(value))
    temporary.replace(path)

def wait(name):
    deadline = time.monotonic() + 15
    while not (output / name).exists():
        if time.monotonic() >= deadline:
            raise RuntimeError('fixture instruction timeout: ' + name)
        time.sleep(0.01)

def checked():
    try:
        with host.current_runner_guard('attempt-local-1', **kwargs) as identity:
            return dict(decision='allowed', identity=asdict(identity))
    except LaunchDenied as error:
        return dict(decision=str(error), pid=os.getpid())

if role == 'descendant':
    result = checked()
    try:
        host.wait_for_runner_registration('attempt-local-1', timeout_seconds=.2)
        result['registration'] = 'allowed'
    except LaunchDenied as error:
        result['registration'] = str(error)
    publish('descendant.json', result)
    wait('release')
else:
    identity = host.wait_for_runner_registration('attempt-local-1')
    publish('ready.json', dict(identity=asdict(identity), initial=checked()))
    if role == 'family':
        child = subprocess.Popen([sys._base_executable, __file__, str(directory), str(output),
                                  'descendant'])
        wait('release')
        child.wait(timeout=5)
    elif role == 'hold':
        wait('enter')
        try:
            with host.current_runner_guard('attempt-local-1', **kwargs):
                publish('held.json', {'pid': os.getpid()})
                wait('release')
                if (output / 'raise').exists():
                    raise RuntimeError('controlled body failure')
        except RuntimeError:
            publish('released.json', {'body_exception': True})
    else:
        wait('check')
        publish('after.json', checked())
    publish('done.json', {'pid': os.getpid()})
"""


def observed(path, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return json.loads(path.read_text())
        time.sleep(0.01)
    pytest.fail(f"Missing real child observation: {path}")


@contextmanager
def child_host(tmp_path, role="check"):
    host = RunnerHost(tmp_path / "host")
    output = tmp_path / "observations"
    output.mkdir()
    script = tmp_path / "owned_child.py"
    script.write_text(f"import sys\nsys.path[:0] = {sys.path!r}\n" + _PROGRAM)
    spec = ProcessSpec(
        (sys._base_executable, str(script), str(host.directory), str(output), role), tmp_path, 25
    )
    host.prepare(manifest(), "independent-owned-child", spec)
    activation = authorize(host)
    host.start("independent-owned-child", activation)
    try:
        ready = observed(output / "ready.json")
        assert ready["initial"]["decision"] == "allowed", ready
        yield host, output, ready, spec, activation
    finally:
        (output / "release").touch()
        (output / "check").touch()
        host.cancel(manifest().id, "independent-cleanup")


def test_same_group_grandchild_is_denied_both_registration_and_runner_authority(tmp_path):
    with child_host(tmp_path, "family") as (host, output, ready, _, _):
        grandchild = observed(output / "descendant.json")
        members = {item.pid for item in host.inspect(manifest().id).processes}
        assert ready["identity"]["pid"] in members
        assert grandchild["pid"] in members
        assert grandchild["pid"] != ready["identity"]["pid"]
        assert grandchild["decision"] == "RUNNER_IDENTITY_NOT_CURRENT"
        assert grandchild["registration"] == "RUNNER_IDENTITY_NOT_CURRENT"


@pytest.mark.parametrize("change", ["disable", "cancel"])
def test_public_control_and_cancellation_wait_until_actual_child_guard_releases(tmp_path, change):
    with child_host(tmp_path, "hold") as (host, output, _, _, _):
        (output / "enter").touch()
        observed(output / "held.json")
        started = Event()

        def withdraw():
            started.set()
            if change == "cancel":
                return host.cancel(manifest().id, "independent-cancel")
            host.set_control(
                manifest().id,
                fence=1,
                authorization_ref=manifest().authorization_ref,
                dispatch_enabled=False,
            )

        with ThreadPoolExecutor(max_workers=1) as executor:
            update = executor.submit(withdraw)
            assert started.wait(timeout=2)
            try:
                with pytest.raises(TimeoutError):
                    update.result(timeout=0.15)
            finally:
                (output / "release").touch()
            update.result(timeout=5)
        with pytest.raises(LaunchDenied, match="CAPTURE_FENCE_NOT_CURRENT"):
            with host.current_fence_guard(
                manifest().id, fence=1, authorization_ref=manifest().authorization_ref
            ):
                pytest.fail("Committed withdrawal left writer authority current")


def test_exception_from_child_guard_releases_lock_without_mutating_control(tmp_path):
    with child_host(tmp_path, "hold") as (host, output, _, _, _):
        (output / "enter").touch()
        observed(output / "held.json")
        (output / "raise").touch()
        (output / "release").touch()
        assert observed(output / "released.json") == {"body_exception": True}
        # The real child unwound its transaction. A fresh public write succeeds.
        host.set_control(
            manifest().id,
            fence=1,
            authorization_ref=manifest().authorization_ref,
            dispatch_enabled=True,
        )
        observed(output / "done.json")


@pytest.mark.parametrize("change", ["fence", "authorization", "disabled"])
def test_direct_child_revalidates_public_control_after_registration(tmp_path, change):
    with child_host(tmp_path) as (host, output, _, _, _):
        host.set_control(
            manifest().id,
            fence=2 if change == "fence" else 1,
            authorization_ref="another-approval"
            if change == "authorization"
            else manifest().authorization_ref,
            dispatch_enabled=change != "disabled",
        )
        (output / "check").touch()
        assert observed(output / "after.json")["decision"] == "CAPTURE_FENCE_NOT_CURRENT"


def test_lost_live_supervisor_cannot_leave_a_child_authorized(tmp_path):
    with child_host(tmp_path) as (host, output, ready, _, _):
        supervisor = host.inspect(manifest().id).supervisor
        assert supervisor is not None and supervisor.pid != os.getpid()
        # This exact PID came from our own public Host launch, never from a scan.
        os.kill(supervisor.pid, signal.SIGTERM)
        deadline = time.monotonic() + 5
        while observe_process(supervisor) == "running" and time.monotonic() < deadline:
            time.sleep(0.01)
        assert observe_process(supervisor) != "running"
        (output / "check").touch()
        child = ProcessIdentity(**ready["identity"])
        after = output / "after.json"
        deadline = time.monotonic() + 5
        while (
            not after.exists()
            and observe_process(child) == "running"
            and (time.monotonic() < deadline)
        ):
            time.sleep(0.01)
        if after.exists():
            assert observed(after)["decision"] == "RUNNER_CONTAINMENT_UNPROVEN"
            print("supervisor_loss_result: live child refused authority")
            observed(output / "done.json")
        else:
            assert observe_process(child) == "exited"
            print("supervisor_loss_result: registered child also observed exited")


def test_unprovable_supervisor_identity_is_rejected_by_live_registered_child(tmp_path):
    with child_host(tmp_path) as (host, output, _, _, _):
        # Deliberate corruption port: the real supervisor remains alive, but its
        # stored incarnation can no longer establish containment.
        with sqlite3.connect(host.database) as db:
            db.execute("UPDATE executions SET supervisor_birth='unproven-incarnation'")
        (output / "check").touch()
        assert observed(output / "after.json")["decision"] == "RUNNER_CONTAINMENT_UNPROVEN"
        observed(output / "done.json")


def test_legacy_execution_schema_migration_preserves_accepted_start_without_respawn(tmp_path):
    host = RunnerHost(tmp_path / "legacy")
    output = tmp_path / "start-count.txt"
    script = f"from pathlib import Path; Path({str(output)!r}).open('a').write('started\\n')"
    spec = ProcessSpec((sys._base_executable, "-c", script), tmp_path)
    host.prepare(manifest(), "legacy-start", spec)
    activation = authorize(host)
    host.start("legacy-start", activation)
    deadline = time.monotonic() + 8
    while (not output.exists() or host.inspect(manifest().id).state != "exited") and (
        time.monotonic() < deadline
    ):
        time.sleep(0.01)
    assert output.read_text() == "started\n"
    original = asdict(host.inspect(manifest().id))
    # Deliberate schema fixture: remove only the columns absent from the base.
    with sqlite3.connect(host.database) as db:
        db.execute("ALTER TABLE executions DROP COLUMN runner_pid")
        db.execute("ALTER TABLE executions DROP COLUMN runner_birth")
    migrated = RunnerHost(host.directory)
    assert asdict(migrated.prepare(manifest(), "legacy-start", spec)) == original
    assert asdict(migrated.start("legacy-start", activation)) == original
    assert output.read_text() == "started\n"
    with pytest.raises(LaunchDenied, match="RUNNER_IDENTITY_NOT_CURRENT"):
        with migrated.current_runner_guard(
            manifest().id, fence=1, authorization_ref=manifest().authorization_ref
        ):
            pytest.fail("Legacy missing child identity was promoted")


def test_missing_ledger_read_handshake_does_not_create_replacement_database(tmp_path):
    host = RunnerHost(tmp_path / "host # % 演示")
    saved = host.database.with_name("retained.sqlite3")
    host.database.rename(saved)
    before = saved.read_bytes()
    with pytest.raises(sqlite3.Error):
        host.wait_for_runner_registration("missing", timeout_seconds=0.01)
    assert not host.database.exists()
    assert saved.read_bytes() == before
