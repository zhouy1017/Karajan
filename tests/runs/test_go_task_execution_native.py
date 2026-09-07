"""Actual Linux Host direct child and native tools; qualification/provider are fixtures."""

import json
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
from karajan.orchestration.go_task_execution import ApprovedGoTaskExecution
from task_execution_fixture import approved_fixture
from test_go_context import accounting as accounting
from test_go_context import artifacts as artifacts
from test_go_execution_intent import case, projected
from test_opencode_go_composition import runtime_artifact

__all__ = ["case", "projected"]
pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="Actual Linux Host/native tool path"
)


def reply(index):
    delta, finish = {"content": "Implemented the approved task."}, "stop"
    if index in (1, 2):
        arguments = {"filePath": "/workspace/src/report.py"}
        if index == 2:
            arguments.update(
                oldString="print('approved task')", newString="print('implemented task')"
            )
        delta = {
            "tool_calls": [
                {
                    "index": 0,
                    "id": f"call_{index}",
                    "type": "function",
                    "function": {
                        "name": "read" if index == 1 else "edit",
                        "arguments": json.dumps(arguments),
                    },
                }
            ]
        }
        finish = "tool_calls"
    frames = [
        {
            "model": "glm-5.3-flash",
            "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
        },
        {"model": "glm-5.3-flash", "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
        {
            "model": "glm-5.3-flash",
            "choices": [],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        },
    ]
    return (
        "".join("data: " + json.dumps(frame) + "\n\n" for frame in frames) + "data: [DONE]\n\n"
    ).encode()


@pytest.mark.parametrize("fault", ["none", "grant_reply_lost", "cancel_after_send"])
def test_actual_host_child_native_candidate_and_replay(
    projected,
    tmp_path,
    accounting,
    artifacts,
    fault,
):
    repository = projected["repository"]
    for path, data in {
        "src/report.py": b"print('approved task')\n",
        "tests/test_report.py": b"assert True\n",
        "assets/opaque.bin": b"\x00\xff preserved\x01",
        "tools/check.sh": b"#!/bin/sh\nexit 0\n",
    }.items():
        target = repository / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (repository / "tools/check.sh").chmod(0o755)
    for arguments in (
        ["add", "."],
        [
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "Task baseline",
        ],
    ):
        subprocess.run(["git", "-C", str(repository), *arguments], check=True, capture_output=True)
    projects = projected["projects"]
    current = projects.get(projected["project_id"])
    projects.update(
        current["id"],
        {
            "name": current["name"],
            "base_ref": "main",
            "target_branch": "main",
            "allowed_target_branches": ["main"],
        },
        expected_revision=current["revision"],
        principal="owner",
        command_key="baseline-refresh",
    )
    calls = []
    errors = []
    services = None

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            try:
                assert self.client_address[0] == "127.0.0.1"
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                operation = services.intents.read(run_id, op_id, principal="owner")
                call = services.journal.snapshot(operation["execution"]["intent"]["grant_id"])[
                    "calls"
                ][-1]
                assert call["state"] == "send_unknown"
                assert call["request_context"]["accounted_input_tokens"] > 0
                calls.append(
                    {
                        "sequence": call["sequence"],
                        "prompt_retained": any(
                            "approved-task-brief" in str(message.get("content"))
                            for message in request["messages"]
                        ),
                        "tool_history": sum(
                            message["role"] == "tool" for message in request["messages"]
                        ),
                    }
                )
                body = reply(len(calls))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.flush()
                if fault == "cancel_after_send":
                    # Headers release Relay's bounded send guard. Withhold body
                    # while real cancellation revokes/stops the original child.
                    ApprovedGoTaskExecution(services).cancel(run_id, op_id, principal="owner")
                    return
                self.wfile.write(body)
            except Exception as error:
                errors.append(type(error).__name__)
                self.send_error(500)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        services, run_id, op_id = approved_fixture(
            projected,
            tmp_path,
            runtime_artifact(),
            accounting,
            artifacts,
            server.server_port,
            fault=fault,
        )
        assert services.client_factory is not None
        facade = ApprovedGoTaskExecution(services)
        advanced = facade.advance(run_id, op_id, principal="owner")
        assert advanced["execution"]["phase"] in {"start_unknown", "effect_claimed"}
        deadline = time.monotonic() + 160
        while time.monotonic() < deadline:
            current = facade.get(run_id, op_id, principal="owner")
            snapshot = services.intents.host.inspect(current["planned_attempt_id"])
            if snapshot.state == "exited":
                break
            time.sleep(0.2)
        else:
            pytest.fail("Owned Host child did not finish")
        result = facade.reconcile(run_id, op_id, principal="owner")
        (tmp_path / "public-operation.json").write_text(json.dumps(result, indent=2))
        if fault != "none":
            assert result["execution"]["effect_claim"] is not None
            assert result["execution"].get("collection") is None
            grant = services.journal.snapshot(result["execution"]["intent"]["grant_id"])
            assert grant["state"] == "revoked"
            assert grant["request_count"] == (1 if fault == "cancel_after_send" else 0)
            assert len(calls) == grant["request_count"]
            if fault == "grant_reply_lost":
                assert not (services.work_root / ("operation-" + op_id)).exists()
            else:
                assert result["cancel_requested"] is True
                assert grant["calls"][0]["state"] == "send_unknown"
            before = services.journal.path.read_bytes()
            for _ in range(2):
                facade.advance(run_id, op_id, principal="owner")
            assert services.journal.path.read_bytes() == before
            assert len(calls) == grant["request_count"]
            return
        diagnostic = tmp_path / "fixture-deployment/fixture-error.json"
        assert result["execution"]["phase"] == "candidate_recorded", (
            diagnostic.read_text() if diagnostic.exists() else result["reason_codes"]
        )
        assert not errors
        assert len(calls) == 3
        assert all(c["prompt_retained"] for c in calls)
        assert [c["tool_history"] for c in calls] == [0, 1, 2]
        collection = result["execution"]["collection"]
        candidate_id = collection["candidate"]["id"]
        services.candidates.materialize(candidate_id, tmp_path / "materialized")
        assert (
            tmp_path / "materialized/src/report.py"
        ).read_bytes() == b"print('implemented task')\n"
        assert (repository / "src/report.py").read_bytes() == b"print('approved task')\n"
        assert (tmp_path / "materialized/assets/opaque.bin").read_bytes() == (
            repository / "assets/opaque.bin"
        ).read_bytes()
        assert (tmp_path / "materialized/tools/check.sh").stat().st_mode & 0o111
        candidate = services.candidates.get(candidate_id)
        gate = services.candidates.gate(
            candidate_id,
            current={
                key: candidate[key]
                for key in ("repository_identity", "base_sha", "input_sha256", "policy_sha256")
            },
        )
        assert gate["delivery_eligible"] is gate["local_gate_passed"] is False
        assert {"CHECK_EVIDENCE_MISSING:tests", "REVIEW_EVIDENCE_MISSING"} <= set(gate["reasons"])
        journal = services.journal.snapshot(result["execution"]["intent"]["grant_id"])
        assert journal["state"] == "revoked" and journal["request_count"] == 3
        assert collection["capture"]["report"]["native_cleanup"]["local_stop"] == "confirmed"
        for _ in range(2):
            assert (
                facade.advance(run_id, op_id, principal="owner")["execution"]["collection"][
                    "candidate"
                ]["id"]
                == candidate_id
            )
        assert (
            len(calls) == 3 and services.journal.snapshot(journal["grant_id"])["request_count"] == 3
        )
    finally:
        if services is not None:
            ApprovedGoTaskExecution(services).cancel(run_id, op_id, principal="owner")
        server.shutdown()
        server.server_close()
        thread.join(5)
