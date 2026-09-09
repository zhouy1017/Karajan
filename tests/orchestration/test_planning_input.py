"""C/P checks for the deterministic planning model-input artifact."""

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

# Reuse the established runs fixtures when this module is collected by its
# direct path; the full suite already exposes this directory as a test module
# root.
_RUNS_TEST_ROOT = str(Path(__file__).parents[1] / "runs")
if _RUNS_TEST_ROOT not in sys.path:
    sys.path.insert(0, _RUNS_TEST_ROOT)

# These imports intentionally follow the direct-path fixture-root bootstrap
# above, which is required when pytest collects this file by path.
from karajan.adapters.opencode.go_context import GoRequestAccounting  # noqa: E402
from karajan.orchestration.planning_execution import PlanningExecution  # noqa: E402
from karajan.orchestration.planning_input import _compile, compile_planning_input  # noqa: E402
from karajan.orchestration.planning_snapshot import PlanningRepositorySnapshotStore  # noqa: E402
from karajan.projects import ProjectRegistry  # noqa: E402
from karajan.runs import RunError, RunPlanner  # noqa: E402
from karajan.runs.routing_authorization import PlanV2  # noqa: E402
from test_routing_authorization import policy_request, request_v2  # noqa: E402

pytest_plugins = ["test_planning_admission"]


class Accounting:
    def source(self):
        return {"model": "glm-5.3-flash"}

    def measure(self, payload, **limits):
        raw = str(payload).encode()
        return {
            "schema_version": "karajan.go-context-measurement.v1",
            "request_digest": hashlib.sha256(raw).hexdigest(),
            "limits": limits,
        }


def records():
    requirement = {"goal": "Write résumé", "acceptance": ["UTF-8 is preserved"]}
    ceiling = {
        "profile_refs": [{"id": "profile", "revision": 1}],
        "read_paths": ["src"],
        "write_paths": ["src"],
        "budget_ref": "run",
        "checks": ["tests"],
        "delivery": "none",
        "target_branch": "main",
    }
    policy = {
        "schema_version": "karajan.execution-policy.v2",
        "id": "policy",
        "revision": 1,
        "configuration_digest": "c" * 64,
        "constraints": {},
        "max_context_tokens": 8192,
        "context_policy": {
            "input_accounting": "explicit_approved_upper_bound",
            "reserved_output_tokens": 1024,
        },
    }
    binding = {
        "execution_id": "execution",
        "run_id": "run",
        "intent_id": "intent",
        "term": 1,
        "principal": "lead",
        "profile_sha256": "d" * 64,
        "attempt_id": "planning:execution",
        "fence": 1,
        "configuration_sha256": "c" * 64,
        "authorization_ceiling_sha256": hashlib.sha256(
            json.dumps(ceiling, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }
    from karajan.runs.planning import digest

    binding["execution_policy_sha256"] = digest(policy)
    snapshot = {
        "schema_version": "karajan.planning-repository-snapshot.v1",
        "binding_sha256": digest(binding),
        "execution_id": "execution",
        "run_id": "run",
        "intent_id": "intent",
        "requirement_sha256": digest(requirement),
        "authorization_ceiling_sha256": digest(ceiling),
        "repository_identity_sha256": "a" * 64,
        "base_sha": "b" * 40,
        "snapshot_sha256": "e" * 64,
        "read_paths_sha256": "f" * 64,
        "files": [
            {
                "path": "src/é.bin",
                "mode": "100644",
                "size": 3,
                "sha256": hashlib.sha256(bytes([0]) + "é".encode()).hexdigest(),
            }
        ],
        "total_bytes": 3,
        "content": {"src/é.bin": bytes([0]) + "é".encode()},
    }
    run = {
        "id": "run",
        "schema_version": "karajan.run-planning.v2",
        "requirement": requirement,
        "authorization_ceiling": ceiling,
        "configuration_snapshot": {
            "project_revision": 1,
            "revision": 1,
            "digest": "c" * 64,
        },
        "execution_policy_snapshot": policy,
        "planning_intents": [
            {
                "id": "intent",
                "term": 1,
                "principal": "lead",
                "profile": {"id": "profile", "revision": 1},
                "budget_ref": "run",
                "state": "awaiting_receipt",
            }
        ],
        "commander": {
            "term": 1,
            "principal": "lead",
            "profile": {"id": "profile", "revision": 1},
        },
    }
    return (
        {
            "id": "execution",
            "state": "awaiting_admission",
            "intent_id": "intent",
            "binding": binding,
            "binding_sha256": digest(binding),
        },
        run,
        snapshot,
    )


def test_compile_is_complete_deterministic_and_preserves_binary_data():
    execution, run, snapshot = records()
    first = _compile(execution, run, snapshot, Accounting())
    second = _compile(execution, run, snapshot, Accounting())
    assert first.artifact_bytes == second.artifact_bytes
    assert first.request_sha256 == hashlib.sha256(first.request_bytes).hexdigest()
    assert first.files[0]["encoding"] == "base64"
    assert first.files[0]["bytes"]
    assert "UTF-8 is preserved" in first.request["messages"][1]["content"]


def test_compile_rejects_legacy_policy_without_frozen_limits():
    execution, run, snapshot = records()
    run["schema_version"] = "karajan.run-planning.v1"
    with pytest.raises(RunError, match="^PLANNING_INPUT_POLICY_UNSUPPORTED$"):
        _compile(execution, run, snapshot, Accounting())


def test_public_compiler_reads_real_persisted_snapshot_and_reopens_deterministically(
    configured, tmp_path: Path
):
    policy = policy_request(configured)
    policy.update(
        schema_version="karajan.execution-policy.v2",
        max_context_tokens=8192,
        context_policy={
            **policy["context_policy"],
            "measurement": {
                "method": "reference_tokenizer_estimate",
                "source_sha256": "a" * 64,
                "fixed_margin": 2048,
                "ratio_margin_basis_points": 1000,
            },
        },
        validation={
            "id": "validation",
            "revision": 1,
            "checks": [
                {
                    "id": "tests",
                    "revision": 1,
                    "argv": ["python", "-m", "pytest"],
                    "environment_ref": {"id": "offline", "revision": 1},
                    "timeout_seconds": 60,
                }
            ],
            "environments": [
                {
                    "id": "offline",
                    "revision": 1,
                    "runtime_kind": "isolated-command",
                    "platform": "windows_x64",
                    "source_sha256": "b" * 64,
                    "filesystem": "candidate_copy",
                    "network": "none",
                    "env": {},
                    "max_log_bytes": 65536,
                }
            ],
            "review": {
                "id": "independent_review",
                "revision": 1,
                "environment_ref": {"id": "offline", "revision": 1},
                "context_policy": "candidate_and_acceptance_only",
                "independence_policy": "existing_candidate_independence_v1",
            },
        },
    )
    registered = configured["registry"].register_execution_policy(
        configured["id"], policy, command_key="policy", principal="owner"
    )
    planner = RunPlanner(tmp_path / "runs.sqlite", configured["registry"])
    request = request_v2(configured, registered)
    request["authorization"]["read_paths"] = ["original.txt"]
    request["authorization"]["write_paths"] = ["original.txt"]
    run = planner.create(request, command_key="run", principal="owner")
    intent = planner.planning_intent(run["id"], term=1, command_key="intent", principal="lead")
    service = PlanningExecution(tmp_path / "planning.sqlite", planner)
    execution = service.begin(run["id"], intent["id"], principal="owner", command_key="begin")
    service.snapshots = PlanningRepositorySnapshotStore(tmp_path / "snapshots.sqlite")
    service.freeze_repository_snapshot(execution["id"], principal="owner", command_key="freeze")
    accounting = GoRequestAccounting(Path(os.environ["KARAJAN_GO_TOKENIZER_DIRECTORY"]))

    first = compile_planning_input(
        service, accounting, execution_id=execution["id"], principal="owner"
    )
    repository = Path(run["configuration_snapshot"]["repository"]["root"])
    (repository / "working-tree-only.txt").write_bytes(b"must not be read")
    reopened_projects = ProjectRegistry(
        configured["registry"].database,
        configured["registry"].allowed_roots,
        existing_only=True,
    )
    reopened_planner = RunPlanner(service.planner.database, reopened_projects, existing_only=True)
    reopened = type(service)(
        service.database,
        reopened_planner,
        snapshots=PlanningRepositorySnapshotStore(
            tmp_path / "snapshots.sqlite", existing_only=True
        ),
        existing_only=True,
    )
    second = compile_planning_input(
        reopened, accounting, execution_id=execution["id"], principal="owner"
    )

    assert first.artifact_bytes == second.artifact_bytes
    assert first.binding == execution["binding"]
    assert first.output_schema["x-karajan-output-version"] == "v2"
    assert first.output_schema["required"] == PlanV2.model_json_schema()["required"]
    assert "$defs" in first.output_schema
    assert "working-tree-only.txt" not in [row["path"] for row in first.files]
