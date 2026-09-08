"""Exercise the Python that the two workflow aggregation jobs actually run."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

EXPECTED_DEPENDENCIES = {
    "quality-gate": {"quick-python", "frontend-quality"},
    "nightly-quality-gate": {"python-nightly", "frontend-nightly"},
}


def aggregation_script(job: str) -> str:
    workflow = (Path(__file__).parents[2] / ".github/workflows/ci.yml").read_text(
        encoding="utf-8"
    )
    start = workflow.index(f"  {job}:\n")
    following = re.search(r"^  [\w-]+:\n", workflow[start + 1 :], re.MULTILINE)
    section = workflow[start : start + 1 + following.start() if following else None]
    block = re.search(r"        run: \|\n(?P<body>(?:          .*\n|\n)+)", section)
    assert block is not None
    heredoc = re.search(
        r"python3 - <<'PY'\n(?P<source>.*)\nPY\n*$",
        textwrap.dedent(block.group("body")),
        re.DOTALL,
    )
    assert heredoc is not None
    return heredoc.group("source")


def run_gate(job: str, results: dict[str, object]) -> subprocess.CompletedProcess[str]:
    environment = {**os.environ, "NEEDS_RESULTS": json.dumps(results)}
    return subprocess.run(
        [sys.executable, "-c", aggregation_script(job)],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


@pytest.mark.parametrize("job", EXPECTED_DEPENDENCIES)
def test_aggregate_gate_accepts_only_its_named_successes(job: str) -> None:
    results = {name: {"result": "success"} for name in EXPECTED_DEPENDENCIES[job]}
    assert run_gate(job, results).returncode == 0


@pytest.mark.parametrize("job", EXPECTED_DEPENDENCIES)
@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped", None])
def test_aggregate_gate_rejects_every_non_success(job: str, result: str | None) -> None:
    results = {name: {"result": "success"} for name in EXPECTED_DEPENDENCIES[job]}
    first = next(iter(results))
    results[first] = {"result": result}
    assert run_gate(job, results).returncode == 1


@pytest.mark.parametrize("job", EXPECTED_DEPENDENCIES)
def test_aggregate_gate_rejects_a_missing_dependency(job: str) -> None:
    first = next(iter(EXPECTED_DEPENDENCIES[job]))
    assert run_gate(job, {first: {"result": "success"}}).returncode == 1
