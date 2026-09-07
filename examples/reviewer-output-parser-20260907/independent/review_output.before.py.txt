"""Parse untrusted review content, without granting authority or recording Evidence."""

import json
import math
from typing import Any, Literal

from pydantic import TypeAdapter, ValidationError

from .models import Contract, Finding, Identifier
from .store import CandidateError, relative_path

PARSER_REVISION = "karajan.review-output-parser.v1"
MAX_OUTPUT_BYTES = 65_536
MAX_DEPTH = 3
MAX_FINDINGS = 32
MAX_DESCRIPTION = 2048
MAX_LINE = 2_147_483_647

_IDENTIFIER = TypeAdapter(Identifier)
# Preflight types establish error precedence; Finding remains the field/enum contract.
_FINDING_TYPES = {
    "severity": str,
    "file": str,
    "line": int,
    "behavior": str,
    "trigger": str,
    "acceptance_ref": str,
    "blocking": bool,
}


class ReviewOutputError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ParsedReviewOutput(Contract):
    verdict: Literal["passed", "failed", "inconclusive"]
    findings: list[Finding]


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
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > MAX_DEPTH:
                raise ReviewOutputError("REVIEW_OUTPUT_LIMIT_EXCEEDED")
        elif char in "]}":
            # Malformed delimiters are left for the JSON decoder to reject.
            depth = max(0, depth - 1)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError
    return parsed


def _reject_constant(value: str) -> None:
    raise ValueError


def _decode(content: str | bytes) -> Any:
    try:
        if type(content) is str:
            encoded = content.encode("utf-8", errors="strict")
            text = content
        elif type(content) is bytes:
            text = content.decode("utf-8", errors="strict")
            encoded = content
        else:
            raise ReviewOutputError("REVIEW_OUTPUT_INPUT_INVALID")
    except UnicodeError:
        raise ReviewOutputError("REVIEW_OUTPUT_INPUT_INVALID") from None
    if len(encoded) > MAX_OUTPUT_BYTES:
        raise ReviewOutputError("REVIEW_OUTPUT_LIMIT_EXCEEDED")
    _depth(text)
    try:
        result = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_float=_finite_float,
            parse_constant=_reject_constant,
        )
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise ReviewOutputError("REVIEW_OUTPUT_JSON_INVALID") from None
    pending = [result]
    while pending:
        value = pending.pop()
        if isinstance(value, str):
            try:
                value.encode("utf-8", errors="strict")
            except UnicodeError:
                raise ReviewOutputError("REVIEW_OUTPUT_INPUT_INVALID") from None
        elif isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    return result


def _reference_syntax(file: str, acceptance_ref: str) -> None:
    try:
        relative_path(file)
        _IDENTIFIER.validate_python(acceptance_ref)
    except (CandidateError, ValidationError):
        raise ReviewOutputError("REVIEW_OUTPUT_REFERENCE_DENIED") from None


def _scope(allowed_files: frozenset[str], allowed_acceptance_refs: frozenset[str]) -> None:
    if type(allowed_files) is not frozenset or type(allowed_acceptance_refs) is not frozenset:
        raise ReviewOutputError("REVIEW_OUTPUT_SCOPE_INVALID")
    try:
        for values in (allowed_files, allowed_acceptance_refs):
            for value in values:
                if type(value) is not str:
                    raise ReviewOutputError("REVIEW_OUTPUT_SCOPE_INVALID")
                value.encode("utf-8", errors="strict")
        for file in allowed_files:
            relative_path(file)
        for acceptance_ref in allowed_acceptance_refs:
            _IDENTIFIER.validate_python(acceptance_ref)
    except (UnicodeError, CandidateError, ValidationError):
        raise ReviewOutputError("REVIEW_OUTPUT_SCOPE_INVALID") from None
    if len({file.casefold() for file in allowed_files}) != len(allowed_files):
        raise ReviewOutputError("REVIEW_OUTPUT_SCOPE_INVALID")


def _findings(document: Any) -> list[Finding]:
    if (
        type(document) is not dict
        or document.keys() != {"verdict", "findings"}
        or type(document["verdict"]) is not str
        or type(document["findings"]) is not list
    ):
        raise ReviewOutputError("REVIEW_OUTPUT_SCHEMA_INVALID")
    items = document["findings"]
    for item in items:
        if (
            type(item) is not dict
            or item.keys() != Finding.model_fields.keys()
            or any(type(item[key]) is not expected for key, expected in _FINDING_TYPES.items())
        ):
            raise ReviewOutputError("REVIEW_OUTPUT_SCHEMA_INVALID")
    if len(items) > MAX_FINDINGS or any(
        len(item["behavior"]) > MAX_DESCRIPTION
        or len(item["trigger"]) > MAX_DESCRIPTION
        or len(item["file"]) > 4096
        or len(item["acceptance_ref"]) > 256
        or item["line"] > MAX_LINE
        for item in items
    ):
        raise ReviewOutputError("REVIEW_OUTPUT_LIMIT_EXCEEDED")
    for item in items:
        _reference_syntax(item["file"], item["acceptance_ref"])
    try:
        findings = [Finding.model_validate(item) for item in items]
    except ValidationError:
        raise ReviewOutputError("REVIEW_OUTPUT_SCHEMA_INVALID") from None
    for item in findings:
        for description in (item.behavior, item.trigger):
            if not description.strip() or any(
                ord(char) < 32 and char not in "\n\r\t" for char in description
            ):
                raise ReviewOutputError("REVIEW_OUTPUT_SCHEMA_INVALID")
    if document["verdict"] not in {"pass", "changes_requested", "inconclusive"}:
        raise ReviewOutputError("REVIEW_OUTPUT_SCHEMA_INVALID")
    return findings


def parse_review_output(
    content: str | bytes,
    *,
    allowed_files: frozenset[str],
    allowed_acceptance_refs: frozenset[str],
) -> ParsedReviewOutput:
    """Validate one completed-message text selected by a trusted observer.

    This function cannot attest completion, provenance, or Review permission.
    Empty controller scopes deny every reference, rather than disabling checks.
    """
    _scope(allowed_files, allowed_acceptance_refs)
    document = _decode(content)
    findings = _findings(document)
    if any(
        item.file not in allowed_files or item.acceptance_ref not in allowed_acceptance_refs
        for item in findings
    ):
        raise ReviewOutputError("REVIEW_OUTPUT_REFERENCE_DENIED")
    if document["verdict"] == "pass" and any(item.blocking for item in findings):
        raise ReviewOutputError("REVIEW_OUTPUT_VERDICT_CONFLICT")
    return ParsedReviewOutput.model_validate(
        {
            "verdict": {
                "pass": "passed",
                "changes_requested": "failed",
                "inconclusive": "inconclusive",
            }[document["verdict"]],
            "findings": findings,
        }
    )
