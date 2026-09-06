"""Validate bounded existing-file bytes before restoring any candidate files."""

import hashlib
from typing import Any

from karajan.isolation._opencode_projection import projection_files

from .store import MAX_FILE_BYTES, MAX_SNAPSHOT_BYTES, CandidateError, relative_path


def prepare_projection(
    projection: list[dict[str, Any]],
    contents: dict[str, bytes],
    baseline: dict[str, Any],
    allowed_paths: list[str],
) -> dict[str, bytes]:
    try:
        rows = projection_files(projection)
        for path in allowed_paths:
            relative_path(path)
    except (ValueError, CandidateError):
        raise CandidateError("PROJECTION_INPUT_INVALID") from None
    if not isinstance(contents, dict) or set(contents) != {row["path"] for row in rows}:
        raise CandidateError("PROJECTION_CONTENT_SET_MISMATCH")
    # This existing-file mechanism binds every writable mount to one exact
    # approved file. A broad directory grant cannot silently expand the mount.
    if set(allowed_paths) != {row["path"] for row in rows if row["writable"]}:
        raise CandidateError("PROJECTION_WRITE_SCOPE_MISMATCH")
    originals = {entry["path"]: entry for entry in baseline["manifest"]}
    prepared = {}
    total = sum(entry["artifact"]["size"] for entry in baseline["manifest"])
    for row in rows:
        path = row["path"]
        original = originals.get(path)
        if original is None or original["artifact"]["sha256"] != row["sha256"]:
            raise CandidateError("PROJECTION_BASELINE_MISMATCH")
        content = contents[path]
        if type(content) is not bytes:
            raise CandidateError("PROJECTION_CONTENT_INVALID")
        if not row["writable"] and hashlib.sha256(content).hexdigest() != row["sha256"]:
            raise CandidateError("PROJECTION_READONLY_CHANGED")
        if len(content) > MAX_FILE_BYTES:
            raise CandidateError("SNAPSHOT_LIMIT_EXCEEDED")
        total += len(content) - original["artifact"]["size"]
        prepared[path] = content
    if total > MAX_SNAPSHOT_BYTES:
        raise CandidateError("SNAPSHOT_LIMIT_EXCEEDED")
    return prepared
