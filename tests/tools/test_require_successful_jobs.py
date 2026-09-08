"""Direct tests for the CI aggregation script's fail-closed contract."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def gate_module():
    script = Path(__file__).parents[2] / ".github/scripts/require_successful_jobs.py"
    specification = importlib.util.spec_from_file_location("require_successful_jobs", script)
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def test_all_named_successes_pass(gate_module) -> None:
    assert gate_module.unsuccessful_jobs(
        {"quick-python": {"result": "success"}, "frontend-quality": {"result": "success"}},
        ["quick-python", "frontend-quality"],
    ) == {}


@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped", None])
def test_non_success_is_never_accepted(gate_module, result: str | None) -> None:
    assert gate_module.unsuccessful_jobs(
        {"quick-python": {"result": result}, "frontend-quality": {"result": "success"}},
        ["quick-python", "frontend-quality"],
    ) == {"quick-python": result or "missing"}


def test_missing_or_extra_dependencies_fail_closed(gate_module) -> None:
    assert gate_module.unsuccessful_jobs(
        {"quick-python": {"result": "success"}},
        ["quick-python", "frontend-quality"],
    ) == {
        "dependency-list": (
            "expected ['frontend-quality', 'quick-python'], got ['quick-python']"
        )
    }
