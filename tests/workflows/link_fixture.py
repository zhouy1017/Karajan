"""Make the portable directory-link helper importable from these cases."""

import sys
from pathlib import Path

_HELPERS = Path(__file__).resolve().parents[1] / "web"
if str(_HELPERS) not in sys.path:
    sys.path.insert(0, str(_HELPERS))

from directory_link import create_directory_link, remove_directory_link  # noqa: E402

__all__ = ["create_directory_link", "remove_directory_link"]
