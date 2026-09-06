"""Routing policy boundaries; synthetic scope values never qualify a live model."""

from copy import deepcopy

import pytest
from karajan.orchestration.go_scope import SUITE, resolve_go_execution


@pytest.fixture
def inputs():
    context = {
        "source_sha256": "b" * 64,
        "approved_input_tokens": 12288,
        "reserved_output_tokens": 4096,
        "operating_context_tokens": 16384,
        "fixed_margin": 2048,
        "ratio_margin_basis_points": 2000,
    }
    scope = {
        "schema_version": "karajan.go-projected-executor-scope.v1",
        "suite_ref": deepcopy(SUITE),
        "projection": "existing_regular_files",
        "new_files_supported": False,
        "tools": ["read", "edit"],
        "supported_roles": ["worker"],
        "task_classes": ["T1"],
        "context": context,
        "max_requests": 6,
        "candidate_capture": True,
    }
    return {
        "registration": {
            "profile": {
                "binding": {
                    "runtime_kind": "opencode-go-isolated",
                    "native_settings": {"suite_ref": deepcopy(SUITE)},
                }
            }
        },
        "observation": {
            "executor_scope": scope,
            "qualification_scope": "projected_native_tools",
            "runtime_tools_status": "passed",
            "facts": {
                "provenance": "imported_observation",
                "context_tokens": 16384,
                "evidence_ref": "synthetic-test-source",
            },
        },
        "task": {"role": "worker", "tools": ["read", "edit"], "context_tokens": 8192},
        "effective_class": "T1",
        "execution": {
            "schema_version": "karajan.execution-policy.v2",
            "digest": "a" * 64,
            "max_context_tokens": 32768,
            "context_policy": {
                "reserved_output_tokens": 4096,
                "measurement": {
                    "method": "reference_tokenizer_estimate",
                    "source_sha256": "b" * 64,
                    "fixed_margin": 2048,
                    "ratio_margin_basis_points": 2000,
                },
            },
        },
    }


def test_task_resolves_qualified_limits_and_approval_without_mutating_source(inputs):
    original = deepcopy(inputs)
    value, reasons = resolve_go_execution(**inputs)
    assert reasons == [] and value is not None
    assert value["context"] == {
        **inputs["observation"]["executor_scope"]["context"],
        "approved_input_tokens": 8192,
    }
    assert value["max_requests"] == 6
    assert value["execution_policy_digest"] == inputs["execution"]["digest"]
    assert value["qualification_ref"] == "synthetic-test-source"
    assert len(value["executor_scope_digest"]) == 64
    assert inputs == original
    value["context"]["approved_input_tokens"] = 1
    assert inputs == original


def test_owner_can_narrow_operating_context_and_increase_margins(inputs):
    inputs["execution"]["max_context_tokens"] = 12288
    inputs["execution"]["context_policy"]["measurement"].update(
        fixed_margin=3072, ratio_margin_basis_points=2500
    )
    value, reasons = resolve_go_execution(**inputs)
    assert reasons == []
    assert value["context"]["operating_context_tokens"] == 12288
    assert value["context"]["fixed_margin"] == 3072
    assert value["context"]["ratio_margin_basis_points"] == 2500


@pytest.mark.parametrize(
    "field,value",
    [
        ("new_files_supported", True),
        ("new_files_supported", 0),
        ("candidate_capture", False),
        ("candidate_capture", 1),
        ("max_requests", 7),
        ("max_requests", 6.0),
        ("supported_roles", ["reviewer"]),
        ("task_classes", ["T2"]),
        ("tools", ["read", "edit", "bash"]),
        ("projection", "whole_repository"),
    ],
)
def test_scope_cannot_claim_unimplemented_or_coerced_capabilities(inputs, field, value):
    inputs["observation"]["executor_scope"][field] = value
    resolved, reasons = resolve_go_execution(**inputs)
    assert resolved is None and reasons == ["PROJECTED_EXECUTOR_SCOPE_INVALID"]


@pytest.mark.parametrize(
    "path,value,reason",
    [
        (
            ("observation", "qualification_scope"),
            "projected_native_tools_fixture",
            "PROJECTED_EXECUTOR_SCOPE_UNQUALIFIED",
        ),
        (
            ("observation", "runtime_tools_status"),
            "not_run",
            "PROJECTED_EXECUTOR_SCOPE_UNQUALIFIED",
        ),
        (("observation", "facts", "provenance"), "fixture", "PROJECTED_EXECUTOR_SCOPE_UNQUALIFIED"),
        (
            ("observation", "facts", "context_tokens"),
            1000000,
            "PROJECTED_EXECUTOR_SCOPE_UNQUALIFIED",
        ),
        (
            ("observation", "executor_scope", "suite_ref", "revision"),
            1,
            "PROJECTED_EXECUTOR_SCOPE_UNQUALIFIED",
        ),
        (
            ("observation", "executor_scope", "context", "operating_context_tokens"),
            1000000,
            "PROJECTED_EXECUTOR_SCOPE_UNQUALIFIED",
        ),
        (
            ("observation", "executor_scope", "context", "reserved_output_tokens"),
            1024,
            "PROJECTED_EXECUTOR_SCOPE_UNQUALIFIED",
        ),
        (("task", "role"), "reviewer", "PROJECTED_TASK_SCOPE_UNQUALIFIED"),
        (("effective_class",), "T3", "PROJECTED_TASK_SCOPE_UNQUALIFIED"),
        (("task", "tools"), ["read", "bash"], "PROJECTED_TASK_SCOPE_UNQUALIFIED"),
        (
            ("execution", "schema_version"),
            "karajan.execution-policy.v1",
            "PROJECTED_CONTEXT_POLICY_REQUIRED",
        ),
        (("execution", "context_policy", "measurement"), None, "PROJECTED_CONTEXT_POLICY_REQUIRED"),
        (
            ("execution", "context_policy", "measurement", "method"),
            "provider_exact",
            "PROJECTED_CONTEXT_SOURCE_OR_MARGIN_UNQUALIFIED",
        ),
        (
            ("execution", "context_policy", "measurement", "source_sha256"),
            "c" * 64,
            "PROJECTED_CONTEXT_SOURCE_OR_MARGIN_UNQUALIFIED",
        ),
        (
            ("execution", "context_policy", "measurement", "fixed_margin"),
            2047,
            "PROJECTED_CONTEXT_SOURCE_OR_MARGIN_UNQUALIFIED",
        ),
        (
            ("execution", "context_policy", "measurement", "ratio_margin_basis_points"),
            1999,
            "PROJECTED_CONTEXT_SOURCE_OR_MARGIN_UNQUALIFIED",
        ),
        (
            ("execution", "context_policy", "reserved_output_tokens"),
            1024,
            "PROJECTED_CONTEXT_LIMIT_UNQUALIFIED",
        ),
        (
            ("execution", "context_policy", "reserved_output_tokens"),
            8192,
            "PROJECTED_CONTEXT_LIMIT_UNQUALIFIED",
        ),
        (("execution", "max_context_tokens"), 12287, "PROJECTED_CONTEXT_LIMIT_UNQUALIFIED"),
        (("task", "context_tokens"), 12289, "PROJECTED_CONTEXT_LIMIT_UNQUALIFIED"),
    ],
)
def test_task_or_owner_policy_cannot_expand_qualified_mechanism(inputs, path, value, reason):
    target = inputs
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    resolved, reasons = resolve_go_execution(**inputs)
    assert resolved is None and reasons == [reason]


@pytest.mark.parametrize(
    "observation,reason",
    [
        (None, "PROJECTED_EXECUTOR_SCOPE_REQUIRED"),
        ({}, "PROJECTED_EXECUTOR_SCOPE_INVALID"),
    ],
)
def test_missing_qualification_never_enables_execution(inputs, observation, reason):
    inputs["observation"] = observation
    assert resolve_go_execution(**inputs) == (None, [reason])


@pytest.mark.parametrize(
    "profile",
    [
        None,
        {
            "binding": {
                "runtime_kind": "opencode-go-isolated",
                "native_settings": {"suite_ref": {**SUITE, "revision": 1}},
            }
        },
        {"binding": {"runtime_kind": "other", "native_settings": {"suite_ref": SUITE}}},
    ],
)
def test_existing_other_execution_routes_are_unchanged(inputs, profile):
    inputs["registration"]["profile"] = profile
    inputs["observation"] = None
    assert resolve_go_execution(**inputs) == (None, [])
