"""Same-content reviewer policy revisions in the existing Candidate ledger.

The caller holds the current Run/Project/rule/qualification guards. Provenance
here is an immutable statement from that controller, never role qualification
or a model admission. Historical reads prove a commit, not present permission.
"""

import copy
import hashlib
import json
import sqlite3
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from ._capture_lookup import _tree_hash
from .models import Freeze, ReviewRebindCommand
from .store import (
    MAX_ENTRIES,
    MAX_FILE_BYTES,
    MAX_SNAPSHOT_BYTES,
    CandidateError,
    CandidateStore,
    canonical,
    digest,
    manifest_digest,
    relative_path,
    timestamp,
)


def _command(binding: object, command_key: str) -> dict[str, Any]:
    try:
        command = ReviewRebindCommand.model_validate(
            {"binding": binding, "command_key": command_key}
        ).model_dump()
    except ValidationError:
        raise CandidateError("REVIEW_BINDING_INVALID") from None
    profiles = [
        (row["reviewer"]["profile_id"], row["reviewer"]["profile_revision"])
        for row in command["binding"]["reviewer_sources"]
    ]
    if len(profiles) != len(set(profiles)):
        raise CandidateError("POLICY_IDENTITY_AMBIGUOUS")
    return command


def _manifest(manifest: list[dict[str, Any]]) -> None:
    if not isinstance(manifest, list) or len(manifest) > MAX_ENTRIES:
        raise CandidateError("CANDIDATE_IDENTITY_INVALID")
    paths = []
    total = 0
    for row in manifest:
        path = row["path"]
        relative_path(path)
        paths.append(path)
        if row["mode"] not in {"100644", "100755"}:
            raise CandidateError("CANDIDATE_IDENTITY_INVALID")
        for field, size in ((row["blob_sha"], 40), (row["artifact"]["sha256"], 64)):
            if (
                not isinstance(field, str)
                or len(field) != size
                or any(char not in "0123456789abcdef" for char in field)
            ):
                raise CandidateError("CANDIDATE_IDENTITY_INVALID")
        size = row["artifact"]["size"]
        if type(size) is not int or not 0 <= size <= MAX_FILE_BYTES:
            raise CandidateError("CANDIDATE_IDENTITY_INVALID")
        total += size
    unique_paths = set(paths)
    if (
        len(paths) != len(unique_paths)
        or total > MAX_SNAPSHOT_BYTES
        or any(parent in unique_paths for path in paths for parent in _parents(path))
    ):
        raise CandidateError("CANDIDATE_IDENTITY_INVALID")


def _parents(path: str) -> list[str]:
    return [path.rsplit("/", count)[0] for count in range(1, path.count("/") + 1)]


def _candidate(connection: sqlite3.Connection, candidate_id: str) -> dict[str, Any]:
    row = connection.execute(
        "SELECT id,series_id,revision,data FROM candidates WHERE id=?", (candidate_id,)
    ).fetchone()
    if row is None:
        raise CandidateError("CANDIDATE_NOT_FOUND")
    try:
        record: dict[str, Any] = json.loads(row[3])
        spec = Freeze.model_validate(record["request"])
        identity = {
            key: record[key]
            for key in ("repository_identity", "base_sha", "tree_sha", "input_sha256")
        }
        _manifest(record["manifest"])
        if (
            record["schema_version"] != "karajan.candidate.v1"
            or any(
                record[key] != row[index]
                for index, key in enumerate(("id", "series_id", "revision"))
            )
            or spec.series_id != record["series_id"]
            or spec.input_sha256 != record["input_sha256"]
            or not spec.writer.stopped
            or not any(
                author.attempt_id == spec.writer.attempt_id and author.fence == spec.writer.fence
                for author in spec.authors
            )
            or record["content_sha256"] != digest(identity)
            or record["manifest_sha256"] != manifest_digest(record["manifest"])
            or record["policy_sha256"] != digest(spec.policy.model_dump())
            or record["tree_sha"] != _tree_hash(record["manifest"])
        ):
            raise CandidateError("CANDIDATE_IDENTITY_INVALID")
        return record
    except (KeyError, TypeError, ValueError, AttributeError, RecursionError):
        raise CandidateError("CANDIDATE_IDENTITY_INVALID") from None


def _identity(record: dict[str, Any]) -> dict[str, Any]:
    return {
        key: record[key]
        for key in (
            "id",
            "series_id",
            "revision",
            "repository_identity",
            "base_sha",
            "tree_sha",
            "content_sha256",
            "manifest_sha256",
            "input_sha256",
            "policy_sha256",
        )
    } | {
        "baseline_id": record["request"]["baseline_id"],
        "request_sha256": digest(record["request"]),
    }


def _source(connection: sqlite3.Connection, command: dict[str, Any]) -> dict[str, Any]:
    expected = command["binding"]["source_candidate"]
    source = _candidate(connection, expected["id"])
    if _identity(source) != expected:
        raise CandidateError("REVIEW_SOURCE_BINDING_MISMATCH")
    return source


def _derived(source: dict[str, Any], command: dict[str, Any]) -> dict[str, Any]:
    record = copy.deepcopy(source)
    policy = record["request"]["policy"]
    policy["review"]["approved_reviewers"] = [
        row["reviewer"] for row in command["binding"]["reviewer_sources"]
    ]
    record["policy_sha256"] = digest(policy)
    record["review_rebind"] = command | {
        "schema_version": "karajan.candidate-review-rebind.v1",
        "binding_sha256": digest(command["binding"]),
        "request_sha256": digest(command),
    }
    return record


def _lookup(connection: sqlite3.Connection, command: dict[str, Any]) -> dict[str, Any] | None:
    rows = connection.execute(
        "SELECT id,data FROM candidates WHERE json_extract(data,'$.review_rebind.command_key')=?",
        (command["command_key"],),
    ).fetchall()
    if not rows:
        return None
    if len(rows) != 1:
        raise CandidateError("REVIEW_REBIND_RECEIPT_AMBIGUOUS")
    try:
        retained = json.loads(rows[0][1])["review_rebind"]
        if retained["binding"] != command["binding"]:
            raise CandidateError("REVIEW_REBIND_IDEMPOTENCY_CONFLICT")
        source = _source(connection, command)
        actual = _candidate(connection, rows[0][0])
        expected = _derived(source, command)
        expected.update(
            id=actual["id"], revision=source["revision"] + 1, frozen_at=actual["frozen_at"]
        )
        if actual != expected:
            raise CandidateError("REVIEW_REBIND_RECEIPT_INVALID")
        return actual
    except (KeyError, TypeError, ValueError):
        raise CandidateError("REVIEW_REBIND_RECEIPT_INVALID") from None


def lookup(store: CandidateStore, binding: object, *, command_key: str) -> dict[str, Any] | None:
    command = _command(binding, command_key)
    with store._connection(readonly=True) as connection:
        connection.execute("BEGIN")
        return _lookup(connection, command)


def rebind(store: CandidateStore, binding: object, *, command_key: str) -> dict[str, Any]:
    command = _command(binding, command_key)
    # Even an exact retry after a lost reply first uses a strictly read-only path.
    prior = lookup(store, binding, command_key=command_key)
    if prior is not None:
        return prior
    with store._connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        prior = _lookup(connection, command)
        if prior is not None:
            return prior
        source = _source(connection, command)
        latest = connection.execute(
            "SELECT MAX(revision) FROM candidates WHERE series_id=?", (source["series_id"],)
        ).fetchone()[0]
        if latest != source["revision"]:
            raise CandidateError("CANDIDATE_SUPERSEDED")
        row = connection.execute(
            "SELECT data FROM baselines WHERE id=?", (source["request"]["baseline_id"],)
        ).fetchone()
        if row is None:
            raise CandidateError("BASELINE_NOT_FOUND")
        baseline = store._baseline(row[0], source["request"]["baseline_id"])
        if any(source[key] != baseline[key] for key in ("repository_identity", "base_sha")):
            raise CandidateError("REVIEW_SOURCE_BINDING_MISMATCH")
        try:
            _manifest(baseline["manifest"])
            if _tree_hash(baseline["manifest"]) != baseline["tree_sha"]:
                raise CandidateError("BASELINE_INVALID")
            # Verify both full manifests, including untouched and deleted baseline
            # files. No source repository, scratch workspace or Git call is used.
            for manifest in (baseline["manifest"], source["manifest"]):
                for entry in manifest:
                    body = store._materialization_content(entry["artifact"])
                    blob = hashlib.sha1(
                        b"blob " + str(len(body)).encode() + b"\0" + body
                    ).hexdigest()
                    if blob != entry["blob_sha"]:
                        raise CandidateError("CANDIDATE_IDENTITY_INVALID")
        except (KeyError, TypeError, ValueError, AttributeError, RecursionError):
            raise CandidateError("CANDIDATE_IDENTITY_INVALID") from None
        record = _derived(source, command)
        record.update(id=str(uuid4()), revision=source["revision"] + 1, frozen_at=timestamp())
        connection.execute(
            "INSERT INTO candidates VALUES (?, ?, ?, ?)",
            (record["id"], record["series_id"], record["revision"], canonical(record).decode()),
        )
        return record
