import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from karajan.contracts.probe import AttemptManifest, Binding
from karajan.execution import (
    Activation,
    LaunchDenied,
    ProbeCrash,
    ProcessIdentity,
    ProcessSpec,
    RunnerHost,
    StartConflict,
    observe_process,
)


def manifest(fence: int = 1) -> AttemptManifest:
    return AttemptManifest(
        id="attempt-local-1",
        fence=fence,
        role="worker",
        profile_id="python-fixture",
        profile_revision=1,
        authorization_ref="authorization-test-1",
        budget_ref="budget-fixture-only",
        permissions=["fixture-process"],
        requested_binding=Binding(
            model_id="no-model",
            channel_id="local-fixture",
            account_id="no-account",
            runtime_kind="python-fixture",
            runtime_version="1",
            auth_mode="none",
            billing_path="subscription_only",
            native_settings={},
        ),
    )


def test_preparation_survives_restart_and_rejects_same_key_with_changed_input(
    tmp_path: Path,
) -> None:
    first = RunnerHost(tmp_path / "state")
    spec = ProcessSpec((sys.executable, "-c", "print('fixture')"), tmp_path)
    prepared = first.prepare(manifest(), "start-1", spec)
    restarted = RunnerHost(tmp_path / "state")
    replay = restarted.prepare(manifest(), "start-1", spec)
    assert replay.prepared_id == prepared.prepared_id
    assert replay.state == "prepared"
    assert replay.supervisor is None
    with pytest.raises(StartConflict):
        restarted.prepare(manifest(2), "start-1", spec)


@pytest.mark.parametrize("denial", ["paused", "old-fence", "expired", "wrong-budget"])
def test_start_requires_current_unpaused_unexpired_authorization(
    tmp_path: Path, denial: str
) -> None:
    host = RunnerHost(tmp_path / "state")
    output = tmp_path / "should-not-exist"
    spec = ProcessSpec((sys.executable, "-c", f"open({str(output)!r}, 'w').close()"), tmp_path)
    host.prepare(manifest(), "start-1", spec)
    host.set_control(
        manifest().id,
        fence=2 if denial == "old-fence" else 1,
        authorization_ref=manifest().authorization_ref,
        dispatch_enabled=denial != "paused",
    )
    activation = Activation(
        "activation-1",
        manifest().id,
        1,
        manifest().authorization_ref,
        "other-budget" if denial == "wrong-budget" else manifest().budget_ref,
        time.time() - 1 if denial == "expired" else time.time() + 60,
    )
    with pytest.raises(LaunchDenied):
        host.start("start-1", activation)
    assert host.inspect(manifest().id).state == "prepared"
    assert not output.exists()


def authorize(host: RunnerHost) -> Activation:
    host.set_control(
        manifest().id,
        fence=1,
        authorization_ref=manifest().authorization_ref,
        dispatch_enabled=True,
    )
    return Activation(
        "activation-1",
        manifest().id,
        1,
        manifest().authorization_ref,
        manifest().budget_ref,
        time.time() + 60,
    )


def wait_for_exit(host: RunnerHost, timeout: float = 10) -> None:
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if host.inspect(manifest().id).state == "exited":
            return
        time.sleep(0.02)
    pytest.fail(f"Execution did not exit: {host.inspect(manifest().id)}")


def test_same_start_survives_new_host_without_executing_command_twice(tmp_path: Path) -> None:
    host = RunnerHost(tmp_path / "state")
    output = tmp_path / "starts.txt"
    command = (
        f"from pathlib import Path; import time; "
        f"p=Path({str(output)!r}); p.open('a').write('started\\n'); time.sleep(0.2)"
    )
    host.prepare(manifest(), "start-1", ProcessSpec((sys.executable, "-c", command), tmp_path))
    activation = authorize(host)
    host.start("start-1", activation)
    restarted = RunnerHost(tmp_path / "state")
    replay = restarted.start("start-1", activation)
    assert replay.state in {"starting", "running", "exited", "unknown"}
    wait_for_exit(restarted)
    assert output.read_text() == "started\n"
    snapshot = restarted.inspect(manifest().id)
    assert snapshot.supervisor is not None
    assert snapshot.supervisor.birth
    assert snapshot.exit_code == 0
    assert snapshot.remote_stop == "unknown"


def test_cancel_stops_a_child_after_its_command_parent_has_exited(tmp_path: Path) -> None:
    host = RunnerHost(tmp_path / "state")
    heartbeat = tmp_path / "heartbeat"
    child = (
        "from pathlib import Path; import time; "
        f"p=Path({str(heartbeat)!r}); "
        "[(p.open('a').write('tick\\n'), time.sleep(0.02)) for _ in range(400)]"
    )
    command = f"import subprocess,sys; subprocess.Popen([sys.executable, '-c', {child!r}])"
    host.prepare(
        manifest(),
        "start-1",
        ProcessSpec((sys.executable, "-c", command), tmp_path, 5),
    )
    host.start("start-1", authorize(host))
    try:
        until = time.monotonic() + 5
        while not heartbeat.exists() and time.monotonic() < until:
            time.sleep(0.02)
        assert heartbeat.exists()
        assert len(host.inspect(manifest().id).processes) >= 2
        cancellation = RunnerHost(tmp_path / "state").cancel(manifest().id, "cancel-1")
        assert cancellation.status == "confirmed"
        assert cancellation.snapshot.state == "exited"
        assert cancellation.snapshot.processes == ()
        assert cancellation.snapshot.remote_stop == "unknown"
        stopped_at = heartbeat.read_bytes()
        time.sleep(0.08)
        assert heartbeat.read_bytes() == stopped_at
    finally:
        host.cancel(manifest().id, "cancel-cleanup")


@pytest.mark.parametrize("crash_at", ["after_accept", "before_spawn", "after_spawn", "after_ack"])
def test_crash_window_never_blindly_restarts_the_accepted_identity(
    tmp_path: Path,
    crash_at: str,
) -> None:
    host = RunnerHost(tmp_path / "state")
    output = tmp_path / "starts.txt"
    command = f"open({str(output)!r}, 'a').write('once\\n')"
    host.prepare(manifest(), "start-1", ProcessSpec((sys.executable, "-c", command), tmp_path))
    activation = authorize(host)
    with pytest.raises(ProbeCrash):
        host.start("start-1", activation, crash_at=crash_at)
    restarted = RunnerHost(tmp_path / "state")
    replay = restarted.start("start-1", activation)
    if crash_at in {"after_accept", "before_spawn"}:
        assert replay.state == "unknown"
        cancelled = restarted.cancel(manifest().id, "cancel-1", timeout_seconds=0)
        assert cancelled.status == "unknown"
        assert not output.exists()
    else:
        wait_for_exit(restarted)
        assert output.read_text() == "once\n"


def test_business_done_does_not_hide_a_still_running_process_from_recovery(tmp_path: Path) -> None:
    host = RunnerHost(tmp_path / "state")
    host.prepare(
        manifest(),
        "start-1",
        ProcessSpec((sys.executable, "-c", "import time; time.sleep(5)"), tmp_path, 6),
    )
    host.start("start-1", authorize(host))
    try:
        until = time.monotonic() + 5
        while host.inspect(manifest().id).state != "running" and time.monotonic() < until:
            time.sleep(0.02)
        decision = host.receive_result(manifest().id, 1, "result-1", {"candidate": "fixture-1"})
        assert decision.accepted
        recovering = RunnerHost(tmp_path / "state").reconcile()
        assert len(recovering) == 1
        assert recovering[0].business_status == "done"
        assert recovering[0].state == "running"
        assert recovering[0].processes
    finally:
        host.cancel(manifest().id, "cleanup")


@pytest.mark.parametrize("operation", ["receive_result", "record_usage"])
def test_observation_inputs_reject_boolean_fences(tmp_path: Path, operation: str) -> None:
    host = RunnerHost(tmp_path / "state")
    host.prepare(manifest(), "start-1", ProcessSpec((sys.executable, "-c", "pass"), tmp_path))
    with pytest.raises(ValueError):
        getattr(host, operation)(manifest().id, True, "event-1", {})
    snapshot = host.inspect(manifest().id)
    assert snapshot.business_status == "pending"
    assert snapshot.usage_events == ()


@pytest.mark.parametrize("concurrent", [False, True])
def test_only_one_distinct_completion_can_win_for_an_attempt(
    tmp_path: Path, concurrent: bool
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    host = RunnerHost(tmp_path / "state")
    host.prepare(manifest(), "start-1", ProcessSpec((sys.executable, "-c", "pass"), tmp_path))
    host.start("start-1", authorize(host))
    wait_for_exit(host)

    def complete(index: int) -> bool:
        return host.receive_result(
            manifest().id, 1, f"completion-{index}", {"candidate": f"candidate-{index}"}
        ).accepted

    if concurrent:
        with ThreadPoolExecutor(max_workers=8) as callers:
            decisions = list(callers.map(complete, range(8)))
    else:
        decisions = [complete(index) for index in range(8)]
    assert decisions.count(True) == 1
    winner = decisions.index(True)
    assert complete(winner)
    host.record_usage(manifest().id, 1, "late-usage", {"tokens": 3})
    assert len(host.inspect(manifest().id).usage_events) == 1


def test_repeated_reconciliation_releases_handles_without_waiting_for_garbage_collection(
    tmp_path: Path,
) -> None:
    import gc
    import os

    def handle_count() -> int:
        if os.name != "nt":
            return len(list(Path("/proc/self/fd").iterdir()))
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        count = wintypes.DWORD()
        assert kernel.GetProcessHandleCount(kernel.GetCurrentProcess(), ctypes.byref(count))
        return count.value

    host = RunnerHost(tmp_path / "state")
    host.reconcile()
    gc.collect()
    enabled = gc.isenabled()
    gc.disable()
    try:
        before = handle_count()
        for _ in range(200):
            assert host.reconcile() == []
        assert handle_count() <= before + 4
    finally:
        if enabled:
            gc.enable()
        gc.collect()


@pytest.mark.parametrize("revocation", ["fence", "cancel"])
def test_a_late_result_cannot_revive_revoked_execution(tmp_path: Path, revocation: str) -> None:
    host = RunnerHost(tmp_path / "state")
    host.prepare(manifest(), "start-1", ProcessSpec((sys.executable, "-c", "pass"), tmp_path))
    host.start("start-1", authorize(host))
    wait_for_exit(host)
    if revocation == "fence":
        host.set_control(
            manifest().id,
            fence=2,
            authorization_ref=manifest().authorization_ref,
            dispatch_enabled=True,
        )
    else:
        host.cancel(manifest().id, "cancel-1")
    decision = host.receive_result(manifest().id, 1, "late-result", {"candidate": "stale"})
    assert not decision.accepted
    assert decision.reason == "RESULT_NOT_CURRENT"
    assert host.inspect(manifest().id).business_status != "done"


def test_late_usage_reopens_reconciliation_without_reviving_cancelled_work(tmp_path: Path) -> None:
    host = RunnerHost(tmp_path / "state")
    host.prepare(manifest(), "start-1", ProcessSpec((sys.executable, "-c", "pass"), tmp_path))
    host.start("start-1", authorize(host))
    wait_for_exit(host)
    host.cancel(manifest().id, "cancel-1")
    host.settle_usage(manifest().id, through_sequence=0)
    assert host.reconcile() == []
    host.set_control(
        manifest().id,
        fence=2,
        authorization_ref=manifest().authorization_ref,
        dispatch_enabled=False,
    )
    usage = {"quantity": 7, "unit": "fixture_ticks"}
    host.record_usage(manifest().id, 1, "usage-1", usage)
    host.record_usage(manifest().id, 1, "usage-1", usage)
    recovered = RunnerHost(tmp_path / "state").reconcile()
    assert len(recovered) == 1
    assert recovered[0].business_status == "cancelled"
    assert recovered[0].state == "exited"
    assert not recovered[0].usage_settled
    assert len(recovered[0].usage_events) == 1
    assert recovered[0].usage_events[0].fence == 1
    assert recovered[0].usage_events[0].usage == usage
    with pytest.raises(StartConflict):
        host.record_usage(manifest().id, 1, "usage-1", {"quantity": 9, "unit": "fixture_ticks"})
    host.settle_usage(manifest().id, through_sequence=1)
    assert host.reconcile() == []


@pytest.mark.parametrize("crash_at", ["after_accept", "before_spawn", "after_spawn", "after_ack"])
def test_cli_process_crash_preserves_the_no_duplicate_launch_contract(
    tmp_path: Path,
    crash_at: str,
) -> None:
    from dataclasses import asdict

    host = RunnerHost(tmp_path / "state")
    output = tmp_path / "starts.txt"
    command = f"open({str(output)!r}, 'a').write('once\\n')"
    host.prepare(manifest(), "start-1", ProcessSpec((sys.executable, "-c", command), tmp_path))
    activation = authorize(host)
    activation_file = tmp_path / "activation.json"
    activation_file.write_text(json.dumps(asdict(activation)), encoding="utf-8")
    crashed = subprocess.run(
        [
            sys.executable,
            "-m",
            "karajan.execution",
            "--state",
            str(tmp_path / "state"),
            "start",
            "start-1",
            str(activation_file),
            "--crash-at",
            crash_at,
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert crashed.returncode == 91, crashed.stderr
    restarted = RunnerHost(tmp_path / "state")
    replay = restarted.start("start-1", activation)
    if crash_at in {"after_accept", "before_spawn"}:
        assert replay.state == "unknown"
        assert not output.exists()
    else:
        wait_for_exit(restarted)
        assert output.read_text() == "once\n"


def test_cli_can_prepare_authorize_observe_and_cancel_a_local_probe(tmp_path: Path) -> None:
    from dataclasses import asdict

    def invoke(*arguments: str) -> dict[str, object]:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "karajan.execution",
                "--state",
                str(tmp_path / "state"),
                *arguments,
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        document = json.loads(completed.stdout)
        assert document["live_qualified"] is False
        return document

    prepared_file = tmp_path / "prepare.json"
    prepared_file.write_text(
        json.dumps(
            {
                "start_key": "start-1",
                "manifest": manifest().model_dump(mode="json"),
                "process": {
                    "argv": [sys.executable, "-c", "import time; time.sleep(5)"],
                    "cwd": str(tmp_path),
                    "timeout_seconds": 6,
                },
            }
        ),
        encoding="utf-8",
    )
    invoke("prepare", str(prepared_file))
    control_file = tmp_path / "control.json"
    control_file.write_text(
        json.dumps(
            {
                "attempt_id": manifest().id,
                "fence": 1,
                "authorization_ref": manifest().authorization_ref,
                "dispatch_enabled": True,
            }
        ),
        encoding="utf-8",
    )
    invoke("control", str(control_file))
    activation = Activation(
        "activation-1",
        manifest().id,
        1,
        manifest().authorization_ref,
        manifest().budget_ref,
        time.time() + 60,
    )
    activation_file = tmp_path / "activation.json"
    activation_file.write_text(json.dumps(asdict(activation)), encoding="utf-8")
    invoke("start", "start-1", str(activation_file))
    try:
        observed = invoke("inspect", manifest().id)
        assert observed["scope"] == "local_process_probe"
        cancelled = invoke("cancel", manifest().id, "cancel-1")
        assert cancelled["data"]["status"] == "confirmed"
        assert cancelled["data"]["snapshot"]["remote_stop"] == "unknown"
        assert invoke("reconcile")["data"]
    finally:
        RunnerHost(tmp_path / "state").cancel(manifest().id, "cleanup")


def test_process_observation_refuses_a_matching_pid_with_different_creation_identity(
    tmp_path: Path,
) -> None:
    host = RunnerHost(tmp_path / "state")
    host.prepare(
        manifest(),
        "start-1",
        ProcessSpec((sys.executable, "-c", "import time; time.sleep(5)"), tmp_path, 6),
    )
    host.start("start-1", authorize(host))
    try:
        until = time.monotonic() + 5
        snapshot = host.inspect(manifest().id)
        while snapshot.supervisor is None and time.monotonic() < until:
            time.sleep(0.02)
            snapshot = host.inspect(manifest().id)
        assert snapshot.supervisor is not None
        current = snapshot.supervisor
        assert observe_process(current) == "running"
        assert (
            observe_process(ProcessIdentity(current.pid, "different-birth")) == "identity_mismatch"
        )
        assert observe_process(current) == "running"
    finally:
        host.cancel(manifest().id, "cleanup")


@pytest.mark.parametrize("timeout", [float("nan"), float("inf")])
def test_nonfinite_process_limits_are_rejected_before_preparation(
    tmp_path: Path,
    timeout: float,
) -> None:
    host = RunnerHost(tmp_path / "state")
    with pytest.raises(ValueError):
        host.prepare(
            manifest(),
            "start-1",
            ProcessSpec((sys.executable, "-c", "pass"), tmp_path, timeout),
        )
    assert host.reconcile() == []


@pytest.mark.parametrize("limit", [float("nan"), float("inf")])
@pytest.mark.parametrize("operation", ["start", "cancel"])
def test_nonfinite_control_limits_are_rejected_without_changing_prepared_state(
    tmp_path: Path,
    limit: float,
    operation: str,
) -> None:
    from dataclasses import replace

    host = RunnerHost(tmp_path / "state")
    host.prepare(manifest(), "start-1", ProcessSpec((sys.executable, "-c", "pass"), tmp_path))
    activation = authorize(host)
    with pytest.raises(ValueError):
        if operation == "start":
            host.start("start-1", replace(activation, expires_at=limit))
        else:
            host.cancel(manifest().id, "cancel-1", timeout_seconds=limit)
    assert host.inspect(manifest().id).state == "prepared"


def test_concurrent_start_delivery_creates_only_one_local_execution(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    host = RunnerHost(tmp_path / "state")
    output = tmp_path / "starts.txt"
    command = f"open({str(output)!r}, 'a').write('once\\n')"
    host.prepare(manifest(), "start-1", ProcessSpec((sys.executable, "-c", command), tmp_path))
    activation = authorize(host)
    with ThreadPoolExecutor(max_workers=8) as callers:
        receipts = list(callers.map(lambda _: host.start("start-1", activation), range(8)))
    assert {receipt.prepared_id for receipt in receipts} == {"start-1"}
    wait_for_exit(host)
    assert output.read_text() == "once\n"


def test_the_supervisor_enforces_the_process_deadline_without_a_cancel_request(
    tmp_path: Path,
) -> None:
    host = RunnerHost(tmp_path / "state")
    marker = tmp_path / "started"
    command = f"import time; open({str(marker)!r}, 'w').close(); time.sleep(30)"
    host.prepare(
        manifest(),
        "start-1",
        ProcessSpec((sys.executable, "-c", command), tmp_path, 1),
    )
    host.start("start-1", authorize(host))
    try:
        wait_for_exit(host, timeout=6)
        assert marker.exists()
        assert host.inspect(manifest().id).processes == ()
        assert host.inspect(manifest().id).remote_stop == "unknown"
    finally:
        host.cancel(manifest().id, "cleanup")


@pytest.mark.parametrize("argv", ["not-an-argument-array", [42], [""]])
def test_cli_rejects_malformed_argument_vectors_before_preparation(
    tmp_path: Path,
    argv: object,
) -> None:
    document = tmp_path / "prepare.json"
    document.write_text(
        json.dumps(
            {
                "start_key": "start-1",
                "manifest": manifest().model_dump(mode="json"),
                "process": {"argv": argv, "cwd": str(tmp_path), "timeout_seconds": 1},
            }
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "karajan.execution",
            "--state",
            str(tmp_path / "state"),
            "prepare",
            str(document),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 1
    assert json.loads(result.stdout)["error"] == "PROBE_COMMAND_REJECTED"
    assert RunnerHost(tmp_path / "state").reconcile() == []


@pytest.mark.parametrize(
    ("command", "field", "value"),
    [
        ("control", "fence", True),
        ("control", "dispatch_enabled", "1"),
        ("control", "dispatch_enabled", "0"),
        ("start", "fence", True),
        ("start", "id", ""),
        ("start", "attempt_id", ""),
        ("start", "authorization_ref", ""),
    ],
)
def test_cli_rejects_coerced_or_empty_control_identities_before_mutation(
    tmp_path: Path, command: str, field: str, value: object
) -> None:
    from dataclasses import asdict

    host = RunnerHost(tmp_path / "state")
    host.prepare(manifest(), "start-1", ProcessSpec((sys.executable, "-c", "pass"), tmp_path))
    activation = authorize(host)
    payload = (
        asdict(activation)
        if command == "start"
        else {
            "attempt_id": manifest().id,
            "fence": 1,
            "authorization_ref": manifest().authorization_ref,
            "dispatch_enabled": True,
        }
    )
    payload[field] = value
    document = tmp_path / "input.json"
    document.write_text(json.dumps(payload), encoding="utf-8")
    arguments = (
        [command, "start-1", str(document)] if command == "start" else [command, str(document)]
    )
    result = subprocess.run(
        [sys.executable, "-m", "karajan.execution", "--state", str(tmp_path / "state"), *arguments],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    try:
        assert result.returncode == 1
        assert json.loads(result.stdout)["error"] == "PROBE_COMMAND_REJECTED"
        assert host.inspect(manifest().id).state == "prepared"
        if command == "control":
            host.start("start-1", activation)
            wait_for_exit(host)
    finally:
        host.cancel(manifest().id, "cleanup")
