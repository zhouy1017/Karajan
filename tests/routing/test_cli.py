"""The public CLI replays JSON without database access or model execution."""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from karajan.routing import evaluate_route

from .test_routing import sample


def test_evaluation_is_pure_even_with_process_network_and_database_access_forbidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task, policy, capacity = sample()

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("No external effects are authorized")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    import socket

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    first = evaluate_route(task, policy, capacity)
    assert first == evaluate_route(task, policy, capacity)
    assert first["selected_profile"] is not None


def test_cli_replays_complete_input_and_binds_source_hashes(tmp_path: Path) -> None:
    task, policy, capacity = sample()
    source = tmp_path / "input.json"
    source.write_text(
        json.dumps({"task": task, "policy": policy, "capacity": capacity}), encoding="utf-8"
    )
    output = tmp_path / "output.json"
    env = {**os.environ, "PYTHONPATH": "backend"}
    command = [
        sys.executable,
        "-m",
        "karajan.routing",
        "evaluate",
        "--input",
        str(source),
        "--output",
        str(output),
    ]
    first = subprocess.run(command, env=env, capture_output=True, timeout=15)
    assert first.returncode == 0, first.stderr
    before = output.read_bytes()
    result = json.loads(before)
    assert result["result"]["selected_profile"]["id"] == "fixture-profile"
    assert result["source_sha256"]["routing/evaluator.py"]
    assert result["source_sha256"]["contracts/credentials.py"]
    assert result["model_calls"] == 0
    second = subprocess.run(command, env=env, capture_output=True, timeout=15)
    assert second.returncode == 0
    assert output.read_bytes() == before


@pytest.mark.parametrize(
    "raw", ['{"task": {}, "task": {}}', '{"task": NaN}', '{"task": "\\ud800"}']
)
def test_cli_rejects_ambiguous_or_invalid_json_without_creating_report(
    tmp_path: Path, raw: str
) -> None:
    source = tmp_path / "input.json"
    source.write_text(raw, encoding="utf-8")
    output = tmp_path / "output.json"
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "karajan.routing",
            "evaluate",
            "--input",
            str(source),
            "--output",
            str(output),
        ],
        env={**os.environ, "PYTHONPATH": "backend"},
        capture_output=True,
        timeout=15,
    )
    assert process.returncode == 2
    assert not output.exists()
    assert json.loads(process.stdout)["error"] == "ROUTING_INPUT_INVALID"
