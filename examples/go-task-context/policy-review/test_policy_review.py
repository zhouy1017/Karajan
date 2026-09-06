"""Independent public Registry/Run policy checks; fixture means no live execution."""

import importlib.machinery
import importlib.util
from copy import deepcopy
from pathlib import Path

import pytest
from karajan.adapters.opencode.go_context import GoContextError
from karajan.projects import ProjectError
from karajan.projects.execution_policy import validate_policy
from karajan.routing.compiler import digest
from karajan.runs import RunError
from test_demand_store import approved, case
from test_go_context import accounting, artifacts

__all__ = ["approved", "case", "accounting", "artifacts"]


def document(approved):
    old = approved["projects"].get_execution_policy(
        approved["project_id"], "execution", 1, principal="owner"
    )
    return {
        key: deepcopy(value)
        for key, value in old.items()
        if key
        not in {"project_id", "digest", "registered_by", "registered_at", "activation_allowed"}
    }


def measured_policy(approved, counter, ratio=500):
    body = document(approved)
    body.update(schema_version="karajan.execution-policy.v2", revision=2)
    body["context_policy"].update(
        revision=2,
        measurement={
            "method": "reference_tokenizer_estimate",
            "source_sha256": digest(counter.source()),
            "fixed_margin": 16,
            "ratio_margin_basis_points": ratio,
        },
    )
    environment = {"id": "spec-env", "revision": 1}
    body["validation"] = {
        "id": "spec-validation",
        "revision": 1,
        "checks": [
            {
                "id": "tests",
                "revision": 1,
                "argv": ["python", "-m", "pytest"],
                "environment_ref": environment,
                "timeout_seconds": 30,
            }
        ],
        "environments": [
            {
                **environment,
                "runtime_kind": "isolated-command",
                "platform": "linux_x64",
                "source_sha256": "b" * 64,
                "filesystem": "candidate_copy",
                "network": "none",
                "env": {},
                "max_log_bytes": 1024,
            }
        ],
        "review": {
            "id": "independent_review",
            "revision": 1,
            "environment_ref": environment,
            "context_policy": "candidate_and_acceptance_only",
            "independence_policy": "existing_candidate_independence_v1",
        },
    }
    return body, counter


def register(approved, body, key="spec-policy"):
    return approved["projects"].register_execution_policy(
        approved["project_id"], body, command_key=key, principal="owner"
    )


def measure(counter, ratio):
    return counter.measure(
        {
            "model": "glm-5.3-flash",
            "stream": True,
            "max_tokens": 128,
            "messages": [{"role": "user", "content": "Hi"}],
        },
        approved_input_tokens=4000,
        reserved_output_tokens=128,
        operating_context_tokens=8192,
        fixed_margin=16,
        ratio_margin_basis_points=ratio,
    )


def test_cannot_register_ratio_that_the_bound_measurement_source_always_rejects(
    approved, accounting
):
    body, counter = measured_policy(approved, accounting, ratio=10_001)
    with pytest.raises(GoContextError, match="^GO_CONTEXT_INVALID_LIMITS$"):
        measure(counter, 10_001)
    with pytest.raises(ProjectError, match="EXECUTION_POLICY_INVALID"):
        register(approved, body)


def test_maximum_supported_ratio_is_accepted_by_both_public_interfaces(approved, accounting):
    body, counter = measured_policy(approved, accounting, ratio=10_000)
    record = register(approved, body)
    receipt = measure(counter, record["context_policy"]["measurement"]["ratio_margin_basis_points"])
    assert receipt["local_input_tokens"] == 13
    assert receipt["accounted_input_tokens"] == 42


@pytest.mark.parametrize("value", [None, [], "not-an-object"])
def test_v1_nonobject_registration_keeps_the_domain_error_contract(approved, value):
    with pytest.raises(ProjectError, match="EXECUTION_POLICY_INVALID"):
        register(approved, value)


def test_v1_normalized_shape_and_digest_match_pinned_old_implementation(approved):
    path = Path(__file__).with_name("legacy-execution-policy.py.txt")
    loader = importlib.machinery.SourceFileLoader(
        "karajan.projects.review_legacy_policy", str(path)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    legacy = importlib.util.module_from_spec(spec)
    loader.exec_module(legacy)
    old = legacy.validate_policy(document(approved), approved["configuration"])
    current = validate_policy(document(approved), approved["configuration"])
    assert current == old and digest(current) == digest(old)
    assert "measurement" not in current["context_policy"]
    assert "validation" not in current


def test_renaming_policy_and_validation_does_not_allow_child_revision_reuse(approved, accounting):
    body, _ = measured_policy(approved, accounting)
    register(approved, body)
    changed = deepcopy(body)
    changed.update(id="a-different-policy", revision=1)
    changed["validation"]["id"] = "a-different-validation"
    changed["validation"]["checks"][0]["argv"].append("--disable-warnings")
    with pytest.raises(ProjectError, match="EXECUTION_POLICY_COMPONENT_REVISION_CONFLICT"):
        register(approved, changed, "rename-does-not-bypass")
    changed["validation"]["checks"][0]["revision"] = 2
    fixed = register(approved, changed, "new-child-version")
    assert fixed["validation"]["checks"][0]["revision"] == 2


@pytest.mark.parametrize(
    "checks", [["unknown-check", "independent_review"], ["tests", "tests", "independent_review"]]
)
def test_unresolved_or_duplicate_required_checks_cannot_create_a_run(approved, accounting, checks):
    body, _ = measured_policy(approved, accounting)
    policy = register(approved, body)
    old = approved["planner"].get(approved["run_id"], principal="owner")
    project = approved["projects"].get(approved["project_id"])
    authorization = deepcopy(old["authorization_ceiling"])
    authorization["checks"] = checks
    with pytest.raises(RunError, match="VALIDATION_CHECKS_NOT_RESOLVED"):
        approved["planner"].create(
            {
                "schema_version": "karajan.create-run.v2",
                "project_id": project["id"],
                "project_revision": project["revision"],
                "configuration_digest": project["configuration"]["digest"],
                "requirement": old["requirement"],
                "participants": old["participants"],
                "authorization": authorization,
                "execution_policy": {key: policy[key] for key in ("id", "revision", "digest")},
            },
            command_key="invalid-run",
            principal="owner",
        )
