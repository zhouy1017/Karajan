import json
from typing import Any

import pytest
from karajan.runs.models import Plan
from karajan.runs.planning_output import (
    MAX_DEPTH,
    MAX_OUTPUT_BYTES,
    PlanningOutputError,
    parse_planning_output,
)
from karajan.runs.routing_authorization import PlanV2


def authorization(*, v2: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {
        "profile_refs": [{"id": "commander-profile", "revision": 1}],
        "read_paths": ["src"],
        "write_paths": ["src"],
        "budget_ref": "run-budget",
        "checks": ["tests"],
        "delivery": "none",
        "target_branch": "main",
    }
    if v2:
        result.update(
            {
                "channel_ids": ["channel"],
                "tools": ["read"],
                "data_destinations": ["local"],
                "required_capabilities": ["planning"],
                "min_isolation": "tool_sandboxed",
                "currency_limits": {"USD": "0"},
                "max_attempt_duration_seconds": 30,
                "max_quality_repair_rounds": 0,
                "stage_permissions": {},
            }
        )
    return result


def document(*, v2: bool = False) -> dict[str, Any]:
    task: dict[str, Any] = {
        "id": "plan-task",
        "revision": 1,
        "role": "worker",
        "readiness": "ready",
        "complexity": "T1",
        "risk": "standard",
        "paths": ["src/output.py"],
        "depends_on": [],
        "acceptance": ["output is repeatable"],
        "required": True,
    }
    if v2:
        task.update(
            {
                "purpose": None,
                "domains": ["code"],
                "required_capabilities": ["planning"],
                "tools": ["read"],
                "context_tokens": 100,
                "duration_seconds": 10,
            }
        )
    return {
        "summary": "Plan the bounded change",
        "authorization": authorization(v2=v2),
        "tasks": [task],
    }


def wire(*, v2: bool = False, **changes: Any) -> str:
    value = document(v2=v2)
    value.update(changes)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def rejected(content: Any, code: str, *, version: str = "v1") -> None:
    with pytest.raises(PlanningOutputError) as error:
        parse_planning_output(content, version=version)  # type: ignore[call-overload]
    assert error.value.code == code
    assert str(error.value) == code


def test_parses_v1_and_v2_using_the_existing_domain_models() -> None:
    v1 = parse_planning_output(wire(), version="v1")
    v2 = parse_planning_output(wire(v2=True), version="v2")
    assert isinstance(v1, Plan)
    assert isinstance(v2, PlanV2)
    assert v1.model_dump() == document()
    assert v2.model_dump() == document(v2=True)


def test_whitespace_and_key_order_do_not_change_the_plan() -> None:
    value = document()
    reordered = json.dumps(
        {
            "tasks": value["tasks"],
            "summary": value["summary"],
            "authorization": value["authorization"],
        },
        indent=2,
    )
    assert parse_planning_output(reordered, version="v1") == parse_planning_output(
        wire(), version="v1"
    )


@pytest.mark.parametrize(
    "content",
    [
        "```json\n" + wire() + "\n```",
        wire() + wire(),
        wire()[:-1],
        wire() + " trailing",
        "not json",
    ],
)
def test_requires_one_complete_json_value(content: str) -> None:
    rejected(content, "PLANNING_OUTPUT_JSON_INVALID")


@pytest.mark.parametrize(
    "content",
    [
        '{"summary":"x","summary":"y","authorization":{},"tasks":[]}',
        '{"summary":"x","authorization":{"profile_refs":[],"read_paths":[],"write_paths":[],"budget_ref":"b","checks":[],"delivery":"none","target_branch":"main"},"tasks":[],"extra":NaN}',
        '{"summary":"x","authorization":{},"tasks":[],"extra":Infinity}',
        '{"summary":"x","authorization":{},"tasks":[],"extra":-Infinity}',
        '{"summary":"x","authorization":{},"tasks":[],"extra":1e999}',
    ],
)
def test_rejects_ambiguous_or_non_finite_json(content: str) -> None:
    rejected(content, "PLANNING_OUTPUT_JSON_INVALID")


def test_rejects_duplicate_unicode_escaped_key() -> None:
    rejected(
        '{"summary":"x","\\u0061uthorization":{},"authorization":{},"tasks":[]}',
        "PLANNING_OUTPUT_JSON_INVALID",
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"summary": ""},
        {"tasks": []},
        {"authorization": {}},
        {"unknown": "field"},
        {"run_id": "run", "summary": "x"},
    ],
)
def test_rejects_missing_unknown_and_trusted_identity_fields(changes: dict[str, Any]) -> None:
    rejected(wire(**changes), "PLANNING_OUTPUT_SCHEMA_INVALID")


def test_rejects_nested_unknown_fields_and_strict_type_coercion() -> None:
    value = document()
    value["tasks"][0]["unknown"] = "field"
    rejected(json.dumps(value), "PLANNING_OUTPUT_SCHEMA_INVALID")
    value = document()
    value["tasks"][0]["revision"] = True
    rejected(json.dumps(value), "PLANNING_OUTPUT_SCHEMA_INVALID")
    value = document()
    value["tasks"][0]["required"] = "true"
    rejected(json.dumps(value), "PLANNING_OUTPUT_SCHEMA_INVALID")


def test_v2_requires_v2_task_and_authorization_fields() -> None:
    rejected(wire(), "PLANNING_OUTPUT_SCHEMA_INVALID", version="v2")
    value = document(v2=True)
    del value["tasks"][0]["tools"]
    rejected(json.dumps(value), "PLANNING_OUTPUT_SCHEMA_INVALID", version="v2")


def test_v1_rejects_v2_only_fields() -> None:
    value = document()
    value["tasks"][0]["purpose"] = None
    rejected(json.dumps(value), "PLANNING_OUTPUT_SCHEMA_INVALID")


def test_rejects_bad_encoding_surrogates_subclasses_and_versions() -> None:
    rejected(b"\xff", "PLANNING_OUTPUT_INPUT_INVALID")
    rejected('{"summary":"\\ud800"}', "PLANNING_OUTPUT_INPUT_INVALID")

    class CustomText(str):
        pass

    class CustomBytes(bytes):
        pass

    rejected(CustomText(wire()), "PLANNING_OUTPUT_INPUT_INVALID")
    rejected(CustomBytes(wire().encode()), "PLANNING_OUTPUT_INPUT_INVALID")
    rejected(1, "PLANNING_OUTPUT_INPUT_INVALID")
    rejected(wire(), "PLANNING_OUTPUT_VERSION_INVALID", version="v3")


def test_enforces_byte_and_json_depth_limits_without_truncating() -> None:
    rejected(b" " * (MAX_OUTPUT_BYTES + 1), "PLANNING_OUTPUT_LIMIT_EXCEEDED")
    deeply_nested = "{" * (MAX_DEPTH + 1) + "}" * (MAX_DEPTH + 1)
    rejected(deeply_nested, "PLANNING_OUTPUT_LIMIT_EXCEEDED")
