"""Fixed, content-free Reviewer Host child.

The controller is the only producer of this argv.  The child has no model,
network, qualification, Evidence, or delivery capability; it merely records
the one observer-claim while proving it is Host's direct registered child.
"""

from __future__ import annotations

import sys
from pathlib import Path

from karajan.orchestration.reviewer_execution_bootstrap import open_reviewer_execution_intents
from karajan.orchestration.reviewer_execution_intent import ReviewerExecutionIntents
from karajan.runs import RunError
from karajan.runs.planning import identifier


def main(argv: list[str] | None = None) -> int:
    values = sys.argv[1:] if argv is None else argv
    if len(values) != 3:
        return 2
    run_id, reviewer_operation_id, principal = values
    try:
        for value in values:
            identifier(value)
        service = open_reviewer_execution_intents(Path.cwd())
        if not isinstance(service, ReviewerExecutionIntents):
            raise RunError("REVIEWER_EXECUTION_CURRENT_EFFECTS_UNAVAILABLE")
        service.claim_registered_observer(
            run_id, reviewer_operation_id, principal=principal, timeout_seconds=5.0
        )
    except (RunError, ValueError, KeyError):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
