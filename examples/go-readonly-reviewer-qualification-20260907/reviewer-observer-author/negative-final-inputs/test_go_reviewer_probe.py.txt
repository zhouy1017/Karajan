"""Public fixed Reviewer observer: real native/Journal, local HTTP fixtures only."""

import json
import sys
import time
from contextlib import nullcontext

import httpx
import pytest
from karajan.adapters.opencode.go_journal import GoCallJournal
from karajan.adapters.opencode.go_relay import GoRelayAuthorization
from karajan.isolation.go_probe import source_digest
from karajan.isolation.go_reviewer_probe import reviewer_probe_spec
from test_go_context import accounting as accounting
from test_go_context import artifacts as artifacts
from test_opencode_go_composition import SECRET, runtime_artifact


def test_fixed_reviewer_spec_is_readonly_bounded_and_detached(accounting):
    spec = reviewer_probe_spec(accounting)
    assert spec["suite_ref"] == {
        "id": "opencode-go-readonly-review-linux",
        "revision": 1,
    }
    assert spec["scenarios"] == ["clean_review", "defect_review", "denied_read"]
    assert spec["max_requests"] == 6
    assert spec["max_total_requests"] == 18
    assert spec["scenario_timeout_seconds"] == 150
    assert spec["suite_timeout_seconds"] == 600
    assert spec["context"]["approved_input_tokens"] == 12288
    assert spec["context"]["reserved_output_tokens"] == 4096
    assert spec["context"]["operating_context_tokens"] == 16384
    assert spec["context"]["fixed_margin"] == 2048
    assert spec["context"]["ratio_margin_basis_points"] == 2000
    for case in spec["cases"].values():
        assert [(row["path"], row["writable"]) for row in case["projection"]] == [
            ("acceptance.md", False),
            ("src/range.py", False),
        ]
        assert case["allowed_acceptance_refs"] == ["acceptance:clamp-v1"]
        assert all("content" not in row for row in case["files"])
    assert spec["cases"]["clean_review"]["files"] != spec["cases"]["defect_review"]["files"]
    spec["cases"]["clean_review"]["projection"][0]["writable"] = True
    assert (
        reviewer_probe_spec(accounting)["cases"]["clean_review"]["projection"][0]["writable"]
        is False
    )


@pytest.mark.skipif(sys.platform != "linux", reason="Fixed Linux source required")
def test_source_binds_fixed_readonly_config_and_parser(accounting):
    from karajan.isolation.go_reviewer_probe import reviewer_runtime_source

    source = reviewer_runtime_source(runtime_artifact(), accounting)
    assert source["execution_path"] == "linux-unshare-chroot-opencode-go-readonly-reviewer-v1"
    assert source["probe_spec"] == reviewer_probe_spec(accounting)
    assert source["native_settings"]["permission"]["edit"] == {"*": "deny"}
    assert source["native_settings"]["permission"]["read"] == {
        "*": "deny",
        "workspace/acceptance.md": "allow",
        "workspace/src/range.py": "allow",
    }
    assert source["native_settings"]["plugin"] == []
    assert source["native_settings"]["mcp"] == {}
    assert source["probe_spec"]["parser_revision"] == "karajan.review-output-parser.v1"
    assert "candidates/review_output.py" in source["source_sha256"]
    assert "isolation/go_reviewer_probe.py" in source["source_sha256"]


def reviewer_authorization(tmp_path, accounting, runtime, scenario_name, **updates):
    from karajan.isolation.go_reviewer_probe import reviewer_runtime_source

    scenario = scenario_name
    source = reviewer_runtime_source(runtime, accounting)
    journal = GoCallJournal(tmp_path / "calls.sqlite")
    binding = {
        "schema_version": "karajan.go-reviewer-qualification-grant.v1",
        "qualification_id": "reviewer-qualification",
        "attempt_id": "reviewer-attempt:" + scenario,
        "fence": 1,
        "profile_digest": "a" * 64,
        "runtime_digest": source_digest(source),
        "channel": "opencode-go",
        "model": "glm-5.3-flash",
        "auth_generation": "synthetic-generation",
        "expires_at": time.time() + 600,
        "max_requests": 6,
        "probe_spec_digest": source["probe_spec_digest"],
        "scenario": scenario,
        "context": source["probe_spec"]["context"],
    }
    binding.update(updates)
    created = journal.create_grant(binding, grant_id="reviewer-grant:" + scenario)
    return GoRelayAuthorization(journal, created["grant_id"], binding, created["capability"])


def reviewer_response(index, scenario, *, final_text=None):
    verdict = {
        "clean_review": "pass",
        "defect_review": "changes_requested",
        "denied_read": "inconclusive",
    }[scenario]
    findings = (
        []
        if scenario != "defect_review"
        else [
            {
                "severity": "high",
                "file": "src/range.py",
                "line": 2,
                "behavior": "Returns the high bound for values below the range.",
                "trigger": "clamp(-1, 0, 2) returns 2 instead of 0.",
                "acceptance_ref": "acceptance:clamp-v1",
                "blocking": True,
            }
        ]
    )
    delta = {
        "content": final_text
        if final_text is not None
        else json.dumps({"verdict": verdict, "findings": findings})
    }
    finish = "stop"
    if index <= (1 if scenario == "denied_read" else 2):
        path = (
            "/workspace/blocked.txt"
            if scenario == "denied_read"
            else ("/workspace/acceptance.md" if index == 1 else "/workspace/src/range.py")
        )
        delta = {
            "tool_calls": [
                {
                    "index": 0,
                    "id": f"reviewer_call_{index}",
                    "type": "function",
                    "function": {"name": "read", "arguments": json.dumps({"filePath": path})},
                }
            ]
        }
        finish = "tool_calls"
    events = [
        {
            "id": "chatcmpl-reviewer",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "glm-5.3-flash",
            "choices": [{"index": 0, "delta": change, "finish_reason": reason}],
        }
        for change, reason in (({"role": "assistant"}, None), (delta, None), ({}, finish))
    ]
    events.append(
        {
            "id": "chatcmpl-reviewer",
            "object": "chat.completion.chunk",
            "model": "glm-5.3-flash",
            "choices": [],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        }
    )
    body = "".join("data: " + json.dumps(event) + "\n\n" for event in events)
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=(body + "data: [DONE]\n\n").encode(),
    )


@pytest.mark.skipif(sys.platform != "linux", reason="Fixed Linux native required")
@pytest.mark.parametrize("scenario", ["clean_review", "defect_review", "denied_read"])
def test_actual_readonly_review_has_new_session_complete_final_and_retained_reads(
    tmp_path, accounting, scenario
):
    from karajan.isolation.go_reviewer_probe import observe_go_reviewer_tools

    runtime = runtime_artifact()
    authorization = reviewer_authorization(tmp_path, accounting, runtime, scenario)
    payloads = []

    def receive(request):
        payloads.append(json.loads(request.content))
        assert (
            authorization.journal.snapshot(authorization.grant_id)["calls"][-1]["state"]
            == "send_unknown"
        )
        return reviewer_response(len(payloads), scenario)

    result = observe_go_reviewer_tools(
        runtime,
        tmp_path / "observation",
        SECRET,
        authorization,
        scenario=scenario,
        accounting=accounting,
        current_guard=nullcontext,
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(receive)),
    )
    (tmp_path / "public-report.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    (tmp_path / "synthetic-wire.json").write_text(json.dumps(payloads, indent=2), encoding="utf-8")
    assert result["status"] == "passed", result
    assert result["scope"] == "readonly_reviewer_tools_fixture"
    assert result["runtime_tools_status"] == "not_run"
    assert result["dispatch_eligible"] is False
    assert result["session"]["initial_message_count"] == 0
    final = result["native_final"]
    assert final["session_id"] == result["session"]["id"]
    assert final["parent_id"] == result["session"]["prompt_message_id"]
    assert final["finish"] == "stop" and final["completed_at"] > 0 and not final["error"]
    assert final["text_part_count"] == 1
    assert (
        result["parsed_review"]["verdict"]
        == {"clean_review": "passed", "defect_review": "failed", "denied_read": "inconclusive"}[
            scenario
        ]
    )
    assert result["readonly"]["before"] == result["readonly"]["after"]
    assert all(
        row["readonly"] and row["host_identity_matches"] for row in result["readonly"]["mounts"]
    )
    assert len(result["readonly"]["mounts"]) == 2
    assert result["journal"]["state"] == "revoked"
    assert result["native_cleanup"]["local_stop"] == "confirmed"
    assert result["provider_remote_stop"] == "unknown"
    assert len(payloads) == (2 if scenario == "denied_read" else 3)
    assert SECRET not in json.dumps(result)
    if scenario != "denied_read":
        reads = result["retention"]["requests"][-1]["read_results"]
        assert {row["path"]: row["content_sha256"] for row in reads} == {
            row["path"]: row["sha256"] for row in result["readonly"]["before"]
        }
    with pytest.raises(ValueError, match="FRESH_ACTIVE_GRANT_REQUIRED"):
        observe_go_reviewer_tools(
            runtime,
            tmp_path / "replay",
            SECRET,
            authorization,
            scenario=scenario,
            accounting=accounting,
            current_guard=nullcontext,
        )
    assert not (tmp_path / "replay").exists()
