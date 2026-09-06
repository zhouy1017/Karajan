"""Reproducible public fixture CLI, with actual subprocesses and durable reports."""

import json
import subprocess
import sys
from pathlib import Path

import pytest


def test_public_probe_freezes_and_checks_a_real_candidate_without_enabling_delivery(
    tmp_path: Path,
) -> None:
    script = Path(__file__).parents[2] / "examples/orchestration/probe.py"
    directory = tmp_path / "probe"
    completed = subprocess.run(
        [sys.executable, str(script), "--directory", str(directory), "--scenario", "success"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    summary = json.loads(completed.stdout)
    assert summary["status"] == "passed"
    report = json.loads((directory / "report.json").read_text())
    assert report["snapshot"]["state"] == "local_gate_passed"
    assert report["snapshot"]["delivery_eligible"] is False
    assert report["snapshot"]["counters"]["total_attempts"] == 3
    assert report["cash_api_calls"] == report["real_model_calls"] == 0
    assert report["live_qualification"] == "not_run"
    assert report["candidate"]["changed_paths"] == ["src/report.py"]
    assert len(report["source_sha256"]) >= 5
    assert all(row["state"] == "exited" for row in report["cleanup"])
    assert not (directory / "fixture/repository/src").exists()


@pytest.mark.parametrize(
    "scenario,reason,attempts",
    [
        ("production_blocked", "LIVE_QUALIFICATION_NOT_RUN", 0),
        ("unapproved", "TASK_SCOPE_NOT_APPROVED", 0),
        ("check_failed", "CHECK_NOT_PASSED", 2),
        ("review_inconclusive", "REVIEW_NOT_PASSED", 3),
    ],
)
def test_public_probe_reports_expected_refusal_without_promoting_fixture_qualification(
    tmp_path: Path, scenario: str, reason: str, attempts: int
) -> None:
    script = Path(__file__).parents[2] / "examples/orchestration/probe.py"
    directory = tmp_path / "probe"
    completed = subprocess.run(
        [sys.executable, str(script), "--directory", str(directory), "--scenario", scenario],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    report = json.loads((directory / "report.json").read_text())
    assert report["status"] == "passed"
    assert report["snapshot"]["state"] == "blocked"
    assert reason in report["snapshot"]["reason_codes"]
    assert len(report["snapshot"]["attempts"]) == attempts
    assert report["snapshot"]["delivery_eligible"] is False
    assert report["live_qualification"] == "not_run"
