"""Pure comparison of a retained projection receipt with complete CAS identities."""

import hashlib
import re
from typing import Any

from karajan.isolation._opencode_projection import projection_files

from .models import Freeze
from .store import MAX_FILE_BYTES, MAX_SNAPSHOT_BYTES, CandidateError, digest, manifest_digest


def expected_files(
    spec: Freeze, projection: object, captured_files: object, baseline: dict[str, Any]
) -> dict[str, tuple[str, str, int]]:
    try:
        rows = projection_files(projection)
        if not isinstance(captured_files, list) or any(
            not isinstance(row, dict)
            or set(row) != {"path", "sha256", "size"}
            or not isinstance(row["path"], str)
            or not isinstance(row["sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", row["sha256"]) is None
            or type(row["size"]) is not int
            or not 0 <= row["size"] <= MAX_FILE_BYTES
            for row in captured_files
        ):
            raise ValueError()
        captured = {row["path"]: row for row in captured_files}
        if len(captured) != len(captured_files) or set(captured) != {row["path"] for row in rows}:
            raise ValueError()
        if len(set(spec.allowed_paths)) != len(spec.allowed_paths) or set(spec.allowed_paths) != {
            row["path"] for row in rows if row["writable"]
        }:
            raise ValueError()
        expected = {
            row["path"]: (row["mode"], row["artifact"]["sha256"], row["artifact"]["size"])
            for row in baseline["manifest"]
        }
        if len(expected) != len(baseline["manifest"]):
            raise ValueError()
        for row in rows:
            path = row["path"]
            original = expected[path]
            after = captured[path]
            if original[1] != row["sha256"] or (
                not row["writable"] and (after["sha256"], after["size"]) != original[1:]
            ):
                raise ValueError()
            expected[path] = original[0], after["sha256"], after["size"]
        if sum(row[2] for row in expected.values()) > MAX_SNAPSHOT_BYTES:
            raise ValueError()
        return expected
    except (ValueError, KeyError, TypeError):
        raise CandidateError("CAPTURE_IDENTITY_INVALID") from None


def _tree_hash(manifest: list[dict[str, Any]], prefix: str = "") -> str:
    entries: dict[bytes, tuple[bytes, bytes, str]] = {}
    for row in manifest:
        if not row["path"].startswith(prefix):
            continue
        suffix = row["path"][len(prefix) :]
        name, separator, _ = suffix.partition("/")
        if separator:
            entries[name.encode() + b"/"] = (b"40000", name.encode(), "")
        else:
            entries[name.encode()] = (row["mode"].encode(), name.encode(), row["blob_sha"])
    body = b"".join(
        mode
        + b" "
        + name
        + b"\0"
        + bytes.fromhex(oid if oid else _tree_hash(manifest, prefix + name.decode() + "/"))
        for _, (mode, name, oid) in sorted(entries.items())
    )
    return hashlib.sha1(b"tree " + str(len(body)).encode() + b"\0" + body).hexdigest()


def matches(
    record: dict[str, Any],
    spec: Freeze,
    baseline: dict[str, Any],
    expected: dict[str, tuple[str, str, int]],
) -> bool:
    if record.get("request") != spec.model_dump():
        return False
    try:
        manifest = record["manifest"]
        actual = {
            row["path"]: (row["mode"], row["artifact"]["sha256"], row["artifact"]["size"])
            for row in manifest
        }
        if actual != expected or len(manifest) != len(expected):
            return False
        tree = _tree_hash(manifest)
        identity = {
            "repository_identity": baseline["repository_identity"],
            "base_sha": baseline["base_sha"],
            "tree_sha": tree,
            "input_sha256": spec.input_sha256,
        }
        return (
            record["schema_version"] == "karajan.candidate.v1"
            and all(record[key] == value for key, value in identity.items())
            and record["content_sha256"] == digest(identity)
            and record["manifest_sha256"] == manifest_digest(manifest)
            and record["policy_sha256"] == digest(spec.policy.model_dump())
        )
    except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
        raise CandidateError("CANDIDATE_IDENTITY_INVALID") from None
