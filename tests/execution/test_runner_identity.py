"""Only the exact Host-owned command child may enter a native effect boundary."""

import json
import os
import sqlite3
import sys
import time

import pytest
from karajan.execution import LaunchDenied, ProcessSpec, RunnerHost
from test_runnerhost import authorize, manifest, wait_for_exit


def wait_file(path):
    deadline = time.monotonic() + 8
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert path.exists(), f"Missing child observation: {path.name}"
    return json.loads(path.read_text())


@pytest.fixture
def runner(tmp_path):
    host = RunnerHost(tmp_path / "host")
    script = tmp_path / "runner.py"
    script.write_text(
        # Windows venv python.exe is a redirector that creates another process.
        # Launch the actual interpreter so the declared ProcessSpec child owns effects.
        f"import sys\nsys.path[:0] = {sys.path!r}\n"
        + """import json, os, subprocess, sys, time
from dataclasses import asdict
from pathlib import Path
from karajan.execution import RunnerHost, LaunchDenied
host = RunnerHost(Path(sys.argv[1]))
output = Path(sys.argv[2])
identity = host.wait_for_runner_registration('attempt-local-1')
kwargs = dict(fence=1, authorization_ref='authorization-test-1')
with host.current_runner_guard('attempt-local-1', **kwargs) as current:
    assert current == identity
    output.write_text(json.dumps(asdict(current)))
while not output.with_suffix('.continue').exists():
    time.sleep(0.02)
try:
    with host.current_runner_guard('attempt-local-1', **kwargs):
        result = 'allowed'
except LaunchDenied as error:
    result = str(error)
output.with_suffix('.after').write_text(json.dumps(result))
"""
    )
    output = tmp_path / "observed.json"
    host.prepare(
        manifest(),
        "owned-runner",
        ProcessSpec(
            (sys._base_executable, str(script), str(host.directory), str(output)), tmp_path, 20
        ),
    )
    host.start("owned-runner", authorize(host))
    try:
        yield host, output
    finally:
        host.cancel(manifest().id, "cleanup-owned-runner")


def test_direct_command_child_gets_its_persisted_pid_and_birth_but_controller_does_not(runner):
    host, output = runner
    identity = wait_file(output)
    snapshot = host.inspect(manifest().id)
    assert identity["pid"] != os.getpid()
    assert identity["pid"] != snapshot.supervisor.pid
    assert any(
        p.pid == identity["pid"] and p.birth == identity["birth"] for p in snapshot.processes
    )
    with pytest.raises(LaunchDenied, match="RUNNER_IDENTITY_NOT_CURRENT"):
        with host.current_runner_guard(
            manifest().id, fence=1, authorization_ref=manifest().authorization_ref
        ):
            pytest.fail("Controller acquired child execution authority")
    with pytest.raises(LaunchDenied, match="RUNNER_IDENTITY_NOT_CURRENT"):
        host.wait_for_runner_registration(manifest().id)
    output.with_suffix(".continue").touch()
    assert wait_file(output.with_suffix(".after")) == "allowed"
    wait_for_exit(host)


@pytest.mark.parametrize("change", ["control", "birth"])
def test_child_rechecks_current_control_and_exact_birth_on_every_guard(runner, change):
    host, output = runner
    wait_file(output)
    if change == "control":
        host.set_control(
            manifest().id,
            fence=1,
            authorization_ref=manifest().authorization_ref,
            dispatch_enabled=False,
        )
        reason = "CAPTURE_FENCE_NOT_CURRENT"
    else:
        # Deliberate ledger corruption; a matching PID is insufficient authority.
        with sqlite3.connect(host.database) as connection:
            connection.execute("UPDATE executions SET runner_birth='wrong-incarnation'")
        reason = "RUNNER_IDENTITY_NOT_CURRENT"
    output.with_suffix(".continue").touch()
    assert wait_file(output.with_suffix(".after")) == reason
    wait_for_exit(host)


def test_prepared_only_and_missing_ledger_never_register_a_runner(tmp_path):
    host = RunnerHost(tmp_path / "host # percent%")
    host.prepare(manifest(), "prepared", ProcessSpec((sys.executable, "-c", "pass"), tmp_path))
    before = host.database.read_bytes()
    with pytest.raises(LaunchDenied, match="RUNNER_REGISTRATION_UNPROVEN"):
        host.wait_for_runner_registration(manifest().id, timeout_seconds=0.02)
    assert host.database.read_bytes() == before
    saved = host.database.with_suffix(".saved")
    host.database.rename(saved)
    with pytest.raises(sqlite3.Error):
        host.wait_for_runner_registration(manifest().id, timeout_seconds=0.02)
    assert not host.database.exists()
