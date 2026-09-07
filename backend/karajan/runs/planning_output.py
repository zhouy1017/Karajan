"""Strict parsing of a model's JSON planning proposal.

This module is deliberately a pure content boundary.  It does not attach an
admission, inspect a Run, or persist a plan.  The controller supplies the
version because that value is part of the trusted execution binding.
"""

import json
import math
from typing import Any, Literal, overload

from pydantic import ValidationError

from .models import Plan
from .routing_authorization import PlanV2

MAX_OUTPUT_BYTES = 262_144
MAX_DEPTH = 16
PlanningOutputVersion = Literal["v1", "v2"]


class PlanningOutputError(ValueError):
    """A stable, content-free rejection from the planning output boundary."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _depth(text: str) -> None:
    depth = 0
    quoted = False
    escaped = False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > MAX_DEPTH:
                raise PlanningOutputError("PLANNING_OUTPUT_LIMIT_EXCEEDED")
        elif char in "]}":
            # Mismatched delimiters remain the JSON decoder's responsibility.
            depth = max(0, depth - 1)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("non-finite JSON number")
    return parsed


def _reject_surrogates(value: Any) -> None:
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, str):
            try:
                current.encode("utf-8", errors="strict")
            except UnicodeError:
                raise PlanningOutputError("PLANNING_OUTPUT_INPUT_INVALID") from None
        elif isinstance(current, dict):
            pending.extend(current.keys())
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)


def _decode(content: str | bytes) -> Any:
    if type(content) is str:
        try:
            encoded = content.encode("utf-8", errors="strict")
        except UnicodeError:
            raise PlanningOutputError("PLANNING_OUTPUT_INPUT_INVALID") from None
        text = content
    elif type(content) is bytes:
        try:
            text = content.decode("utf-8", errors="strict")
        except UnicodeError:
            raise PlanningOutputError("PLANNING_OUTPUT_INPUT_INVALID") from None
        encoded = content
    else:
        raise PlanningOutputError("PLANNING_OUTPUT_INPUT_INVALID")

    if len(encoded) > MAX_OUTPUT_BYTES:
        raise PlanningOutputError("PLANNING_OUTPUT_LIMIT_EXCEEDED")
    _depth(text)
    try:
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        )
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise PlanningOutputError("PLANNING_OUTPUT_JSON_INVALID") from None
    _reject_surrogates(value)
    return value


@overload
def parse_planning_output(content: str | bytes, *, version: Literal["v1"]) -> Plan: ...


@overload
def parse_planning_output(content: str | bytes, *, version: Literal["v2"]) -> PlanV2: ...


def parse_planning_output(
    content: str | bytes, *, version: PlanningOutputVersion
) -> Plan | PlanV2:
    """Parse one complete, untrusted model output into the selected Plan model.

    The returned model contains only the plan proposal.  A successful parse
    does not establish source identity, admission, authorization, or owner
    approval; those checks remain in the planning and execution layers.
    """

    if type(version) is not str or version not in ("v1", "v2"):
        raise PlanningOutputError("PLANNING_OUTPUT_VERSION_INVALID")
    value = _decode(content)
    if not isinstance(value, dict):
        raise PlanningOutputError("PLANNING_OUTPUT_SCHEMA_INVALID")
    model = PlanV2 if version == "v2" else Plan
    try:
        return model.model_validate(value)
    except ValidationError:
        raise PlanningOutputError("PLANNING_OUTPUT_SCHEMA_INVALID") from None


__all__ = [
    "MAX_DEPTH",
    "MAX_OUTPUT_BYTES",
    "PlanningOutputError",
    "PlanningOutputVersion",
    "parse_planning_output",
]
