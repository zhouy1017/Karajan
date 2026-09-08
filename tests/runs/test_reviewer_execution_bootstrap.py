from pathlib import Path

import pytest
from karajan.orchestration.reviewer_execution_bootstrap import (
    ReviewerExecutionSettings,
    open_existing_reviewer_execution_bootstrap,
    provision_reviewer_execution_bootstrap,
)
from karajan.runs import RunError


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
