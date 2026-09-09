"""Strict parsing of a model's JSON planning proposal.

This module is deliberately a pure content boundary.  It does not attach an
admission, inspect a Run, or persist a plan.  The controller supplies the
version because that value is part of the trusted execution binding.
"""

import hashlib
import json
import math
from typing import Any, Literal, overload

from pydantic import ValidationError

from .models import Plan
from .routing_authorization import PlanV2

MAX_OUTPUT_BYTES = 262_144
MAX_DEPTH = 16
PlanningOutputVersion = Literal["v1", "v2"]
_DIAGNOSTIC_SCHEMA = "karajan.planning-output-diagnostic.v1"
_DIAGNOSTIC_CATEGORIES = {
    "success",
    "json_syntax",
    "duplicate_key",
    "non_finite_number",
    "json_structure",
    "schema",
    "input",
    "limit",
    "unknown",
}
_DIAGNOSTIC_FINISHES = {"stop", "tool-calls", "unknown", "missing"}


class _DiagnosticDuplicateKey(ValueError):
    pass


class _DiagnosticNonFiniteNumber(ValueError):
    pass


class PlanningOutputError(ValueError):
    """A stable, content-free rejection from the planning output boundary."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _diagnostic_bytes(content: str | bytes) -> bytes:
    if type(content) is bytes:
        return content
    if type(content) is str:
        try:
            return content.encode("utf-8", errors="strict")
        except UnicodeError:
            return b""
    return b""


def _diagnostic_category(
    content: str | bytes, reason_code: str
) -> tuple[str, int | None, int | None]:
    if reason_code == "SUCCESS":
        return "success", None, None
    if reason_code == "PLANNING_OUTPUT_SCHEMA_INVALID":
        return "schema", None, None
    if reason_code == "PLANNING_OUTPUT_INPUT_INVALID":
        return "input", None, None
    if reason_code == "PLANNING_OUTPUT_LIMIT_EXCEEDED":
        return "limit", None, None
    if reason_code != "PLANNING_OUTPUT_JSON_INVALID":
        return "unknown", None, None
    if type(content) not in (str, bytes):
        return "input", None, None
    try:
        json.loads(
            content,
            object_pairs_hook=_diagnostic_pairs,
            parse_constant=_diagnostic_constant,
            parse_float=_diagnostic_float,
        )
    except _DiagnosticDuplicateKey:
        return "duplicate_key", None, None
    except _DiagnosticNonFiniteNumber:
        return "non_finite_number", None, None
    except json.JSONDecodeError as error:
        return "json_syntax", error.lineno, error.colno
    except (ValueError, TypeError, RecursionError, OverflowError):
        return "json_structure", None, None
    return "json_structure", None, None


def _diagnostic_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: set[str] = set()
    for key, _ in pairs:
        if key in seen:
            raise _DiagnosticDuplicateKey
        seen.add(key)
    return dict(pairs)


def _diagnostic_constant(value: str) -> None:
    raise _DiagnosticNonFiniteNumber


def _diagnostic_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise _DiagnosticNonFiniteNumber
    return parsed


def planning_output_diagnostic(
    content: str | bytes,
    *,
    reason_code: str,
    finish: str = "unknown",
    text_part_count: int = 0,
) -> dict[str, Any]:
    """Return bounded, content-free diagnostics for one parser boundary result."""

    encoded = _diagnostic_bytes(content)
    category, line, column = _diagnostic_category(content, reason_code)
    safe_finish = finish if finish in _DIAGNOSTIC_FINISHES else "unknown"
    safe_count = text_part_count if type(text_part_count) is int and text_part_count >= 0 else 0
    return {
        "schema_version": _DIAGNOSTIC_SCHEMA,
        "category": category if category in _DIAGNOSTIC_CATEGORIES else "unknown",
        "byte_length": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "finish": safe_finish,
        "text_part_count": safe_count,
        "text_part_shape": (
            "one_text"
            if safe_count == 1
            else "none"
            if safe_count == 0
            else "multiple"
        ),
        "decoder_line": line,
        "decoder_column": column,
    }


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
    "planning_output_diagnostic",
    "parse_planning_output",
]
