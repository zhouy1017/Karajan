"""Actual local canaries never imply a model runtime's dispatch qualification."""

from .probe import run_probe
from .qualification import require_qualified

__all__ = ["run_probe", "require_qualified"]
