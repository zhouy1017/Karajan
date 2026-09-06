"""Exact file projection shared by the trusted parent and namespace setup.

This is a low-level mount description, not task authorization or qualification.
Only a controller may construct it from the approved workspace manifest.
"""

import hashlib
import re
import stat
from pathlib import Path
from typing import Any


def projection_files(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 512:
        raise ValueError("WORKSPACE_PROJECTION_INVALID")
    result = []
    seen: set[str] = set()
    for row in value:
        if not isinstance(row, dict) or set(row) != {"path", "sha256", "writable"}:
            raise ValueError("WORKSPACE_PROJECTION_INVALID")
        path = row["path"]
        if (
            not isinstance(path, str)
            or len(path) > 1024
            or any(ord(c) < 32 or c in '\\:*?"<>|[]' for c in path)
            or any(
                not part
                or part in {".", ".."}
                or part.casefold() == ".git"
                or part.endswith((".", " "))
                for part in path.split("/")
            )
            or not isinstance(row["sha256"], str)
            or re.fullmatch(r"[a-f0-9]{64}", row["sha256"]) is None
            or type(row["writable"]) is not bool
        ):
            raise ValueError("WORKSPACE_PROJECTION_INVALID")
        folded = path.casefold()
        if any(
            folded == p or folded.startswith(p + "/") or p.startswith(folded + "/") for p in seen
        ):
            raise ValueError("WORKSPACE_PROJECTION_PATH_CONFLICT")
        seen.add(folded)
        result.append(dict(row))
    return sorted(result, key=lambda row: row["path"])


def verify_projected_file(root: Path, row: dict[str, Any]) -> Path:
    path: Path = root / row["path"]
    for directory in (root, *path.parents):
        if directory == root.parent:
            break
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("WORKSPACE_PROJECTION_FILE_INVALID")
    identity = path.lstat()
    if not stat.S_ISREG(identity.st_mode) or identity.st_nlink != 1:
        raise ValueError("WORKSPACE_PROJECTION_FILE_INVALID")
    with path.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != row["sha256"]:
            raise ValueError("WORKSPACE_PROJECTION_CONTENT_CHANGED")
    return path
