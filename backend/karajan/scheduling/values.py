"""Validated identities, scopes and the one path-containment rule.

Three facts are decided here and nowhere else, so an authority boundary cannot
be widened by re-implementing it somewhere else:

* **Canonical serialization.** One JSON spelling is used for every stored record
  and every digest, so a value read back from SQLite hashes to the same digest it
  had in memory.
* **Path containment.** A scope pattern such as ``src/config/**`` is compared
  with a requested path through normalised, segment-wise matching. ``src/a``
  therefore never contains ``src/ab`` or ``src/a/../outside``: a prefix *string*
  comparison would accept both.
* **Bounded identifiers.** A decision, a task, a grant and a credential are each
  named by one addressable identity, never by a host location.

Nothing here reads a clock, opens a file, starts a process or resolves a
credential.
"""

import hashlib
import json
import unicodedata
from collections.abc import Sequence
from pathlib import PurePosixPath
from typing import Any

from karajan.workflows.errors import Diagnostic

from .errors import SchedulingError, located

DIGEST_LENGTH = 64

#: Characters that may not appear in an identity. A control character or a
#: separator would make an identity ambiguous between a name and a location.
_FORBIDDEN = frozenset('<>:"|?*\\')


def canonical_json(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise SchedulingError("SCHEDULING_CONTENT_NOT_SERIALIZABLE") from None


def content_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def bounded_identifier(value: object, code: str, pointer: str) -> str:
    """One addressable identity: printable, bounded and never a location."""
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        raise SchedulingError(
            code,
            diagnostics=[
                _located(code, pointer, "an identity is 1..128 printable characters"),
            ],
        )
    if any(character < " " or character == "\x7f" for character in value):
        raise SchedulingError(
            code, diagnostics=[_located(code, pointer, "an identity is printable text")]
        )
    if any(character in _FORBIDDEN for character in value) or "/" in value:
        raise SchedulingError(
            code, diagnostics=[_located(code, pointer, "an identity is one name, not a location")]
        )
    if value.startswith("."):
        raise SchedulingError(
            code, diagnostics=[_located(code, pointer, "an identity is not a hidden name")]
        )
    return value


def bounded_reference(value: object, code: str, pointer: str) -> str:
    """One fixed reference such as ``role:source-researcher@2``.

    A reference is not a *name*: it names a registered identity through a
    namespace, so ``:`` and ``@`` are part of its grammar. It is still bounded,
    printable and free of separators, so it can never be a host location, and a
    caller cannot smuggle a path into a reference slot.
    """
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        raise SchedulingError(
            code,
            diagnostics=[_located(code, pointer, "a reference is 1..128 printable characters")],
        )
    if any(character < " " or character == "" for character in value):
        raise SchedulingError(
            code, diagnostics=[_located(code, pointer, "a reference is printable text")]
        )
    if any(character in "<>|?*/" for character in value):
        raise SchedulingError(
            code, diagnostics=[_located(code, pointer, "a reference is one name, not a location")]
        )
    if value.startswith("."):
        raise SchedulingError(
            code, diagnostics=[_located(code, pointer, "a reference is not a hidden name")]
        )
    return value


def reference_list(value: object, code: str, pointer: str, *, limit: int = 256) -> list[str]:
    """A bounded list of fixed references, each validated and unique."""
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise SchedulingError(
            code, diagnostics=[_located(code, pointer, "a list of references is required")]
        )
    if len(value) > limit:
        raise SchedulingError(
            code, diagnostics=[_located(code, pointer, f"at most {limit} entries are accepted")]
        )
    if len(set(value)) != len(value):
        raise SchedulingError(
            code, diagnostics=[_located(code, pointer, "each entry appears once")]
        )
    return [bounded_reference(item, code, pointer) for item in value]


def _located(code: str, pointer: str, detail: str) -> Diagnostic:
    return located(code, pointer, detail)


def digest(value: object, code: str, pointer: str) -> str:
    if not isinstance(value, str) or len(value) != DIGEST_LENGTH:
        raise SchedulingError(
            code, diagnostics=[_located(code, pointer, "a sha256 digest is 64 hexadecimal digits")]
        )
    if not all(character in "0123456789abcdef" for character in value):
        raise SchedulingError(
            code, diagnostics=[_located(code, pointer, "a sha256 digest is lowercase hexadecimal")]
        )
    return value


def positive_count(value: object, code: str, pointer: str, *, maximum: int = 1_000_000) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise SchedulingError(
            code,
            diagnostics=[
                _located(code, pointer, f"a positive integer up to {maximum} is required")
            ],
        )
    return value


def non_negative_count(value: object, code: str, pointer: str, *, maximum: int = 1_000_000) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise SchedulingError(
            code,
            diagnostics=[_located(code, pointer, f"an integer from 0 to {maximum} is required")],
        )
    return value


def number(value: object, code: str, pointer: str) -> float:
    if type(value) not in (int, float) or isinstance(value, bool):
        raise SchedulingError(
            code, diagnostics=[_located(code, pointer, "a finite number is required")]
        )
    result = float(str(value))
    if result != result or result in (float("inf"), float("-inf")):
        raise SchedulingError(
            code, diagnostics=[_located(code, pointer, "a finite number is required")]
        )
    return result


# ------------------------------------------------------------------- path scope


def normalized_path(value: object, code: str, pointer: str) -> str:
    """One forward-slash relative path, normalised and containment-checked.

    A backslash, a drive letter, a UNC prefix, a ``.``/``..`` segment, an empty
    segment or a control character is refused rather than resolved: a scope is
    compared as written, so a value that would change meaning under a host's
    normalisation must not be accepted in the first place.
    """
    if not isinstance(value, str) or not 1 <= len(value) <= 256:
        raise SchedulingError(
            code, diagnostics=[_located(code, pointer, "a relative path is 1..256 characters")]
        )
    if any(character < " " or character == "\x7f" for character in value):
        raise SchedulingError(
            code, diagnostics=[_located(code, pointer, "a path has no control characters")]
        )
    if value.startswith(("/", "\\")) or value[1:2] == ":":
        raise SchedulingError(
            code, diagnostics=[_located(code, pointer, "a path is relative, not rooted")]
        )
    if "\\" in value:
        raise SchedulingError(
            code,
            diagnostics=[_located(code, pointer, "a path is written with forward slashes only")],
        )
    if value != unicodedata.normalize("NFC", value):
        raise SchedulingError(
            code, diagnostics=[_located(code, pointer, "a path is written in NFC form")]
        )
    segments = value.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        raise SchedulingError(
            code, diagnostics=[_located(code, pointer, "a path has no empty, '.' or '..' segment")]
        )
    if PurePosixPath(*segments).is_absolute():
        raise SchedulingError(
            code, diagnostics=[_located(code, pointer, "a path is relative, not rooted")]
        )
    return value


def normalized_scopes(value: object, code: str, pointer: str) -> list[str]:
    """A declared scope: an explicit list of path patterns, never a bare string.

    A bare string is refused rather than iterated, because iterating one would
    quietly turn ``src/**`` into its characters and change an authority boundary
    without anyone editing a policy.
    """
    if not isinstance(value, list) or not value:
        raise SchedulingError(
            code,
            diagnostics=[_located(code, pointer, "a scope is a non-empty list of path patterns")],
        )
    if len(value) > 256:
        raise SchedulingError(
            code, diagnostics=[_located(code, pointer, "a scope has at most 256 patterns")]
        )
    patterns: list[str] = []
    for index, item in enumerate(value):
        patterns.append(
            normalized_pattern(item, code, f"{pointer}/{index}")
        )
    if len(set(patterns)) != len(patterns):
        raise SchedulingError(
            code, diagnostics=[_located(code, pointer, "a scope declares each pattern once")]
        )
    return patterns


def normalized_pattern(value: object, code: str, pointer: str) -> str:
    """One scope pattern: a literal path, a directory prefix, or a ``**`` suffix.

    Only three shapes are accepted, so containment is decidable by inspection:
    ``src/a.txt`` (this file), ``src/config`` (this file or directory) and
    ``src/config/**`` (everything below it). A pattern with a wildcard anywhere
    but the final ``**`` is refused instead of being interpreted by a glob
    engine whose edge cases would decide an authority boundary.
    """
    if not isinstance(value, str) or not 1 <= len(value) <= 256:
        raise SchedulingError(
            code, diagnostics=[_located(code, pointer, "a pattern is 1..256 characters")]
        )
    if value.endswith("/**"):
        head = value[: -len("/**")]
        if not head:
            raise SchedulingError(
                code, diagnostics=[_located(code, pointer, "'**' needs a directory before it")]
            )
        return f"{normalized_path(head, code, pointer)}/**"
    if "*" in value or "?" in value or "[" in value:
        raise SchedulingError(
            code,
            diagnostics=[
                _located(code, pointer, "only a trailing '/**' wildcard is supported"),
            ],
        )
    return normalized_path(value, code, pointer)


def contains_scope(outer: Sequence[str], inner: Sequence[str]) -> bool:
    """Whether every inner pattern is contained in some outer pattern.

    Containment is decided segment by segment on normalised paths, so ``src/a``
    does not contain ``src/ab`` and ``src/a`` does not contain ``src/a/../b`` —
    the latter is refused outright by normalisation.
    """
    return all(any(pattern_covers(pattern, candidate) for pattern in outer) for candidate in inner)


def pattern_covers(pattern: str, candidate: str) -> bool:
    """Whether one normalised pattern already permits one normalised candidate."""
    if pattern == candidate:
        return True
    if pattern.endswith("/**"):
        head = pattern[: -len("/**")]
        return candidate == head or candidate.startswith(head + "/") or candidate == f"{head}/**"
    # A plain path covers itself and, when it names a directory, everything below.
    return candidate.startswith(pattern + "/") or (
        candidate.endswith("/**") and candidate[: -len("/**")] == pattern
    )


def resolve_write_zone(value: object, code: str, pointer: str) -> str:
    """The exclusive write zone one task declares.

    A zone is a relative directory path, returned in its canonical form: the
    identity two zones are compared by. Two tasks naming the same zone are
    mutually exclusive; two tasks naming independent zones are not, and the
    engine adds no global single-writer rule of its own.
    """
    return normalized_path(value, code, pointer)


def zone_key(zone: str) -> str:
    """The one identity two declared write zones are the same directory by.

    Comparison is not string equality. On the hosts this engine runs on, two
    spellings of one directory are the same directory, so ``docs/zone`` and
    ``docs/ZONE`` are one zone and admitting both would hand one directory to two
    writers. The comparison key is therefore the normalised, case-folded path,
    with each segment's trailing dots and spaces removed - the same rule the
    bundle layout uses to decide that two spellings name one file, so the engine
    has a single notion of path identity rather than two that can disagree.
    """
    return "/".join(
        unicodedata.normalize("NFC", segment).casefold().rstrip(". ")
        for segment in zone.split("/")
    )


def zones_overlap(one: str, other: str) -> bool:
    """Whether two declared zones can reach the same directory or file.

    Three ways exist, and all three have to be refused:

    * the same directory spelled differently, which ``zone_key`` answers;
    * one zone contained in the other, because a writer holding ``docs/zone``
      and a writer holding ``docs/zone/sub`` are writing in one tree; and
    * one zone nested inside the other *as a path segment prefix*, so ``docs/z``
      does not collide with ``docs/zone`` while ``docs/zone`` does collide with
      ``docs/zone/sub``.
    """
    left, right = zone_key(one).rstrip("/"), zone_key(other).rstrip("/")
    if left == right:
        return True
    return left.startswith(right + "/") or right.startswith(left + "/")
