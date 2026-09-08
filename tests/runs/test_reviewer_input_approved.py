"""Reviewer input uses the actual approved worker/reviewer task pair."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest
from karajan.orchestration.reviewer_input import compile_reviewer_input
from karajan.runs import RunError
from test_reviewer_binding import _passed_reviewer_subject

pytest_plugins = (
    "test_projected_qualification_store",
    "test_candidate_checks",
    "test_reviewer_binding",
)


def _evidence_ids(intents, args) -> list[str]:
    operation = intents.read(*args, principal="owner")
    return [row["evidence"]["id"] for row in operation["validation"]["checks"]["runs"]]


def test_public_compiler_includes_both_actual_approved_task_acceptances(binding_case) -> None:
    intents, args, candidates, _, _ = _passed_reviewer_subject(binding_case)

    result = compile_reviewer_input(
        intents.admissions,
        candidates,
        run_id=args[0],
        operation_id=args[1],
        principal="owner",
        final_check_evidence_ids=_evidence_ids(intents, args),
    )

    document = json.loads(result.content)
    source = intents.read(*args, principal="owner")["workspace"]["source_binding"]
    tasks = {task["id"]: task for task in source["plan"]["plan"]["tasks"]}
    assert document["schema_version"] == "karajan.reviewer-input.v2"
    assert document["approved"]["worker"] == {
        key: tasks["implement"][key] for key in ("id", "revision", "role", "paths", "acceptance")
    }
    assert document["approved"]["reviewer"] == {
        key: tasks["review"][key] for key in ("id", "revision", "role", "paths", "acceptance")
    }
    assert document["approved"]["plan"]["plan_digest"] == source["plan"]["plan_digest"]
    assert document["approved"]["approval"]["plan_digest"] == source["approval"]["plan_digest"]


@pytest.mark.parametrize("fault", ["plan", "task", "approval", "approval_digest"])
def test_public_compiler_rejects_tampered_approved_context(binding_case, fault: str) -> None:
    intents, args, candidates, _, _ = _passed_reviewer_subject(binding_case)
    operation = intents.read(*args, principal="owner")
    changed = deepcopy(operation)
    source = changed["workspace"]["source_binding"]
    if fault == "plan":
        source["plan"]["plan"]["summary"] = "unapproved plan"
    elif fault == "task":
        source["plan"]["plan"]["tasks"][0]["acceptance"] = ["unapproved acceptance"]
    elif fault == "approval":
        source["approval"]["term"] += 1
    else:
        source["approval"]["plan_digest"] = "0" * 64
    with intents.admissions._transaction() as db:
        intents.admissions._save(db, changed)

    with pytest.raises(RunError, match="REVIEWER_INPUT_APPROVAL_CHANGED"):
        compile_reviewer_input(
            intents.admissions,
            candidates,
            run_id=args[0],
            operation_id=args[1],
            principal="owner",
            final_check_evidence_ids=_evidence_ids(intents, args),
        )
