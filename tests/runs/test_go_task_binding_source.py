"""Real offline runtime/accounting source compilers, without execution effects.

The approved Run uses a synthetic qualification producer and scripted planning
admission. Its policy is registered with the real tokenizer digest before Run
creation, never repaired by rewriting an operation after approval. No native
namespace, Host child, HTTP transport or provider credential is used here.
"""

import hashlib
import sys
from dataclasses import asdict
from pathlib import Path

import pytest
from karajan.execution import RunnerHost
from karajan.isolation.go_probe import source_digest
from karajan.isolation.go_task import native_task_source
from karajan.isolation.opencode_runtime import RUNTIME_SHA256
from karajan.orchestration.go_execution_intent import GoExecutionIntents
from karajan.orchestration.go_task_binding import (
    execution_source,
    task_relay_context,
    task_runner_source,
)
from test_go_context import accounting as accounting
from test_go_context import artifacts as artifacts
from test_go_context import tool_history
from test_go_execution_intent import case, projected, ready, reservation
from test_opencode_go_composition import runtime_artifact
from test_projected_qualification_store import CONTEXT

__all__ = ["case", "projected", "ready", "reservation"]
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Fixed Linux source descriptor")


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def test_real_source_envelope_keeps_native_and_execution_digests_separate(accounting, artifacts):
    runtime = runtime_artifact()
    before_artifacts = {
        path.name: (path.stat().st_size, path.stat().st_mtime_ns, sha(path))
        for path in artifacts.iterdir()
        if path.is_file()
    }
    source = task_runner_source(runtime, accounting)
    native = native_task_source(runtime, accounting)
    assert source["schema_version"] == "karajan.go-task-runner-source.v1"
    assert source["native_task"] == native
    mechanism = native["qualified_mechanism_descriptor"]
    assert mechanism["artifact_sha256"] == RUNTIME_SHA256
    assert native["qualified_mechanism_digest"] == source_digest(mechanism)
    assert mechanism["probe_spec"]["context"]["source_sha256"] == source_digest(accounting.source())
    result = execution_source(source)
    assert asdict(result) == {
        "runner_source_sha256": source_digest(source),
        "native_source_sha256": source_digest(native),
    }
    assert result.runner_source_sha256 != result.native_source_sha256
    root = Path(__file__).resolve().parents[2] / "backend/karajan"
    assert source["controller_sources"] == {
        name: sha(root / name) for name in source["controller_sources"]
    }
    assert {
        "orchestration/go_task_binding.py",
        "orchestration/go_task_input.py",
        "orchestration/go_execution_intent.py",
        "execution/host.py",
        "execution/_supervisor.py",
    } <= source["controller_sources"].keys()
    assert task_runner_source(runtime, accounting) == source
    assert before_artifacts == {
        path.name: (path.stat().st_size, path.stat().st_mtime_ns, sha(path))
        for path in artifacts.iterdir()
        if path.is_file()
    }


@pytest.fixture
def measured_ready(accounting, monkeypatch, request):
    # Only fixture declarations change, before any policy registration/approval.
    # This does not turn SyntheticSuite into a real runtime qualification.
    monkeypatch.setitem(CONTEXT, "source_sha256", source_digest(accounting.source()))
    return request.getfixturevalue("ready")


def test_real_reference_accounting_consumes_the_original_approved_limits(
    measured_ready, accounting, tmp_path
):
    admissions, routing, run, operation, workspace = measured_ready
    source = task_runner_source(runtime_artifact(), accounting)
    service = GoExecutionIntents(
        admissions, source=execution_source(source), host=RunnerHost(tmp_path / "unstarted-host")
    )
    prepared = service.prepare_intent(
        run["id"], operation["id"], principal="owner", command_key="measured-intent"
    )
    paths = (admissions.database, routing.planner.database, routing.capacity.path)
    before = {path: path.read_bytes() for path in paths}
    compiled = task_relay_context(prepared, accounting)
    policy = workspace["source_binding"]["execution_policy"]
    assert compiled.source_sha256 == source_digest(accounting.source())
    assert compiled.execution_policy_digest == policy["digest"]
    assert (
        compiled.approved_input_tokens,
        compiled.reserved_output_tokens,
        compiled.operating_context_tokens,
        compiled.fixed_margin,
        compiled.ratio_margin_basis_points,
    ) == (6000, 4096, 12000, 2300, 2200)
    request = tool_history()
    measured = compiled.measure(request)
    assert measured["accounted_input_tokens"] > measured["local_input_tokens"] > 0
    assert measured["requested_output_tokens"] == 128
    assert measured["measurement_confidence"] == "local_estimate"
    assert measured == accounting.measure(
        request,
        approved_input_tokens=6000,
        reserved_output_tokens=4096,
        operating_context_tokens=12000,
        fixed_margin=2300,
        ratio_margin_basis_points=2200,
    )
    assert before == {path: path.read_bytes() for path in paths}
    assert service.read(run["id"], operation["id"], principal="owner") == prepared
    assert routing.capacity.snapshot()["reservations"][0]["state"] == "reserved"
