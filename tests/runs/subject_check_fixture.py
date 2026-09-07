"""Fixed test-only child with explicit synthetic Reviewer authority.

The production factory has no fixture flag. This separate executable is used
only by the subject consumer P test. Its bytes are in the frozen controller
source for every execution; the Host and Check namespace are real.
"""

import hashlib
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))


def synthetic_current(project_db, run, operation, transition, *, principal):
    assert project_db.in_transaction and principal == "owner"
    assert transition["binding"]["run_id"] == run["id"] == operation["run_id"]
    assert transition["semantic_digest"] == "c" * 64


def fixture_services(service):
    from karajan.execution import ProcessSpec
    from karajan.orchestration.candidate_checks import CheckLaunchSpec

    source, launch = service.controller_source, service.launch_compiler
    entry = Path(__file__).resolve()

    def current_source():
        return {
            "production_controller": source(),
            "fixture": {
                "schema_version": "synthetic.subject-check-controller.v1",
                "qualification": "explicit_test_double",
                "files": {str(entry): hashlib.sha256(entry.read_bytes()).hexdigest()},
            },
        }

    def fixed_launch(execution):
        original = launch(execution)
        spec = original.process_spec
        argv = (
            spec.argv[0],
            "-I",
            str(entry),
            *(execution[key] for key in ("run_id", "operation_id", "check_run_id", "principal")),
        )
        return CheckLaunchSpec(
            ProcessSpec(argv, spec.cwd, spec.timeout_seconds), original.bootstrap_digest
        )

    service.subject_validator = synthetic_current
    service.controller_source = current_source
    service.launch_compiler = fixed_launch
    return service


def main():
    import os

    from karajan.execution._platform import process_identity
    from karajan.orchestration.candidate_subjects import check_is_current, transition_pending
    from karajan.orchestration.check_services_factory import (
        load_check_services_from_fixed_bootstrap,
    )
    from karajan.orchestration.go_execution_intent import GoExecutionIntents

    if len(sys.argv) != 5:
        raise RuntimeError("FIXTURE_IDS_REQUIRED")
    run_id, operation_id, check_id, principal = sys.argv[1:]
    history = load_check_services_from_fixed_bootstrap(run_id, operation_id, principal)
    operation = GoExecutionIntents.read_operation(
        history.admissions, run_id, operation_id, principal=principal
    )
    row = history._row(operation, check_id)
    if (
        operation["cancel_requested"]
        or transition_pending(operation)
        or not check_is_current(operation, check_id)
        or any(row.get(key) is not None for key in ("native_claim", "observation", "evidence"))
    ):
        history.reconcile(run_id, operation_id, principal=principal)
        return 0
    services = fixture_services(
        load_check_services_from_fixed_bootstrap(
            run_id, operation_id, principal, for_execution=True
        )
    )
    identity = process_identity(os.getpid())
    if identity is None:
        raise RuntimeError("FIXTURE_RUNNER_IDENTITY_REQUIRED")
    services.consume_check(
        run_id, operation_id, check_id, principal=principal, runner_identity=identity
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
