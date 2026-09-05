"""Trusted local project and configuration registration; never model dispatch."""

from .registry import ProjectError, ProjectRegistry

__all__ = ["ProjectError", "ProjectRegistry"]
