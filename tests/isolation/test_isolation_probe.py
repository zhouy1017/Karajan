"""Public local-canary CLI: fixed inputs, real child processes, no model accounts."""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class IsolationProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="karajan-isolation-test-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.spec = json.loads(
            (ROOT / "examples/isolation/python-canary.json").read_text(encoding="utf-8")
        )

    def invoke(self) -> tuple[int, dict]:
        source = self.directory / "spec.json"
        source.write_text(json.dumps(self.spec), encoding="utf-8")
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(ROOT / "backend")
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "karajan.isolation",
                "probe",
                "--spec",
                str(source),
                "--directory",
                str(self.directory / "probe"),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=40,
            check=False,
            env=environment,
        )
        self.assertEqual(result.stderr, "")
        return result.returncode, json.loads(result.stdout)

    @unittest.skipIf(sys.platform == "linux", "Windows cannot qualify Linux namespaces")
    def test_native_windows_does_not_claim_linux_isolation(self) -> None:
        code, report = self.invoke()

        self.assertEqual(code, 2)
        self.assertEqual(report["status"], "unsupported")
        self.assertEqual(report["reason_codes"], ["LINUX_NAMESPACE_REQUIRED"])
        self.assertEqual(report["binding"]["attempt_id"], "isolation-canary-1")
        self.assertFalse(report["dispatch_eligible"])
        self.assertEqual(len(report["checks"]), 13)
        self.assertTrue(all(check["status"] == "not_run" for check in report["checks"]))

    @unittest.skipUnless(sys.platform == "linux", "Actual namespace execution requires Linux")
    def test_ambient_host_authority_is_not_inherited_by_the_canary(self) -> None:
        code, report = self.invoke()
        if code == 2 and os.environ.get("KARAJAN_REQUIRE_UNSHARE") != "1":
            self.skipTest("This environment cannot create the required namespaces")

        self.assertEqual(code, 0, report)
        checks = {check["id"]: check for check in report["checks"]}
        for name in ["symlink_escape", "environment", "inherited_fds", "host_proc", "capabilities"]:
            self.assertEqual(checks[name]["status"], "passed", checks[name])
        self.assertEqual(checks["capabilities"]["evidence"]["no_new_privs"], "1")
        self.assertEqual(report["runtime_tools_status"], "not_run")
        self.assertTrue((self.directory / "probe/report.json").is_file())

    @unittest.skipUnless(sys.platform == "linux", "Actual namespace execution requires Linux")
    def test_allowed_workspace_remains_usable_while_fake_secrets_are_unreachable(self) -> None:
        code, report = self.invoke()
        if code == 2 and os.environ.get("KARAJAN_REQUIRE_UNSHARE") != "1":
            self.skipTest("This environment cannot create the required namespaces")

        self.assertEqual(code, 0)
        checks = {check["id"]: check for check in report["checks"]}
        self.assertEqual(checks["workspace_read_write"]["status"], "passed")
        self.assertEqual(checks["protected_files"]["status"], "passed")
        self.assertTrue(checks["protected_files"]["evidence"]["baseline_all_readable"])
        self.assertTrue(checks["protected_files"]["evidence"]["outside_unchanged"])
        self.assertFalse(report["dispatch_eligible"])

    @unittest.skipUnless(sys.platform == "linux", "Actual namespace execution requires Linux")
    def test_network_admin_and_bare_remote_are_reachable_only_in_the_control(self) -> None:
        code, report = self.invoke()
        if code == 2 and os.environ.get("KARAJAN_REQUIRE_UNSHARE") != "1":
            self.skipTest("This environment cannot create the required namespaces")

        self.assertEqual(code, 0, report)
        checks = {check["id"]: check for check in report["checks"]}
        self.assertEqual(checks["network_endpoints"]["status"], "passed")
        self.assertEqual(checks["network_endpoints"]["evidence"]["baseline_receipts"], 4)
        self.assertEqual(checks["network_endpoints"]["evidence"]["sandbox_receipts"], 0)
        self.assertEqual(checks["git_remote"]["status"], "passed")
        self.assertTrue(checks["git_remote"]["evidence"]["workspace_git_usable"])

    @unittest.skipUnless(
        Path("/mnt/c/Windows/System32/cmd.exe").is_file(), "WSL PE control required"
    )
    def test_windows_interop_and_privileged_namespace_mutations_are_blocked(self) -> None:
        code, report = self.invoke()

        self.assertEqual(code, 0, report)
        checks = {check["id"]: check for check in report["checks"]}
        self.assertEqual(checks["wsl_interop"]["status"], "passed")
        self.assertTrue(checks["wsl_interop"]["evidence"]["baseline_executed"])
        self.assertFalse(checks["wsl_interop"]["evidence"]["sandbox_executed"])
        self.assertFalse(checks["capabilities"]["evidence"]["chroot_succeeded"])
        self.assertFalse(checks["capabilities"]["evidence"]["mount_succeeded"])

    @unittest.skipUnless(sys.platform == "linux", "Actual namespace execution requires Linux")
    def test_cancellation_stops_an_observed_writing_child_without_claiming_remote_stop(
        self,
    ) -> None:
        code, report = self.invoke()
        if code == 2 and os.environ.get("KARAJAN_REQUIRE_UNSHARE") != "1":
            self.skipTest("This environment cannot create the required namespaces")

        self.assertEqual(code, 0, report)
        checks = {check["id"]: check for check in report["checks"]}
        self.assertEqual(checks["process_cancel"]["status"], "passed")
        facts = checks["process_cancel"]["evidence"]
        self.assertTrue(facts["child_wrote_before_cancel"])
        self.assertTrue(facts["heartbeat_stopped"])
        self.assertEqual(facts["remote_stop"], "unknown")

    @unittest.skipUnless(sys.platform == "linux", "Actual namespace execution requires Linux")
    def test_interruption_after_canary_write_is_failed_evidence_not_unsupported(self) -> None:
        source = self.directory / "spec.json"
        source.write_text(json.dumps(self.spec), encoding="utf-8")
        environment = {**os.environ, "PYTHONPATH": str(ROOT / "backend")}
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "karajan.isolation",
                "probe",
                "--spec",
                str(source),
                "--directory",
                str(self.directory / "probe"),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
        killed = False
        try:
            deadline = time.monotonic() + 15
            allowed = self.directory / "probe/workspace/allowed.txt"
            while process.poll() is None and time.monotonic() < deadline:
                if allowed.exists() and allowed.read_text() == "allowed update":
                    children = Path(f"/proc/{process.pid}/task/{process.pid}/children")
                    for child in children.read_text().split():
                        command = Path(f"/proc/{child}/cmdline").read_bytes().split(b"\0")
                        if command[0] == b"/usr/bin/unshare":
                            os.kill(int(child), signal.SIGKILL)
                            killed = True
                            break
                    if killed:
                        break
                time.sleep(0.001)
            output, error = process.communicate(timeout=15)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
        report = json.loads(output)
        if not killed and process.returncode == 2 and os.getenv("KARAJAN_REQUIRE_UNSHARE") != "1":
            self.skipTest("This environment cannot create the required namespaces")
        self.assertTrue(killed, report)
        self.assertEqual(error, "")
        self.assertEqual(process.returncode, 1)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["reason_codes"], ["NAMESPACE_EXECUTION_UNCONFIRMED"])
        self.assertEqual(report["execution"]["exit_code"], -signal.SIGKILL)
        self.assertTrue(report["execution"]["workspace_write_observed"])
        self.assertFalse(report["execution"]["report_received"])
        self.assertTrue(report["execution"]["supervisor_pid"])
        self.assertTrue(all(item["status"] == "not_run" for item in report["checks"]))
        self.assertFalse(report["dispatch_eligible"])

    @unittest.skipUnless(sys.platform == "linux", "Actual namespace execution requires Linux")
    def test_collection_freezes_bytes_without_running_repository_git_configuration(self) -> None:
        code, report = self.invoke()
        if code == 2 and os.environ.get("KARAJAN_REQUIRE_UNSHARE") != "1":
            self.skipTest("This environment cannot create the required namespaces")

        self.assertEqual(code, 0, report)
        checks = {check["id"]: check for check in report["checks"]}
        self.assertEqual(checks["candidate_collection"]["status"], "passed")
        evidence = checks["candidate_collection"]["evidence"]
        self.assertTrue(evidence["hook_positive_control"])
        self.assertTrue(evidence["marker_unchanged_during_collection"])
        self.assertEqual(
            (self.directory / "probe/candidate/allowed.txt").read_text(), "allowed update"
        )
        self.assertEqual(len(evidence["candidate_digest"]), 64)

    def test_wrong_profile_kind_or_extra_target_cannot_create_a_probe(self) -> None:
        for mutation in ("runtime_kind", "fence", "extra_target"):
            with self.subTest(mutation=mutation):
                original = json.loads(json.dumps(self.spec))
                if mutation == "extra_target":
                    self.spec["secret_path"] = "/not-a-generated-canary"
                else:
                    self.spec["binding"][mutation] = "codex" if mutation == "runtime_kind" else True
                code, report = self.invoke()
                self.assertEqual(code, 1)
                self.assertEqual(report["reason_codes"], ["PROBE_INPUT_REJECTED"])
                self.assertFalse((self.directory / "probe").exists())
                self.spec = original

    def test_canary_report_cannot_enable_a_real_runtime(self) -> None:
        from karajan.isolation import require_qualified

        _, report = self.invoke()
        report["status"] = "passed"
        report["runtime_tools_status"] = "passed"
        report["dispatch_eligible"] = True
        with self.assertRaisesRegex(ValueError, "RUNTIME_TOOLS_NOT_QUALIFIED"):
            require_qualified(report, self.spec["binding"])

    @unittest.skipUnless(sys.platform == "linux", "Actual namespace execution requires Linux")
    def test_only_exact_binding_and_complete_canary_evidence_can_be_acknowledged(self) -> None:
        from karajan.isolation import require_qualified

        code, report = self.invoke()
        if code == 2 and os.environ.get("KARAJAN_REQUIRE_UNSHARE") != "1":
            self.skipTest("This environment cannot create the required namespaces")
        acknowledgement = require_qualified(
            report, self.spec["binding"], scope="fixed_python_canary"
        )
        self.assertFalse(acknowledgement["dispatch_eligible"])
        wrong_binding = {**self.spec["binding"], "fence": 2}
        with self.assertRaisesRegex(ValueError, "BINDING_MISMATCH"):
            require_qualified(report, wrong_binding, scope="fixed_python_canary")
        report["checks"].pop()
        with self.assertRaisesRegex(ValueError, "CANARY_EVIDENCE_INCOMPLETE"):
            require_qualified(report, self.spec["binding"], scope="fixed_python_canary")

    def test_probe_rejects_an_existing_directory_without_overwriting_evidence(self) -> None:
        self.invoke()
        evidence = (self.directory / "probe/report.json").read_bytes()

        code, report = self.invoke()

        self.assertEqual(code, 1)
        self.assertEqual(report["reason_codes"], ["PROBE_INPUT_REJECTED"])
        self.assertEqual((self.directory / "probe/report.json").read_bytes(), evidence)

    def test_probe_cannot_target_the_repository_instead_of_a_temporary_canary(self) -> None:
        from karajan.isolation import run_probe

        with self.assertRaisesRegex(ValueError, "temporary root"):
            run_probe(self.spec, ROOT)


if __name__ == "__main__":
    unittest.main()
