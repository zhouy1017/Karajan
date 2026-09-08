import json
import os
import shutil
import sys
from pathlib import Path

import pytest
from karajan.execution import ProcessSpec, RunnerHost
from karajan.orchestration.go_task_runtime import GoTaskSettings, write_go_task_bootstrap
from karajan.orchestration.reviewer_execution_bootstrap import (
    ReviewerExecutionSettings,
    open_existing_reviewer_execution_bootstrap,
    open_reviewer_execution_intents,
    provision_reviewer_execution_bootstrap,
)
from karajan.orchestration.reviewer_execution_intent import (
    ReviewerExecutionIntents,
    ReviewerExecutionSource,
    ReviewerLaunchSpec,
)
from karajan.runs import RunError
from test_reviewer_binding import _activate_reviewer_reservation, _passed_reviewer_subject

pytest_plugins = (
    "test_projected_qualification_store",
    "test_candidate_checks",
    "test_reviewer_binding",
)


def settings(tmp_path: Path) -> ReviewerExecutionSettings:
    for name in ("control", "state", "candidate", "host"):
        (tmp_path / name).mkdir()
    ledger = tmp_path / "state" / "reviewer.sqlite"
    ledger.write_bytes(b"sqlite-placeholder")
    return ReviewerExecutionSettings(
        tmp_path / "control", ledger, tmp_path / "state", tmp_path / "candidate", tmp_path / "host"
    )


def test_provisioned_descriptor_reopens_exact_existing_paths(tmp_path):
    value = settings(tmp_path)
    provision_reviewer_execution_bootstrap(value)
    reopened, digest = open_existing_reviewer_execution_bootstrap(value.control_directory)
    assert reopened == value
    assert len(digest) == 64


def test_missing_store_is_rejected_without_creating_it(tmp_path):
    value = settings(tmp_path)
    provision_reviewer_execution_bootstrap(value)
    value.execution_database.unlink()
    with pytest.raises(RunError, match="REVIEWER_EXECUTION_BOOTSTRAP_INVALID"):
        open_existing_reviewer_execution_bootstrap(value.control_directory)
    assert not value.execution_database.exists()


def test_tampered_descriptor_is_rejected(tmp_path):
    value = settings(tmp_path)
    path = provision_reviewer_execution_bootstrap(value)
    path.write_text('{"schema_version":"other"}')
    with pytest.raises(RunError, match="REVIEWER_EXECUTION_BOOTSTRAP_INVALID"):
        open_existing_reviewer_execution_bootstrap(value.control_directory)


@pytest.mark.skipif(sys.platform == "win32", reason="fixed Reviewer factory is Linux-only")
def test_existing_factory_reopens_identity_and_rechecks_own_descriptor(tmp_path, binding_case):
    """The production factory reads the pinned runtime and existing descriptors."""
    runtime = Path(os.environ["KARAJAN_OPENCODE_LINUX_BINARY"])
    tokenizer = Path(os.environ["KARAJAN_GO_TOKENIZER_DIRECTORY"])
    assert runtime.is_file() and tokenizer.is_dir()
    intents, (run_id, _), candidates, _, _ = _passed_reviewer_subject(binding_case)
    reviewer = intents.admissions.advance(
        run_id,
        intents.admissions.enqueue(run_id, "review", principal="owner", command_key="review")["id"],
        principal="owner",
    )
    _activate_reviewer_reservation(intents, run_id, reviewer)
    control, state, work, private = (
        tmp_path / "control",
        tmp_path / "state",
        tmp_path / "work",
        tmp_path / "private",
    )
    for path in (control, state, work, private):
        path.mkdir(mode=0o700)
    stores = {
        "projects.sqlite": intents.admissions.routing.planner.projects.database,
        "runs.sqlite": intents.admissions.routing.planner.database,
        "capacity.sqlite": intents.admissions.routing.capacity.path,
        "task-admissions.sqlite": intents.admissions.database,
    }
    for name, source in stores.items():
        shutil.copyfile(source, state / name)
    # Existing credential material is copied solely to preserve the original
    # descriptor's private-store shape; factory construction never resolves it
    # or makes a model/provider request.
    credential_private = binding_case[1].original.credentials._directory
    for name in ("material-seal.key", "material-seals.sqlite"):
        shutil.copyfile(credential_private / name, private / name)
        (private / name).chmod(0o600)
    journal = binding_case[1].original.go_suite.journal.path
    journal.chmod(0o600)
    task = GoTaskSettings(
        control,
        state,
        candidates.directory,
        intents.host.directory,
        journal,
        work,
        tmp_path / "task-work",
        Path(sys.executable),
        runtime,
        tokenizer,
        private,
        tuple(intents.admissions.routing.planner.projects.allowed_roots),
        (),
    )
    (tmp_path / "task-work").mkdir(mode=0o700)
    write_go_task_bootstrap(task)
    database = tmp_path / "reviewer.sqlite"
    reviewer_settings = ReviewerExecutionSettings(
        control,
        database,
        task.state_directory,
        task.candidate_directory,
        task.host_directory,
    )
    provision_reviewer_execution_bootstrap(reviewer_settings)
    # Explicit private provisioning, never an accepted empty ``touch()`` DB.
    ReviewerExecutionIntents(
        database,
        intents.admissions,
        candidates,
        source=ReviewerExecutionSource("1" * 64, "2" * 64),
        host=RunnerHost(intents.host.directory, existing_only=True),
        launch_compiler=lambda _: ReviewerLaunchSpec(ProcessSpec(("fixture",), tmp_path), "3" * 64),
    )

    # Production opens actual existing descriptors/stores and hashes the pinned
    # runtime/tokenizer source. This does not qualify or call a model.
    first = open_reviewer_execution_intents(control)
    seeded = ReviewerExecutionIntents(
        database,
        intents.admissions,
        candidates,
        source=first.source,
        host=RunnerHost(intents.host.directory, existing_only=True),
        launch_compiler=lambda _: ReviewerLaunchSpec(ProcessSpec(("fixture",), tmp_path), "3" * 64),
    )
    original = seeded.prepare(run_id, reviewer["id"], principal="owner", command_key="prepare")
    before = database.read_bytes()
    reopened = open_reviewer_execution_intents(control)
    assert reopened.read(run_id, reviewer["id"], principal="owner") == original
    assert database.read_bytes() == before

    # Replacing the facade's own descriptor is a current-source change, not a
    # historical-read failure.  The next effect guard must see it.
    descriptor = control / "reviewer-execution-bootstrap.json"
    other = tmp_path / "other.sqlite"
    ReviewerExecutionIntents(
        other,
        intents.admissions,
        candidates,
        source=first.source,
        host=RunnerHost(intents.host.directory, existing_only=True),
        launch_compiler=first.launch_compiler,
    )
    other_before = other.read_bytes()
    changed = reviewer_settings.document() | {"execution_database": str(other)}
    descriptor.write_text(json.dumps(changed, sort_keys=True, separators=(",", ":")) + "\n")
    with pytest.raises(RunError, match="REVIEWER_EXECUTION_BOOTSTRAP_CHANGED"):
        reopened.freeze_launch(run_id, reviewer["id"], principal="owner")
    assert reopened.read(run_id, reviewer["id"], principal="owner") == original
    assert other.read_bytes() == other_before
