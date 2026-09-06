import json

import pytest
from karajan.adapters.deepseek import ProtocolError, observe_response, prepare_request


def test_request_binds_model_output_limit_and_disables_default_thinking() -> None:
    prepared = prepare_request(
        json.dumps(
            {
                "model": "deepseek-v4-flash",
                "messages": [{"role": "user", "content": "Read fixture.txt"}],
                "max_tokens": 256,
                "stream": True,
            }
        ).encode(),
        model="deepseek-v4-flash",
        output_limit=256,
    )
    wire = json.loads(prepared.body)
    assert prepared.endpoint == "https://api.deepseek.com/chat/completions"
    assert wire["thinking"] == {"type": "disabled"}
    assert wire["stream_options"] == {"include_usage": True}
    assert wire["max_tokens"] == 256
    assert prepared.model == "deepseek-v4-flash"


@pytest.mark.parametrize(
    "change",
    [
        {"model": "other-provider-model"},
        {"max_tokens": None},
        {"max_tokens": True},
        {"max_tokens": 257},
        {"thinking": {"type": "enabled"}},
        {"base_url": "https://unexpected.invalid"},
        {"stream": "true"},
        {"tools": [{"type": "function", "function": {"name": "bash", "parameters": {}}}]},
        {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {}}]}]},
    ],
)
def test_request_rejects_profile_drift_and_unbounded_or_unsupported_parameters(
    change: dict[str, object],
) -> None:
    body = {
        "model": "deepseek-v4-flash",
        "max_tokens": 256,
        "stream": True,
        "messages": [{"role": "user", "content": "fixture"}],
        **change,
    }
    with pytest.raises(ProtocolError):
        prepare_request(json.dumps(body).encode(), model="deepseek-v4-flash", output_limit=256)


@pytest.mark.parametrize(
    "payload",
    [
        b'{"model":"a","model":"b"}',
        b"[]",
        b"null",
        b'{"temperature":NaN}',
        b'{"messages":"\\ud800"}',
        b"\xff",
        b" " * 1_000_001,
    ],
    ids=["duplicate", "array", "null", "nan", "surrogate", "encoding", "oversize"],
)
def test_untrusted_request_is_bounded_and_unambiguous(payload: bytes) -> None:
    with pytest.raises(ProtocolError):
        prepare_request(payload, model="deepseek-v4-flash", output_limit=256)


def stream_fixture(*, usage: object = None, done: bool = True) -> bytes:
    chunks = []
    for delta, finish in [
        ({"role": "assistant"}, None),
        ({"content": "fixture OK"}, None),
        ({}, "stop"),
    ]:
        chunk = {
            "id": "request-fixture",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "deepseek-v4-flash",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            "usage": usage if finish else None,
        }
        chunks.append("data: " + json.dumps(chunk) + "\n\n")
    return ("".join(chunks) + ("data: [DONE]\n\n" if done else "")).encode()


def test_final_content_chunk_usage_is_observed_without_double_counting_reasoning() -> None:
    result = observe_response(
        stream_fixture(
            usage={
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
                "prompt_cache_hit_tokens": 60,
                "prompt_cache_miss_tokens": 40,
                "completion_tokens_details": {"reasoning_tokens": 5},
            }
        ),
        model="deepseek-v4-flash",
        content_type="text/event-stream",
        status=200,
    )
    assert result.status == "completed"
    assert result.content == "fixture OK"
    assert result.request_id == "request-fixture"
    assert result.usage == {
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "total_tokens": 120,
        "prompt_cache_hit_tokens": 60,
        "prompt_cache_miss_tokens": 40,
        "reasoning_tokens": 5,
    }
    assert result.usage_status == "observed"
    assert result.actual_charge is None


def test_missing_usage_is_unknown_and_missing_done_cannot_complete() -> None:
    result = observe_response(
        stream_fixture(),
        model="deepseek-v4-flash",
        content_type="text/event-stream",
        status=200,
    )
    assert result.status == "completed"
    assert result.usage_status == "unknown"
    assert result.usage["prompt_cache_hit_tokens"] is None
    partial = observe_response(
        stream_fixture(done=False),
        model="deepseek-v4-flash",
        content_type="text/event-stream",
        status=200,
    )
    assert partial.status == "unknown"
    assert "STREAM_INCOMPLETE" in partial.reason_codes


@pytest.mark.parametrize(
    "change",
    [
        {"total_tokens": 121},
        {"prompt_cache_hit_tokens": 101},
        {"completion_tokens": True},
        {"completion_tokens_details": {"reasoning_tokens": 21}},
    ],
)
def test_inconsistent_usage_is_rejected_without_inventing_a_bill(change: dict[str, object]) -> None:
    result = observe_response(
        stream_fixture(
            usage={
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
                "prompt_cache_hit_tokens": 60,
                "prompt_cache_miss_tokens": 40,
                **change,
            }
        ),
        model="deepseek-v4-flash",
        content_type="text/event-stream",
        status=200,
    )
    assert result.status == "failed"
    assert "USAGE_INVALID" in result.reason_codes
    assert result.actual_charge is None


@pytest.mark.parametrize(
    "mutator",
    [
        lambda body: body.replace(b"deepseek-v4-flash", b"other-model"),
        lambda body: body.replace(b'"index": 0', b'"index": 1'),
        lambda body: body.replace(b'"finish_reason": "stop"', b'"finish_reason": "mystery"'),
        lambda body: body.replace(b"data: [DONE]", b"data: [DONE]\n\ndata: {}"),
        lambda body: body.replace(b'"created": 1', b'"created": true'),
    ],
)
def test_stream_drift_or_extra_completion_cannot_be_accepted(mutator: object) -> None:
    assert callable(mutator)
    result = observe_response(
        mutator(stream_fixture()),
        model="deepseek-v4-flash",
        content_type="text/event-stream",
        status=200,
    )
    assert result.status == "failed"


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (400, "PROVIDER_REQUEST_INVALID"),
        (401, "PROVIDER_AUTH_REQUIRED"),
        (402, "PROVIDER_BALANCE_EXHAUSTED"),
        (422, "PROVIDER_PARAMETER_INVALID"),
        (429, "PROVIDER_RATE_LIMITED"),
        (500, "PROVIDER_SERVER_ERROR"),
        (503, "PROVIDER_OVERLOADED"),
    ],
)
def test_http_error_is_observation_without_automatic_retry_or_fallback(
    status: int,
    reason: str,
) -> None:
    result = observe_response(
        b'{"error":{"message":"secret synthetic diagnostic"}}',
        model="deepseek-v4-flash",
        content_type="application/json",
        status=status,
    )
    assert result.status == "failed"
    assert result.reason_codes == (reason,)
    assert "secret" not in repr(result)


@pytest.mark.parametrize("stream", [True, False])
def test_read_tool_call_is_observed_for_json_and_split_sse(stream: bool) -> None:
    base = {"id": "request-tool", "created": 1, "model": "deepseek-v4-flash"}
    call = {
        "id": "call-read",
        "type": "function",
        "function": {"name": "read", "arguments": '{"filePath":"fixture.txt"}'},
    }
    if stream:
        deltas = [
            {
                "tool_calls": [
                    {"index": 0, **call, "function": {"name": "read", "arguments": '{"file'}}
                ]
            },
            {"tool_calls": [{"index": 0, "function": {"arguments": 'Path":"fixture.txt"}'}}]},
            {},
        ]
        payload = (
            b"".join(
                (
                    "data: "
                    + json.dumps(
                        {
                            **base,
                            "object": "chat.completion.chunk",
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": delta,
                                    "finish_reason": "tool_calls" if index == 2 else None,
                                }
                            ],
                        }
                    )
                    + "\n\n"
                ).encode()
                for index, delta in enumerate(deltas)
            )
            + b"data: [DONE]\n\n"
        )
    else:
        payload = json.dumps(
            {
                **base,
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": None, "tool_calls": [call]},
                        "finish_reason": "tool_calls",
                    }
                ],
            }
        ).encode()
    result = observe_response(
        payload,
        model="deepseek-v4-flash",
        status=200,
        content_type="text/event-stream" if stream else "application/json",
    )
    assert result.status == "tool_requested"
    assert result.tool_calls == (call,)
    assert result.usage_status == "unknown"


@pytest.mark.parametrize("choice", [None, 0, [], "malformed"])
def test_malformed_json_choice_returns_failed_observation(choice: object) -> None:
    result = observe_response(
        json.dumps({"object": "chat.completion", "choices": [choice]}).encode(),
        model="deepseek-v4-flash",
        content_type="application/json",
        status=200,
    )
    assert result.status == "failed"
    assert result.reason_codes == ("CHOICE_UNSUPPORTED",)


def test_truncated_sse_done_event_cannot_complete() -> None:
    result = observe_response(
        stream_fixture().removesuffix(b"\n\n"),
        model="deepseek-v4-flash",
        content_type="text/event-stream",
        status=200,
    )
    assert result.status == "unknown"
    assert result.reason_codes == ("STREAM_INCOMPLETE",)


def test_invalid_utf8_after_usage_keeps_observed_usage_without_completing() -> None:
    result = observe_response(
        stream_fixture(
            usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}, done=False
        )
        + b"data: \xff\n\n",
        model="deepseek-v4-flash",
        content_type="text/event-stream",
        status=200,
    )
    assert result.status == "failed"
    assert result.usage_status == "observed"
    assert result.usage["total_tokens"] == 5
    assert result.actual_charge is None


@pytest.mark.parametrize("role", [[], {}, 3])
def test_request_malformed_role_uses_stable_protocol_error(role: object) -> None:
    with pytest.raises(ProtocolError):
        prepare_request(
            json.dumps(
                {
                    "model": "deepseek-v4-flash",
                    "max_tokens": 32,
                    "stream": True,
                    "messages": [{"role": role, "content": "fixture"}],
                }
            ).encode(),
            model="deepseek-v4-flash",
            output_limit=256,
        )


def test_text_parts_are_normalized_to_supported_text_wire_shape() -> None:
    prepared = prepare_request(
        json.dumps(
            {
                "model": "deepseek-v4-flash",
                "max_tokens": 32,
                "stream": True,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Hello "},
                            {"type": "text", "text": "fixture"},
                        ],
                    }
                ],
            }
        ).encode(),
        model="deepseek-v4-flash",
        output_limit=256,
    )
    assert json.loads(prepared.body)["messages"][0]["content"] == "Hello fixture"


@pytest.mark.parametrize("reasoning", [None, "", "unexpected thinking"])
def test_non_thinking_sdk_message_normalizes_only_empty_reasoning(reasoning: object) -> None:
    payload = json.dumps(
        {
            "model": "deepseek-v4-flash",
            "max_tokens": 32,
            "stream": True,
            "messages": [
                {"role": "assistant", "content": "fixture", "reasoning_content": reasoning}
            ],
        }
    ).encode()
    if reasoning:
        with pytest.raises(ProtocolError):
            prepare_request(payload, model="deepseek-v4-flash", output_limit=256)
    else:
        prepared = prepare_request(payload, model="deepseek-v4-flash", output_limit=256)
        assert "reasoning_content" not in json.loads(prepared.body)["messages"][0]


def test_unexpected_thinking_fails_closed_but_keeps_later_usage() -> None:
    payload = stream_fixture(
        usage={
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
        }
    ).replace(b'"content": "fixture OK"', b'"reasoning_content": "unexpected private thinking"')
    result = observe_response(
        payload, model="deepseek-v4-flash", content_type="text/event-stream", status=200
    )
    assert result.status == "failed"
    assert result.reason_codes == ("THINKING_UNEXPECTED",)
    assert result.usage_status == "observed"
    assert result.usage["total_tokens"] == 15
    assert result.actual_charge is None
    assert "unexpected private thinking" not in repr(result)


def test_incomplete_tool_arguments_are_not_exposed_as_executable_calls() -> None:
    payload = (
        "data: "
        + json.dumps(
            {
                "id": "tool-partial",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "deepseek-v4-flash",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": None,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "read-partial",
                                    "type": "function",
                                    "function": {"name": "read", "arguments": '{"file'},
                                }
                            ]
                        },
                    }
                ],
            }
        )
        + "\n\n"
    ).encode()
    result = observe_response(
        payload, model="deepseek-v4-flash", content_type="text/event-stream", status=200
    )
    assert result.status == "unknown"
    assert result.reason_codes == ("STREAM_INCOMPLETE",)
    assert result.tool_calls == ()


@pytest.mark.parametrize("parameter", ["temperature", "top_p"])
def test_huge_json_integer_returns_stable_error_instead_of_float_overflow(parameter: str) -> None:
    payload = json.dumps(
        {
            "model": "deepseek-v4-flash",
            "max_tokens": 32,
            "stream": True,
            "messages": [{"role": "user", "content": "fixture"}],
            parameter: 10**400,
        }
    ).encode()
    with pytest.raises(ProtocolError, match="REQUEST_INVALID"):
        prepare_request(payload, model="deepseek-v4-flash", output_limit=256)
