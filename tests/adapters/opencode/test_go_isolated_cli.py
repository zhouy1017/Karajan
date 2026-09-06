"""The explicit diagnostic switch gates even local credential reads and writes."""

import json
import os
import subprocess
import sys
from pathlib import Path


def test_cli_without_live_does_not_read_key_or_create_directory(tmp_path):
    root = Path(__file__).resolve().parents[3]
    destination = tmp_path / "must-not-exist"
    result = subprocess.run(
        [
            sys.executable,
            str(root / "examples/opencode-go-isolated/run_live.py"),
            "--runtime",
            str(tmp_path / "absent-binary"),
            "--credential-file",
            str(tmp_path / "absent-key"),
            "--directory",
            str(destination),
            "--scenario",
            "edit",
        ],
        env={**os.environ, "PYTHONPATH": str(root / "backend")},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 1
    assert json.loads(result.stdout) == {
        "status": "not_run",
        "reason": "LIVE_AUTHORIZATION_REQUIRED",
    }
    assert not destination.exists()
    assert result.stderr == ""
