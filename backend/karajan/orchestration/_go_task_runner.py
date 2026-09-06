"""Fixed Host direct-child entry; only three original identities are arguments."""

import sys
from pathlib import Path

# Host invokes this exact file with Python -I. Resolve our trusted package from
# its installed/source location, never from a repository cwd or PYTHONPATH.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def main() -> int:
    if len(sys.argv) != 4 or any(
        not value or len(value) > 256 or any(char.isspace() or ord(char) < 32 for char in value)
        for value in sys.argv[1:]
    ):
        print("TASK_RUNNER_IDENTITIES_REQUIRED")
        return 2
    try:
        from karajan.orchestration.go_task_execution import ApprovedGoTaskExecution, consume_go_task
        from karajan.orchestration.go_task_runtime import load_go_task_services_from_fixed_bootstrap

        run_id, operation_id, principal = sys.argv[1:]
        history = load_go_task_services_from_fixed_bootstrap(
            run_id, operation_id, principal, for_execution=False
        )
        facade = ApprovedGoTaskExecution(history)
        operation = facade.get(run_id, operation_id, principal=principal)
        execution = operation.get("execution", {})
        if operation["cancel_requested"] or execution.get("effect_claim") is not None:
            facade.reconcile(run_id, operation_id, principal=principal)
            return 0
        services = load_go_task_services_from_fixed_bootstrap(
            run_id, operation_id, principal, for_execution=True
        )
        consume_go_task(services, run_id, operation_id, principal=principal)
        return 0
    except Exception:
        # The owned stores retain structured observations. Native/provider text,
        # bootstrap contents and exception strings must not reach the tool log.
        print("TASK_RUNNER_FAILED")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
