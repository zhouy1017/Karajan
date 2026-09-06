"""Nullable Chat Completions name fragments through the public relay transport."""

import pytest
from test_go_relay import answer, event, post, running, stream


def tool_frame(function):
    return event(
        choices=[
            {
                "index": 0,
                "delta": {"tool_calls": [{"index": 0, "function": function}]},
                "finish_reason": None,
            }
        ]
    )


def response(functions):
    return stream(
        *(tool_frame(function) for function in functions),
        event(choices=[{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]),
    )


@pytest.mark.parametrize("names", [["read", None], [None, "re", None, "ad"], ["edit", None]])
def test_null_is_no_new_name_fragment_but_records_content_free_count(names):
    raw = response([{"name": name, "arguments": ""} for name in names])
    with running(lambda request: answer(raw)) as (relay, upstream):
        result = post(relay)
        assert result.status_code == 200
        receipt = relay.receipts[0]
        assert receipt["protocol_passed"] is True
        assert receipt["tool_names"] == ["".join(n for n in names if n is not None)]
        assert receipt["tool_name_null_fragments"] == names.count(None)
        assert len(upstream) == 1


def test_absent_name_keeps_legacy_receipt_shape():
    raw = response([{"name": "read"}, {"arguments": "{}"}])
    with running(lambda request: answer(raw)) as (relay, _upstream):
        assert post(relay).status_code == 200
        assert "tool_name_null_fragments" not in relay.receipts[0]


@pytest.mark.parametrize(
    ("names", "reason"),
    [
        ([None, None], "UNAPPROVED_TOOL"),
        (["rea", None], "UNAPPROVED_TOOL"),
        (["shell", None], "UNAPPROVED_TOOL"),
        (["read", "read", None], "UNAPPROVED_TOOL"),
        (["read", False], "INVALID_TOOL_NAME"),
        (["read", 0], "INVALID_TOOL_NAME"),
        (["read", ["read"]], "INVALID_TOOL_NAME"),
    ],
)
def test_nullable_names_never_expand_final_allowlist_or_accept_other_types(names, reason):
    raw = response([{"name": name} for name in names])
    with running(lambda request: answer(raw)) as (relay, _upstream):
        assert post(relay).status_code == 502
        assert relay.receipts[0]["protocol_passed"] is False
        assert relay.receipts[0]["reason_codes"] == [reason]
