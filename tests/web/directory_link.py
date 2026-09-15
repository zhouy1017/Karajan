"""Create and remove a real directory link portably, for the redirect tests.

The property under test is "a redirect planted above or inside a managed
directory must not be followed". That property is not Windows-specific, so the
tests are not skipped on Linux: on Windows a directory *junction* is used (the
form an attacker would realistically plant there, and the one ``Path.is_symlink``
alone does not report), and elsewhere a directory *symlink* is used.

Only a platform that genuinely cannot create a link is skipped, and it is skipped
with an explicit reason rather than silently passing.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

WINDOWS = sys.platform == "win32"


def create_directory_link(link: Path, target: Path) -> None:
    """Create a directory link at ``link`` pointing at ``target``.

    Skips the requesting test when this platform cannot create one, so the case
    is reported as skipped rather than as a false failure or a silent pass.
    """
    if WINDOWS:
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            check=False,
            capture_output=True,
        )
        if completed.returncode != 0:
            pytest.skip(
                "Directory junctions are unavailable here: "
                + completed.stderr.decode("utf-8", "replace").strip()[:120]
            )
        return
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"Directory symlinks are unavailable here: {error}")


def remove_directory_link(link: Path) -> None:
    """Remove the link itself, leaving whatever it pointed at untouched.

    A Windows junction is removed with ``rmdir`` (it is a directory entry); a
    POSIX symlink is removed with ``unlink``.
    """
    if WINDOWS:
        os.rmdir(link)
    else:
        link.unlink()
