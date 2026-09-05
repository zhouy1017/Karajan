"""The standalone local resource probe is a public evidence-producing command."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def test_demo_exports_receipts_unknown_snapshot_and_reconciled_native_amounts(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[2]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(root / "backend")
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "karajan.resources",
            "demo",
            "--directory",
            str(tmp_path / "probe"),
            "--scenario",
            str(root / "examples/resources/local-fake-scenario.json"),
        ],
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report["status"] == "passed"
    assert report["live_qualified"] is False
    assert report["cash_api_enabled"] is False
    assert len(report["provider_records"]) == 2
    assert report["unknown_before_reconciliation"]["budgets"]["USD"]["held"] == "4.000000"
    assert report["final_snapshot"]["budgets"]["USD"]["held"] == "3.000000"
    assert (tmp_path / "probe/report.json").is_file()


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "0.0000001", 2.0])
def test_invalid_scenario_amounts_fail_before_creating_a_probe(
    tmp_path: Path,
    amount: str | float,
) -> None:
    root = Path(__file__).resolve().parents[2]
    scenario = json.loads((root / "examples/resources/local-fake-scenario.json").read_text())
    scenario["call_upper"] = amount
    source = tmp_path / "invalid.json"
    source.write_text(json.dumps(scenario), encoding="utf-8")
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(root / "backend")
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "karajan.resources",
            "demo",
            "--directory",
            str(tmp_path / "probe"),
            "--scenario",
            str(source),
        ],
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
        check=False,
    )

    assert completed.returncode == 1
    assert completed.stderr == ""
    assert json.loads(completed.stdout)["status"] == "failed"
    assert not (tmp_path / "probe").exists()
