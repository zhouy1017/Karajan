"""Probe acceptance tests at the agreed public CLI seam."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class ProbeCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.input_path = Path(self.directory.name) / "probe.json"
        self.document = json.loads(
            (ROOT / "examples/probes/fixture-passed.json").read_text(encoding="utf-8")
        )

    def run_probe(self) -> subprocess.CompletedProcess[str]:
        self.input_path.write_text(json.dumps(self.document), encoding="utf-8")
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(ROOT / "backend")
        return subprocess.run(
            [sys.executable, "-m", "karajan", "probe", str(self.input_path)],
            cwd=self.directory.name,
            env=environment,
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=20,
            check=False,
        )

    def test_valid_fixture_reports_contract_pass_without_live_qualification(self) -> None:
        completed = self.run_probe()

        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertEqual(report["schema_version"], "karajan.qualification.v1")
        self.assertEqual(report["case_id"], "offline-binding-example")
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["qualification_scope"], "offline_contract")
        self.assertFalse(report["live_qualified"])
        self.assertFalse(report["profile_enabled"])

    def test_unconfigured_model_is_rejected_without_echoing_input_secrets(self) -> None:
        self.document["profile"]["binding"]["model_id"] = None
        self.document["profile"]["auth_ref"] = "DO_NOT_ECHO_THIS_INPUT_VALUE"

        completed = self.run_probe()

        self.assertNotEqual(completed.returncode, 0)
        report = json.loads(completed.stdout)
        self.assertEqual(report["status"], "failed")
        self.assertIn("INPUT_INVALID", report["reason_codes"])
        self.assertIn(
            "profile.binding.model_id", [issue["path"] for issue in report["validation_issues"]]
        )
        self.assertNotIn("DO_NOT_ECHO_THIS_INPUT_VALUE", completed.stdout + completed.stderr)

    def test_attempt_without_a_required_permission_is_rejected(self) -> None:
        self.document["attempt"]["permissions"] = []

        completed = self.run_probe()

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("REQUIRED_PERMISSION_MISSING", json.loads(completed.stdout)["reason_codes"])

    def test_attempt_must_reference_the_supplied_profile_revision(self) -> None:
        self.document["attempt"]["profile_revision"] = 2

        completed = self.run_probe()

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("PROFILE_IDENTITY_MISMATCH", json.loads(completed.stdout)["reason_codes"])

    def test_requested_native_settings_cannot_change_the_profile_binding(self) -> None:
        self.document["attempt"]["requested_binding"]["native_settings"] = {"effort": "low"}

        completed = self.run_probe()

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("REQUESTED_BINDING_MISMATCH", json.loads(completed.stdout)["reason_codes"])

    def test_accepted_feedback_cannot_silently_change_billing(self) -> None:
        self.document["events"][0]["binding"]["billing_path"] = "api_cash"

        completed = self.run_probe()

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("ACCEPTED_BINDING_MISMATCH", json.loads(completed.stdout)["reason_codes"])

    def test_events_from_a_different_attempt_fence_are_rejected(self) -> None:
        self.document["events"][0]["fence"] = 2

        completed = self.run_probe()

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("EVENT_IDENTITY_MISMATCH", json.loads(completed.stdout)["reason_codes"])
        self.assertEqual(
            json.loads(completed.stdout)["binding_observations"]["accepted"]["state"], "unknown"
        )

    def test_missing_accepted_feedback_remains_unknown(self) -> None:
        self.document["events"].pop(0)

        completed = self.run_probe()

        self.assertNotEqual(completed.returncode, 0)
        report = json.loads(completed.stdout)
        self.assertEqual(report["status"], "not_run")
        self.assertIn("BINDING_UNCONFIRMED", report["reason_codes"])
        self.assertEqual(report["binding_observations"]["accepted"]["state"], "unknown")

    def test_required_capabilities_never_promote_nonpasses_to_passed(self) -> None:
        for status in ("failed", "not_run", "unsupported"):
            with self.subTest(status=status):
                self.document["events"][1]["status"] = status

                completed = self.run_probe()

                self.assertNotEqual(completed.returncode, 0)
                report = json.loads(completed.stdout)
                self.assertEqual(report["status"], status)
                self.assertEqual(report["capabilities"][0]["status"], status)
                self.assertIn("CAPABILITY_" + status.upper(), report["reason_codes"])

    def test_required_capability_without_an_observation_is_not_run(self) -> None:
        self.document["required_capabilities"].append("cancel_confirmation")

        completed = self.run_probe()

        self.assertNotEqual(completed.returncode, 0)
        report = json.loads(completed.stdout)
        self.assertEqual(report["status"], "not_run")
        self.assertIn("CAPABILITY_MISSING", report["reason_codes"])
        self.assertIn(
            {"capability": "cancel_confirmation", "status": "not_run"}, report["capabilities"]
        )

    def test_an_empty_capability_requirement_is_invalid(self) -> None:
        self.document["required_capabilities"] = []

        completed = self.run_probe()

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("INPUT_INVALID", json.loads(completed.stdout)["reason_codes"])

    def test_passed_capability_requires_an_evidence_reference_in_provenance(self) -> None:
        for evidence in ([], ["evidence:undeclared"]):
            with self.subTest(evidence=evidence):
                self.document["events"][1]["evidence_refs"] = evidence

                completed = self.run_probe()

                self.assertNotEqual(completed.returncode, 0)
                report = json.loads(completed.stdout)
                self.assertEqual(report["status"], "not_run")
                self.assertIn("CAPABILITY_EVIDENCE_MISSING", report["reason_codes"])

    def test_identical_duplicate_events_are_coalesced_and_counted(self) -> None:
        self.document["events"].append(dict(self.document["events"][1]))

        completed = self.run_probe()

        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertEqual(report["event_summary"]["duplicate_count"], 1)
        self.assertEqual(report["event_summary"]["unique_count"], 2)
        self.assertEqual(len(report["capabilities"]), 1)

    def test_conflicting_duplicate_event_ids_are_rejected(self) -> None:
        conflicting = dict(self.document["events"][1])
        conflicting["limitations"] = ["different observation"]
        self.document["events"].append(conflicting)

        completed = self.run_probe()

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("EVENT_ID_CONFLICT", json.loads(completed.stdout)["reason_codes"])

    def test_multiple_results_cannot_disagree_about_one_capability(self) -> None:
        conflicting = dict(self.document["events"][1])
        conflicting["event_id"] = "capability-2"
        conflicting["status"] = "unsupported"
        self.document["events"].append(conflicting)

        completed = self.run_probe()

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(json.loads(completed.stdout)["status"], "failed")
        self.assertIn("CAPABILITY_RESULT_CONFLICT", json.loads(completed.stdout)["reason_codes"])

    def test_provider_report_is_partial_and_cannot_replace_acceptance(self) -> None:
        event = self.document["events"][0]
        event["type"] = "binding.provider_reported"
        event["binding"] = {"model_id": "fixture-model-v1"}

        completed = self.run_probe()

        self.assertNotEqual(completed.returncode, 0)
        report = json.loads(completed.stdout)
        self.assertEqual(report["status"], "not_run")
        observations = report["binding_observations"]
        self.assertEqual(observations["accepted"]["state"], "unknown")
        self.assertEqual(observations["provider_reported"][0]["fields"], ["model_id"])
        self.assertEqual(observations["provider_reported"][0]["state"], "observed")

    def test_provider_reported_model_mismatch_is_rejected(self) -> None:
        event = dict(self.document["events"][0])
        event["event_id"] = "provider-1"
        event["type"] = "binding.provider_reported"
        event["binding"] = {"model_id": "different-model"}
        self.document["events"].append(event)

        completed = self.run_probe()

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("PROVIDER_BINDING_MISMATCH", json.loads(completed.stdout)["reason_codes"])

    def test_report_carries_reproducible_metadata_without_inventing_call_coverage(self) -> None:
        self.document["profile"]["usage_coverage"] = "unknown"
        self.document["provenance"]["kind"] = "imported_observation"

        completed = self.run_probe()

        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertEqual(report["profile"], {"id": "profile-fixture", "revision": 1})
        self.assertEqual(
            report["attempt"], {"id": "attempt-fixture-1", "fence": 1, "role": "worker"}
        )
        self.assertEqual(report["coverage"]["admission_granularity"], "attempt")
        self.assertEqual(report["coverage"]["usage_coverage"], "unknown")
        self.assertIsNone(report["coverage"]["observed_model_call_count"])
        self.assertEqual(report["provenance"]["runtime_version"], "1.0")
        self.assertEqual(report["provenance"]["os"], "synthetic")
        self.assertEqual(report["provenance"]["isolation"], "none_offline")
        self.assertEqual(report["provenance"]["observed_at"], "2026-09-05T08:00:00Z")
        self.assertEqual(report["provenance"]["evidence_refs"], ["fixture:binding"])
        self.assertEqual(len(report["input_sha256"]), 64)
        self.assertTrue(report["provenance"]["limitations"])
        self.assertEqual(report["binding_observations"]["requested"]["state"], "requested")
        self.assertFalse(report["live_qualified"])
        self.assertFalse(report["profile_enabled"])

    def test_provenance_must_match_the_bound_runtime_version(self) -> None:
        self.document["provenance"]["runtime_version"] = "different-version"

        completed = self.run_probe()

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("PROVENANCE_RUNTIME_MISMATCH", json.loads(completed.stdout)["reason_codes"])

    def test_unreadable_and_malformed_inputs_return_safe_json_errors(self) -> None:
        for payload, reason in (
            (None, "INPUT_UNREADABLE"),
            (b"\xff", "INPUT_ENCODING_INVALID"),
            (b'{"auth_ref":"DO_NOT_ECHO_SECRET",', "INPUT_INVALID"),
        ):
            with self.subTest(reason=reason):
                path = Path(self.directory.name) / reason
                if payload is not None:
                    path.write_bytes(payload)
                completed = subprocess.run(
                    [sys.executable, "-m", "karajan", "probe", str(path)],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    check=False,
                    timeout=20,
                )
                self.assertNotEqual(completed.returncode, 0)
                self.assertEqual(completed.stderr, "")
                report = json.loads(completed.stdout)
                self.assertEqual(report["status"], "failed")
                self.assertIn(reason, report["reason_codes"])
                self.assertNotIn("DO_NOT_ECHO_SECRET", completed.stdout)

    def test_provider_observations_include_verifiable_sanitized_values(self) -> None:
        event = dict(self.document["events"][0])
        event["event_id"] = "provider-1"
        event["type"] = "binding.provider_reported"
        event["binding"] = {"model_id": "fixture-model-v1", "native_settings": {"effort": "high"}}
        self.document["events"].append(event)

        completed = self.run_probe()

        self.assertEqual(completed.returncode, 0, completed.stderr)
        observation = json.loads(completed.stdout)["binding_observations"]["provider_reported"][0]
        self.assertEqual(observation["values"]["model_id"], "fixture-model-v1")
        self.assertEqual(len(observation["values"]["native_settings_sha256"]), 64)
        self.assertNotIn("native_settings", observation["values"])

    def test_failed_report_summary_does_not_claim_a_pass(self) -> None:
        self.document["events"][1]["status"] = "failed"

        completed = self.run_probe()

        report = json.loads(completed.stdout)
        self.assertIn("failed", report["summary"])
        self.assertNotIn("passed", report["summary"])

    def test_empty_provider_report_is_explicitly_unknown(self) -> None:
        event = dict(self.document["events"][0])
        event["event_id"] = "provider-1"
        event["type"] = "binding.provider_reported"
        event["binding"] = {"model_id": None}
        self.document["events"].append(event)

        completed = self.run_probe()

        self.assertEqual(completed.returncode, 0, completed.stderr)
        observation = json.loads(completed.stdout)["binding_observations"]["provider_reported"][0]
        self.assertEqual(observation["state"], "unknown")
        self.assertEqual(observation["fields"], [])

    def test_nonrequired_observed_capability_nonpass_keeps_the_report_nonpassed(self) -> None:
        event = dict(self.document["events"][1])
        event["event_id"] = "optional-1"
        event["capability"] = "optional_feature"
        event["status"] = "unsupported"
        self.document["events"].append(event)

        completed = self.run_probe()

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(json.loads(completed.stdout)["status"], "unsupported")

    def test_native_settings_must_match_json_types_not_python_coercions(self) -> None:
        self.document["profile"]["binding"]["native_settings"]["token_limit"] = 1
        self.document["events"][0]["binding"]["native_settings"]["token_limit"] = 1
        self.document["attempt"]["requested_binding"]["native_settings"]["token_limit"] = True

        completed = self.run_probe()

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("REQUESTED_BINDING_MISMATCH", json.loads(completed.stdout)["reason_codes"])

    def test_required_references_and_strict_identity_types_cannot_be_omitted(self) -> None:
        cases = (
            ("profile", "auth_ref", ""),
            ("attempt", "budget_ref", None),
            ("attempt", "authorization_ref", " "),
            ("profile", "revision", True),
        )
        for section, field, value in cases:
            with self.subTest(field=field):
                previous = self.document[section][field]
                self.document[section][field] = value
                completed = self.run_probe()
                self.document[section][field] = previous
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn("INPUT_INVALID", json.loads(completed.stdout)["reason_codes"])

    def test_cli_has_zero_process_and_network_effects_on_valid_and_invalid_input(self) -> None:
        audit_script = """
import json
import runpy
import sys
effects = []
def record(event, args):
    if event in {"subprocess.Popen", "os.system", "os.posix_spawn", "os.spawn",
                 "socket.__new__", "socket.connect", "socket.getaddrinfo"}:
        effects.append(event)
        raise RuntimeError("external effect forbidden by acceptance harness")
sys.addaudithook(record)
sys.argv = ["karajan", "probe", sys.argv[1]]
try:
    runpy.run_module("karajan", run_name="__main__")
except SystemExit:
    print(json.dumps({"external_effect_count": len(effects)}), file=sys.stderr)
    raise
"""
        for invalid in (False, True):
            with self.subTest(invalid=invalid):
                if invalid:
                    self.document["attempt"]["permissions"] = []
                self.input_path.write_text(json.dumps(self.document), encoding="utf-8")
                environment = os.environ.copy()
                environment["PYTHONPATH"] = str(ROOT / "backend")
                completed = subprocess.run(
                    [sys.executable, "-c", audit_script, str(self.input_path)],
                    cwd=self.directory.name,
                    env=environment,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    check=False,
                    timeout=20,
                )
                self.assertEqual(completed.returncode, 1 if invalid else 0, completed.stderr)
                self.assertEqual(json.loads(completed.stderr)["external_effect_count"], 0)
                self.assertEqual(
                    json.loads(completed.stdout)["status"], "failed" if invalid else "passed"
                )

    def test_partial_provider_settings_only_compare_parameters_actually_reported(self) -> None:
        for binding in (
            self.document["profile"]["binding"],
            self.document["attempt"]["requested_binding"],
            self.document["events"][0]["binding"],
        ):
            binding["native_settings"]["token_limit"] = 2048
        event = dict(self.document["events"][0])
        event["event_id"] = "provider-1"
        event["type"] = "binding.provider_reported"
        event["binding"] = {"native_settings": {"effort": "high"}}
        self.document["events"].append(event)

        completed = self.run_probe()

        self.assertEqual(completed.returncode, 0, completed.stdout)
        observation = json.loads(completed.stdout)["binding_observations"]["provider_reported"][0]
        self.assertEqual(observation["native_settings_fields"], ["effort"])

    def test_each_capability_preserves_its_evidence_and_limitations(self) -> None:
        self.document["events"][1]["limitations"] = ["Only a scripted acceptance receipt."]

        completed = self.run_probe()

        self.assertEqual(completed.returncode, 0, completed.stdout)
        capability = json.loads(completed.stdout)["capabilities"][0]
        self.assertEqual(capability["event_id"], "capability-1")
        self.assertEqual(capability["evidence_refs"], ["fixture:binding"])
        self.assertEqual(capability["limitations"], ["Only a scripted acceptance receipt."])


if __name__ == "__main__":
    unittest.main()
