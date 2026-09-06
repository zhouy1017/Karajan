"""Registered Git baselines are restored through the public CandidateStore boundary."""

import json
import os
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import pytest
from karajan.candidates import CandidateError, CandidateStore


def git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def registered(tmp_path: Path) -> tuple[CandidateStore, dict[str, Any], dict[str, bytes]]:
    repository = tmp_path / "trusted"
    repository.mkdir()
    git(repository, "init", "-q")
    git(repository, "config", "user.name", "Baseline Fixture")
    git(repository, "config", "user.email", "baseline@example.invalid")
    git(repository, "config", "core.autocrlf", "false")
    files = {
        "src/task.py": b"print('base')\n",
        "docs/untouched.md": b"Preserve this unprojected file.\n",
        "data/bytes.bin": b"\x00\xff\x80\r\n",
        ".empty": b"",
        "bin/run": b"#!/bin/sh\nexit 0\n",
    }
    for name, content in files.items():
        path = repository / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    git(repository, "add", ".")
    git(repository, "update-index", "--chmod=+x", "bin/run")
    git(repository, "commit", "-qm", "baseline fixture")
    store = CandidateStore(tmp_path / "state")
    baseline = store.register_baseline(
        repository,
        repository_identity="baseline-fixture",
        base_sha=git(repository, "rev-parse", "HEAD"),
    )
    return store, baseline, files


def test_registered_baseline_restores_complete_tree_after_source_changes_and_reopen(
    registered: tuple[CandidateStore, dict[str, Any], dict[str, bytes]], tmp_path: Path
) -> None:
    _, baseline, files = registered
    (tmp_path / "trusted").rename(tmp_path / "source-no-longer-registered")
    (tmp_path / "trusted").mkdir()
    (tmp_path / "trusted/untracked.txt").write_text("not in the baseline")
    store = CandidateStore(tmp_path / "state")
    destination = tmp_path / "restored"
    result = store.materialize_baseline(baseline["id"], destination)

    assert result["baseline_id"] == baseline["id"]
    assert result["base_sha"] == baseline["base_sha"]
    assert result["tree_sha"] == baseline["tree_sha"]
    assert result["repository_identity"] == "baseline-fixture"
    assert len(result["manifest_sha256"]) == 64
    assert result["directory"] == str(destination.resolve())
    assert {
        path.relative_to(destination).as_posix(): path.read_bytes()
        for path in destination.rglob("*")
        if path.is_file()
    } == files
    if os.name != "nt":
        assert (destination / "bin/run").stat().st_mode & 0o777 == 0o755
        assert (destination / "src/task.py").stat().st_mode & 0o777 == 0o644
    assert not (destination / ".git").exists()
    if os.name != "nt":
        git(destination, "init", "-q")
        git(destination, "add", ".")
        assert git(destination, "write-tree") == baseline["tree_sha"]


@pytest.mark.parametrize("fault", ["changed", "missing", "hardlink", "directory_link"])
def test_artifact_faults_are_rejected_before_creating_destination(
    registered: tuple[CandidateStore, dict[str, Any], dict[str, bytes]],
    tmp_path: Path,
    fault: str,
) -> None:
    store, baseline, _ = registered
    artifact = Path(baseline["manifest"][0]["artifact"]["path"])
    if fault == "changed":
        artifact.write_bytes(b"tampered")
    elif fault == "missing":
        artifact.unlink()
    elif fault == "hardlink":
        os.link(artifact, tmp_path / "shared-artifact")
    else:
        relocated = tmp_path / "relocated-artifacts"
        artifact.parent.rename(relocated)
        directory_link(artifact.parent, relocated)
    destination = tmp_path / "restored"
    with pytest.raises(CandidateError, match="ARTIFACT_UNAVAILABLE"):
        store.materialize_baseline(baseline["id"], destination)
    assert not destination.exists()


def directory_link(link: Path, target: Path) -> None:
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


def test_destination_through_directory_link_is_rejected(
    registered: tuple[CandidateStore, dict[str, Any], dict[str, bytes]], tmp_path: Path
) -> None:
    store, baseline, _ = registered
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "link"
    directory_link(link, outside)
    with pytest.raises(CandidateError, match="DESTINATION_LINK_UNSUPPORTED"):
        store.materialize_baseline(baseline["id"], link / "restored")
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("fault", ["contents", "mode", "malformed_json"])
def test_corrupted_registered_record_is_rejected(
    registered: tuple[CandidateStore, dict[str, Any], dict[str, bytes]],
    tmp_path: Path,
    fault: str,
) -> None:
    store, baseline, _ = registered
    # Storage-corruption injection; callers still submit only the registered ID.
    if fault == "contents":
        baseline["manifest"].pop()
    elif fault == "mode":
        baseline["manifest"][0]["mode"] = "100755"
    data = "{" if fault == "malformed_json" else json.dumps(baseline)
    with sqlite3.connect(tmp_path / "state/candidates.sqlite") as connection:
        connection.execute("UPDATE baselines SET data=? WHERE id=?", (data, baseline["id"]))
    destination = tmp_path / "restored"
    with pytest.raises(CandidateError, match="BASELINE_INVALID"):
        store.materialize_baseline(baseline["id"], destination)
    assert not destination.exists()


@pytest.mark.parametrize("destination_kind", ["existing", "control", "control_child", "ancestor"])
def test_destination_must_be_new_and_disjoint_from_control_storage(
    registered: tuple[CandidateStore, dict[str, Any], dict[str, bytes]],
    tmp_path: Path,
    destination_kind: str,
) -> None:
    store, baseline, _ = registered
    existing = tmp_path / "existing"
    existing.mkdir()
    (existing / "keep.txt").write_text("untouched")
    destinations = {
        "existing": existing,
        "control": tmp_path / "state",
        "control_child": tmp_path / "state/exported",
        "ancestor": tmp_path,
    }
    code = "DESTINATION_EXISTS" if destination_kind == "existing" else "CONTROL_STORAGE_OVERLAP"
    with pytest.raises(CandidateError, match=code):
        store.materialize_baseline(baseline["id"], destinations[destination_kind])
    assert (existing / "keep.txt").read_text() == "untouched"
    assert not (tmp_path / "state/exported").exists()


@pytest.mark.parametrize("identifier", ["missing", {"manifest": []}])
def test_only_an_existing_baseline_id_can_be_materialized(
    registered: tuple[CandidateStore, dict[str, Any], dict[str, bytes]],
    tmp_path: Path,
    identifier: Any,
) -> None:
    store, _, _ = registered
    code = "BASELINE_NOT_FOUND" if isinstance(identifier, str) else "BASELINE_INVALID"
    with pytest.raises(CandidateError, match=code):
        store.materialize_baseline(identifier, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


def test_overlay_then_freeze_keeps_every_unprojected_baseline_file(
    registered: tuple[CandidateStore, dict[str, Any], dict[str, bytes]], tmp_path: Path
) -> None:
    store, baseline, files = registered
    destination = tmp_path / "collector"
    store.materialize_baseline(baseline["id"], destination)
    changed = b"print('approved task change')\n"
    (destination / "src/task.py").write_bytes(changed)
    candidate = store.freeze(
        destination,
        {
            "series_id": "run/task",
            "baseline_id": baseline["id"],
            "input_sha256": "a" * 64,
            "allowed_paths": ["src/task.py"],
            "task_class": "T1",
            "writer": {
                "attempt_id": "attempt",
                "fence": 1,
                "stopped": True,
                "observation_ref": "fixture:stopped",
            },
            "authors": [
                {
                    "attempt_id": "attempt",
                    "fence": 1,
                    "profile_id": "profile",
                    "profile_revision": 1,
                    "model_family": "fixture-family",
                    "context_id": "context",
                    "provenance_ref": "fixture:author",
                }
            ],
            "policy": {
                "id": "policy",
                "revision": 1,
                "checks": [
                    {
                        "id": "test",
                        "revision": 1,
                        "argv": ["python", "-m", "pytest"],
                        "environment_sha256": "b" * 64,
                    }
                ],
                "review": {
                    "revision": 1,
                    "environment_sha256": "c" * 64,
                    "approved_reviewers": [],
                },
            },
        },
    )
    assert candidate["changed_paths"] == ["src/task.py"]
    exported = tmp_path / "review"
    store.materialize(candidate["id"], exported)
    assert {
        path.relative_to(exported).as_posix(): path.read_bytes()
        for path in exported.rglob("*")
        if path.is_file()
    } == files | {"src/task.py": changed}
