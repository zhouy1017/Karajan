"""Independent negative CLI controls; only local synthetic credential files."""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from karajan.adapters.opencode.go_journal import GoCallJournal

WORKTREE = Path(__file__).resolve().parents[2]
ENTRYPOINT = WORKTREE / "examples/opencode-go-isolated/run_live.py"


def load_entrypoint():
    specification = importlib.util.spec_from_file_location("independent_go_cli", ENTRYPOINT)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def arguments(directory, credential, *switches):
    return [
        str(ENTRYPOINT),
        *switches,
        "--runtime",
        str(directory.parent / "unreadable-runtime"),
        "--credential-file",
        str(credential),
        "--directory",
        str(directory),
        "--scenario",
        "denied_read",
    ]


def test_no_live_in_real_subprocess_has_no_local_effects(tmp_path):
    output = tmp_path / "no-effects"
    child = subprocess.run(
        [sys.executable, *arguments(output, tmp_path / "absent-credential")],
        cwd=WORKTREE,
        env={**os.environ, "PYTHONPATH": str(WORKTREE / "backend")},
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert child.returncode == 1
    assert json.loads(child.stdout) == {
        "status": "not_run",
        "reason": "LIVE_AUTHORIZATION_REQUIRED",
    }
    assert child.stderr == ""
    assert list(tmp_path.iterdir()) == []


def test_existing_directory_rejection_precedes_source_and_credential_reads(
    tmp_path, monkeypatch, capsys
):
    module = load_entrypoint()
    output = tmp_path / "old"
    output.mkdir()
    sentinel = output / "start.json"
    sentinel.write_text("original-controller-identity")
    monkeypatch.setattr(sys, "argv", arguments(output, tmp_path / "absent", "--live"))

    def forbidden(*_):
        pytest.fail("Rejected reuse must not inspect runtime or credentials")

    monkeypatch.setattr(module, "go_runtime_source", forbidden)
    assert module.main() == 1
    assert json.loads(capsys.readouterr().out) == {"status": "failed", "error_type": "ValueError"}
    assert sentinel.read_text() == "original-controller-identity"
    assert list(output.iterdir()) == [sentinel]


def test_observer_exception_revokes_the_persisted_grant_without_echoing_secret(
    tmp_path, monkeypatch, capsys
):
    module = load_entrypoint()
    secret = "synthetic-only-upstream-secret-for-cli-review"
    credential = tmp_path / "synthetic-fixture.txt"
    credential.write_text(secret)
    output = tmp_path / "failed-observation"
    monkeypatch.setattr(sys, "argv", arguments(output, credential, "--live"))
    monkeypatch.setattr(module, "go_runtime_source", lambda _: {"synthetic_source": True})

    def failed_observer(runtime, directory, actual_secret, authorization, *, scenario):
        assert actual_secret == secret
        assert (output / "start.json").is_file()
        assert authorization.journal.snapshot(authorization.grant_id)["request_count"] == 0
        raise ValueError(secret)

    monkeypatch.setattr(module, "observe_go_tools", failed_observer)
    assert module.main() == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    assert secret not in captured.out
    assert json.loads(captured.out) == {"status": "failed", "error_type": "ValueError"}
    start = json.loads((output / "start.json").read_text())
    assert start["registered_profile"] is False
    assert all(secret.encode() not in path.read_bytes() for path in output.iterdir())
    snapshot = GoCallJournal(output / "journal.sqlite").snapshot(start["binding"]["attempt_id"])
    assert snapshot["request_count"] == 0
    assert snapshot["state"] == "revoked"


def test_stdout_summary_does_not_echo_a_credential_embedded_in_the_output_path(
    tmp_path, monkeypatch, capsys
):
    module = load_entrypoint()
    secret = "synthetic-sensitive-directory-credential"
    credential = tmp_path / "synthetic-fixture.txt"
    credential.write_text(secret)
    output = tmp_path / secret
    monkeypatch.setattr(sys, "argv", arguments(output, credential, "--live"))
    monkeypatch.setattr(module, "go_runtime_source", lambda _: {"synthetic_source": True})
    monkeypatch.setattr(
        module,
        "observe_go_tools",
        lambda *_, **__: {
            "status": "failed",
            "reason_codes": ["PROBE_EXECUTION_FAILED"],
            "requests": [],
            "native_cleanup": {"local_stop": "confirmed"},
            "dispatch_eligible": False,
        },
    )
    assert module.main() == 1
    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err
