"""C boundaries: real Journal/accounting; explicit native/source/relay doubles.

These doubles prove rejection and final-message selection, never OS or service
qualification. The companion three P cases use the actual native mechanism.
"""

import copy
import json
import sys
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest
from karajan.isolation import go_reviewer_probe as probe
from test_go_context import accounting as accounting
from test_go_context import artifacts as artifacts
from test_go_reviewer_probe import reviewer_authorization
from test_opencode_go_composition import SECRET


@pytest.fixture
def boundary(tmp_path, accounting, monkeypatch):
    spec = probe.reviewer_probe_spec(accounting)
    settings = {
        "permission": {"*": "deny", "read": {"*": "deny"}, "edit": {"*": "deny"}},
        "model": "opencode-go/glm-5.3-flash",
        "plugin": [],
        "mcp": {},
        "lsp": False,
        "formatter": False,
    }
    source = {
        "probe_spec": spec,
        "probe_spec_digest": probe.source_digest(spec),
        "source_sha256": {"synthetic-native-boundary": "a" * 64},
        "native_settings": settings,
    }
    state = {
        "created": 0,
        "started": 0,
        "prompted": 0,
        "mutate": lambda m: None,
        "initial_messages": [],
        "error": None,
    }
    monkeypatch.setattr(probe, "reviewer_runtime_source", lambda *args: copy.deepcopy(source))

    class NativeBoundary:
        def __init__(self, runtime, directory, socket, capability, *, projection):
            state["created"] += 1
            self.workspace = directory / "workspace"
            self.workspace.mkdir(parents=True)
            self.projection = projection
            self.prompt = None

        def start(self):
            state["started"] += 1
            if state["error"]:
                raise OSError(state["error"])
            return {
                "namespace_pid": 1,
                "host_mount_visible": False,
                "wsl_interop_visible": False,
                "native_control_fd_inherited": False,
                "network_interfaces": ["lo"],
                "ipv4_routes": [],
                "no_new_privileges": True,
                "capabilities": {
                    k: "0000000000000000" for k in ("effective", "permitted", "bounding")
                },
            }

        def readonly_projection_observation(self):
            return [
                {
                    "path": row["path"],
                    "readonly": True,
                    "mount_flags": 1,
                    "device": 1,
                    "inode": index + 1,
                    "host_identity_matches": True,
                }
                for index, row in enumerate(self.projection)
            ]

        def request(self, method, route, body=None):
            if route == "/config":
                return settings
            if route == "/session":
                return {"id": "ses_boundary"}
            if route.endswith("prompt_async"):
                self.prompt = body["parts"][0]["text"]
                state["prompted"] += 1
                return None
            if self.prompt is None:
                return state["initial_messages"]
            messages = [
                {
                    "info": {"role": "user", "id": "msg_user", "sessionID": "ses_boundary"},
                    "parts": [{"type": "text", "text": self.prompt}],
                },
                {
                    "info": {
                        "role": "assistant",
                        "id": "msg_final",
                        "sessionID": "ses_boundary",
                        "parentID": "msg_user",
                        "providerID": "opencode-go",
                        "modelID": "glm-5.3-flash",
                        "finish": "stop",
                        "time": {"created": 1, "completed": 2},
                    },
                    "parts": [
                        {
                            "type": "text",
                            "id": "prt_final",
                            "messageID": "msg_final",
                            "sessionID": "ses_boundary",
                            "time": {"start": 1, "end": 2},
                            "text": '{"verdict":"pass","findings":[]}',
                        }
                    ],
                },
            ]
            state["mutate"](messages)
            return messages

        def capture_projection(self):
            if "capture_mode" in state:
                (self.workspace / self.projection[0]["path"]).chmod(state["capture_mode"])
            return SimpleNamespace(
                files=tuple(
                    (row["path"], (self.workspace / row["path"]).read_bytes())
                    for row in self.projection
                ),
                stop_evidence={"local_stop": "confirmed"},
            )

        def close(self):
            return {"local_stop": "confirmed"}

    class RelayBoundary:
        capability = "synthetic-local-relay-capability"
        receipts = []

        def __init__(self, *args, **kwargs):
            state["send_guard"] = kwargs.get("send_guard")

        def start(self, **kwargs):
            pass

        def close(self):
            return {"status": "closed"}

    monkeypatch.setattr(probe, "IsolatedOpenCode", NativeBoundary)
    monkeypatch.setattr(probe, "GoRelay", RelayBoundary)
    monkeypatch.setattr(probe, "_relay_socket_root", lambda: SimpleNamespace(path=tmp_path))
    monkeypatch.setattr(probe, "_cleanup_relay_socket_root", lambda root: None)
    return state


def observe(tmp_path, accounting, *, current_guard=nullcontext, **kwargs):
    auth = reviewer_authorization(tmp_path, accounting, Path("synthetic-ELF"), "clean_review")
    result = probe.observe_go_reviewer_tools(
        Path("synthetic-ELF"),
        tmp_path / "observation",
        SECRET,
        auth,
        scenario="clean_review",
        accounting=accounting,
        client_factory=lambda: None,
        current_guard=current_guard,
        **kwargs,
    )
    assert result["status"] == "failed"  # The explicit relay double never sends/qualifies.
    assert result["journal"]["state"] == "revoked"
    assert result["journal"]["request_count"] == 0
    assert SECRET not in json.dumps(result)
    return result


def test_boundary_control_selects_only_complete_final_text(tmp_path, accounting, boundary):
    result = observe(tmp_path, accounting)
    assert result["parsed_review"] == {"verdict": "passed", "findings": []}
    assert result["native_final"]["parent_id"] == "msg_user"
    assert "PROVIDER_PROTOCOL_INCOMPLETE" in result["reason_codes"]
    assert boundary["started"] == boundary["prompted"] == 1


@pytest.mark.skipif(sys.platform != "linux", reason="Actual POSIX chmod semantics required")
def test_readonly_modes_are_observed_from_files_not_filled_from_spec(
    tmp_path, accounting, boundary
):
    boundary["capture_mode"] = 0o755
    result = observe(tmp_path, accounting)
    assert result["readonly"]["after"][0]["mode"] == "100755"
    assert result["readonly"]["unchanged"] is False
    assert "READONLY_FILES_CHANGED_OR_UNOBSERVED" in result["reason_codes"]


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("sessionID", "ses_old", "NATIVE_ASSISTANT_IDENTITY_MISMATCH"),
        ("parentID", "msg_old", "NATIVE_ASSISTANT_IDENTITY_MISMATCH"),
        ("modelID", "different", "NATIVE_ASSISTANT_IDENTITY_MISMATCH"),
        ("providerID", "different", "NATIVE_ASSISTANT_IDENTITY_MISMATCH"),
        ("error", {"message": SECRET}, "SENSITIVE_NATIVE_OUTPUT"),
        ("error", {"message": "synthetic failure"}, "NATIVE_ASSISTANT_IDENTITY_MISMATCH"),
        ("finish", "length", "NATIVE_FINAL_INCOMPLETE"),
        ("time", {"completed": True}, "NATIVE_FINAL_INCOMPLETE"),
        ("time", {"completed": float("inf")}, "NATIVE_FINAL_INCOMPLETE"),
    ],
)
def test_final_native_identity_and_completion_are_not_model_claims(
    tmp_path, accounting, boundary, field, value, reason
):
    boundary["mutate"] = lambda messages: messages[-1]["info"].update({field: value})
    result = observe(tmp_path, accounting)
    assert reason in result["reason_codes"]
    assert result["parsed_review"] is None


@pytest.mark.parametrize(
    "change",
    [
        "duplicate_final",
        "duplicate_text",
        "tool_only",
        "unfinished_tool",
        "foreign_part",
        "unfinished_text",
    ],
)
def test_ambiguous_or_unfinished_native_parts_are_never_parsed(
    tmp_path, accounting, boundary, change
):
    def mutate(messages):
        parts = messages[-1]["parts"]
        if change == "duplicate_final":
            messages.append(copy.deepcopy(messages[-1]))
        elif change == "duplicate_text":
            parts.append(copy.deepcopy(parts[0]))
        elif change == "tool_only":
            parts[:] = [{"type": "tool", "tool": "read", "state": {"status": "completed"}}]
        elif change == "unfinished_tool":
            parts.append({"type": "tool", "tool": "read", "state": {"status": "running"}})
        elif change == "foreign_part":
            parts[0]["messageID"] = "msg_foreign"
        else:
            parts[0]["time"].pop("end")

    boundary["mutate"] = mutate
    result = observe(tmp_path, accounting)
    assert result["parsed_review"] is None
    assert result["native_final"] is None


@pytest.mark.parametrize(
    "text",
    [
        'prefix {"verdict":"pass","findings":[]}',
        '```json\n{"verdict":"pass","findings":[]}\n```',
        '{"verdict":"pass","verdict":"pass","findings":[]}',
        '{"verdict":"pass","findings":[],"completed":true}',
        '{"verdict":"pass","findings":[],"candidate_id":"trusted-by-model"}',
    ],
)
def test_whole_final_text_is_parsed_without_json_slicing(tmp_path, accounting, boundary, text):
    boundary["mutate"] = lambda messages: messages[-1]["parts"][0].update(text=text)
    result = observe(tmp_path, accounting)
    assert any(code.startswith("REVIEW_OUTPUT_") for code in result["reason_codes"])
    assert result["native_final"] is None and result["parsed_review"] is None


def test_inconclusive_is_not_clean_review_success(tmp_path, accounting, boundary):
    boundary["mutate"] = lambda messages: messages[-1]["parts"][0].update(
        text='{"verdict":"inconclusive","findings":[]}'
    )
    result = observe(tmp_path, accounting)
    assert result["parsed_review"]["verdict"] == "inconclusive"
    assert "FIXED_REVIEW_VERDICT_MISMATCH" in result["reason_codes"]


def test_new_session_must_be_empty_before_prompt(tmp_path, accounting, boundary):
    boundary["initial_messages"] = [{"info": {"role": "assistant"}}]
    result = observe(tmp_path, accounting)
    assert "NEW_EMPTY_SESSION_REQUIRED" in result["reason_codes"]
    assert boundary["prompted"] == 0


def test_native_exception_does_not_echo_sensitive_details(tmp_path, accounting, boundary):
    boundary["error"] = "synthetic path " + SECRET
    result = observe(tmp_path, accounting)
    assert "REVIEWER_PROBE_EXECUTION_FAILED" in result["reason_codes"]
    assert SECRET not in json.dumps(result)


@pytest.mark.parametrize("secret", ['synthetic"private-material', "synthetic\\private-material"])
def test_sensitive_native_strings_are_detected_before_json_escaping(
    tmp_path, accounting, boundary, secret
):
    boundary["mutate"] = lambda messages: messages[-1]["info"].update(error={"message": secret})
    auth = reviewer_authorization(tmp_path, accounting, Path("synthetic-ELF"), "clean_review")
    result = probe.observe_go_reviewer_tools(
        Path("synthetic-ELF"),
        tmp_path / "observation",
        secret,
        auth,
        scenario="clean_review",
        accounting=accounting,
        client_factory=lambda: None,
        current_guard=nullcontext,
    )
    assert "SENSITIVE_NATIVE_OUTPUT" in result["reason_codes"]
    assert result["native_final"] is None


@pytest.mark.parametrize(
    "update",
    [
        {"runtime_digest": "b" * 64},
        {"probe_spec_digest": "b" * 64},
        {"scenario": "defect_review"},
        {"max_requests": 5},
    ],
)
def test_grant_mismatch_is_zero_native_and_does_not_revoke_foreign_binding(
    tmp_path, accounting, boundary, update
):
    auth = reviewer_authorization(
        tmp_path, accounting, Path("synthetic-ELF"), "clean_review", **update
    )
    with pytest.raises(ValueError, match="REVIEWER_GRANT_SOURCE_MISMATCH"):
        probe.observe_go_reviewer_tools(
            Path("synthetic-ELF"),
            tmp_path / "observation",
            SECRET,
            auth,
            scenario="clean_review",
            accounting=accounting,
            current_guard=nullcontext,
        )
    assert boundary["created"] == 0 and not (tmp_path / "observation").exists()
    assert auth.journal.snapshot(auth.grant_id)["state"] == "active"
    assert auth.journal.snapshot(auth.grant_id)["request_count"] == 0


def test_revoked_grant_is_never_resumed(tmp_path, accounting, boundary):
    auth = reviewer_authorization(tmp_path, accounting, Path("synthetic-ELF"), "clean_review")
    auth.journal.revoke_grant(auth.grant_id)
    with pytest.raises(ValueError, match="FRESH_ACTIVE_GRANT_REQUIRED"):
        probe.observe_go_reviewer_tools(
            Path("synthetic-ELF"),
            tmp_path / "observation",
            SECRET,
            auth,
            scenario="clean_review",
            accounting=accounting,
            current_guard=nullcontext,
        )
    assert boundary["created"] == 0 and not (tmp_path / "observation").exists()


@pytest.mark.parametrize("reject_on", [1, 2])
def test_current_identity_guard_blocks_start_or_prompt_and_reaches_relay(
    tmp_path, accounting, boundary, reject_on
):
    entries = []

    @contextmanager
    def guard():
        entries.append(len(entries) + 1)
        if len(entries) == reject_on:
            raise ValueError("synthetic-current-identity-withdrawn")
        yield

    result = observe(tmp_path, accounting, current_guard=guard)
    assert boundary["send_guard"] is guard
    assert boundary["started"] == (0 if reject_on == 1 else 1)
    assert boundary["prompted"] == 0
    assert result["parsed_review"] is None
    assert result["journal"]["request_count"] == 0
    assert "synthetic-current-identity-withdrawn" not in json.dumps(result)
