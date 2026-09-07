"""Actual running Check cancellation race; planning/Worker are explicit fixtures.

The private candidate-copy marker is only test synchronization, never Evidence.
The completed controller log must independently contain the printed marker.
"""

import hashlib
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from candidate_checks_case import approved_check_candidate
from karajan.execution import ProcessIdentity
from karajan.execution._platform import observe_process
from karajan.isolation.check_runner import PythonCheckEnvironment
from karajan.orchestration.candidate_checks import execution_document
from karajan.orchestration.check_services_factory import open_check_services
from karajan.orchestration.execution_budget import RunExecutionBudget
from test_candidate_checks_native import deploy
from test_go_execution_intent import case, projected

__all__ = ["case", "projected"]
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Actual Linux Check namespace")
MARKER = "approved-check-running-before-cancel"


def test_running_check_two_advances_and_cancel_do_not_start_another_check(projected, tmp_path):
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
        f"print({MARKER!r}, flush=True); "
        f"Path('progress.marker').write_text({MARKER!r}); "
        "time.sleep(45)"
    )
    approved = approved_check_candidate(
        projected,
        tmp_path / "approved",
        environment=environment,
        checks=[
            {
                "id": "running-contract",
                "revision": 1,
                "argv": ["python3.12", "-I", "-c", command],
                "environment_ref": reference,
                "timeout_seconds": 60,
            },
            {
                "id": "must-not-start",
                "revision": 1,
                "argv": ["python3.12", "-I", "-c", "raise AssertionError('second check ran')"],
                "environment_ref": reference,
                "timeout_seconds": 60,
            },
        ],
    )
    settings = deploy(approved, tmp_path, image)
    ids = approved.args
    service = open_check_services(
        settings, run_id=ids[0], operation_id=ids[1], principal="owner", for_execution=True
    )
    try:
        for _ in range(4):
            original = service.advance(*ids, principal="owner")
        first, second = original["checks"]["runs"]
        keys = [
            (row["check_run_id"], row["attempt_id"], row["evidence_key"]) for row in (first, second)
        ]
        runtime = (
            settings.check_work_root / hashlib.sha256(first["check_run_id"].encode()).hexdigest()
        )
        until = time.monotonic() + 65
        identity = None
        while time.monotonic() < until:
            try:
                receipt = json.loads((runtime / "namespace-init.json").read_text())
                candidate = ProcessIdentity(receipt["pid"], receipt["birth"])
                marker = Path(f"/proc/{candidate.pid}/root/workspace/progress.marker")
                if (
                    receipt["execution_digest"] == first["execution_digest"]
                    and observe_process(candidate) == "running"
                    and marker.read_text() == MARKER
                    and observe_process(candidate) == "running"
                ):
                    identity = candidate
                    break
            except (FileNotFoundError, ProcessLookupError, json.JSONDecodeError):
                pass
            time.sleep(0.05)
        assert identity is not None, (
            "The actual isolated command never produced its synchronization marker"
        )
        # No lifecycle claim substitutes for the preceding actual PID/birth and
        # command-created marker observation. The marker is not trusted Evidence.
        assert observe_process(identity) == "running"
        barrier = threading.Barrier(3)

        def advance():
            barrier.wait(timeout=10)
            return service.advance(*ids, principal="owner")

        def cancel():
            barrier.wait(timeout=10)
            return service.cancel(*ids, principal="owner")

        with ThreadPoolExecutor(max_workers=3) as workers:
            futures = [workers.submit(advance), workers.submit(advance), workers.submit(cancel)]
            for future in futures:
                future.result(timeout=20)
        until = time.monotonic() + 12
        while time.monotonic() < until:
            service.reconcile(*ids, principal="owner")
            final = service.advance(*ids, principal="owner")
            if final["checks"]["runs"][0].get("observation") is not None:
                break
            time.sleep(0.05)
        first_final, second_final = final["checks"]["runs"]
        (tmp_path / "race-observation.json").write_text(json.dumps(final, indent=2))
        assert observe_process(identity) in {"exited", "identity_mismatch"}
        assert first_final["observation"]["local_stop"] == "confirmed"
        # An external kill can be observed as completed with a nonzero exit
        # before the poll loop reads cancellation. It still cannot be success.
        assert not (
            first_final["observation"]["outcome"] == "completed"
            and first_final["observation"]["exit_code"] == 0
        )
        observed = service.runner.inspect(execution_document(first_final))
        assert observed is not None
        assert MARKER.encode() in service.runner.read_log(execution_document(first_final), observed)
        assert not first_final.get("evidence") or first_final["evidence"]["status"] != "passed"
        assert "claimed_at" not in second_final and second_final.get("native_claim") is None
        assert second_final["phase"] == "cancelled"
        assert final["checks"]["phase"] == "cancelled"
        assert final["local_gate_passed"] is final["delivery_eligible"] is False
        budget = RunExecutionBudget(service.admissions).get(ids[0], principal="owner")
        assert [claim["scope"] for claim in budget["claims"]] == ["writer", "check"]
        assert len(list(settings.check_work_root.glob("*/started.json"))) == 1
        history = open_check_services(
            settings, run_id=ids[0], operation_id=ids[1], principal="owner"
        )
        before_evidence = first_final.get("evidence")
        for _ in range(2):
            reopened = history.reconcile(*ids, principal="owner")
            assert [
                (row["check_run_id"], row["attempt_id"], row["evidence_key"])
                for row in reopened["checks"]["runs"]
            ] == keys
            assert reopened["checks"]["runs"][0].get("evidence") == before_evidence
        assert len(list(settings.check_work_root.glob("*/started.json"))) == 1
        assert not (approved.repository / "progress.marker").exists()
    finally:
        service.cancel(*ids, principal="owner")
