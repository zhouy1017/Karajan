"""A narrow existing-file projection never deletes the unprojected baseline."""

import copy
import hashlib
import sqlite3

import pytest
from karajan.candidates import CandidateError, CandidateStore
from test_baseline_materialization import registered as registered


def inputs(baseline, files):
    projection = [
        {"path": path, "sha256": hashlib.sha256(files[path]).hexdigest(), "writable": writable}
        for path, writable in [("src/task.py", True), ("docs/untouched.md", False)]
    ]
    contents = {row["path"]: files[row["path"]] for row in projection}
    contents["src/task.py"] = b"print('changed')\n"
    author = {
        "attempt_id": "capture-1",
        "fence": 1,
        "profile_id": "worker",
        "profile_revision": 1,
        "model_family": "fixture",
        "context_id": "context-1",
        "provenance_ref": "fixture",
    }
    request = {
        "series_id": "run-1/task-1",
        "baseline_id": baseline["id"],
        "input_sha256": "a" * 64,
        "allowed_paths": ["src/task.py"],
        "task_class": "T1",
        "authors": [author],
        "writer": {
            "attempt_id": "capture-1",
            "fence": 1,
            "stopped": True,
            "observation_ref": "trusted-stop-observation",
        },
        "policy": {
            "id": "validation",
            "revision": 1,
            "checks": [
                {
                    "id": "tests",
                    "revision": 1,
                    "argv": ["python", "-m", "pytest"],
                    "environment_sha256": "b" * 64,
                }
            ],
            "review": {"revision": 1, "environment_sha256": "c" * 64, "approved_reviewers": []},
        },
    }
    return projection, contents, request


def test_projected_change_restores_full_baseline_and_waits_for_validation(registered, tmp_path):
    store, baseline, files = registered
    projection, contents, request = inputs(baseline, files)
    (tmp_path / "trusted").rename(tmp_path / "old-source")
    result = store.freeze_projection(projection, contents, request)
    assert result["changed_paths"] == ["src/task.py"]
    assert len(result["manifest"]) == len(files)
    reopened = CandidateStore(store.directory)
    assert reopened.freeze_projection(projection, contents, request) == result
    restored = tmp_path / "restored"
    reopened.materialize(result["id"], restored)
    assert {
        p.relative_to(restored).as_posix(): p.read_bytes()
        for p in restored.rglob("*")
        if p.is_file()
    } == files | contents
    assert {e["path"]: e["mode"] for e in result["manifest"]} == {
        e["path"]: e["mode"] for e in baseline["manifest"]
    }
    gate = reopened.gate(
        result["id"],
        current={
            key: result[key]
            for key in ("repository_identity", "base_sha", "input_sha256", "policy_sha256")
        },
    )
    assert gate["local_gate_passed"] is False
    assert gate["reasons"] == ["CHECK_EVIDENCE_MISSING:tests", "REVIEW_EVIDENCE_MISSING"]
    assert not list(tmp_path.glob("karajan-projection-*"))


@pytest.mark.parametrize(
    "fault",
    [
        "readonly",
        "missing",
        "extra",
        "new",
        "baseline_hash",
        "outside",
        "write_scope",
        "empty",
        "wrong_content_type",
        "large",
        "not_stopped",
        "wrong_fence",
        "invalid_policy",
    ],
)
def test_invalid_projection_never_commits_candidate(registered, fault):
    store, baseline, files = registered
    projection, contents, request = inputs(baseline, files)
    expected = "PROJECTION_"
    if fault == "readonly":
        contents["docs/untouched.md"] = b"unauthorized"
    elif fault == "missing":
        contents.pop("src/task.py")
    elif fault == "extra":
        contents["other.py"] = b"unauthorized"
    elif fault == "new":
        projection[0]["path"] = "new.py"
        contents["new.py"] = contents.pop("src/task.py")
    elif fault == "baseline_hash":
        projection[0]["sha256"] = "0" * 64
    elif fault == "outside":
        projection[0]["path"] = "../outside.py"
    elif fault == "write_scope":
        request["allowed_paths"] = ["docs/untouched.md"]
    elif fault == "empty":
        projection.clear()
    elif fault == "wrong_content_type":
        contents["src/task.py"] = "not bytes"
    elif fault == "large":
        contents["src/task.py"] = b"x" * (8 * 1024 * 1024 + 1)
        expected = "SNAPSHOT_LIMIT_EXCEEDED"
    elif fault == "not_stopped":
        request["writer"]["stopped"] = False
        expected = "WRITER_STOP_NOT_CONFIRMED"
    elif fault == "wrong_fence":
        request["writer"]["fence"] = 2
        expected = "WRITER_STOP_NOT_CONFIRMED"
    elif fault == "invalid_policy":
        request["policy"]["checks"] = []
        expected = "FREEZE_INPUT_INVALID"
    with pytest.raises(CandidateError, match=expected):
        store.freeze_projection(projection, contents, request)
    with sqlite3.connect(store.directory / "candidates.sqlite") as db:
        assert db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 0


def test_capture_uses_detached_inputs_and_changed_bytes_create_new_revision(registered):
    store, baseline, files = registered
    projection, contents, request = inputs(baseline, files)
    before = copy.deepcopy((projection, contents, request))
    first = store.freeze_projection(projection, contents, request)
    assert (projection, contents, request) == before
    contents["src/task.py"] = b"print('second')\n"
    second = store.freeze_projection(projection, contents, request)
    assert second["revision"] == first["revision"] + 1
    assert second["content_sha256"] != first["content_sha256"]
