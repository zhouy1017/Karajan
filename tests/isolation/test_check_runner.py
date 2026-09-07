"""Public real Linux Check execution; no model or Profile qualification is implied."""

import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Event

import pytest
from karajan.candidates import CandidateStore
from karajan.isolation.check_runner import FixedCheckRunner, PythonCheckEnvironment
from karajan.routing.compiler import digest

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Fixed Linux Python environment")


@pytest.fixture(scope="module")
def environment(tmp_path_factory):
    return PythonCheckEnvironment.provision(tmp_path_factory.mktemp("python-image") / "image")


def candidate(tmp_path, code, environment, *, max_log_bytes=65536, timeout=20):
    repository = tmp_path / "repository"
    repository.mkdir()
    for args in (
        ["init", "-q"],
        ["config", "user.name", "Synthetic"],
        ["config", "user.email", "fixture@example.invalid"],
    ):
        subprocess.run(["git", "-C", str(repository), *args], check=True, capture_output=True)
    (repository / "check.py").write_text(code)
    (repository / "untouched.bin").write_bytes(b"\x00\xff\x01")
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repository), "commit", "-qm", "synthetic"], check=True)
    base = subprocess.check_output(
        ["git", "-C", str(repository), "rev-parse", "HEAD"], text=True
    ).strip()
    store = CandidateStore(tmp_path / "candidates")
    baseline = store.register_baseline(repository, repository_identity="synthetic", base_sha=base)
    image_source = environment.source()
    env = {
        "id": "python",
        "revision": 1,
        "runtime_kind": "python312-stdlib",
        "platform": "linux_x64",
        "source_sha256": image_source["environment_sha256"],
        "filesystem": "candidate_copy",
        "network": "none",
        "env": {},
        "max_log_bytes": max_log_bytes,
    }
    argv = ["python", "check.py"]
    author = {
        "attempt_id": "synthetic-writer",
        "fence": 1,
        "profile_id": "synthetic",
        "profile_revision": 1,
        "model_family": "fixture",
        "context_id": "fixture",
        "provenance_ref": "fixture",
    }
    frozen = store.freeze(
        repository,
        {
            "series_id": "synthetic/task",
            "baseline_id": baseline["id"],
            "input_sha256": "a" * 64,
            "allowed_paths": ["check.py"],
            "task_class": "T1",
            "writer": {
                "attempt_id": "synthetic-writer",
                "fence": 1,
                "stopped": True,
                "observation_ref": "fixture",
            },
            "authors": [author],
            "policy": {
                "id": "checks",
                "revision": 1,
                "checks": [
                    {
                        "id": "test",
                        "revision": 1,
                        "argv": argv,
                        "environment_sha256": env["source_sha256"],
                    }
                ],
                "review": {"revision": 1, "environment_sha256": "b" * 64, "approved_reviewers": []},
            },
        },
    )
    runner = FixedCheckRunner(
        tmp_path / "results", store, environments={("python", 1): environment}
    )
    now = time.time()
    identity = {
        key: frozen[key]
        for key in (
            "id",
            "series_id",
            "revision",
            "repository_identity",
            "base_sha",
            "tree_sha",
            "content_sha256",
            "manifest_sha256",
            "input_sha256",
            "policy_sha256",
        )
    }
    identity["baseline_id"] = baseline["id"]
    execution = {
        "schema_version": "karajan.candidate-check-execution.v1",
        "check_run_id": "check:synthetic",
        "attempt_id": "check-attempt:synthetic",
        "fence": 1,
        "start_key": "check-start:synthetic",
        "activation_key": "check-activate:synthetic",
        "authorization_ref": "synthetic-approval",
        "evidence_key": "check-evidence:synthetic",
        "budget_ref": "synthetic-budget",
        "run_id": "synthetic-run",
        "operation_id": "synthetic-worker",
        "root_task_id": "synthetic-task",
        "subject_digest": digest(identity),
        "candidate": identity,
        "check": {
            "id": "test",
            "revision": 1,
            "argv": argv,
            "environment_ref": {"id": "python", "revision": 1},
            "timeout_seconds": timeout,
        },
        "environment": env,
        "source": {"runner": runner.source(env), "controller": {"fixture": True}},
        "effective_timeout_seconds": timeout,
        "claimed_at": now,
        "deadline": now + timeout,
    }
    return runner, execution, frozen


@contextmanager
def allowed_start():
    """Explicit test-only business guard; production fixed Host child supplies current locks."""
    yield


def test_real_check_logs_and_restart_preserve_single_execution(environment, tmp_path):
    runner, execution, frozen = candidate(
        tmp_path,
        (
            "import pathlib, sys\n"
            "assert pathlib.Path('untouched.bin').read_bytes() == bytes([0,255,1])\n"
            "pathlib.Path('scratch.txt').write_text('only in copy')\n"
            "print('real-check-output', flush=True)\n"
            "print('real-check-error-stream', file=sys.stderr, flush=True)\n"
        ),
        environment,
    )
    observed = runner.run(execution, start_guard=allowed_start, cancelled=lambda: False)
    assert observed.outcome == "completed" and observed.exit_code == 0
    assert observed.local_stop == "confirmed" and observed.log_complete
    log = runner.read_log(execution, observed)
    assert b"real-check-output" in log and b"real-check-error-stream" in log
    assert observed.log_sha256 == hashlib.sha256(log).hexdigest()
    reopened = FixedCheckRunner(
        runner.directory, runner.candidates, environments={("python", 1): environment}
    )
    assert reopened.inspect(execution) == observed
    assert (
        reopened.run(
            execution,
            start_guard=lambda: pytest.fail("must not start twice"),
            cancelled=lambda: False,
        )
        == observed
    )
    exported = tmp_path / "exported"
    runner.candidates.materialize(frozen["id"], exported)
    assert not (exported / "scratch.txt").exists()
    assert (exported / "untouched.bin").read_bytes() == b"\x00\xff\x01"


def test_incomplete_candidate_binding_is_rejected_before_claim(environment, tmp_path):
    runner, execution, _ = candidate(tmp_path, "print('must not execute')", environment)
    del execution["candidate"]["input_sha256"]
    with pytest.raises(ValueError, match="CHECK_CANDIDATE_IDENTITY_CONFLICT"):
        runner.run(
            execution,
            start_guard=lambda: pytest.fail("must reject before effect gate"),
            cancelled=lambda: False,
        )
    assert not runner.directory.exists()


def test_real_sandbox_denies_host_credentials_control_network_and_escalation(
    environment, tmp_path, monkeypatch
):
    protected = tmp_path / "private-controller"
    protected.mkdir()
    targets = [
        protected / name for name in ("credentials", "control.sqlite", "delivery.py", "git-config")
    ]
    for target in targets:
        target.write_text("synthetic-private-host-canary")
    monkeypatch.setenv("SYNTHETIC_HOST_SECRET", "not inherited")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    code = (
        "import os, pathlib, socket, subprocess\n"
        f"targets = {json.dumps([str(path) for path in targets])}\n"
        "for target in targets:\n"
        "    try: pathlib.Path(target).read_bytes()\n"
        "    except OSError: pass\n"
        "    else: raise AssertionError('host read')\n"
        "assert 'SYNTHETIC_HOST_SECRET' not in os.environ\n"
        "assert not pathlib.Path('/mnt/c').exists()\n"
        "assert not pathlib.Path('/usr/bin/git').exists()\n"
        "assert not pathlib.Path('/bin/sh').exists()\n"
        "try: pathlib.Path('/usr/bin/python').write_bytes(b'changed')\n"
        "except OSError: pass\n"
        "else: raise AssertionError('image writable')\n"
        f"try: socket.create_connection(('127.0.0.1', {port}), timeout=.2)\n"
        "except OSError: pass\n"
        "else: raise AssertionError('host network')\n"
        "status = pathlib.Path('/proc/self/status').read_text()\n"
        "assert 'CapEff:\\t0000000000000000' in status\n"
        "assert 'CapBnd:\\t0000000000000000' in status\n"
        "assert 'NoNewPrivs:\\t1' in status\n"
        "pathlib.Path('escape').symlink_to(targets[0])\n"
        "try: pathlib.Path('escape').read_bytes()\n"
        "except OSError: pass\n"
        "else: raise AssertionError('symlink escape')\n"
        "print('all-real-isolation-checks-passed')\n"
    )
    try:
        runner, execution, _ = candidate(tmp_path, code, environment)
        observed = runner.run(execution, start_guard=allowed_start, cancelled=lambda: False)
        assert observed.outcome == "completed" and observed.exit_code == 0, runner.read_log(
            execution, observed
        )
        assert b"all-real-isolation-checks-passed" in runner.read_log(execution, observed)
        assert all(path.read_text() == "synthetic-private-host-canary" for path in targets)
        listener.settimeout(0.05)
        with pytest.raises(TimeoutError):
            listener.accept()
    finally:
        listener.close()


@pytest.mark.parametrize("mode", ["nonzero", "timeout", "limit"])
def test_actual_failure_is_never_a_passed_complete_check(environment, tmp_path, mode):
    code = {
        "nonzero": "print('passed is only text'); raise SystemExit(7)",
        "timeout": "import time; print('started',flush=True); time.sleep(20)",
        "limit": "print('x' * 100000, flush=True)",
    }[mode]
    runner, execution, _ = candidate(
        tmp_path,
        code,
        environment,
        max_log_bytes=1024 if mode == "limit" else 65536,
        timeout=20,
    )
    if mode == "timeout":
        # Test execution timeout after Popen, allowing independent setup/source reads.
        execution["effective_timeout_seconds"] = 0.5
    observed = runner.run(execution, start_guard=allowed_start, cancelled=lambda: False)
    assert observed.local_stop == "confirmed", observed
    if mode == "nonzero":
        assert observed.outcome == "completed" and observed.exit_code == 7
        assert observed.log_complete
    elif mode == "timeout":
        assert observed.outcome == "timed_out" and observed.exit_code != 0
    else:
        assert observed.outcome == "unknown" and not observed.log_complete
        assert observed.log_size <= 1024
        assert "CHECK_LOG_LIMIT_EXCEEDED" in observed.reason_codes


def test_cancel_poll_is_outside_business_guard_and_stops_owned_namespace(environment, tmp_path):
    runner, execution, _ = candidate(
        tmp_path, "import time; print('running',flush=True); time.sleep(20)", environment
    )
    held, started = [], []

    @contextmanager
    def guarded():
        held.append(True)
        try:
            yield
        finally:
            held.clear()
            started.append(time.monotonic())

    def cancel_later():
        assert not held, "poll must not reenter business locks"
        return bool(started) and time.monotonic() - started[0] > 0.5

    observed = runner.run(execution, start_guard=guarded, cancelled=cancel_later)
    assert observed.outcome == "cancelled" and observed.local_stop == "confirmed"
    assert b"running" in runner.read_log(execution, observed)


def test_silent_success_has_a_complete_trusted_log_for_candidate_evidence(environment, tmp_path):
    runner, execution, frozen = candidate(tmp_path, "pass", environment)
    observed = runner.run(execution, start_guard=allowed_start, cancelled=lambda: False)
    evidence = runner.candidates.record_check(
        {
            "evidence_key": execution["evidence_key"],
            "candidate_id": frozen["id"],
            "policy_sha256": frozen["policy_sha256"],
            "input_sha256": frozen["input_sha256"],
            "environment_sha256": observed.environment_sha256,
            "observation_ref": observed.observation_ref,
            "provenance": "trusted_observation",
            "check_id": "test",
            "check_revision": 1,
            "executor_ref": observed.executor_ref,
            "exit_code": observed.exit_code,
            "outcome": observed.outcome,
        },
        log=runner.read_log(execution, observed),
    )
    assert evidence["status"] == "passed"
    assert runner.read_log(execution, observed) == b"karajan-check-log.v1\nmerged_stdout_stderr:\n"


def test_environment_reference_must_match_approved_check(environment, tmp_path):
    runner, execution, _ = candidate(tmp_path, "pass", environment)
    execution["check"]["environment_ref"]["id"] = "another-environment"
    with pytest.raises(ValueError, match="CHECK_SPEC_NOT_APPROVED"):
        runner.run(
            execution,
            start_guard=lambda: pytest.fail("must reject before effect gate"),
            cancelled=lambda: False,
        )
    assert not runner.directory.exists()


def test_snapshot_changed_after_materialization_cannot_produce_passed_evidence(
    environment, tmp_path
):
    runner, execution, _ = candidate(tmp_path, "print('approved program')", environment)

    @contextmanager
    def corrupt_before_start():
        snapshot = next(runner.directory.glob("*/snapshot/check.py"))
        snapshot.write_text("print('unapproved replacement')")
        yield

    observed = runner.run(execution, start_guard=corrupt_before_start, cancelled=lambda: False)
    assert observed.exit_code != 0 or observed.outcome != "completed"
    assert b"unapproved replacement" not in runner.read_log(execution, observed)


def test_lost_actual_popen_reply_never_claims_not_started_or_launches_again(
    environment, tmp_path, monkeypatch
):
    runner, execution, _ = candidate(tmp_path, "import time; time.sleep(10)", environment)
    original = subprocess.Popen
    children = []

    def lost_reply(*args, **kwargs):
        children.append(original(*args, **kwargs))
        raise OSError("synthetic lost process return")

    monkeypatch.setattr(subprocess, "Popen", lost_reply)
    try:
        observed = runner.run(execution, start_guard=allowed_start, cancelled=lambda: False)
        assert observed.outcome == "unknown" and observed.local_stop != "not_started"
        reopened = FixedCheckRunner(runner.directory, runner.candidates, environments={})
        assert (
            reopened.run(
                execution,
                start_guard=lambda: pytest.fail("no second launch"),
                cancelled=lambda: False,
            )
            == observed
        )
        assert len(children) == 1
    finally:
        time.sleep(0.1)
        runner.cancel(execution)
        for process in children:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)


def test_lost_result_commit_reply_recovers_readonly_and_log_corruption_blocks(
    environment, tmp_path, monkeypatch
):
    runner, execution, _ = candidate(tmp_path, "print('durable output')", environment)
    original = os.replace

    def lost_reply(source, target):
        original(source, target)
        if Path(target).name == "result.json":
            raise OSError("synthetic lost result reply")

    monkeypatch.setattr(os, "replace", lost_reply)
    with pytest.raises(OSError, match="synthetic lost result reply"):
        runner.run(execution, start_guard=allowed_start, cancelled=lambda: False)
    reopened = FixedCheckRunner(runner.directory, runner.candidates, environments={})
    observed = reopened.inspect(execution)
    assert observed is not None and observed.exit_code == 0 and observed.log_complete
    assert (
        reopened.run(
            execution, start_guard=lambda: pytest.fail("must not rerun"), cancelled=lambda: False
        )
        == observed
    )
    log = next(runner.directory.glob("*/output.log"))
    log.write_bytes(b"forged passed log")
    damaged = reopened.inspect(execution)
    assert damaged.outcome == "unknown" and not damaged.log_complete
    assert reopened.read_log(execution, observed) is None


def test_two_callers_and_cancel_share_one_claim_before_actual_popen(environment, tmp_path):
    runner, execution, _ = candidate(
        tmp_path, "raise AssertionError('must not start')", environment
    )
    entered, release = Event(), Event()

    @contextmanager
    def hold_before_spawn():
        entered.set()
        assert release.wait(3)
        yield

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            runner.run, execution, start_guard=hold_before_spawn, cancelled=lambda: False
        )
        assert entered.wait(3)
        other = FixedCheckRunner(runner.directory, runner.candidates, environments={})
        duplicate = other.run(
            execution, start_guard=lambda: pytest.fail("no second start"), cancelled=lambda: False
        )
        assert duplicate.outcome == "unknown" and duplicate.local_stop == "unknown"
        assert other.cancel(execution) is None
        release.set()
        observed = future.result(timeout=3)
    assert observed.local_stop == "not_started" and observed.outcome == "unknown"
    assert not list(runner.directory.glob("*/spawn-intent.json"))


def test_missing_result_after_claim_does_not_recreate_start(environment, tmp_path, monkeypatch):
    runner, execution, _ = candidate(tmp_path, "pass", environment)
    original = os.replace

    def lose_claim(source, target):
        original(source, target)
        if Path(target).name == "claim.json":
            raise OSError("synthetic committed claim reply lost")

    monkeypatch.setattr(os, "replace", lose_claim)
    with pytest.raises(OSError, match="synthetic committed claim"):
        runner.run(
            execution, start_guard=lambda: pytest.fail("never started"), cancelled=lambda: False
        )
    reopened = FixedCheckRunner(runner.directory, runner.candidates, environments={})
    before = {
        str(p.relative_to(runner.directory)): p.read_bytes()
        for p in runner.directory.rglob("*")
        if p.is_file()
    }
    assert reopened.inspect(execution) is None
    result = reopened.run(
        execution, start_guard=lambda: pytest.fail("no replay launch"), cancelled=lambda: False
    )
    assert result.outcome == "unknown" and result.local_stop == "unknown"
    assert before == {
        str(p.relative_to(runner.directory)): p.read_bytes()
        for p in runner.directory.rglob("*")
        if p.is_file()
    }


def test_source_change_and_unknown_environment_are_zero_claim_rejections(environment, tmp_path):
    runner, execution, _ = candidate(tmp_path, "pass", environment)
    execution["environment"]["source_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="CHECK_ENVIRONMENT_SOURCE_CHANGED"):
        runner.run(execution, start_guard=lambda: pytest.fail("no effect"), cancelled=lambda: False)
    assert not runner.directory.exists()
    execution["environment"]["runtime_kind"] = "unsupported-generic-container"
    with pytest.raises(ValueError, match="CHECK_ENVIRONMENT_UNSUPPORTED"):
        runner.run(execution, start_guard=lambda: pytest.fail("no effect"), cancelled=lambda: False)
    assert not runner.directory.exists()


def test_real_asset_mutation_cannot_reuse_approved_environment(environment, tmp_path):
    runner, execution, _ = candidate(tmp_path, "pass", environment)
    asset = environment.root / "usr/bin/python"
    original = asset.read_bytes()
    try:
        asset.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
        with pytest.raises(ValueError, match="CHECK_ENVIRONMENT_SOURCE_CHANGED"):
            runner.run(
                execution, start_guard=lambda: pytest.fail("no effect"), cancelled=lambda: False
            )
        assert not runner.directory.exists()
    finally:
        asset.write_bytes(original)


def test_candidate_timeout_cannot_borrow_host_cleanup_allowance(environment, tmp_path):
    runner, execution, _ = candidate(tmp_path, "pass", environment, timeout=1)
    execution["timeout_seconds"] = 31
    execution["effective_timeout_seconds"] = 31
    with pytest.raises(ValueError, match="CHECK_TIMEOUT_INVALID"):
        runner.run(execution, start_guard=lambda: pytest.fail("no effect"), cancelled=lambda: False)
    assert not runner.directory.exists()


def test_refused_business_guard_has_no_popen_and_does_not_echo_arbitrary_errors(
    environment, tmp_path
):
    runner, execution, _ = candidate(tmp_path, "pass", environment)

    @contextmanager
    def refused():
        raise ValueError("CHECK_fake prefix includes synthetic-sensitive-value")
        yield

    observed = runner.run(execution, start_guard=refused, cancelled=lambda: False)
    assert observed.local_stop == "not_started" and observed.outcome == "unknown"
    assert observed.reason_codes == ("CHECK_EXECUTION_FAILED",)
    assert "synthetic-sensitive-value" not in repr(observed)


def test_durable_write_cannot_carry_popen_past_its_effective_deadline(
    environment, tmp_path, monkeypatch
):
    runner, execution, _ = candidate(tmp_path, "pass", environment)
    execution["effective_timeout_seconds"] = 0.05
    original = os.replace
    popen = subprocess.Popen
    started = []

    def delayed_commit(source, target):
        original(source, target)
        if Path(target).name == "spawn-intent.json":
            time.sleep(0.1)

    def counted_popen(*args, **kwargs):
        started.append(True)
        return popen(*args, **kwargs)

    monkeypatch.setattr(os, "replace", delayed_commit)
    monkeypatch.setattr(subprocess, "Popen", counted_popen)
    observed = runner.run(execution, start_guard=allowed_start, cancelled=lambda: False)
    assert not started
    assert observed.outcome == "unknown"
