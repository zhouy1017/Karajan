"""Subscription adapter acceptance tests at its public replay CLI."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


class CodexReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.input_path = Path(self.directory.name) / "replay.json"
        self.document = json.loads(
            (ROOT / "examples/subscription/command-accept.json").read_text(encoding="utf-8")
        )

    def replay(self) -> tuple[int, dict]:
        self.input_path.write_text(json.dumps(self.document), encoding="utf-8")
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(ROOT / "backend")
        completed = subprocess.run(
            [sys.executable, "-m", "karajan.adapters.codex", "replay", str(self.input_path)],
            cwd=self.directory.name,
            env=environment,
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(completed.stderr, "")
        return completed.returncode, json.loads(completed.stdout)

    def test_an_authorized_native_command_gets_one_use_acceptance_not_live_qualification(
        self,
    ) -> None:
        code, report = self.replay()

        self.assertEqual(code, 0)
        self.assertEqual(report["schema_version"], "karajan.codex-replay-report.v1")
        self.assertEqual(report["responses"], [{"id": 301, "result": {"decision": "accept"}}])
        self.assertEqual(report["qualification"]["live_status"], "not_run")
        self.assertFalse(report["qualification"]["dispatch_eligible"])

    def test_only_an_in_progress_turn_start_can_enable_approval(self) -> None:
        for status in ("failed", "completed"):
            with self.subTest(status=status):
                self.document["steps"][1]["message"]["params"]["turn"]["status"] = status

                code, report = self.replay()

                self.assertNotEqual(code, 0)
                self.assertIn("TURN_STATUS_INVALID", report["reason_codes"])
                self.assertNotIn({"id": 301, "result": {"decision": "accept"}}, report["responses"])
                self.assertEqual(report["qualification"]["live_status"], "not_run")

    def test_missing_model_and_wrong_protocol_version_cannot_create_native_approval(self) -> None:
        for field in ("model", "runtime_version"):
            with self.subTest(field=field):
                if field == "model":
                    self.document["requested"]["model"] = None
                else:
                    self.document["requested"]["model"] = "fixture-model"
                    self.document["runtime_version"] = "unreviewed-version"
                code, report = self.replay()
                self.assertNotEqual(code, 0)
                self.assertEqual(report["responses"], [])
                self.assertIn("INPUT_INVALID", report["reason_codes"])

    def completion(self, status: str, error: dict | None = None) -> dict:
        return {
            "kind": "native",
            "at": "2026-09-05T10:00:02Z",
            "message": {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-fixture",
                    "turn": {"id": "turn-fixture", "status": status, "items": [], "error": error},
                },
            },
        }

    def test_failed_completion_cancels_pending_approval_without_echoing_turn_error(self) -> None:
        self.document["steps"].insert(
            3, self.completion("failed", {"message": "SECRET native details"})
        )

        code, report = self.replay()

        self.assertNotEqual(code, 0)
        self.assertIn("NATIVE_TURN_FAILED", report["reason_codes"])
        self.assertEqual(report["responses"], [{"id": 301, "result": {"decision": "cancel"}}])
        self.assertNotIn("SECRET", json.dumps(report))
        self.assertEqual(report["qualification"]["live_status"], "not_run")

    def test_interrupted_completion_is_reported_as_nonpassing(self) -> None:
        self.document["steps"] = self.document["steps"][:2] + [self.completion("interrupted")]

        code, report = self.replay()

        self.assertNotEqual(code, 0)
        self.assertIn("NATIVE_TURN_INTERRUPTED", report["reason_codes"])
        self.assertEqual(report["qualification"]["live_status"], "not_run")

    def test_in_progress_completion_cannot_be_a_successful_terminal_event(self) -> None:
        self.document["steps"] = self.document["steps"][:2] + [self.completion("inProgress")]

        code, report = self.replay()

        self.assertNotEqual(code, 0)
        self.assertIn("TURN_STATUS_INVALID", report["reason_codes"])

    def test_completion_requires_an_active_turn_and_cannot_close_twice(self) -> None:
        initial = self.document["steps"][:2]
        for started in (False, True):
            with self.subTest(started=started):
                prefix = initial if started else initial[:1]
                completion = self.completion("completed")
                self.document["steps"] = prefix + [completion]
                if started:
                    self.document["steps"].append(completion)

                code, report = self.replay()

                self.assertNotEqual(code, 0)
                self.assertIn("EVENT_ORDER_INVALID", report["reason_codes"])

    def test_terminal_outcome_remains_visible_after_an_earlier_command_acceptance(self) -> None:
        initial = self.document["steps"][:]
        for status, error, expected_code in (
            ("completed", None, 0),
            ("completed", {"message": "SECRET inconsistent error"}, 1),
            ("failed", {"message": "SECRET failed turn"}, 1),
            ("interrupted", None, 1),
        ):
            with self.subTest(status=status, error=error):
                terminal = self.completion(status, error)
                terminal["at"] = "2026-09-05T10:00:04Z"
                self.document["steps"] = initial + [terminal]

                code, report = self.replay()

                self.assertEqual(code, expected_code)
                self.assertEqual(
                    report["responses"], [{"id": 301, "result": {"decision": "accept"}}]
                )
                self.assertNotIn("SECRET", json.dumps(report))
                self.assertEqual(report["qualification"]["live_status"], "not_run")

    def test_permission_requires_the_exact_request_digest_in_current_authorization(self) -> None:
        self.document["authorization"]["allowed_request_digests"] = []

        code, report = self.replay()

        self.assertNotEqual(code, 0)
        self.assertNotIn({"id": 301, "result": {"decision": "accept"}}, report["responses"])
        self.assertIn("REQUEST_NOT_AUTHORIZED", report["reason_codes"])

    def test_decision_is_bound_to_the_whole_attempt_and_authorization_identity(self) -> None:
        changes = {
            "attempt_id": "other",
            "fence": 2,
            "profile_id": "other",
            "profile_revision": 2,
            "profile_digest": "d" * 64,
            "thread_id": "other",
            "turn_id": "other",
            "authorization_hash": "d" * 64,
            "request_digest": "d" * 64,
        }
        for field, value in changes.items():
            with self.subTest(field=field):
                original = self.document["steps"][3]["decision"][field]
                self.document["steps"][3]["decision"][field] = value
                code, report = self.replay()
                self.document["steps"][3]["decision"][field] = original
                self.assertNotEqual(code, 0)
                self.assertIn("DECISION_BINDING_MISMATCH", report["reason_codes"])
                self.assertNotIn({"id": 301, "result": {"decision": "accept"}}, report["responses"])

    def test_expired_decision_never_grants_permission(self) -> None:
        self.document["steps"][3]["at"] = "2026-09-05T10:02:00Z"

        code, report = self.replay()

        self.assertNotEqual(code, 0)
        self.assertIn("PERMISSION_EXPIRED", report["reason_codes"])
        self.assertEqual(report["responses"], [{"id": 301, "result": {"decision": "cancel"}}])

    def test_cancel_or_fence_invalidation_makes_late_approval_unusable(self) -> None:
        for kind in ("cancel", "invalidate"):
            with self.subTest(kind=kind):
                self.document["steps"].insert(3, {"kind": kind, "at": "2026-09-05T10:00:02Z"})
                code, report = self.replay()
                self.document["steps"].pop(3)
                self.assertNotEqual(code, 0)
                self.assertIn("ATTEMPT_INACTIVE", report["reason_codes"])
                self.assertEqual(
                    report["responses"], [{"id": 301, "result": {"decision": "cancel"}}]
                )

    def test_session_and_persistent_policy_decisions_are_never_forwarded(self) -> None:
        for decision in (
            "acceptForSession",
            {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": ["git"]}},
            {
                "applyNetworkPolicyAmendment": {
                    "network_policy_amendment": {"action": "allow", "host": "example.invalid"}
                }
            },
        ):
            with self.subTest(decision=decision):
                self.document["steps"][3]["decision"]["decision"] = decision
                code, report = self.replay()
                self.assertNotEqual(code, 0)
                self.assertIn("DECISION_SCOPE_UNSUPPORTED", report["reason_codes"])
                self.assertEqual(
                    report["responses"], [{"id": 301, "result": {"decision": "cancel"}}]
                )

    def test_replayed_request_and_decision_cannot_issue_a_second_acceptance(self) -> None:
        duplicate_steps = json.loads(json.dumps(self.document["steps"][2:4]))
        duplicate_steps[0]["at"] = "2026-09-05T10:00:04Z"
        duplicate_steps[1]["at"] = "2026-09-05T10:00:05Z"
        self.document["steps"].extend(duplicate_steps)

        code, report = self.replay()

        self.assertNotEqual(code, 0)
        self.assertEqual(
            report["responses"].count({"id": 301, "result": {"decision": "accept"}}), 1
        )
        self.assertIn("REQUEST_ALREADY_SEEN", report["reason_codes"])

    def authorize_request(self) -> None:
        request = self.document["steps"][2]["message"]
        digest = hashlib.sha256(
            json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        self.document["authorization"]["allowed_request_digests"] = [digest]
        self.document["steps"][3]["decision"]["request_digest"] = digest

    def test_native_request_from_another_turn_is_rejected_even_if_its_digest_is_authorized(
        self,
    ) -> None:
        self.document["steps"][2]["message"]["params"]["turnId"] = "other-turn"
        self.authorize_request()

        code, report = self.replay()

        self.assertNotEqual(code, 0)
        self.assertIn("NATIVE_BINDING_MISMATCH", report["reason_codes"])
        self.assertNotIn({"id": 301, "result": {"decision": "accept"}}, report["responses"])

    def test_broader_permission_methods_and_unknown_requests_never_receive_accept(self) -> None:
        for method in (
            "item/permissions/requestApproval",
            "item/fileChange/requestApproval",
            "unknown/request",
        ):
            with self.subTest(method=method):
                self.document["steps"][2]["message"]["method"] = method
                self.authorize_request()
                code, report = self.replay()
                self.assertNotEqual(code, 0)
                self.assertIn("NATIVE_METHOD_UNSUPPORTED", report["reason_codes"])
                self.assertNotIn({"id": 301, "result": {"decision": "accept"}}, report["responses"])
                if method == "item/permissions/requestApproval":
                    self.assertEqual(
                        report["responses"][0]["result"], {"permissions": {}, "scope": "turn"}
                    )

    def test_missing_or_changed_native_configuration_receipt_prevents_approval(self) -> None:
        for missing in (False, True):
            with self.subTest(missing=missing):
                original = self.document["steps"][0]
                if missing:
                    self.document["steps"].pop(0)
                else:
                    self.document["steps"][0]["message"]["result"]["model"] = "different-model"
                code, report = self.replay()
                if missing:
                    self.document["steps"].insert(0, original)
                else:
                    self.document["steps"][0]["message"]["result"]["model"] = "fixture-model"
                self.assertNotEqual(code, 0)
                self.assertIn(
                    "BINDING_UNCONFIRMED" if missing else "ACCEPTED_BINDING_MISMATCH",
                    report["reason_codes"],
                )
                self.assertNotIn({"id": 301, "result": {"decision": "accept"}}, report["responses"])

    def test_permission_cannot_precede_the_bound_turn_start_or_follow_completion(self) -> None:
        self.document["steps"][1]["message"]["method"] = "turn/completed"
        self.document["steps"][1]["message"]["params"]["turn"]["status"] = "completed"

        code, report = self.replay()

        self.assertNotEqual(code, 0)
        self.assertIn("TURN_NOT_ACTIVE", report["reason_codes"])
        self.assertNotIn({"id": 301, "result": {"decision": "accept"}}, report["responses"])

    def test_native_model_reroute_stops_pending_permission_and_cannot_be_silent(self) -> None:
        self.document["steps"].insert(
            3,
            {
                "kind": "native",
                "at": "2026-09-05T10:00:02Z",
                "message": {
                    "method": "model/rerouted",
                    "params": {
                        "threadId": "thread-fixture",
                        "turnId": "turn-fixture",
                        "fromModel": "fixture-model",
                        "toModel": "other-model",
                        "reason": "highRiskCyberActivity",
                    },
                },
            },
        )

        code, report = self.replay()

        self.assertNotEqual(code, 0)
        self.assertIn("MODEL_REROUTED", report["reason_codes"])
        self.assertEqual(report["responses"], [{"id": 301, "result": {"decision": "cancel"}}])

    def test_official_auth_update_cannot_silently_change_to_cash_api_credentials(self) -> None:
        self.document["steps"].insert(
            3,
            {
                "kind": "native",
                "at": "2026-09-05T10:00:02Z",
                "message": {
                    "method": "account/updated",
                    "params": {"authMode": "apikey", "planType": None},
                },
            },
        )

        code, report = self.replay()

        self.assertNotEqual(code, 0)
        self.assertIn("AUTH_MODE_MISMATCH", report["reason_codes"])
        self.assertNotIn({"id": 301, "result": {"decision": "accept"}}, report["responses"])

    def test_malformed_native_command_cannot_be_approved_despite_an_authorized_digest(self) -> None:
        self.document["steps"][2]["message"]["params"]["command"] = None
        self.authorize_request()

        code, report = self.replay()

        self.assertNotEqual(code, 0)
        self.assertIn("NATIVE_REQUEST_INVALID", report["reason_codes"])
        self.assertNotIn({"id": 301, "result": {"decision": "accept"}}, report["responses"])

    def test_command_in_an_unverified_environment_or_stdin_scope_is_blocked(self) -> None:
        for field, value in (
            ("environmentId", "remote-environment"),
            ("kind", "writeStdin"),
            ("networkApprovalContext", {"host": "example.invalid", "protocol": "https"}),
        ):
            with self.subTest(field=field):
                params = self.document["steps"][2]["message"]["params"]
                old = params.get(field)
                params[field] = value
                self.authorize_request()
                code, report = self.replay()
                if old is None:
                    params.pop(field)
                else:
                    params[field] = old
                self.assertNotEqual(code, 0)
                self.assertIn("NATIVE_SCOPE_UNSUPPORTED", report["reason_codes"])

    def test_unrestricted_sandbox_configuration_cannot_enter_the_supported_subset(self) -> None:
        self.document["requested"]["sandbox"] = {"type": "dangerFullAccess"}
        self.document["steps"][0]["message"]["result"]["sandbox"] = {"type": "dangerFullAccess"}

        code, report = self.replay()

        self.assertNotEqual(code, 0)
        self.assertIn("INPUT_INVALID", report["reason_codes"])
        self.assertEqual(report["responses"], [])

    def test_protocol_schema_hash_is_pinned_before_any_permission_response(self) -> None:
        self.document["schema_sha256"] = "0" * 64

        code, report = self.replay()

        self.assertNotEqual(code, 0)
        self.assertIn("PROTOCOL_SCHEMA_MISMATCH", report["reason_codes"])
        self.assertEqual(report["responses"], [])

    def test_report_keeps_evidence_and_unknown_qualification_separate_from_replay_pass(
        self,
    ) -> None:
        code, report = self.replay()

        self.assertEqual(code, 0)
        self.assertEqual(report["protocol"]["runtime_version"], "0.153.2")
        self.assertEqual(report["protocol"]["schema_sha256"], self.document["schema_sha256"])
        self.assertEqual(report["attempt"], self.document["attempt"])
        self.assertEqual(len(report["input_sha256"]), 64)
        self.assertEqual(len(report["event_order"]), 4)
        self.assertEqual(report["provenance"]["evidence_refs"], ["fixture:codex-command-accept"])
        self.assertIsNone(report["bindings"]["provider_reported"])
        self.assertEqual(
            report["qualification"]["capabilities"]["hidden_fallback_excluded"], "not_run"
        )
        self.assertEqual(
            report["qualification"]["capabilities"]["extra_delegation_disabled"], "not_run"
        )
        self.assertEqual(report["usage"]["state"], "unknown")
        self.assertEqual(report["quota"]["state"], "unknown")
        self.assertEqual(
            report["qualification"]["remaining_live_cases"],
            ["official_login", "inference", "file_tools", "cancel"],
        )

    def test_file_errors_and_malformed_json_produce_safe_json_reports(self) -> None:
        for payload in (None, b"\xff", b'{"secret":"DO_NOT_ECHO",'):
            with self.subTest(payload=payload):
                path = Path(self.directory.name) / "bad.json"
                if payload is not None:
                    path.write_bytes(payload)
                result = subprocess.run(
                    [sys.executable, "-m", "karajan.adapters.codex", "replay", str(path)],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=20,
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stderr, "")
                report = json.loads(result.stdout)
                self.assertEqual(report["status"], "failed")
                self.assertEqual(report["responses"], [])
                self.assertNotIn("DO_NOT_ECHO", result.stdout)

    def test_replay_time_cannot_move_backwards_to_resurrect_a_permission(self) -> None:
        self.document["steps"][3]["at"] = "2026-09-05T09:59:59Z"

        code, report = self.replay()

        self.assertNotEqual(code, 0)
        self.assertIn("EVENT_TIME_REVERSED", report["reason_codes"])
        self.assertNotIn({"id": 301, "result": {"decision": "accept"}}, report["responses"])

    def test_pending_permission_without_a_decision_is_not_run(self) -> None:
        self.document["steps"].pop()

        code, report = self.replay()

        self.assertNotEqual(code, 0)
        self.assertEqual(report["status"], "not_run")
        self.assertIn("PERMISSION_DECISION_MISSING", report["reason_codes"])
        pending = report["permission_outcomes"][0]
        self.assertEqual(pending["ticket"]["request_id"], 301)
        self.assertEqual(pending["ticket"]["authorization_hash"], "b" * 64)
        self.assertEqual(pending["expires_at"], "2026-09-05T10:02:00+00:00")

    def test_native_protocol_error_is_classified_without_echoing_its_message(self) -> None:
        self.document["steps"][0]["message"] = {
            "id": 101,
            "error": {"code": -32602, "message": "DO_NOT_ECHO_NATIVE_SECRET"},
        }

        code, report = self.replay()

        self.assertNotEqual(code, 0)
        self.assertIn("NATIVE_RPC_ERROR", report["reason_codes"])
        self.assertEqual(
            report["native_errors"],
            [{"request_id": 101, "code": -32602, "category": "invalid_params"}],
        )
        self.assertNotIn("DO_NOT_ECHO_NATIVE_SECRET", json.dumps(report))

    def test_visible_usage_and_quota_keep_their_native_coverage_and_unknown_fields(self) -> None:
        usage = {
            "inputTokens": 10,
            "outputTokens": 3,
            "cachedInputTokens": 2,
            "reasoningOutputTokens": 1,
            "totalTokens": 13,
        }
        self.document["steps"].extend(
            [
                {
                    "kind": "native",
                    "at": "2026-09-05T10:00:04Z",
                    "message": {
                        "method": "thread/tokenUsage/updated",
                        "params": {
                            "threadId": "thread-fixture",
                            "turnId": "turn-fixture",
                            "tokenUsage": {"total": usage, "last": usage},
                        },
                    },
                },
                {
                    "kind": "native",
                    "at": "2026-09-05T10:00:05Z",
                    "message": {
                        "method": "account/rateLimits/updated",
                        "params": {
                            "rateLimits": {
                                "limitId": "codex",
                                "primary": {"usedPercent": 25},
                                "secondary": None,
                            }
                        },
                    },
                },
            ]
        )

        code, report = self.replay()

        self.assertEqual(code, 0)
        self.assertEqual(report["usage"]["state"], "observed")
        self.assertEqual(report["usage"]["coverage"], "thread_and_turn_reported")
        self.assertIsNone(report["usage"]["model_call_count"])
        self.assertEqual(report["quota"]["primary"]["usedPercent"], 25)
        self.assertIsNone(report["quota"]["primary"]["resetsAt"])
        self.assertIsNone(report["quota"]["secondary"])
        self.assertEqual(report["estimates"]["state"], "not_provided")

    def test_empty_protocol_observations_remain_not_run(self) -> None:
        self.document["steps"] = []

        code, report = self.replay()

        self.assertNotEqual(code, 0)
        self.assertEqual(report["status"], "not_run")
        self.assertIn("BINDING_UNCONFIRMED", report["reason_codes"])

    def test_server_cleared_permission_request_cannot_be_approved_later(self) -> None:
        self.document["steps"].insert(
            3,
            {
                "kind": "native",
                "at": "2026-09-05T10:00:02Z",
                "message": {
                    "method": "serverRequest/resolved",
                    "params": {"threadId": "thread-fixture", "requestId": 301},
                },
            },
        )

        code, report = self.replay()

        self.assertNotEqual(code, 0)
        self.assertIn("REQUEST_NOT_PENDING", report["reason_codes"])
        self.assertEqual(report["responses"], [])

    def test_native_turn_error_and_internal_retry_are_observed_before_late_approval(self) -> None:
        self.document["steps"].insert(
            3,
            {
                "kind": "native",
                "at": "2026-09-05T10:00:02Z",
                "message": {
                    "method": "error",
                    "params": {
                        "threadId": "thread-fixture",
                        "turnId": "turn-fixture",
                        "willRetry": True,
                        "error": {
                            "message": "DO_NOT_ECHO_NATIVE_SECRET",
                            "codexErrorInfo": "usageLimitExceeded",
                        },
                    },
                },
            },
        )

        code, report = self.replay()

        self.assertNotEqual(code, 0)
        self.assertIn("NATIVE_TURN_ERROR", report["reason_codes"])
        self.assertTrue(report["observed_internal_retry"])
        self.assertNotIn({"id": 301, "result": {"decision": "accept"}}, report["responses"])
        self.assertNotIn("DO_NOT_ECHO_NATIVE_SECRET", json.dumps(report))

    def test_replay_has_no_runtime_or_network_side_effects(self) -> None:
        self.input_path.write_text(json.dumps(self.document), encoding="utf-8")
        script = """
import json
import runpy
import sys
effects = []
def audit(event, args):
    if event in {"subprocess.Popen", "os.system", "os.spawn", "os.posix_spawn",
                 "socket.__new__", "socket.connect", "socket.getaddrinfo"}:
        effects.append(event)
        raise RuntimeError("external action blocked by test harness")
sys.addaudithook(audit)
sys.argv = ["karajan.adapters.codex", "replay", sys.argv[1]]
try:
    runpy.run_module("karajan.adapters.codex", run_name="__main__")
except SystemExit:
    print(json.dumps({"effects": len(effects)}), file=sys.stderr)
    raise
"""
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(ROOT / "backend")
        result = subprocess.run(
            [sys.executable, "-c", script, str(self.input_path)],
            cwd=self.directory.name,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stderr)["effects"], 0)

    def test_thread_configuration_change_after_acceptance_stops_pending_approval(self) -> None:
        settings = dict(self.document["steps"][0]["message"]["result"])
        settings["model"] = "different-model"
        settings["sandboxPolicy"] = settings.pop("sandbox")
        self.document["steps"].insert(
            3,
            {
                "kind": "native",
                "at": "2026-09-05T10:00:02Z",
                "message": {
                    "method": "thread/settings/updated",
                    "params": {"threadId": "thread-fixture", "threadSettings": settings},
                },
            },
        )

        code, report = self.replay()

        self.assertNotEqual(code, 0)
        self.assertIn("CONFIGURATION_CHANGED", report["reason_codes"])
        self.assertNotIn({"id": 301, "result": {"decision": "accept"}}, report["responses"])

    def test_invalid_native_error_code_is_not_echoed_as_diagnostic_content(self) -> None:
        self.document["steps"][0]["message"] = {
            "id": 101,
            "error": {"code": "DO_NOT_ECHO_NATIVE_SECRET", "message": "bad"},
        }

        code, report = self.replay()

        self.assertNotEqual(code, 0)
        self.assertIsNone(report["native_errors"][0]["code"])
        self.assertNotIn("DO_NOT_ECHO_NATIVE_SECRET", json.dumps(report))


if __name__ == "__main__":
    unittest.main()
