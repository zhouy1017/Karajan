"""C/P checks for the deterministic planning model-input artifact."""

import hashlib
import json

import pytest
from karajan.orchestration.planning_input import _compile
from karajan.runs import RunError


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
