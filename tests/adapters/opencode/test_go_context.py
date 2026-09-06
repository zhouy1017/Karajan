"""Public request accounting against fixed official tokenizer artifacts, without a provider."""

import json
import os
import shutil
import socket
from pathlib import Path
from typing import Any

import pytest
from karajan.adapters.opencode.go_context import (
    ContextMeasurement,
    GoContextError,
    GoRequestAccounting,
)
from pydantic import ValidationError


@pytest.fixture(scope="module")
def artifacts() -> Path:
    directory = Path(
        os.environ.get("KARAJAN_GO_TOKENIZER_DIRECTORY", ".cache/go-context-artifacts")
    ).resolve()
    if not directory.is_dir():
        if os.environ.get("KARAJAN_REQUIRE_GO_TOKENIZER") == "1":
            pytest.fail("Required pinned official tokenizer artifacts were not provisioned")
        pytest.skip("Pinned official tokenizer artifacts have not been provisioned locally")
    return directory


@pytest.fixture(scope="module")
def accounting(artifacts: Path) -> GoRequestAccounting:
    return GoRequestAccounting(artifacts)


def payload(
    text: str = "Fix the clamp function. 修复边界。\ndef clamp(x): return x\n",
) -> dict[str, Any]:
    return {
        "model": "glm-5.3-flash",
        "stream": True,
        "max_tokens": 128,
        "messages": [
            {"role": "system", "content": "Make only the approved change."},
            {"role": "user", "content": text},
        ],
    }


def measure(accounting: GoRequestAccounting, request: Any, **limits: Any) -> dict[str, Any]:
    options = {
        "approved_input_tokens": 4000,
        "reserved_output_tokens": 128,
        "operating_context_tokens": 8192,
        "fixed_margin": 16,
        "ratio_margin_basis_points": 1000,
    }
    options.update(limits)
    return accounting.measure(request, **options)


def test_counts_actual_official_template_without_exposing_request(
    accounting: GoRequestAccounting,
) -> None:
    request = payload("PRIVATE_INPUT_CANARY 中英文与代码 x = 1")
    before = json.dumps(request)
    receipt = measure(accounting, request)
    assert receipt["local_input_tokens"] > 0
    assert receipt["accounted_input_tokens"] > receipt["local_input_tokens"]
    assert receipt["requested_output_tokens"] == 128
    assert receipt["measurement_confidence"] == "local_estimate"
    assert "PRIVATE_INPUT_CANARY" not in json.dumps(receipt)
    assert "PRIVATE_INPUT_CANARY" not in json.dumps(accounting.source())
    assert json.dumps(request) == before
    assert receipt == measure(accounting, request)


def tool_history() -> dict[str, Any]:
    request = payload()
    request["tools"] = [
        {
            "type": "function",
            "function": {
                "name": "read",
                "description": "Read the approved source file.",
                "parameters": {
                    "type": "object",
                    "properties": {"filePath": {"type": "string"}},
                    "required": ["filePath"],
                    "additionalProperties": False,
                },
                "strict": True,
            },
        }
    ]
    request["messages"].extend(
        [
            {
                "role": "assistant",
                "content": None,
                "reasoning_content": "PRIVATE_REASONING_CANARY inspect the source first.",
                "tool_calls": [
                    {
                        "id": "call_read_1",
                        "type": "function",
                        "function": {"name": "read", "arguments": '{"filePath":"src/clamp.py"}'},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_read_1",
                "content": "def clamp(x, lo, hi):\n    return x\n",
            },
        ]
    )
    return request


def test_counts_json_string_tool_arguments_reasoning_and_complete_tool_history(
    accounting: GoRequestAccounting,
) -> None:
    initial = measure(accounting, payload())
    request = tool_history()
    original = json.dumps(request)
    after_tool = measure(accounting, request)
    assert json.dumps(request) == original
    assert after_tool["local_input_tokens"] > initial["local_input_tokens"]
    request["messages"][-1]["content"] += "\n# additional relevant source\n" * 80
    grown = measure(accounting, request)
    assert grown["local_input_tokens"] > after_tool["local_input_tokens"]
    assert after_tool["request_digest"] != grown["request_digest"]
    assert "PRIVATE_REASONING_CANARY" not in json.dumps(after_tool)


@pytest.mark.parametrize(
    "change",
    [
        {"messages": [{"role": "developer", "content": "Must not disappear"}]},
        {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": "x"}]}]},
        {"messages": [{"role": "user", "content": "x", "unknown": "Must not disappear"}]},
        {"response_format": {"type": "json_schema", "json_schema": {"name": "x"}}},
        {"chat_template_kwargs": {"clear_thinking": True}},
        {"clear_thinking": True},
        {"stream_options": {"include_usage": 1}},
    ],
)
def test_rejects_shapes_that_could_lose_input_in_the_reference_template(
    accounting: GoRequestAccounting, change: dict[str, Any]
) -> None:
    request = payload()
    request.update(change)
    with pytest.raises(GoContextError, match="^GO_CONTEXT_UNSUPPORTED_SHAPE$"):
        measure(accounting, request)


@pytest.mark.parametrize(
    ("limits", "reason"),
    [
        ({"approved_input_tokens": 1}, "GO_CONTEXT_INPUT_LIMIT_EXCEEDED"),
        ({"reserved_output_tokens": 127}, "GO_CONTEXT_OUTPUT_LIMIT_EXCEEDED"),
        ({"operating_context_tokens": 129}, "GO_CONTEXT_WINDOW_EXCEEDED"),
        ({"operating_context_tokens": 1_000_001}, "GO_CONTEXT_INVALID_LIMITS"),
        ({"approved_input_tokens": True}, "GO_CONTEXT_INVALID_LIMITS"),
        ({"fixed_margin": -1}, "GO_CONTEXT_INVALID_LIMITS"),
        ({"ratio_margin_basis_points": 1.5}, "GO_CONTEXT_INVALID_LIMITS"),
    ],
)
def test_limit_failures_are_explicit_before_a_receipt_can_authorize_sending(
    accounting: GoRequestAccounting, limits: dict[str, Any], reason: str
) -> None:
    with pytest.raises(GoContextError, match=f"^{reason}$") as failure:
        measure(accounting, payload(), **limits)
    assert failure.value.code == reason


def test_matches_manually_expanded_official_template_and_rounds_margin_up(
    accounting: GoRequestAccounting,
) -> None:
    request = payload()
    request["messages"] = [{"role": "user", "content": "Hi"}]
    # Independent worked example of the pinned template's prefix/role/generation tokens:
    # [gMASK]<sop><|system|>Reasoning Effort: Max<|user|>Hi<|assistant|><think>
    # Direct official tokenizer ids: 154822,154824,154826,25062,287,29905,371,
    # 25,7487,154827,13041,154828,154841. 13 input + 16 fixed + ceil(13/10) = 31.
    receipt = measure(accounting, request, approved_input_tokens=31, operating_context_tokens=159)
    assert receipt["local_input_tokens"] == 13
    assert receipt["margin_tokens"] == 18
    assert receipt["accounted_input_tokens"] == 31
    with pytest.raises(GoContextError, match="^GO_CONTEXT_WINDOW_EXCEEDED$"):
        measure(accounting, request, approved_input_tokens=31, operating_context_tokens=158)


def test_text_parts_are_all_included_and_generation_output_does_not_change_input_count(
    accounting: GoRequestAccounting,
) -> None:
    request = payload("first part 第二部分")
    before = measure(accounting, request)
    request["messages"][-1]["content"] = [
        {"type": "text", "text": "first part "},
        {"type": "text", "text": "第二部分"},
    ]
    request["max_tokens"] = 64
    after = measure(accounting, request)
    assert before["local_input_tokens"] == after["local_input_tokens"]
    assert after["requested_output_tokens"] == 64
    assert after["reserved_output_tokens"] == 128


@pytest.mark.parametrize("arguments", ["[]", '{"x":1,"x":2}', '{"x":NaN}', '{"x":', {}])
def test_invalid_tool_arguments_never_become_empty_input(
    accounting: GoRequestAccounting, arguments: Any
) -> None:
    request = tool_history()
    request["messages"][-2]["tool_calls"][0]["function"]["arguments"] = arguments
    with pytest.raises(GoContextError, match="^GO_CONTEXT_INVALID_TOOL_ARGUMENTS$"):
        measure(accounting, request)


@pytest.mark.parametrize("broken", ["missing_result", "unknown_result", "duplicate_call"])
def test_incomplete_or_ambiguous_tool_history_is_rejected(
    accounting: GoRequestAccounting, broken: str
) -> None:
    request = tool_history()
    if broken == "missing_result":
        request["messages"].pop()
    elif broken == "unknown_result":
        request["messages"][-1]["tool_call_id"] = "another_call"
    else:
        request["messages"][-2]["tool_calls"] *= 2
    with pytest.raises(GoContextError, match="^GO_CONTEXT_UNSUPPORTED_SHAPE$"):
        measure(accounting, request)


def test_missing_artifact_is_an_explicit_error_without_a_download(tmp_path: Path) -> None:
    with pytest.raises(GoContextError, match="^GO_CONTEXT_ARTIFACT_MISSING$"):
        GoRequestAccounting(tmp_path)


@pytest.mark.parametrize("name", ["tokenizer.json", "tokenizer_config.json", "chat_template.jinja"])
def test_changed_artifact_is_rejected_before_loading(
    artifacts: Path, tmp_path: Path, name: str
) -> None:
    shutil.copytree(artifacts, tmp_path / "artifacts")
    (tmp_path / "artifacts" / name).write_text("SYNTHETIC_PRIVATE_FILE", encoding="utf-8")
    with pytest.raises(GoContextError, match="^GO_CONTEXT_ARTIFACT_CHANGED$") as failure:
        GoRequestAccounting(tmp_path / "artifacts")
    assert "SYNTHETIC_PRIVATE_FILE" not in str(failure.value)
    assert str(tmp_path) not in str(failure.value)


def test_public_constructor_and_measure_work_with_network_connect_blocked(
    artifacts: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("NETWORK_NOT_PERMITTED")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    offline = GoRequestAccounting(artifacts)
    assert measure(offline, tool_history())["measurement_confidence"] == "local_estimate"


def test_source_and_receipts_are_independent_allowlisted_snapshots(
    accounting: GoRequestAccounting,
) -> None:
    source = accounting.source()
    assert source["declared_capacity"]["context_tokens"] == 1_000_000
    assert source["server_exact_accounting"] is False
    assert source["qualification_granted"] is False
    assert "go-context-artifacts" not in json.dumps(source)
    source["artifacts"]["tokenizer.json"]["sha256"] = "changed"
    assert accounting.source()["artifacts"]["tokenizer.json"]["sha256"] != "changed"
    receipt = measure(accounting, payload())
    receipt["accounted_input_tokens"] = 1
    with pytest.raises(ValidationError):
        ContextMeasurement.model_validate(receipt)
    receipt = measure(accounting, payload())
    receipt["private_text"] = "must not persist"
    with pytest.raises(ValidationError):
        ContextMeasurement.model_validate(receipt)


@pytest.mark.parametrize("value", [[], None, {"model": "another-model", "stream": True}])
def test_invalid_root_has_only_a_fixed_error(accounting: GoRequestAccounting, value: Any) -> None:
    with pytest.raises(GoContextError, match="^GO_CONTEXT_UNSUPPORTED_SHAPE$"):
        measure(accounting, value)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"model": "another-model"}, "GO_CONTEXT_INVALID_MODEL"),
        ({"max_tokens": 131_073}, "GO_CONTEXT_OUTPUT_LIMIT_EXCEEDED"),
        ({"max_tokens": True}, "GO_CONTEXT_OUTPUT_LIMIT_EXCEEDED"),
        (
            {"messages": [{"role": "user", "content": "x" * 262_144}]},
            "GO_CONTEXT_REQUEST_TOO_LARGE",
        ),
    ],
)
def test_model_output_and_body_boundaries_are_not_relaxed_by_large_approved_input(
    accounting: GoRequestAccounting, change: dict[str, Any], reason: str
) -> None:
    request = payload()
    request.update(change)
    with pytest.raises(GoContextError, match=f"^{reason}$"):
        measure(
            accounting, request, approved_input_tokens=800_000, operating_context_tokens=1_000_000
        )
