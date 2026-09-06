"""Independent actual CLI negatives; synthetic key only and no external network."""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ENTRY = ROOT / "examples/go-profile-qualification/run_live.py"
SECRET = "synthetic-credential-path-marker"


def invoke(*arguments, network_disabled=False):
    command = [sys.executable, str(ENTRY), *map(str, arguments)]
    if network_disabled:
        command = ["/usr/bin/unshare", "--user", "--map-root-user", "--net", *command]
    result = subprocess.run(
        command,
        env={**os.environ, "PYTHONPATH": str(ROOT / "backend")},
        capture_output=True,
        text=True,
        timeout=40,
        check=False,
    )
    assert SECRET not in result.stdout + result.stderr
    return result


def test_no_arguments_has_no_live_effect():
    result = invoke()
    assert result.returncode == 0
    assert result.stderr == ""
    assert json.loads(result.stdout) == {"status": "not_run", "reason": "EXPLICIT_LIVE_REQUIRED"}


def test_no_live_never_reads_or_creates_supplied_paths(tmp_path):
    directory = tmp_path / SECRET / "not-created"
    result = invoke(
        "--runtime",
        tmp_path / "absent-runtime",
        "--credential-file",
        tmp_path / "absent-credential",
        "--directory",
        directory,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["reason"] == "EXPLICIT_LIVE_REQUIRED"
    assert not directory.parent.exists()


def test_live_requires_all_named_paths_before_any_effect(tmp_path):
    directory = tmp_path / SECRET / "not-created"
    result = invoke("--live", "--directory", directory)
    assert result.returncode == 2
    assert "required for --live" in result.stderr
    assert not directory.parent.exists()


@pytest.fixture
def artifact():
    if sys.platform != "linux":
        pytest.skip("The fixed runtime source requires Linux")
    result = Path(os.environ["KARAJAN_OPENCODE_LINUX_BINARY"])
    assert result.is_file()
    return result


def test_existing_directory_is_never_reentered_or_used_for_key_resolution(tmp_path, artifact):
    directory = tmp_path / SECRET
    directory.mkdir()
    marker = directory / "existing.txt"
    marker.write_bytes(b"keep original evidence")
    result = invoke(
        "--live",
        "--runtime",
        artifact,
        "--credential-file",
        tmp_path / "absent-key",
        "--directory",
        directory,
        network_disabled=True,
    )
    assert result.returncode == 1
    assert json.loads(result.stdout) == {"status": "failed", "error_type": "ValueError"}
    assert marker.read_bytes() == b"keep original evidence"
    assert [item.name for item in directory.iterdir()] == ["existing.txt"]


def test_literal_credential_in_output_path_is_rejected_before_creation(tmp_path, artifact):
    key = tmp_path / "synthetic-material.txt"
    key.write_text(SECRET, encoding="ascii")
    directory = tmp_path / SECRET
    result = invoke(
        "--live",
        "--runtime",
        artifact,
        "--credential-file",
        key,
        "--directory",
        directory,
        network_disabled=True,
    )
    assert result.returncode == 1
    assert json.loads(result.stdout)["status"] == "failed"
    assert not directory.exists()


def test_resolved_parent_never_persists_credential_text_as_public_metadata(tmp_path, artifact):
    key = tmp_path / "synthetic-material.txt"
    key.write_text(SECRET, encoding="ascii")
    physical = tmp_path / SECRET
    physical.mkdir()
    alias = tmp_path / "plain-alias"
    alias.symlink_to(physical, target_is_directory=True)
    directory = alias / "new-diagnostic"
    assert SECRET not in str(directory)
    result = invoke(
        "--live",
        "--runtime",
        artifact,
        "--credential-file",
        key,
        "--directory",
        directory,
        network_disabled=True,
    )
    assert result.returncode == 1
    assert json.loads(result.stdout)["status"] == "failed"
    database = directory / "projects.sqlite"
    if database.exists():
        with sqlite3.connect(database) as connection:
            public_rows = "\n".join(connection.iterdump())
        assert SECRET not in public_rows, (
            "Resolved source path leaked the credential into public project metadata"
        )
    assert not (directory / "calls.sqlite").exists()
