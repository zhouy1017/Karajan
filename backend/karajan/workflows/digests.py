"""Canonical serialization and content digests for workflow bundles.

One canonical JSON spelling is used for every digest so that a value read back
from SQLite, from a file on disk or from a request body always hashes to the
same digest. Nothing here hashes a value that contains its own digest, so no
published digest is self-referential (docs/architecture/10 §3).
"""

import hashlib
import json
from typing import Any

from .errors import WorkflowError

#: Digest length produced by every function in this module.
DIGEST_PATTERN_LENGTH = 64


def canonical_json(value: Any) -> str:
    """Return the one canonical JSON spelling used for storage and hashing."""
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise WorkflowError("WORKFLOW_CONTENT_NOT_SERIALIZABLE") from None


def content_digest(value: Any) -> str:
    """Digest a JSON value through its canonical spelling.

    ``canonical_json`` already refuses anything JSON cannot carry, and a value
    that survived it is encodable, so this encoding cannot fail on a structure
    that came through the normal path.
    """
    try:
        encoded = canonical_json(value).encode("utf-8")
    except UnicodeEncodeError:
        raise WorkflowError("WORKFLOW_CONTENT_NOT_SERIALIZABLE") from None
    return hashlib.sha256(encoded).hexdigest()


def byte_digest(data: bytes) -> str:
    """Digest raw file bytes exactly as they are stored on disk."""
    return hashlib.sha256(data).hexdigest()
