"""Fixed projected qualification through real accounting, native tools and Collector."""

import json
import sys
import time
from dataclasses import replace

import httpx
import pytest
from karajan.adapters.opencode.go_journal import GoCallJournal
from karajan.adapters.opencode.go_relay import GoRelayAuthorization
from karajan.isolation.go_probe import source_digest
from karajan.isolation.go_projected_probe import (
    observe_go_projected_tools,
    projected_probe_spec,
    projected_runtime_source,
)
from test_go_context import accounting as accounting
from test_go_context import artifacts as artifacts
from test_opencode_go_composition import SECRET, runtime_artifact


def test_fixed_spec_binds_accounting_and_exact_projection(accounting):
    spec = projected_probe_spec(accounting)
    assert spec["context"] == {
        "source_sha256": source_digest(accounting.source()),
        "approved_input_tokens": 12288,
        "reserved_output_tokens": 4096,
        "operating_context_tokens": 16384,
        "fixed_margin": 2048,
        "ratio_margin_basis_points": 2000,
    }
    assert spec["scenarios"] == ["edit", "denied_read"]
    assert [(r["path"], r["writable"]) for r in spec["projection"]] == [
        ("reference.md", False),
        ("src/fixture.py", True),
    ]
    assert spec["max_requests"] == 6
    spec["context"]["approved_input_tokens"] = 1
    assert projected_probe_spec(accounting)["context"]["approved_input_tokens"] == 12288


def authorization(tmp_path, accounting, runtime, scenario_name, **updates):
    source = projected_runtime_source(runtime, accounting)
    spec = source["probe_spec"]
    journal = GoCallJournal(tmp_path / "calls.sqlite")
    binding = {
        "schema_version": "karajan.go-qualification-grant.v2",
        "qualification_id": "projected-qualification",
        "attempt_id": "projected-attempt",
        "fence": 1,
        "profile_digest": "a" * 64,
        "runtime_digest": source_digest(source),
        "channel": "opencode-go",
        "model": "glm-5.3-flash",
        "auth_generation": "synthetic-generation",
        "expires_at": time.time() + 420,
        "max_requests": 6,
        "probe_spec_digest": source_digest(spec),
        "scenario": scenario_name,
        "context": spec["context"],
    }
    binding.update(updates)
    created = journal.create_grant(binding, grant_id="projected-grant")
    return GoRelayAuthorization(journal, created["grant_id"], binding, created["capability"])


@pytest.mark.skipif(sys.platform != "linux", reason="Fixed Linux runtime required")
@pytest.mark.parametrize("scenario", ["edit", "denied_read"])
def test_native_projected_qualification_accounts_retains_and_captures(
    tmp_path, accounting, scenario
):
    runtime = runtime_artifact()
    auth = authorization(tmp_path, accounting, runtime, scenario)
    payloads = []

    def receive(request):
        body = json.loads(request.content)
        payloads.append(body)
        facts = auth.journal.snapshot(auth.grant_id)
        assert facts["calls"][-1]["state"] == "send_unknown"
        assert facts["calls"][-1]["request_context"]["request_digest"]
        assert facts["request_count"] == len(payloads)
        return response(len(payloads), scenario)

    result = observe_go_projected_tools(
        runtime,
        tmp_path / "observation",
        SECRET,
        auth,
        scenario=scenario,
        accounting=accounting,
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(receive)),
    )
    # Synthetic provider payloads are kept in memory; reports are content-free.
    (tmp_path / "public-report.json").write_text(json.dumps(result, indent=2))
    assert result["status"] == "passed", result["reason_codes"]
    assert result["scope"] == "projected_native_tools_fixture"
    assert result["retention"]["initial_input_retained"] is True
    assert result["retention"]["tool_history_retained"] is True
    assert result["capture"]["status"] == "passed"
    assert result["capture"]["changed_paths"] == (["src/fixture.py"] if scenario == "edit" else [])
    assert result["capture"]["validation_gate"] == {
        "local_gate_passed": False,
        "reasons": ["CHECK_EVIDENCE_MISSING:fixture_check", "REVIEW_EVIDENCE_MISSING"],
    }
    assert len(payloads) == (4 if scenario == "edit" else 2)
    assert result["journal"]["state"] == "revoked"
    assert SECRET not in json.dumps(result)
    with pytest.raises(ValueError, match="FRESH_ACTIVE_GRANT_REQUIRED"):
        observe_go_projected_tools(
            runtime,
            tmp_path / "replayed",
            SECRET,
            auth,
            scenario=scenario,
            accounting=accounting,
        )
    assert not (tmp_path / "replayed").exists()
    assert auth.journal.snapshot(auth.grant_id)["request_count"] == len(payloads)


def response(index, scenario):
    delta, finish = {"content": "Done."}, "stop"
    path = (
        "/workspace/blocked.txt"
        if scenario == "denied_read"
        else ("/workspace/reference.md" if index == 1 else "/workspace/src/fixture.py")
    )
    if index <= (3 if scenario == "edit" else 1):
        arguments = {"filePath": path}
        if index == 3:
            arguments.update(
                oldString="return min(low, max(value, high))",
                newString="return min(high, max(low, value))",
            )
        delta = {
            "tool_calls": [
                {
                    "index": 0,
                    "id": f"projected_call_{index}",
                    "type": "function",
                    "function": {
                        "name": "edit" if index == 3 else "read",
                        "arguments": json.dumps(arguments),
                    },
                }
            ]
        }
        finish = "tool_calls"
    events = []
    for change, reason in (({"role": "assistant"}, None), (delta, None), ({}, finish)):
        events.append(
            {
                "id": "chatcmpl-projected",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "glm-5.3-flash",
                "choices": [{"index": 0, "delta": change, "finish_reason": reason}],
            }
        )
    events.append(
        {
            "id": "chatcmpl-projected",
            "object": "chat.completion.chunk",
            "model": "glm-5.3-flash",
            "choices": [],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        }
    )
    data = "".join("data: " + json.dumps(event) + "\n\n" for event in events)
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=(data + "data: [DONE]\n\n").encode(),
    )


@pytest.mark.skipif(sys.platform != "linux", reason="Fixed Linux runtime required")
@pytest.mark.parametrize(
    "fault", ["capability", "runtime", "spec", "scenario", "limits", "max_requests", "revoked"]
)
def test_wrong_fresh_grant_cannot_create_namespace_or_send(tmp_path, accounting, fault):
    runtime = runtime_artifact()
    updates = {}
    if fault == "runtime":
        updates["runtime_digest"] = "b" * 64
    elif fault == "spec":
        updates["probe_spec_digest"] = "c" * 64
    elif fault == "scenario":
        updates["scenario"] = "denied_read"
    elif fault == "limits":
        updates["context"] = projected_probe_spec(accounting)["context"] | {"fixed_margin": 2047}
    elif fault == "max_requests":
        updates["max_requests"] = 5
    auth = authorization(tmp_path, accounting, runtime, "edit", **updates)
    if fault == "capability":
        auth = replace(auth, capability="wrong-synthetic-capability")
    elif fault == "revoked":
        auth.journal.revoke_grant(auth.grant_id)
    calls = []

    def unexpected():
        calls.append(True)
        raise AssertionError("No fixture transport should be constructed")

    with pytest.raises(ValueError):
        observe_go_projected_tools(
            runtime,
            tmp_path / "never-created",
            SECRET,
            auth,
            scenario="edit",
            accounting=accounting,
            client_factory=unexpected,
        )
    assert not (tmp_path / "never-created").exists()
    assert calls == []
    assert auth.journal.snapshot(auth.grant_id)["request_count"] == 0


@pytest.mark.skipif(sys.platform != "linux", reason="Fixed Linux runtime required")
def test_actual_unknown_usage_revokes_exact_grant_and_cannot_pass(tmp_path, accounting):
    runtime = runtime_artifact()
    auth = authorization(tmp_path, accounting, runtime, "denied_read")
    count = 0

    def receive(request):
        nonlocal count
        count += 1
        result = response(count, "denied_read")
        # Actual SSE protocol finishes, but neither billable count was reported.
        return httpx.Response(
            200,
            headers=result.headers,
            content=b"\n\n".join(
                line for line in result.content.split(b"\n\n") if b'"usage"' not in line
            ),
        )

    result = observe_go_projected_tools(
        runtime,
        tmp_path / "unknown",
        SECRET,
        auth,
        scenario="denied_read",
        accounting=accounting,
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(receive)),
    )
    assert result["status"] == "failed"
    assert "PROVIDER_PROTOCOL_INCOMPLETE" in result["reason_codes"]
    assert result["journal"]["state"] == "revoked"
    assert result["native_cleanup"]["local_stop"] == "confirmed"
    assert result["relay_cleanup"]["status"] == "closed"
    assert count == 1


@pytest.mark.parametrize("kind", ["legacy", "task_attempt"])
def test_legacy_and_task_grants_never_authorize_projected_qualification(tmp_path, accounting, kind):
    binding = {
        "attempt_id": "synthetic-attempt",
        "fence": 1,
        "profile_digest": "a" * 64,
        "runtime_digest": "b" * 64,
        "channel": "opencode-go",
        "model": "glm-5.3-flash",
        "auth_generation": "synthetic",
        "expires_at": 2000.0,
        "max_requests": 6,
    }
    if kind == "legacy":
        binding["qualification_id"] = "synthetic-legacy"
    else:
        binding.update(
            {
                "subject": {
                    "kind": "task_attempt",
                    "project_id": "p",
                    "run_id": "r",
                    "task_id": "t",
                },
                "approval_digest": "c" * 64,
                "execution_policy_digest": "d" * 64,
                "workspace_digest": "e" * 64,
                "authentication_source_digest": "f" * 64,
            }
        )
    journal = GoCallJournal(tmp_path / "calls.sqlite", clock=lambda: 1000.0)
    grant = journal.create_grant(binding, grant_id="wrong-subject")
    auth = GoRelayAuthorization(journal, grant["grant_id"], binding, grant["capability"])
    with pytest.raises(ValueError, match="PROJECTED_QUALIFICATION_GRANT_REQUIRED"):
        observe_go_projected_tools(
            tmp_path / "not-an-ELF",
            tmp_path / "not-created",
            SECRET,
            auth,
            scenario="edit",
            accounting=accounting,
        )
    assert not (tmp_path / "not-created").exists()
    assert journal.snapshot(auth.grant_id)["request_count"] == 0
    assert journal.snapshot(auth.grant_id)["state"] == "active"
