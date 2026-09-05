"""Public offline Claude replay, with no runtime launch or account fixture."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def invoke(tmp_path: Path, document: dict) -> tuple[int, dict]:
    source = tmp_path / "claude-replay.json"
    source.write_text(json.dumps(document), encoding="utf-8")
    return invoke_path(source)


def invoke_path(source: Path) -> tuple[int, dict]:
    result = subprocess.run(
        [sys.executable, "-m", "karajan.adapters.claude", "replay", str(source)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )
    assert result.stderr == ""
    return result.returncode, json.loads(result.stdout)


def fixture() -> dict:
    return json.loads((ROOT / "examples/claude/completed.json").read_text(encoding="utf-8"))


def seal(document: dict) -> None:
    document["profile_digest"] = hashlib.sha256(
        json.dumps(document["profile"], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def assistant_step(event_id: str, message_id: str = "msg-1") -> dict:
    return {
        "kind": "native",
        "at": "2026-09-05T13:30:00.500Z",
        "message": {
            "type": "assistant",
            "uuid": event_id,
            "session_id": fixture()["session_id"],
            "parent_tool_use_id": None,
            "message": {
                "id": message_id,
                "model": "claude-fixture-model-v1",
                "content": [{"type": "text", "text": "private generated text"}],
                "usage": {
                    "input_tokens": 12,
                    "output_tokens": 1,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 2,
                },
            },
        },
    }


def test_completed_fragment_reports_a_result_without_qualifying_subscription_tools(
    tmp_path: Path,
) -> None:
    code, report = invoke(tmp_path, fixture())

    assert code == 0
    assert report["status"] == "passed"
    assert report["result"]["state"] == "completed"
    assert report["result"]["text_length"] == 16
    assert report["qualification"]["live_status"] == "not_run"
    assert report["qualification"]["dispatch_eligible"] is False


@pytest.mark.parametrize(
    ("subtype", "api_status", "outcome"),
    [
        ("error_during_execution", 401, "authentication_error"),
        ("error_during_execution", 429, "rate_limited"),
        ("error_during_execution", None, "execution_error"),
        ("error_max_turns", None, "turn_limit"),
        ("error_max_budget_usd", None, "runtime_budget_limit"),
    ],
)
def test_error_results_are_mapped_without_echoing_native_error_text(
    tmp_path: Path, subtype: str, api_status: int | None, outcome: str
) -> None:
    document = fixture()
    result = document["steps"][1]["message"]
    result.update(
        subtype=subtype,
        is_error=True,
        api_error_status=api_status,
        errors=["FAKE-PRIVATE-CREDENTIAL in native diagnostic"],
    )
    result.pop("result")

    code, report = invoke(tmp_path, document)

    assert code == 0
    assert report["result"]["state"] == "failed"
    assert report["result"]["category"] == outcome
    assert "FAKE-PRIVATE" not in json.dumps(report)
    assert report["stopping"]["main_process"] == "unknown"


@pytest.mark.parametrize("variant", ["missing-result", "boolean-duration", "error-contradiction"])
def test_malformed_result_is_not_a_success(tmp_path: Path, variant: str) -> None:
    document = fixture()
    result = document["steps"][1]["message"]
    if variant == "missing-result":
        result.pop("result")
    elif variant == "boolean-duration":
        result["duration_ms"] = True
    else:
        result["is_error"] = True

    code, report = invoke(tmp_path, document)

    assert code == 1
    assert report["reason_codes"] == ["NATIVE_RECORD_INVALID"]
    assert report["result"]["state"] == "unknown"
    assert report["stopping"]["process_tree"] == "unknown"
    assert "synthetic answer" not in json.dumps(report)


@pytest.mark.parametrize("variant", ["version", "unknown-field", "boolean-fence", "unbounded"])
def test_replay_envelope_cannot_silently_change_runtime_or_authority(
    tmp_path: Path, variant: str
) -> None:
    document = fixture()
    if variant == "version":
        document["runtime_version"] = "2.1.261"
    elif variant == "unknown-field":
        document["api_key"] = "FAKE-PRIVATE-CREDENTIAL"
    elif variant == "boolean-fence":
        document["attempt"]["fence"] = True
    else:
        document["max_attempt_duration_seconds"] = None

    code, report = invoke(tmp_path, document)

    assert code == 1
    assert report["reason_codes"] == ["INPUT_INVALID"]
    assert "FAKE-PRIVATE" not in json.dumps(report)


@pytest.mark.parametrize("variant", ["digest", "binding", "permissions", "settings"])
def test_attempt_must_match_the_sealed_supported_profile(tmp_path: Path, variant: str) -> None:
    document = fixture()
    if variant == "digest":
        document["profile"]["revision"] = 2
    elif variant == "binding":
        document["attempt"]["requested_binding"]["model_id"] = "other-model"
    elif variant == "permissions":
        document["attempt"]["permissions"].append("shell")
    else:
        document["profile"]["binding"]["native_settings"]["fallback_model"] = "cheaper-model"
        document["attempt"]["requested_binding"] = document["profile"]["binding"]
        seal(document)

    code, report = invoke(tmp_path, document)

    assert code == 1
    assert report["result"]["state"] == "unknown"
    assert report["reason_codes"] == [
        "PROFILE_UNSUPPORTED" if variant == "settings" else "ATTEMPT_PROFILE_MISMATCH"
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("model", "other-model"),
        ("claude_code_version", "2.1.261"),
        ("cwd", "C:/other"),
        ("permissionMode", "bypassPermissions"),
        ("tools", ["Read", "Bash"]),
        ("mcp_servers", [{"name": "unexpected"}]),
    ],
)
def test_native_init_drift_prevents_result_acceptance(
    tmp_path: Path, field: str, value: object
) -> None:
    document = fixture()
    document["steps"][0]["message"][field] = value

    code, report = invoke(tmp_path, document)

    assert code == 1
    assert "NATIVE_BINDING_MISMATCH" in report["reason_codes"]
    assert report["result"]["state"] == "unknown"


def test_init_is_reported_evidence_not_full_settings_or_authentication_confirmation(
    tmp_path: Path,
) -> None:
    _, report = invoke(tmp_path, fixture())

    assert report["binding"]["native_reported"]["model_id"] == "claude-fixture-model-v1"
    assert report["binding"]["native_reported"]["auth_mode"] is None
    assert report["binding"]["native_reported"]["billing_path"] is None
    assert report["binding"]["settings_confirmation"] == "partial"
    assert report["capabilities"]["tool_isolation"] == "not_run"


@pytest.mark.parametrize("variant", ["session", "missing-init", "unknown-event"])
def test_native_sequence_is_bound_to_one_session(tmp_path: Path, variant: str) -> None:
    document = fixture()
    if variant == "session":
        document["steps"][1]["message"]["session_id"] = "other-session"
    elif variant == "missing-init":
        document["steps"].pop(0)
    else:
        document["steps"][1]["message"]["type"] = "invented.grant"

    code, report = invoke(tmp_path, document)

    assert code == 1
    assert report["result"]["state"] == "unknown"
    assert report["qualification"]["dispatch_eligible"] is False


def test_usage_deduplicates_assistant_fragments_and_keeps_final_usage_separate(
    tmp_path: Path,
) -> None:
    document = fixture()
    first, second = assistant_step("assistant-1"), assistant_step("assistant-2")
    second["message"]["message"]["content"][0]["text"] = "another fragment"
    document["steps"][1:1] = [first, second, first]

    code, report = invoke(tmp_path, document)

    assert code == 0
    assert report["usage"]["partial_main_input"] == {
        "input_tokens": 12,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 2,
    }
    assert report["usage"]["main_loop_final"]["output_tokens"] == 4
    assert report["usage"]["client_cost_estimate_usd"] == 0.001
    assert report["usage"]["cash_charged_usd"] is None
    assert report["usage"]["account_remaining"] is None
    assert report["events"]["duplicates"] == 1
    assert "private generated" not in json.dumps(report)


def test_truncated_stream_keeps_partial_usage_without_inventing_zero_cost(tmp_path: Path) -> None:
    document = fixture()
    document["steps"][1:] = [assistant_step("assistant-1")]

    code, report = invoke(tmp_path, document)

    assert code == 1
    assert report["status"] == "not_run"
    assert report["reason_codes"] == ["TERMINAL_RESULT_MISSING"]
    assert report["usage"]["main_loop_final"] is None
    assert report["usage"]["partial_main_input"]["input_tokens"] == 12
    assert report["usage"]["client_cost_estimate_usd"] is None
    assert report["result"]["state"] == "unknown"


def test_conflicting_event_replay_cannot_replace_the_first_result(tmp_path: Path) -> None:
    document = fixture()
    second = json.loads(json.dumps(document["steps"][1]))
    second["message"]["result"] = "a different result"
    document["steps"].append(second)

    code, report = invoke(tmp_path, document)

    assert code == 1
    assert report["reason_codes"] == ["EVENT_ID_CONFLICT"]
    assert report["result"]["text_length"] == 16


@pytest.mark.parametrize("action", ["cancel_requested", "authorization_revoked", "fence_replaced"])
def test_invalidation_rejects_late_result_but_keeps_usage(tmp_path: Path, action: str) -> None:
    document = fixture()
    document["steps"].insert(
        1,
        {
            "kind": "controller",
            "at": "2026-09-05T13:30:00.500Z",
            "event_id": "control-1",
            "action": action,
            "attempt_id": document["attempt"]["id"],
            "fence": document["attempt"]["fence"],
        },
    )

    code, report = invoke(tmp_path, document)

    assert code == 0
    assert report["result"]["state"] == "invalidated"
    assert report["result_decisions"] == {"accepted": 0, "rejected": 1}
    assert report["usage"]["main_loop_final"]["output_tokens"] == 4
    assert report["stopping"] == {
        "main_process": "unknown",
        "process_tree": "unknown",
        "remote": "unknown",
    }


def test_first_completion_wins_while_second_usage_observation_is_retained(tmp_path: Path) -> None:
    document = fixture()
    second = json.loads(json.dumps(document["steps"][1]))
    second["message"].update(uuid="different-result-event", result="second completion")
    second["message"]["usage"]["output_tokens"] = 8
    document["steps"].append(second)

    code, report = invoke(tmp_path, document)

    assert code == 0
    assert report["result"]["text_length"] == 16
    assert report["result_decisions"] == {"accepted": 1, "rejected": 1}
    assert report["usage"]["terminal_observations"] == 2
    assert report["usage"]["terminal_snapshots"][1]["usage"]["output_tokens"] == 8
    assert report["usage"]["main_loop_final"]["output_tokens"] == 4


def test_declared_duration_bounds_result_acceptance_without_claiming_kill(tmp_path: Path) -> None:
    document = fixture()
    document["steps"][1]["at"] = "2026-09-05T13:31:00Z"

    code, report = invoke(tmp_path, document)

    assert code == 0
    assert report["result"]["state"] == "invalidated"
    assert report["invalidation"] == "duration_exceeded"
    assert report["result_decisions"]["accepted"] == 0
    assert report["usage"]["main_loop_final"]["output_tokens"] == 4
    assert report["stopping"]["process_tree"] == "unknown"


@pytest.mark.parametrize("source", ["system", "result"])
def test_permission_denial_blocks_delivery_without_issuing_a_grant(
    tmp_path: Path, source: str
) -> None:
    document = fixture()
    if source == "system":
        document["steps"].insert(
            1,
            {
                "kind": "native",
                "at": "2026-09-05T13:30:00.500Z",
                "message": {
                    "type": "system",
                    "subtype": "permission_denied",
                    "uuid": "denial-1",
                    "session_id": document["session_id"],
                    "diagnostic": "FAKE-PRIVATE",
                },
            },
        )
    else:
        document["steps"][1]["message"]["permission_denials"] = [
            {"tool_name": "Read", "tool_use_id": "read-1", "tool_input": {"path": "FAKE-PRIVATE"}}
        ]

    code, report = invoke(tmp_path, document)

    assert code == 0
    assert report["result"]["state"] == "blocked"
    assert report["permissions"]["requires_new_attempt"] is True
    assert report["permissions"]["outbound_grants"] == 0
    assert report["capabilities"]["dynamic_permission_grant"] == "unsupported"
    assert "FAKE-PRIVATE" not in json.dumps(report)


def test_tool_request_is_only_observed_and_unlisted_tools_close_the_gate(tmp_path: Path) -> None:
    document = fixture()
    tool = assistant_step("tool-request")
    tool["message"]["message"]["content"] = [
        {
            "type": "tool_use",
            "id": "tool-1",
            "name": "Bash",
            "input": {"command": "FAKE-PRIVATE"},
        }
    ]
    document["steps"].insert(1, tool)

    code, report = invoke(tmp_path, document)

    assert code == 1
    assert report["reason_codes"] == ["TOOL_OUTSIDE_PROFILE"]
    assert report["result_decisions"]["accepted"] == 0
    assert "FAKE-PRIVATE" not in json.dumps(report)


def test_native_control_request_is_unsupported_and_cannot_grant_permission(tmp_path: Path) -> None:
    document = fixture()
    document["steps"][1]["message"].update(
        type="control_request", request={"subtype": "can_use_tool"}
    )

    code, report = invoke(tmp_path, document)

    assert code == 1
    assert report["reason_codes"] == ["PROTOCOL_UNSUPPORTED"]
    assert report["permissions"]["outbound_grants"] == 0


def test_stream_and_read_tool_records_are_observations_without_content_echo(tmp_path: Path) -> None:
    document = fixture()
    tool = assistant_step("assistant-tool")
    tool["message"]["message"]["content"] = [
        {
            "type": "tool_use",
            "id": "read-1",
            "name": "Read",
            "input": {"file_path": "secret.txt"},
        }
    ]
    stream = {
        "kind": "native",
        "at": "2026-09-05T13:30:00.500Z",
        "message": {
            "type": "stream_event",
            "uuid": "stream-1",
            "session_id": document["session_id"],
            "parent_tool_use_id": None,
            "event": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "FAKE-PRIVATE"},
            },
        },
    }
    response = {
        "kind": "native",
        "at": "2026-09-05T13:30:00.500Z",
        "message": {
            "type": "user",
            "uuid": "read-response",
            "session_id": document["session_id"],
            "parent_tool_use_id": None,
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "read-1",
                        "content": "FAKE-PRIVATE",
                    }
                ],
            },
        },
    }
    document["steps"][1:1] = [stream, tool, response]

    code, report = invoke(tmp_path, document)

    assert code == 0
    assert report["stream"]["events"] == 1
    assert report["permissions"]["tool_requests_observed"] == 1
    assert report["permissions"]["tool_results_observed"] == 1
    assert report["capabilities"]["tool_isolation"] == "not_run"
    assert "secret.txt" not in json.dumps(report)
    assert "FAKE-PRIVATE" not in json.dumps(report)


def retry_step() -> dict:
    return {
        "kind": "native",
        "at": "2026-09-05T13:30:00.500Z",
        "message": {
            "type": "system",
            "subtype": "api_retry",
            "uuid": "retry-1",
            "session_id": fixture()["session_id"],
            "attempt": 1,
            "max_retries": 3,
            "retry_delay_ms": 500,
            "error_status": 429,
            "error": "rate_limit",
        },
    }


def test_retry_and_window_report_are_advisory_not_a_cash_or_quota_balance(tmp_path: Path) -> None:
    document = fixture()
    quota = {
        "kind": "native",
        "at": "2026-09-05T13:30:00.500Z",
        "message": {
            "type": "rate_limit_event",
            "uuid": "quota-1",
            "session_id": document["session_id"],
            "rate_limit_info": {
                "status": "allowed_warning",
                "rateLimitType": "five_hour",
                "utilization": 0.8,
                "resetsAt": 1788624000,
            },
        },
    }
    document["steps"][1:1] = [retry_step(), quota]

    code, report = invoke(tmp_path, document)

    assert code == 0
    assert report["retries"][0]["http_status"] == 429
    assert report["quota"]["observations"][0]["window"] == "five_hour"
    assert report["quota"]["observations"][0]["utilization"] == 0.8
    assert report["usage"]["account_remaining"] is None
    assert report["usage"]["hidden_retry_usage"] == "unknown"
    assert report["usage"]["cash_charged_usd"] is None


def test_261_retry_extension_cannot_masquerade_as_the_pinned_260_protocol(tmp_path: Path) -> None:
    document = fixture()
    retry = retry_step()
    retry["message"]["no_response"] = {"waited_ms": 1000, "retry_wait_ms": 2000}
    document["steps"].insert(1, retry)

    code, report = invoke(tmp_path, document)

    assert code == 1
    assert report["reason_codes"] == ["PROTOCOL_VERSION_UNSUPPORTED"]
    assert report["result_decisions"]["accepted"] == 0


@pytest.mark.parametrize("value", [-1, True, "12"])
def test_invalid_token_counters_do_not_become_consumption_facts(
    tmp_path: Path, value: object
) -> None:
    document = fixture()
    document["steps"][1]["message"]["usage"]["input_tokens"] = value

    code, report = invoke(tmp_path, document)

    assert code == 1
    assert report["reason_codes"] == ["NATIVE_RECORD_INVALID"]
    assert report["usage"]["main_loop_final"] is None


@pytest.mark.parametrize("variant", ["oversized", "duplicate-key", "bad-encoding", "surrogate"])
def test_untrusted_file_is_bounded_and_invalid_input_never_prints_a_traceback(
    tmp_path: Path, variant: str
) -> None:
    source = tmp_path / "untrusted.json"
    content = json.dumps(fixture()).encode()
    if variant == "oversized":
        content += b" " * (4 * 1024 * 1024)
    elif variant == "duplicate-key":
        content = b'{"runtime_version":"wrong",' + content[1:]
    elif variant == "bad-encoding":
        content = b"\xff"
    else:
        document = fixture()
        document["steps"][1]["message"]["result"] = "\ud800"
        content = json.dumps(document).encode()
    source.write_bytes(content)

    code, report = invoke_path(source)

    assert code == 1
    assert report["reason_codes"] == ["INPUT_INVALID"]


def test_result_counters_from_an_unrequested_model_close_the_gate(tmp_path: Path) -> None:
    document = fixture()
    document["steps"][1]["message"]["modelUsage"] = {"unexpected-fallback": {"inputTokens": 1}}

    code, report = invoke(tmp_path, document)

    assert code == 1
    assert report["reason_codes"] == ["NATIVE_BINDING_MISMATCH"]
    assert report["result_decisions"]["accepted"] == 0
    assert report["usage"]["model_usage"]["unexpected-fallback"]["inputTokens"] == 1


def test_native_authentication_error_is_mapped_without_treating_synthetic_model_as_fallback(
    tmp_path: Path,
) -> None:
    document = fixture()
    error = assistant_step("auth-error")
    error["message"]["error"] = "authentication_failed"
    error["message"]["message"]["model"] = "<synthetic>"
    error["message"]["message"]["usage"] = None
    document["steps"].insert(1, error)
    document["steps"][-1]["message"].update(subtype="error_during_execution", is_error=True)

    code, report = invoke(tmp_path, document)

    assert code == 0
    assert report["native_errors"] == ["authentication_error"]
    assert report["result"]["category"] == "authentication_error"


def test_report_is_tied_to_profile_attempt_and_input_without_exporting_auth_ref(
    tmp_path: Path,
) -> None:
    document = fixture()

    code, report = invoke(tmp_path, document)

    assert code == 0
    assert report["validation_scope"] == "offline_protocol_replay"
    assert report["identity"]["attempt_id"] == document["attempt"]["id"]
    assert report["identity"]["fence"] == 1
    assert report["identity"]["profile_digest"] == document["profile_digest"]
    assert report["provenance"]["kind"] == "fixture"
    assert report["provenance"]["native_stream_verified"] is False
    assert "secret-ref:fake-official-login" not in json.dumps(report)


@pytest.mark.parametrize("kind", ["assistant", "stream_event", "user"])
def test_unenabled_subagent_cannot_supply_a_success_but_keeps_separate_usage(
    tmp_path: Path, kind: str
) -> None:
    document = fixture()
    child = assistant_step("child-event")
    child["message"]["parent_tool_use_id"] = "unbound-agent-call"
    child["message"]["message"]["usage"].update(input_tokens=100, output_tokens=50)
    if kind == "stream_event":
        body = child["message"].pop("message")
        child["message"].update(type=kind, event={"type": "message_start", "message": body})
    elif kind == "user":
        child["message"].update(type=kind, message={"role": "user", "content": "private child"})
    document["steps"].insert(1, child)

    code, report = invoke(tmp_path, document)

    assert code == 1
    assert report["reason_codes"] == ["NATIVE_SUBAGENT_UNSUPPORTED"]
    assert report["result"]["state"] == "unknown"
    assert report["result_decisions"] == {"accepted": 0, "rejected": 1}
    assert report["usage"]["partial_main_input"] is None
    if kind != "user":
        observation = report["usage"]["child_message_snapshots"][0]
        assert observation["reported_usage"]["input_tokens"] == 100
        assert observation["reported_usage"]["output_tokens"] == 50
        assert observation["output_counter_basis"] == "assistant_placeholder"
    assert report["usage"]["main_loop_final"]["output_tokens"] == 4
    assert report["usage"]["cash_charged_usd"] is None
    assert "private child" not in json.dumps(report)


@pytest.mark.parametrize("status", [200, 401, 429, 500])
def test_success_with_an_api_error_status_is_an_invalid_outcome(
    tmp_path: Path, status: int
) -> None:
    document = fixture()
    document["steps"][1]["message"]["api_error_status"] = status

    code, report = invoke(tmp_path, document)

    assert code == 1
    assert report["reason_codes"] == ["NATIVE_RECORD_INVALID"]
    assert report["result"]["state"] == "unknown"
    assert report["result_decisions"]["accepted"] == 0
    assert report["qualification"]["dispatch_eligible"] is False
