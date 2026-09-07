"""Fixed Host child; all Check instructions come from existing controller stores."""

import os
import sys
from pathlib import Path

# The registered Host starts this exact source with -I in the private control
# directory. Neither a candidate cwd nor PYTHONPATH chooses the controller code.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def main() -> int:
    if len(sys.argv) != 5 or any(
        not value or len(value) > 256 or any(char.isspace() or ord(char) < 32 for char in value)
        for value in sys.argv[1:]
    ):
        print("CHECK_RUNNER_IDENTITIES_REQUIRED")
        return 2
    try:
        from karajan.execution._platform import process_identity
        from karajan.orchestration.check_services_factory import (
            load_check_services_from_fixed_bootstrap,
        )
        from karajan.orchestration.go_execution_intent import GoExecutionIntents

        run_id, operation_id, check_run_id, principal = sys.argv[1:]
        history = load_check_services_from_fixed_bootstrap(
            run_id, operation_id, principal, for_execution=False
        )
        operation = GoExecutionIntents.read_operation(
            history.admissions, run_id, operation_id, principal=principal
        )
        rows = operation.get("validation", {}).get("checks", {}).get("runs", [])
        selected = [row for row in rows if row.get("check_run_id") == check_run_id]
        if len(selected) != 1:
            print("CHECK_RUNNER_RECORD_REQUIRED")
            return 1
        original = selected[0]
        if operation["cancel_requested"] or any(
            original.get(key) is not None for key in ("native_claim", "observation", "evidence")
        ):
            history.reconcile(run_id, operation_id, principal=principal)
            return 0
        identity = process_identity(os.getpid())
        if identity is None:
            print("CHECK_RUNNER_IDENTITY_UNAVAILABLE")
            return 1
        services = load_check_services_from_fixed_bootstrap(
            run_id, operation_id, principal, for_execution=True
        )
        services.consume_check(
            run_id,
            operation_id,
            check_run_id,
            principal=principal,
            runner_identity=identity,
        )
        return 0
    except Exception:
        # No source paths, bootstrap values, candidate output or arbitrary
        # exception text are exposed through the Host's control-process log.
        print("CHECK_RUNNER_FAILED")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
