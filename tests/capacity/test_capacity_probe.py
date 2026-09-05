"""The capacity CLI drives durable contention and a real loopback receiver."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def run_cli(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "karajan.capacity", *arguments],
        cwd=ROOT,
        env=dict(os.environ, PYTHONPATH=str(ROOT / "backend")),
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_fixed_probe_records_only_admitted_loopback_requests(tmp_path: Path) -> None:
    directory = tmp_path / "evidence"
    result = run_cli(
        "probe", str(ROOT / "examples/capacity/shared-pools.json"), "--directory", str(directory)
    )
    assert result.returncode == 0, result.stderr + result.stdout
    report = json.loads((directory / "report.json").read_bytes())
    assert report["case_status"] == "passed"
    assert report["receiver_count"] == 2
    assert report["blocked_request_receiver_count"] == 0
    assert len(report["receiver_requests"]) == 2
    assert len(report["snapshot"]["usage"]) == 2
    assert report["official_model_calls"] == 0
    assert report["live_qualification"] == "not_run"
    assert report["profile_enabled"] is False
    assert sorted(item["decision"] for item in report["contention"]) == ["admitted", "rejected"]
    assert report["reopened_snapshot_matches"] is True
    assert report["source_sha256"]
    assert report["input_sha256"]


def test_probe_refuses_existing_output_and_unknown_cli_options(tmp_path: Path) -> None:
    case = str(ROOT / "examples/capacity/shared-pools.json")
    existing = run_cli("probe", case, "--directory", str(tmp_path))
    assert existing.returncode == 2
    assert json.loads(existing.stdout)["reason"] == "CAPACITY_OUTPUT_EXISTS"
    unknown = run_cli(
        "probe",
        case,
        "--directory",
        str(tmp_path / "new"),
        "--endpoint",
        "https://api.example.test",
    )
    assert unknown.returncode == 2
    assert not (tmp_path / "new").exists()


@pytest.mark.parametrize("body", [b'{"schema_version":"a","schema_version":"b"}', b"[]", b"null"])
def test_invalid_probe_json_is_rejected_before_creating_output(tmp_path: Path, body: bytes) -> None:
    path = tmp_path / "invalid.json"
    path.write_bytes(body)
    result = run_cli("probe", str(path), "--directory", str(tmp_path / "output"))
    assert result.returncode == 2
    assert json.loads(result.stdout)["reason"] == "CAPACITY_INPUT_INVALID"
    assert not (tmp_path / "output").exists()


def test_real_observation_claim_is_rejected_by_the_fixture_entry(tmp_path: Path) -> None:
    case = json.loads((ROOT / "examples/capacity/shared-pools.json").read_bytes())
    case["observations"][0]["source"] = "official"
    path = tmp_path / "not-fixture.json"
    path.write_text(json.dumps(case), encoding="utf-8")
    result = run_cli("probe", str(path), "--directory", str(tmp_path / "output"))
    assert result.returncode == 2
    assert json.loads(result.stdout)["reason"] == "FIXTURE_OBSERVATIONS_ONLY"
    assert not (tmp_path / "output").exists()
