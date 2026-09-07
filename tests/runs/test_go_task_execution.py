"""Public Task facade, actual stores; explicitly synthetic planning/qualification."""

import json
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
from karajan.adapters.opencode.go_journal import GoCallJournal
from karajan.candidates import CandidateStore
from karajan.execution import ProcessIdentity, ProcessSpec, RunnerHost
from karajan.orchestration.go_execution_intent import (
    GoExecutionIntents,
    GoExecutionSource,
    GoLaunchSpec,
)
from karajan.orchestration.go_task_execution import (
    ApprovedGoTaskExecution,
    GoTaskServices,
    consume_go_task,
)
from karajan.runs import RunError
from test_go_context import accounting as accounting
from test_go_context import artifacts as artifacts
from test_go_execution_intent import case, projected, ready, reservation
from test_opencode_go_composition import runtime_artifact

__all__ = ["case", "projected", "ready", "reservation"]


@pytest.fixture
def history(ready, tmp_path):
    admissions, _, run, operation, _ = ready
    intents = GoExecutionIntents(
        admissions,
        source=GoExecutionSource("a" * 64, "b" * 64),
        host=RunnerHost(tmp_path / "history-host"),
    )

    def forbidden(*args):
        raise AssertionError("History must not load execution source, keys or a ProcessSpec")

    services = GoTaskServices(
        intents=intents,
        candidates=CandidateStore(tmp_path / "candidates"),
        journal=GoCallJournal(tmp_path / "history-journal.sqlite"),
        credentials=None,
        runtime=Path("runtime-not-loaded"),
        accounting=None,
        work_root=tmp_path / "private-work",
        fresh_source=forbidden,
        fixed_runner_spec=forbidden,
    )
    return ApprovedGoTaskExecution(services), run["id"], operation["id"]


def test_get_reads_original_operation_without_execution_dependencies_or_writes(history):
    facade, run_id, operation_id = history
    intents = facade.services.intents
    paths = (intents.admissions.database, intents.admissions.routing.planner.database)
    before = {path: path.read_bytes() for path in paths}
    actual = facade.get(run_id, operation_id, principal="owner")
    assert actual["state"] == "reserved"
    actual["workspace"]["write_paths"].append("unapproved.py")
    assert facade.get(run_id, operation_id, principal="owner")["workspace"]["write_paths"] == [
        "src/report.py"
    ]
    assert before == {path: path.read_bytes() for path in paths}
    assert not facade.services.work_root.exists()


def test_history_read_requires_original_owner(history):
    facade, run_id, operation_id = history
    with pytest.raises(RunError, match="USER_DECISION_REQUIRED"):
        facade.get(run_id, operation_id, principal="another-owner")


def test_advance_cannot_use_history_only_factory_to_initialize_execution(history):
    facade, run_id, operation_id = history
    before = facade.get(run_id, operation_id, principal="owner")
    with pytest.raises(RunError, match="TASK_EXECUTION_SERVICES_REQUIRED"):
        facade.advance(run_id, operation_id, principal="owner")
    assert facade.get(run_id, operation_id, principal="owner") == before


def test_fixed_entry_rejects_extra_payload_and_ignores_project_imports(tmp_path):
    entry = Path(__file__).resolve().parents[2] / "backend/karajan/orchestration/_go_task_runner.py"
    hostile = tmp_path / "karajan"
    hostile.mkdir()
    marker = tmp_path / "project-imported"
    (hostile / "__init__.py").write_text(f"open({str(marker)!r}, 'w').write('bad')")
    result = subprocess.run(
        [sys.executable, "-I", str(entry), "run", "op", "owner", "untrusted-payload"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 2
    assert result.stdout.strip() == "TASK_RUNNER_IDENTITIES_REQUIRED"
    assert result.stderr == ""
    assert not marker.exists()


def test_reconcile_without_execution_is_historical_read_only(history):
    facade, run_id, operation_id = history
    before = facade.services.intents.admissions.database.read_bytes()
    assert facade.reconcile(run_id, operation_id, principal="owner")["state"] == "reserved"
    assert facade.services.intents.admissions.database.read_bytes() == before


def test_cancel_prepared_intent_uses_no_source_or_credential_dependency(history):
    facade, run_id, operation_id = history
    facade.services.intents.prepare_intent(
        run_id, operation_id, principal="owner", command_key="prepare"
    )
    capacity = facade.services.intents.admissions.routing.capacity
    before = capacity.path.read_bytes()
    result = facade.cancel(run_id, operation_id, principal="owner")
    assert result["cancel_requested"] is True
    assert result["execution"]["observation"]["native_stop"] == "unknown"
    assert capacity.path.read_bytes() == before
    assert facade.advance(run_id, operation_id, principal="owner")["cancel_requested"] is True
    assert not facade.services.work_root.exists()


def test_changed_controller_source_rejects_before_intent_or_capacity(history, projected):
    from dataclasses import replace

    facade, run_id, operation_id = history
    services = replace(
        facade.services,
        credentials=projected["credentials"],
        accounting=object(),
        fresh_source=lambda: {"native_task": {"fixture": True}},
    )
    before = services.intents.admissions.database.read_bytes()
    with pytest.raises(RunError, match="TASK_EXECUTION_SOURCE_CHANGED"):
        ApprovedGoTaskExecution(services).advance(run_id, operation_id, principal="owner")
    assert services.intents.admissions.database.read_bytes() == before
    assert services.intents.host.reconcile() == []


@pytest.mark.parametrize("foreign_grant", [False, True])
def test_reconcile_resumes_lost_cancel_cleanup_with_original_identity(
    history,
    tmp_path,
    foreign_grant,
):
    from karajan.contracts.probe import AttemptManifest
    from karajan.execution import ProcessSpec
    from karajan.orchestration.go_execution_intent import GoLaunchSpec

    facade, run_id, operation_id = history
    intents = facade.services.intents
    operation = intents.prepare_intent(
        run_id, operation_id, principal="owner", command_key="prepare"
    )
    intent = operation["execution"]["intent"]
    intents.admissions.routing.capacity.activate(
        intent["admission_id"], command_key=intent["activation_key"]
    )
    intents.activation_recorded(run_id, operation_id, principal="owner")
    intents.launch_compiler = lambda op: GoLaunchSpec(
        ProcessSpec((sys.executable, "-c", "pass"), tmp_path), "c" * 64
    )
    launch = intents.freeze_launch(run_id, operation_id, principal="owner")["execution"]["launch"]
    intents.host.prepare(
        AttemptManifest.model_validate(launch["manifest"]),
        intent["start_key"],
        ProcessSpec((sys.executable, "-c", "pass"), tmp_path),
    )
    intents.host.initialize_control_once(
        intent["attempt_id"],
        prepared_id=intent["start_key"],
        fence=intent["fence"],
        authorization_ref=intent["authorization_ref"],
    )
    facade.services.journal.clock = lambda: 1000.0
    binding = dict(launch["grant_binding"])
    if foreign_grant:
        binding["auth_generation"] = "another-credential-generation"
    facade.services.journal.create_grant(binding, grant_id=intent["grant_id"])
    intents.cancel_intent(run_id, operation_id, principal="owner")  # persisted; reply/cleanup lost
    before = intents.admissions.routing.capacity.path.read_bytes()
    result = facade.reconcile(run_id, operation_id, principal="owner")
    assert result["cancel_requested"] is True
    assert facade.services.journal.snapshot(intent["grant_id"])["state"] == (
        "active" if foreign_grant else "revoked"
    )
    stopped = intents.host.inspect(intent["attempt_id"])
    assert stopped.state == "exited" and stopped.business_status == "cancelled"
    assert stopped.supervisor is None
    assert intents.admissions.routing.capacity.path.read_bytes() == before


def test_direct_unregistered_caller_cannot_claim_or_resolve_material(
    history, projected, monkeypatch
):
    from dataclasses import replace

    from karajan.orchestration.go_task_binding import execution_source
    from karajan.orchestration.go_task_execution import consume_go_task

    facade, run_id, operation_id = history
    operation = facade.get(run_id, operation_id, principal="owner")
    mechanism = operation["workspace"]["source_binding"]["profile_source"]["qualification"][
        "observation"
    ]["binding"]["execution_start"]["source"]["runtime_source"]
    source = {
        "native_task": {"qualified_mechanism_descriptor": mechanism},
        "explicit_test_source": True,
    }
    facade.services.intents.source = execution_source(source)
    facade.services.intents.prepare_intent(
        run_id, operation_id, principal="owner", command_key="prepare"
    )

    def forbidden(*args, **kwargs):
        pytest.fail("An unregistered caller resolved credential material")

    monkeypatch.setattr(projected["credentials"], "resolve_exact", forbidden)
    services = replace(
        facade.services,
        credentials=projected["credentials"],
        accounting=object(),
        fresh_source=lambda: source,
    )
    with pytest.raises(KeyError):
        consume_go_task(services, run_id, operation_id, principal="owner")
    current = facade.get(run_id, operation_id, principal="owner")
    assert current["execution"]["effect_claim"] is None
    assert not services.work_root.exists()


@pytest.mark.skipif(sys.platform != "linux", reason="Fixed Linux runtime required")
def test_consume_preserves_native_failure_when_collector_rejects_missing_capture(
    projected, tmp_path, accounting, artifacts, monkeypatch
):
    from karajan.isolation import go_task
    from task_execution_fixture import approved_fixture
    from test_task_workspace import git

    repository = projected["repository"]
    for name, data in {
        "src/report.py": b"print('approved task')\n",
        "tests/test_report.py": b"assert True\n",
    }.items():
        path = repository / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    git(repository, "add", ".")
    git(
        repository,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-qm",
        "independent task baseline",
    )
    projects = projected["projects"]
    current = projects.get(projected["project_id"])
    projects.update(
        current["id"],
        {
            "name": current["name"],
            "base_ref": "main",
            "target_branch": "main",
            "allowed_target_branches": ["main"],
        },
        expected_revision=current["revision"],
        principal="owner",
        command_key="baseline-refresh",
    )
    services, run_id, op_id = approved_fixture(
        projected, tmp_path, runtime_artifact(), accounting, artifacts, 1
    )
    spec = ProcessSpec((sys.executable, "-c", "pass"), tmp_path)
    services.intents.launch_compiler = lambda op: GoLaunchSpec(spec, "c" * 64)
    identity = ProcessIdentity(42, "explicit-formal-child-boundary")
    monkeypatch.setattr(
        services.intents.host, "wait_for_runner_registration", lambda *a, **k: identity
    )

    @contextmanager
    def current_runner(*args, **kwargs):
        yield identity

    monkeypatch.setattr(services.intents.host, "current_runner_guard", current_runner)
    facade = ApprovedGoTaskExecution(services)
    facade.advance(run_id, op_id, principal="owner")
    calls = []

    def failed_native(*args, **kwargs):
        calls.append(1)
        return go_task.GoTaskResult(
            None,
            json.dumps(
                {
                    "schema_version": "karajan.go-native-task-observation.v1",
                    "status": "failed",
                    "reason_codes": ["TASK_EXECUTION_FAILED"],
                    "error_type": "RuntimeError",
                    "error_reason_code": "UNIX_RELAY_PATH_TOO_LONG",
                    "native_cleanup": {"local_stop": "not_started"},
                    "relay_cleanup": {"status": "closed"},
                }
            ),
        )

    monkeypatch.setattr("karajan.orchestration.go_task_execution.execute_go_task", failed_native)
    with pytest.raises(RunError, match="TASK_STOPPED_CAPTURE_REQUIRED"):
        consume_go_task(services, run_id, op_id, principal="owner")
    current = facade.get(run_id, op_id, principal="owner")
    assert len(calls) == 1
    assert current["execution"]["failure_diagnostic"] == {
        "schema_version": "karajan.go-task-failure-diagnostic.v1",
        "intent_digest": current["execution"]["intent_digest"],
        "reason_code": "UNIX_RELAY_PATH_TOO_LONG",
        "error_type": "RuntimeError",
        "native_cleanup": {"local_stop": "not_started"},
        "relay_cleanup": {"status": "closed"},
    }
    assert current["execution"]["effect_claim"] is not None
    grant = services.journal.snapshot(current["execution"]["intent"]["grant_id"])
    assert grant["state"] == "revoked" and grant["request_count"] == 0
    replay = consume_go_task(services, run_id, op_id, principal="owner")
    assert len(calls) == 1
    assert replay["execution"]["failure_diagnostic"] == current["execution"]["failure_diagnostic"]
    assert replay["execution"]["effect_claim"] == current["execution"]["effect_claim"]


def test_reconcile_recovers_original_committed_activation_without_activating(history):
    facade, run_id, operation_id = history
    intents = facade.services.intents
    operation = intents.prepare_intent(
        run_id, operation_id, principal="owner", command_key="prepare"
    )
    intent = operation["execution"]["intent"]
    receipt = intents.admissions.routing.capacity.activate(
        intent["admission_id"], command_key=intent["activation_key"]
    )
    before = intents.admissions.routing.capacity.path.read_bytes()
    assert operation["execution"]["capacity_activation"] is None
    recovered = facade.reconcile(run_id, operation_id, principal="owner")
    assert recovered["execution"]["capacity_activation"] == receipt
    assert recovered["execution"]["effect_claim"] is None
    assert intents.admissions.routing.capacity.path.read_bytes() == before
    assert intents.host.reconcile() == []
