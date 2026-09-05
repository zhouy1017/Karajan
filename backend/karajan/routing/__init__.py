"""Deterministic simulation only; this module cannot authorize or execute a model."""

from .compiler import RoutingError, compile_rulebook
from .evaluator import evaluate_route
from .fixture import fixture_from_configuration

__all__ = ["RoutingError", "compile_rulebook", "evaluate_route", "fixture_from_configuration"]
