"""The one accepted bundle layout, and the path rules that enforce it.

A bundle is a small, fixed set of bundle-relative paths. Every path is validated
before it is joined to a root, so a caller cannot name a host location, escape
through ``..``, alias one file to another with a different spelling, use a
Windows device name or an alternate data stream, or reach a file through a
symbolic link, junction or other reparse point on the way in.

The layout is deliberately closed: an unrecognised path is refused rather than
ignored, so a bundle containing something the compiler never reads cannot be
published as if it had been validated.
"""

import os
import unicodedata
from pathlib import Path, PurePosixPath

from .errors import WorkflowError, located

MANIFEST_PATH = "manifest.json"
WORKFLOW_PATH = "workflow.yaml"
ROLES_PREFIX = "roles/"
TEMPLATES_PREFIX = "templates/"

#: ``manifest.json`` is written by the controller; ``workflow.yaml`` is the one
#: file a caller must supply. Everything else is optional.
REQUIRED_PATHS = (MANIFEST_PATH, WORKFLOW_PATH)

MAXIMUM_FILES = 256
MAXIMUM_PATH_LENGTH = 200
MAXIMUM_SEGMENT_LENGTH = 96

#: Windows reserved device names, compared on the stem before the first dot.
RESERVED_STEMS = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
    | {"com0", "lpt0", "conin$", "conout$"}
)

#: Characters Windows refuses in a file name, plus the alternate data stream
#: separator. Everything below U+0020 is refused separately.
_FORBIDDEN_CHARACTERS = frozenset('<>:"|?*')

#: Windows marks a directory junction or symlink with this attribute. Python's
#: ``os.path.islink`` covers symlinks; a junction is only visible here.
FILE_ATTRIBUTE_REPARSE_POINT = 0x400

ALLOWED_EXTENSIONS = frozenset({"yaml", "yml", "txt", "md"})


def is_reparse_point(path: Path) -> bool:
    """Whether a path is a link, junction or other reparse point.

    ``Path.is_symlink`` reports a Windows directory junction as ``False``, so the
    file attribute is inspected directly; without it a junction planted inside the
    bundle root would look like an ordinary directory and its contents would be
    read from outside the managed tree.
    """
    try:
        if path.is_symlink():
            return True
        attributes = os.stat(path, follow_symlinks=False).st_file_attributes
    except (OSError, AttributeError):
        return False
    return bool(attributes & FILE_ATTRIBUTE_REPARSE_POINT)


def normalized_key(relative: str) -> str:
    """The identity two spellings must share to be the same file on this host."""
    folded = unicodedata.normalize("NFC", relative).casefold().replace("\\", "/")
    return "/".join(segment.rstrip(". ") for segment in folded.split("/"))


class BundlePaths:
    """Validate, normalize and read the files a bundle may declare."""

    @staticmethod
    def validate(value: object) -> str:
        """Return the canonical spelling of one bundle-relative file path."""
        if not isinstance(value, str) or not 1 <= len(value) <= MAXIMUM_PATH_LENGTH:
            raise _path_error("WORKFLOW_PATH_INVALID", str(value)[:80])
        # A drive letter, UNC prefix or rooted path is refused before any
        # normalization, because normalization would hide what was submitted.
        if value.startswith(("/", "\\")) or value[1:2] == ":":
            raise _path_error("WORKFLOW_PATH_NOT_RELATIVE", value)
        if value != unicodedata.normalize("NFC", value):
            raise _path_error("WORKFLOW_PATH_NOT_NORMALIZED", value)
        if any(character < " " or character == "\x7f" for character in value):
            raise _path_error("WORKFLOW_PATH_INVALID", value)
        if any(character in _FORBIDDEN_CHARACTERS for character in value):
            # Refuses ``a:b`` (an NTFS alternate data stream) and any other
            # ambiguous Win32 name while allowing ordinary relative paths.
            raise _path_error("WORKFLOW_PATH_AMBIGUOUS", value)
        segments = value.replace("\\", "/").split("/")
        if PurePosixPath(*segments).is_absolute():
            raise _path_error("WORKFLOW_PATH_NOT_RELATIVE", value)
        if any(segment in {"", ".", ".."} for segment in segments):
            raise _path_error("WORKFLOW_PATH_INVALID", value)
        for segment in segments:
            if len(segment) > MAXIMUM_SEGMENT_LENGTH:
                raise _path_error("WORKFLOW_PATH_INVALID", value)
            if segment != segment.rstrip(". "):
                # Win32 strips a trailing dot or space, so ``roles/a.yaml.`` and
                # ``roles/a.yaml`` would name one file on disk.
                raise _path_error("WORKFLOW_PATH_AMBIGUOUS", value)
            stem = segment.split(".", 1)[0].casefold()
            if stem in RESERVED_STEMS or not stem.strip():
                raise _path_error("WORKFLOW_PATH_AMBIGUOUS", value)
        return value.replace("\\", "/")

    @staticmethod
    def require_allowed(relative: str) -> None:
        """Refuse a path outside the deliberately small documented layout."""
        if relative in REQUIRED_PATHS:
            return
        for prefix in (ROLES_PREFIX, TEMPLATES_PREFIX):
            if relative.startswith(prefix) and relative.count("/") == 1:
                BundlePaths._check_declared_name(relative[len(prefix) :])
                return
        raise _path_error("WORKFLOW_PATH_NOT_ALLOWED", relative)

    @staticmethod
    def _check_declared_name(name: str) -> None:
        extension = name.rsplit(".", 1)[-1].casefold() if "." in name else ""
        if extension not in ALLOWED_EXTENSIONS:
            raise _path_error("WORKFLOW_PATH_NOT_ALLOWED", name)

    @staticmethod
    def claim(seen: dict[str, str], relative: str) -> None:
        """Refuse two spellings that name one file on a case-insensitive host."""
        key = normalized_key(relative)
        previous = seen.get(key)
        if previous is None:
            seen[key] = relative
            return
        detail = (
            f"collides with {previous} after normalization"
            if previous != relative
            else "declared more than once"
        )
        raise WorkflowError(
            "WORKFLOW_PATH_DUPLICATE",
            diagnostics=[located("WORKFLOW_PATH_DUPLICATE", relative, detail)],
        )

    @staticmethod
    def resolve(root: Path, relative: str, *, must_exist: bool = True) -> Path:
        """Join a validated path to the root, refusing a reparse-point escape.

        Every component between the root and the target is inspected, not only
        the target: a junction on an ancestor directory redirects the whole
        subtree, so checking the leaf alone would be insufficient.
        """
        candidate = root
        for segment in relative.split("/"):
            candidate = candidate / segment
            if is_reparse_point(candidate):
                raise _path_error("WORKFLOW_PATH_LINK_ESCAPE", relative)
        try:
            resolved = candidate.resolve(strict=must_exist)
        except OSError:
            raise _path_error("WORKFLOW_FILE_MISSING", relative) from None
        if not resolved.is_relative_to(root):
            raise _path_error("WORKFLOW_PATH_LINK_ESCAPE", relative)
        if must_exist and not resolved.is_file():
            raise _path_error("WORKFLOW_FILE_MISSING", relative)
        return resolved


def _path_error(code: str, relative: str) -> WorkflowError:
    """Locate a rejection without leaking or echoing a host path.

    The pointer is always one of the fixed bundle-relative files: the submitted
    text is untrusted, so it is truncated into the detail (where it is plainly a
    quoted value) rather than used to build a location.
    """
    return WorkflowError(
        code,
        diagnostics=[
            located(code, "workflow.yaml#/files", f"{code}: {_shown(relative)}")
        ],
    )


def _shown(relative: str) -> str:
    """A bounded, non-echoing rendering of a rejected path."""
    printable = "".join(
        character if character.isprintable() and character not in "\r\n\t" else "?"
        for character in relative
    )
    return printable[:64]
