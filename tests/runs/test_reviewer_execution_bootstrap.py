import hashlib
import json
import os
import shutil
import stat
import sys
from pathlib import Path

import pytest
from karajan.execution import LaunchDenied, ProcessSpec, RunnerHost
from karajan.orchestration.go_task_runtime import GoTaskSettings, write_go_task_bootstrap
from karajan.orchestration.reviewer_execution_bootstrap import (
    ReviewerExecutionSettings,
    open_existing_reviewer_execution_bootstrap,
    open_reviewer_execution_intents,
    provision_reviewer_execution_bootstrap,
)
from karajan.orchestration.reviewer_execution_intent import (
    ReviewerExecutionHistory,
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


def linux_runtime_artifact() -> Path:
    """Follow the CI runtime lookup without relying on isolation test collection."""
    runtime = Path(
        os.environ.get(
            "KARAJAN_OPENCODE_LINUX_BINARY",
            str(
                Path(__file__).resolve().parents[2]
                / "runtimes/opencode/node_modules/opencode-linux-x64/bin/opencode"
            ),
        )
    )
    if not runtime.is_file():
        if os.environ.get("KARAJAN_REQUIRE_OPENCODE_ISOLATION") == "1":
            pytest.fail("Prepared fixed Linux OpenCode artifact is required")
        pytest.skip("Prepared fixed Linux OpenCode artifact is unavailable")
    return runtime


def _provision_private_linux_runtime(source: Path, tmp_path: Path) -> Path:
    """Copy the installed package artifact into a private, standalone inode."""
    source_info = source.stat()
    assert stat.S_ISREG(source_info.st_mode)
    source_identity = (source_info.st_dev, source_info.st_ino, source_info.st_nlink)
    deployment_root = tmp_path / "private-deployment"
    deployment_root.mkdir(mode=0o700)
    target = deployment_root / "opencode"
    shutil.copyfile(source, target)
    os.chmod(target, stat.S_IMODE(source_info.st_mode))

    target_info = target.stat()
    assert stat.S_ISREG(target_info.st_mode)
    assert target_info.st_nlink == 1
    assert stat.S_IMODE(target_info.st_mode) == stat.S_IMODE(source_info.st_mode)
    assert target_info.st_size == source_info.st_size

    def digest(path: Path) -> str:
        value = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                value.update(chunk)
        return value.hexdigest()

    assert digest(target) == digest(source)
    current_source_info = source.stat()
    current_source_identity = (
        current_source_info.st_dev,
        current_source_info.st_ino,
        current_source_info.st_nlink,
    )
    assert current_source_identity == source_identity
    return target


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
def test_existing_factory_reopens_identity_and_rechecks_own_descriptor(
    tmp_path, binding_case, monkeypatch
):
    """The factory receives a private copy of the installed pinned runtime."""
    installed_runtime = linux_runtime_artifact()
    runtime = _provision_private_linux_runtime(installed_runtime, tmp_path)
    tokenizer = Path(os.environ["KARAJAN_GO_TOKENIZER_DIRECTORY"]).resolve()
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

    # Production opens actual existing descriptors/stores and hashes the fixed,
    # standalone runtime/tokenizer source. This does not qualify or call a model.
    first = open_reviewer_execution_intents(control)
    from karajan.adapters.opencode.go_journal import GoCallJournal

    counters = {name: 0 for name in ("native", "grant", "call", "gate", "evidence")}

    def count(name, original):
        def wrapped(*args, **kwargs):
            counters[name] += 1
            return original(*args, **kwargs)

        return wrapped

    monkeypatch.setattr(RunnerHost, "start", count("native", RunnerHost.start))
    monkeypatch.setattr(GoCallJournal, "create_grant", count("grant", GoCallJournal.create_grant))
    monkeypatch.setattr(GoCallJournal, "begin_call", count("call", GoCallJournal.begin_call))
    monkeypatch.setattr(type(candidates), "gate", count("gate", type(candidates).gate))
    monkeypatch.setattr(
        type(candidates), "_save_evidence", count("evidence", type(candidates)._save_evidence)
    )
    journal_before = journal.read_bytes()
    descriptor = control / "reviewer-execution-bootstrap.json"
    other = tmp_path / "other.sqlite"
    seeded = ReviewerExecutionIntents(
        database,
        intents.admissions,
        candidates,
        source=first.source,
        host=RunnerHost(intents.host.directory, existing_only=True),
        launch_compiler=lambda _: ReviewerLaunchSpec(ProcessSpec(("fixture",), tmp_path), "3" * 64),
    )
    original = seeded.prepare(run_id, reviewer["id"], principal="owner", command_key="prepare")
    seeded.freeze_launch(run_id, reviewer["id"], principal="owner")
    with pytest.raises(LaunchDenied):
        seeded.claim_registered_observer(
            run_id, reviewer["id"], principal="owner", timeout_seconds=0.01
        )
    historical = seeded.read(run_id, reviewer["id"], principal="owner")
    assert historical is not None
    before = database.read_bytes()
    reopened = open_reviewer_execution_intents(control)
    assert reopened.read(run_id, reviewer["id"], principal="owner") == historical
    assert database.read_bytes() == before

    # A cold process may still inspect the fixed historical intent when one
    # current execution asset disappears.  The explicit history interface has
    # no effect methods and does not provision a replacement store or seal.
    task_descriptor = control / "go-task-bootstrap.json"
    original_task = json.loads(task_descriptor.read_text())
    seal = private / "material-seal.key"
    seal_bytes = seal.read_bytes()
    for missing in ("runtime", "tokenizer_directory", "credential_seal"):
        document = dict(original_task)
        if missing == "credential_seal":
            seal.unlink()
        else:
            document[missing] = str(tmp_path / f"missing-{missing}")
            task_descriptor.write_text(
                json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
            )
        cold = open_reviewer_execution_intents(control)
        assert isinstance(cold, ReviewerExecutionHistory)
        assert cold.read(run_id, reviewer["id"], principal="owner") == historical
        assert database.read_bytes() == before
        assert not hasattr(cold, "freeze_launch")
        task_descriptor.write_text(
            json.dumps(original_task, sort_keys=True, separators=(",", ":")) + "\n"
        )
        if missing == "credential_seal":
            seal.write_bytes(seal_bytes)
            seal.chmod(0o600)

    # Replacing the facade's own descriptor is a current-source change, not a
    # historical-read failure.  The next effect guard must see it.
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
    assert reopened.read(run_id, reviewer["id"], principal="owner") == historical
    assert other.read_bytes() == other_before
    assert reopened.cancel(run_id, reviewer["id"], principal="owner")["cancel_requested"]
    # The real Candidate gate is a read-only current-context check required by
    # compilation; its observed calls are not quality effects.  The connected
    # Evidence writer and every forbidden transport/native boundary stay zero.
    assert counters["gate"] > 0
    assert {key: value for key, value in counters.items() if key != "gate"} == {
        "native": 0,
        "grant": 0,
        "call": 0,
        "evidence": 0,
    }
    assert journal.read_bytes() == journal_before
    public = json.dumps(original, sort_keys=True)
    assert "material-seal" not in public and "credential" not in public

    # The factory must use actual ProjectRegistry roots for every persistent
    # execution root, including Host.  Both descriptors agree, the Host store
    # already exists, and the independent execution ledger stays outside.
    descriptor.write_text(
        json.dumps(reviewer_settings.document(), sort_keys=True, separators=(",", ":")) + "\n"
    )
    repository = Path(intents.admissions.routing.planner.projects.list()[0]["repository"]["root"])
    repository_host = repository / "reviewer-host-storage"
    repository_host.mkdir()
    RunnerHost(repository_host)
    repository_host_before = (repository_host / "runnerhost.sqlite3").read_bytes()
    ledger_before_repository_check = database.read_bytes()
    task_inside_repository = original_task | {"host_directory": str(repository_host)}
    task_descriptor.write_text(
        json.dumps(task_inside_repository, sort_keys=True, separators=(",", ":")) + "\n"
    )
    reviewer_inside_repository = reviewer_settings.document() | {
        "host_directory": str(repository_host)
    }
    descriptor.write_text(
        json.dumps(reviewer_inside_repository, sort_keys=True, separators=(",", ":")) + "\n"
    )
    with pytest.raises(RunError, match="REVIEWER_EXECUTION_HOST_IN_REPOSITORY"):
        open_reviewer_execution_intents(control)
    assert database.read_bytes() == ledger_before_repository_check
    assert (repository_host / "runnerhost.sqlite3").read_bytes() == repository_host_before
