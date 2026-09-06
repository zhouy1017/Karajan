"""Independent compiler identity checks; synthetic qualification, no effects."""

from copy import deepcopy

import pytest
from karajan.orchestration.go_task_binding import task_grant_binding
from karajan.runs import RunError
from test_go_task_binding import activated, case, projected, ready, reservation

__all__ = ["activated", "case", "projected", "ready", "reservation"]


def test_original_operation_compiles_historical_task_identity(activated):
    result = task_grant_binding(activated)
    assert result["subject"]["run_id"] == activated["run_id"]
    assert result["attempt_id"] == activated["planned_attempt_id"]
    assert result["expires_at"] == activated["execution"]["capacity_activation"]["expires_at"]


@pytest.mark.parametrize("change", ["operation_id", "context_id", "execution_schema"])
def test_cross_record_or_unknown_version_is_not_a_compilable_original_operation(activated, change):
    operation = deepcopy(activated)
    if change == "operation_id":
        operation["id"] = "another-operation"
    elif change == "context_id":
        operation["planned_context_id"] = "another-context"
    else:
        operation["execution"]["schema_version"] = "karajan.go-task-execution-intent.v99"
    with pytest.raises(RunError, match="TASK_EXECUTION_BINDING_MISMATCH"):
        task_grant_binding(operation)
