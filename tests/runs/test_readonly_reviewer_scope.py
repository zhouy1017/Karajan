"""Scope checks plus the real binding facade; qualification here is a named C double."""

from copy import deepcopy

import pytest
from karajan.orchestration.go_reviewer_scope import resolve_go_reviewer_execution
from test_reviewer_binding import (
    binding_case,
    captured_case,
    case,
    check_case,
    collection_case,
    projected,
    workspace_case,
)

__all__ = [
    "binding_case",
    "captured_case",
    "case",
    "check_case",
    "collection_case",
    "projected",
    "workspace_case",
]


def resolver_inputs(binding_case):
    service, _, _, args, _, _, _ = binding_case
    first = service.advance(*args, principal="owner")
    assert first["state"] == "prepared", first
    membership = first["assessment"]["membership"]
    task = deepcopy(membership["snapshots"]["task"])
    operation = service.admissions.get(*args, principal="owner")
    source = operation["workspace"]["source_binding"]
    execution = source["execution_policy"]
    fixed = source["profile_registration"]
    project_id = service.admissions.routing.planner.get(args[0], principal="owner")["project_id"]
    with service.qualifications.projects._transaction() as db:
        observation = service.qualifications._facts(
            db, project_id, fixed, "runtime_tools", None
        )
    return fixed, observation, task, execution


def test_public_binding_consumes_and_records_the_resolved_limits(binding_case):
    service, _, _, args, _, _, _ = binding_case
    first = service.advance(*args, principal="owner")
    assert first["state"] == "prepared", first
    limits = [row["limits"] for row in first["assessment"]["profile_limits"]]
    assert len(limits) == 1
    assert limits[0]["max_requests"] == 6
    assert limits[0]["context"]["reserved_output_tokens"] == 4096
    assert limits[0]["context"]["approved_input_tokens"] == 6000
    assert first["assessment"]["actual_reviewer_attempt"] is None


@pytest.mark.parametrize(
    "fault", ["missing", "worker", "unknown", "tools", "parser", "margin", "O"]
)
def test_public_binding_never_passes_ineligible_scope_to_membership(binding_case, fault):
    service, qualification, intents, args, candidates, captured, _ = binding_case
    before = intents.admissions.routing.capacity.path.read_bytes()

    def mutate(observed):
        if fault == "missing":
            del observed["executor_scope"]
        elif fault == "worker":
            observed["qualification_scope"] = "projected_native_tools"
        elif fault == "unknown":
            observed["executor_scope"]["schema_version"] = "unknown"
        elif fault == "tools":
            observed["executor_scope"]["tools"] = ["read", "edit"]
        elif fault == "parser":
            observed["executor_scope"]["output_parser_revision"] = "old-parser"
        elif fault == "margin":
            observed["executor_scope"]["context"]["fixed_margin"] = 2049
        else:
            observed["executor_scope"]["context"]["reserved_output_tokens"] = 2048

    qualification.mutate = mutate
    result = service.advance(*args, principal="owner")
    assert result["state"] == "blocked", result
    assert result["transition"] is None
    assert result["assessment"]["membership"]["eligible_profiles"] == []
    assert result["assessment"]["profile_limits"] == []
    assert candidates.get(captured["id"])["request"]["policy"]["review"]["approved_reviewers"] == []
    assert intents.admissions.routing.capacity.path.read_bytes() == before


@pytest.mark.parametrize(
    "fault", ["T2", "T3", "edit", "wrong_source", "small_margin", "small_ratio", "O", "I", "C"]
)
def test_pure_scope_rejects_unqualified_approved_task_limits(binding_case, fault):
    registration, observation, task, execution = resolver_inputs(binding_case)
    task, execution = deepcopy(task), deepcopy(execution)
    effective = "T1"
    if fault in {"T2", "T3"}:
        effective = fault
    elif fault == "edit":
        task["tools"] = ["read", "edit"]
    elif fault == "wrong_source":
        execution["context_policy"]["measurement"]["source_sha256"] = "f" * 64
    elif fault == "small_margin":
        execution["context_policy"]["measurement"]["fixed_margin"] = 2047
    elif fault == "small_ratio":
        execution["context_policy"]["measurement"]["ratio_margin_basis_points"] = 1999
    elif fault == "O":
        execution["context_policy"]["reserved_output_tokens"] = 2048
    elif fault == "I":
        task["context_tokens"] = 12289
    else:
        execution["max_context_tokens"] = 10000
    resolved, errors = resolve_go_reviewer_execution(
        registration, observation, task, execution, effective
    )
    assert resolved is None
    assert errors and all(error.startswith("READONLY_REVIEWER_") for error in errors)
