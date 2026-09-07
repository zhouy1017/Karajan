"""Actual Host/namespace, fixed explicit qualifier fixture; not production S."""

import hashlib
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest
from candidate_checks_case import approved_check_candidate
from karajan.isolation.check_runner import PythonCheckEnvironment
from karajan.orchestration.check_services_factory import open_check_services
from karajan.orchestration.execution_budget import RunExecutionBudget
from karajan.orchestration.go_execution_intent import GoExecutionIntents
from subject_check_fixture import fixture_services
from test_candidate_checks_native import deploy
from test_candidate_subjects import synthetic_transition
from test_go_execution_intent import case, projected

__all__ = ["case", "projected"]
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Actual Linux Check namespace")


def run_cycle(service, args, directory):
    deadline = time.monotonic() + 150
    while time.monotonic() < deadline:
        service.advance(*args, principal="owner")
        current = service.reconcile(*args, principal="owner")
        if current and all(row.get("evidence") for row in current["checks"]["runs"]):
            return current
        if current and current["checks"]["phase"] == "reconciliation_required":
            rows = current["checks"]["runs"]
            if any(
                "CANDIDATE_CHECK_HOST_EXITED_WITHOUT_RESULT" in row.get("reason_codes", [])
                and row.get("observation") is None
                for row in rows
            ):
                (directory / "failed-validation.json").write_text(json.dumps(current, indent=2))
                pytest.fail("Actual fixed fixture child exited without a Check result")
        time.sleep(0.1)
    (directory / "failed-validation.json").write_text(json.dumps(current, indent=2))
    pytest.fail("Both approved checks did not finish")


def test_rebound_subject_reruns_all_checks_in_real_fixed_host_namespace(projected, tmp_path):
    image = PythonCheckEnvironment.provision(tmp_path / "image")
    environment = {
        "id": "stdlib",
        "revision": 1,
        "runtime_kind": "python312-stdlib",
        "platform": "linux_x64",
        "source_sha256": image.source()["environment_sha256"],
        "filesystem": "candidate_copy",
        "network": "none",
        "env": {},
        "max_log_bytes": 16384,
    }
    approved = approved_check_candidate(
        projected,
        tmp_path / "approved",
        environment=environment,
        checks=[
            {
                "id": "behavior",
                "revision": 1,
                "environment_ref": {"id": "stdlib", "revision": 1},
                "timeout_seconds": 60,
                "argv": [
                    "python3.12",
                    "-I",
                    "-c",
                    "from pathlib import Path; "
                    "assert Path('src/report.py').read_bytes()==b\"print('collected')\\n\"; "
                    "print('behavior-ok')",
                ],
            },
            {
                "id": "syntax",
                "revision": 1,
                "environment_ref": {"id": "stdlib", "revision": 1},
                "timeout_seconds": 60,
                "argv": ["python3.12", "-I", "-m", "py_compile", "src/report.py"],
            },
        ],
    )
    settings = deploy(approved, tmp_path, image)
    services = fixture_services(
        open_check_services(
            settings,
            run_id=approved.args[0],
            operation_id=approved.args[1],
            principal="owner",
            for_execution=True,
        )
    )
    deployed = replace(approved, admissions=services.admissions, candidates=services.candidates)
    before_tree = {
        str(path.relative_to(approved.repository)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in approved.repository.rglob("*")
        if path.is_file() and ".git" not in path.parts
    }
    try:
        first = run_cycle(services, approved.args, tmp_path)
        original = GoExecutionIntents.read_operation(
            services.admissions, *approved.args, principal="owner"
        )
        first_budget = RunExecutionBudget(services.admissions).get(
            approved.args[0], principal="owner"
        )
        synthetic_transition(deployed)
        installed = services.advance(*approved.args, principal="owner")
        assert installed["subject"]["revision"] == 2
        final = run_cycle(services, approved.args, tmp_path)
        assert final["checks"]["phase"] == "checks_passed"
        assert final["history"][0]["checks"] == first["checks"]
        rows = first["checks"]["runs"] + final["checks"]["runs"]
        assert len({row["attempt_id"] for row in rows}) == 4
        assert len({row["evidence"]["id"] for row in rows}) == 4
        for row in rows:
            assert row["evidence"]["status"] == "passed"
            assert row["observation"]["local_stop"] == "confirmed"
            assert row["native_claim"]["runner"]["pid"] > 0
            fixture = row["source"]["controller"]["fixture"]
            assert fixture["qualification"] == "explicit_test_double"
            assert all(
                hashlib.sha256(Path(path).read_bytes()).hexdigest() == sha
                for path, sha in fixture["files"].items()
            )
        retained = GoExecutionIntents.read_operation(
            services.admissions, *approved.args, principal="owner"
        )
        assert retained["execution"]["collection"] == original["execution"]["collection"]
        a = services.candidates.get(first["subject"]["candidate"]["id"])
        b = services.candidates.get(final["subject"]["candidate"]["id"])
        assert a["manifest"] == b["manifest"]
        assert a["request"]["authors"] == b["request"]["authors"]
        assert a["request"]["baseline_id"] == b["request"]["baseline_id"]
        after_budget = RunExecutionBudget(services.admissions).get(
            approved.args[0], principal="owner"
        )
        assert after_budget["started_at"] == first_budget["started_at"]
        assert len(after_budget["claims"]) == len(first_budget["claims"]) + 2
        assert not final["delivery_eligible"] and final["review"] == "not_run"
        history = open_check_services(
            settings, run_id=approved.args[0], operation_id=approved.args[1], principal="owner"
        )
        assert history.reconcile(*approved.args, principal="owner") == final
        assert {
            str(path.relative_to(approved.repository)): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in approved.repository.rglob("*")
            if path.is_file() and ".git" not in path.parts
        } == before_tree
        (tmp_path / "subject-native-report.json").write_text(
            json.dumps(
                {
                    "qualification": "explicit_test_double",
                    "host_and_namespace": "actual",
                    "validation": final,
                    "budget": after_budget,
                },
                indent=2,
            )
        )
    finally:
        history = open_check_services(
            settings, run_id=approved.args[0], operation_id=approved.args[1], principal="owner"
        )
        history.cancel(*approved.args, principal="owner")


def test_active_old_namespace_blocks_ready_subject_and_concurrent_cancel(projected, tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from karajan.execution import ProcessIdentity
    from karajan.execution._platform import observe_process
    from karajan.orchestration.candidate_checks import execution_document
    from karajan.runs import RunError

    marker_text = "old-subject-check-is-actually-running"
    image = PythonCheckEnvironment.provision(tmp_path / "image")
    reference = {"id": "stdlib", "revision": 1}
    environment = {
        **reference,
        "runtime_kind": "python312-stdlib",
        "platform": "linux_x64",
        "source_sha256": image.source()["environment_sha256"],
        "filesystem": "candidate_copy",
        "network": "none",
        "env": {},
        "max_log_bytes": 16384,
    }
    command = (
        "from pathlib import Path; import time; "
        f"print({marker_text!r}, flush=True); "
        f"Path('progress.marker').write_text({marker_text!r}); time.sleep(45)"
    )
    approved = approved_check_candidate(
        projected,
        tmp_path / "approved",
        environment=environment,
        checks=[
            {
                "id": "running",
                "revision": 1,
                "environment_ref": reference,
                "timeout_seconds": 60,
                "argv": ["python3.12", "-I", "-c", command],
            },
            {
                "id": "must-not-run",
                "revision": 1,
                "environment_ref": reference,
                "timeout_seconds": 60,
                "argv": ["python3.12", "-I", "-c", "raise AssertionError('second check')"],
            },
        ],
    )
    settings = deploy(approved, tmp_path, image)
    ids = approved.args
    services = fixture_services(
        open_check_services(
            settings, run_id=ids[0], operation_id=ids[1], principal="owner", for_execution=True
        )
    )
    try:
        for _ in range(4):
            before = services.advance(*ids, principal="owner")
        first = before["checks"]["runs"][0]
        runtime = (
            settings.check_work_root / hashlib.sha256(first["check_run_id"].encode()).hexdigest()
        )
        deadline = time.monotonic() + 65
        identity = None
        while time.monotonic() < deadline:
            try:
                report = json.loads((runtime / "namespace-init.json").read_text())
                candidate = ProcessIdentity(report["pid"], report["birth"])
                marker = Path(f"/proc/{candidate.pid}/root/workspace/progress.marker")
                if (
                    report["execution_digest"] == first["execution_digest"]
                    and observe_process(candidate) == "running"
                    and marker.read_text() == marker_text
                    and observe_process(candidate) == "running"
                ):
                    identity = candidate
                    break
            except (FileNotFoundError, ProcessLookupError, json.JSONDecodeError):
                pass
            time.sleep(0.05)
        assert identity is not None, "The actual old Check never emitted its synchronization marker"
        # Deliberately bypass only the producer's quiescence check. The real
        # consumer must independently reject this ready receipt while A runs.
        synthetic_transition(
            replace(approved, admissions=services.admissions, candidates=services.candidates)
        )
        with pytest.raises(RunError, match="REVIEW_SUBJECT_CHECK_STOP_REQUIRED"):
            services.advance(*ids, principal="owner")
        barrier = threading.Barrier(3)

        def advance():
            barrier.wait(10)
            try:
                return services.advance(*ids, principal="owner")
            except RunError as error:
                assert error.code in {
                    "REVIEW_SUBJECT_CHECK_STOP_REQUIRED",
                    "CANDIDATE_CHECKS_CANCELLED",
                }
                return error.code

        def cancel():
            barrier.wait(10)
            return services.cancel(*ids, principal="owner")

        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(advance), pool.submit(advance), pool.submit(cancel)]
            for future in futures:
                future.result(25)
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            final = services.reconcile(*ids, principal="owner")
            if final["checks"]["phase"] == "cancelled" and final["checks"]["runs"][0].get(
                "observation"
            ):
                break
            time.sleep(0.05)
        (tmp_path / "active-subject-report.json").write_text(json.dumps(final, indent=2))
        assert final["subject"] == before["subject"] and not final.get("history")
        assert observe_process(identity) in {"exited", "identity_mismatch"}
        first_final, second = final["checks"]["runs"]
        assert first_final["observation"]["local_stop"] == "confirmed"
        assert not (
            first_final["observation"]["outcome"] == "completed"
            and first_final["observation"]["exit_code"] == 0
        )
        observed = services.runner.inspect(execution_document(first_final))
        assert marker_text.encode() in services.runner.read_log(
            execution_document(first_final), observed
        )
        assert not first_final.get("evidence") or first_final["evidence"]["status"] != "passed"
        assert "claimed_at" not in second and second.get("native_claim") is None
        assert final["checks"]["phase"] == "cancelled"
        assert len(list(settings.check_work_root.glob("*/started.json"))) == 1
        history = open_check_services(
            settings, run_id=ids[0], operation_id=ids[1], principal="owner"
        )
        for _ in range(2):
            assert history.reconcile(*ids, principal="owner") == final
        assert not (approved.repository / "progress.marker").exists()
    finally:
        services.cancel(*ids, principal="owner")
