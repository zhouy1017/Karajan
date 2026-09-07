"""Actual approved operation and journal, with explicitly synthetic qualification."""

from copy import deepcopy

import pytest
from karajan.adapters.opencode.go_journal import GoCallJournal
from karajan.execution import RunnerHost
from karajan.orchestration.go_execution_intent import GoExecutionIntents, GoExecutionSource
from karajan.routing.compiler import digest
from karajan.runs import RunError
from test_go_execution_intent import case, projected, ready, reservation

__all__ = ["case", "projected", "ready", "reservation"]


@pytest.fixture
def activated(ready, tmp_path):
    admissions, routing, run, operation, _ = ready
    intents = GoExecutionIntents(
        admissions,
        source=GoExecutionSource("a" * 64, "b" * 64),
        host=RunnerHost(tmp_path / "host-binding"),
    )
    result = intents.prepare_intent(
        run["id"], operation["id"], principal="owner", command_key="execution-binding"
    )
    intent = result["execution"]["intent"]
    routing.capacity.activate(intent["admission_id"], command_key=intent["activation_key"])
    return intents.activation_recorded(run["id"], operation["id"], principal="owner")


def test_original_task_identity_derives_host_manifest_activation_and_one_journal_grant(
    activated, tmp_path
):
    from karajan.orchestration.go_task_binding import (
        task_grant_binding,
        task_host_activation,
        task_host_manifest,
    )

    operation = activated
    execution = operation["execution"]
    intent = execution["intent"]
    manifest = task_host_manifest(operation)
    activation = task_host_activation(operation)
    binding = task_grant_binding(operation)
    assert manifest.id == activation.attempt_id == binding["attempt_id"] == intent["attempt_id"]
    assert manifest.fence == activation.fence == binding["fence"] == 1
    assert manifest.permissions == ["read", "edit"]
    assert binding["subject"] == {
        "kind": "task_attempt",
        "project_id": intent["project_id"],
        "run_id": operation["run_id"],
        "task_id": operation["task_id"],
    }
    assert binding["runtime_digest"] == "a" * 64
    assert binding["workspace_digest"] == operation["workspace"]["digest"]
    assert binding["approval_digest"] == digest(
        operation["workspace"]["source_binding"]["approval"]
    )
    assert (
        activation.expires_at
        == binding["expires_at"]
        == execution["capacity_activation"]["expires_at"]
    )
    # The shared approved Run/Capacity fixture uses the explicit clock 1000.0.
    journal = GoCallJournal(tmp_path / "binding-journal.sqlite", clock=lambda: 1000.0)
    capability = journal.create_grant(binding, grant_id=intent["grant_id"])["capability"]
    current = journal.authenticate_grant(intent["grant_id"], capability=capability, binding=binding)
    assert current["request_count"] == 0
    assert current["binding"] == binding


@pytest.mark.parametrize("change", ["intent", "workspace", "activation", "cancel"])
def test_changed_immutable_or_withdrawn_operation_cannot_produce_execution_binding(
    activated, change
):
    from karajan.orchestration.go_task_binding import task_grant_binding

    changed = deepcopy(activated)
    if change == "intent":
        changed["execution"]["intent"]["attempt_id"] = "replacement"
    elif change == "workspace":
        changed["workspace"]["write_paths"].append("outside.txt")
    elif change == "activation":
        changed["execution"]["capacity_activation"]["admission_id"] = "replacement"
    else:
        changed["cancel_requested"] = True
    with pytest.raises(RunError):
        task_grant_binding(changed)
