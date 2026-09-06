"""Actual native projected suite via public Store; HTTP upstream alone is synthetic."""

import json
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest
from karajan.adapters.opencode.go_journal import GoCallJournal
from karajan.projects.go_suite import FixedGoSuite
from karajan.projects.qualification import ProfileQualificationStore, QualificationError
from test_go_context import accounting, artifacts
from test_projected_qualification_store import SUITE, case, projected, qualify

__all__ = ["accounting", "artifacts", "case", "projected"]


@pytest.mark.skipif(sys.platform != "linux", reason="Real native qualification requires Linux")
def test_actual_projected_native_suite_is_durable_before_http_and_fixture_scope_stays_separate(
    projected, accounting, tmp_path
):
    from test_go_projected_probe import response
    from test_opencode_go_composition import runtime_artifact

    initial_wall, initial_monotonic = time.time(), time.monotonic()

    def clock():
        # Keep this non-clock test stable if WSL resynchronizes wall time.
        return initial_wall + time.monotonic() - initial_monotonic

    requests, starts, sessions, null_continuations = [], [], {}, []
    journal = GoCallJournal(tmp_path / "actual-journal.sqlite", clock=clock)

    def receive(request):
        session = request.headers["x-opencode-session"]
        sessions[session] = sessions.get(session, 0) + 1
        requests.append(request.url.path)
        start = projected["store"].get_command_start(
            projected["project_id"], "projected-qualification", principal="owner"
        )
        assert start["completed"] is False
        if not starts:
            scenarios = start["binding"]["execution_start"]["scenarios"]
            assert len(scenarios) == 2
            actual = [journal.snapshot(item["grant_id"]) for item in scenarios]
            assert actual[0]["calls"][-1]["state"] == "send_unknown"
            assert actual[0]["calls"][-1]["request_context"]["request_digest"]
            assert actual[1]["request_count"] == 0
            assert all(
                row["binding"]["schema_version"] == "karajan.go-qualification-grant.v2"
                for row in actual
            )
            with pytest.raises(QualificationError, match="QUALIFICATION_IN_PROGRESS_OR_UNKNOWN"):
                qualify(projected)
            starts.append(start)
        body = json.loads(request.content)
        scenario = "denied_read" if "KARAJAN_READ_DENIED" in json.dumps(body) else "edit"
        original = response(sessions[session], scenario)
        # Exercise the documented nullable continuation through the real relay
        # and native SDK, after an actual tool name was already supplied.
        frames = []
        for frame in original.content.decode().split("\n\n"):
            if not frame:
                continue
            frames.append(frame)
            if frame == "data: [DONE]":
                continue
            event = json.loads(frame.removeprefix("data: "))
            choices = event.get("choices", [])
            if choices and choices[0].get("delta", {}).get("tool_calls"):
                event["choices"] = [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"name": None, "arguments": ""}}
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
                frames.append("data: " + json.dumps(event))
                null_continuations.append(scenario)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=("\n\n".join(frames) + "\n\n").encode(),
        )

    with tempfile.TemporaryDirectory(prefix="kgps-") as root:
        suite = FixedGoSuite(
            runtime_artifact(),
            Path(root),
            journal,
            suite_ref=SUITE,
            accounting=accounting,
            client_factory=lambda: httpx.Client(transport=httpx.MockTransport(receive)),
            clock=clock,
        )
        projected["suite"] = suite
        projected["store"] = ProfileQualificationStore(
            projected["projects"], credentials=projected["credentials"], go_suite=suite, clock=clock
        )
        record = qualify(projected)
        assert record["status"] == "passed", record
        assert len(starts) == 1 and len(requests) == 6
        assert null_continuations == ["edit", "edit", "edit", "denied_read"]
        assert record["qualification_scope"] == "projected_native_tools_fixture"
        assert record["runtime_tools_status"] == "not_run"
        scenarios = record["observation"]["scenarios"]
        assert [item["status"] for item in scenarios] == ["passed", "passed"]
        assert [
            [
                request.get("tool_name_null_fragments", 0)
                for request in item["observation"]["requests"]
            ]
            for item in scenarios
        ] == [[1, 1, 1, 0], [1, 0]]
        assert [item["observation"]["capture"]["changed_paths"] for item in scenarios] == [
            ["src/fixture.py"],
            [],
        ]
        assert all(
            item["observation"]["capture"]["validation_gate"]["local_gate_passed"] is False
            for item in scenarios
        )
        reopened = ProfileQualificationStore(
            projected["projects"], credentials=projected["credentials"], go_suite=suite, clock=clock
        )
        projected["store"] = reopened
        assert qualify(projected) == record
        assert len(requests) == 6
        with pytest.raises(QualificationError, match="RUNTIME_TOOLS_NOT_QUALIFIED"):
            reopened.facts_for_profile(
                projected["project_id"],
                projected["registration"],
                principal="owner",
                scope="runtime_tools",
            )
        observed = reopened.facts_for_profile(
            projected["project_id"],
            projected["registration"],
            principal="owner",
            scope="projected_native_tools_fixture",
        )
        assert observed["dispatch_eligible"] is False
        assert "executor_scope" not in observed
