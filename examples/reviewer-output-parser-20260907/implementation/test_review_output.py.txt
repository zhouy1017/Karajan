"""The public model-text boundary; no source qualification or review authorization."""

import builtins
import json
import socket
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest
from karajan.candidates import CandidateStore
from karajan.candidates.models import ReviewResult
from karajan.candidates.review_output import ReviewOutputError, parse_review_output
from pydantic import ValidationError
from test_validation import case as case
from test_validation import check_record, review_record


@pytest.mark.parametrize(
    ("wire", "stored"),
    [("pass", "passed"), ("changes_requested", "failed"), ("inconclusive", "inconclusive")],
)
def test_maps_only_review_content(wire: str, stored: str) -> None:
    result = parse_review_output(
        '{"verdict":"' + wire + '","findings":[]}',
        allowed_files=frozenset(),
        allowed_acceptance_refs=frozenset(),
    )
    assert result.model_dump() == {"verdict": stored, "findings": []}


FILES = frozenset({"src/export.py"})
ACCEPTANCE = frozenset({"acceptance:csv-v1"})


def finding(**changes: Any) -> dict[str, Any]:
    return {
        "severity": "high",
        "file": "src/export.py",
        "line": 27,
        "behavior": "空结果没有输出批准的表头",
        "trigger": "查询没有匹配行时调用导出",
        "acceptance_ref": "acceptance:csv-v1",
        "blocking": True,
    } | changes


def wire(*findings: dict[str, Any], verdict: str = "changes_requested") -> str:
    return json.dumps({"verdict": verdict, "findings": list(findings)}, ensure_ascii=False)


def parse(content: Any, **scope: Any) -> Any:
    return parse_review_output(
        content,
        **({"allowed_files": FILES, "allowed_acceptance_refs": ACCEPTANCE} | scope),
    )


def rejected(content: Any, code: str, **scope: Any) -> None:
    with pytest.raises(ReviewOutputError) as caught:
        parse(content, **scope)
    assert getattr(caught.value, "code", None) == "REVIEW_OUTPUT_" + code


def test_unicode_escapes_and_findings_order_are_preserved() -> None:
    first = finding(behavior='\t文字 { [ \\" quoted\n😺 e\u0301', blocking=False)
    second = finding(severity="low", behavior="  第二条\r\n保留  ", blocking=False)
    text = " \r\n\t" + wire(first, second, verdict="pass") + "\n "
    result = parse(text.encode("utf-8"))
    assert result.model_dump() == {"verdict": "passed", "findings": [first, second]}


@pytest.mark.parametrize(
    "content",
    [None, {}, [], 1, True, bytearray(b"{}"), b"\xff", "\ud800", '"\\udfff"'],
)
def test_rejects_invalid_input_without_interpreter_errors(content: Any) -> None:
    rejected(content, "INPUT_INVALID")


@pytest.mark.parametrize(
    "content",
    [
        "",
        " ",
        "{",
        "{}{}",
        "```json\n{}\n```",
        "prefix {}",
        "{} suffix",
        "\ufeff{}",
        '{"verdict":"pass","verdict":"changes_requested","findings":[]}',
        '{"verdict":"pass","ver\\u0064ict":"pass","findings":[]}',
        '{"findings":[{"blocking":true,"blocking":false}],"verdict":"pass"}',
        '{"findings":[{"blocking":true,"block\\u0069ng":false}],"verdict":"pass"}',
        '{"verdict":"pass","findings":[],}',
        '{"verdict":"pass" "findings":[]}',
        '{"verdict":"pass","findings":[]}\x00',
        '{"verdict":"pass","findings":[],"extra":NaN}',
        '{"verdict":"pass","findings":[],"extra":Infinity}',
        '{"verdict":"pass","findings":[],"extra":-Infinity}',
        '{"verdict":"pass","findings":[],"extra":1e999}',
        '{"verdict":"pass","findings":[],"extra":-1e999}',
        '{"verdict":"pass","findings":[],"extra":' + "9" * 5000 + "}",
    ],
)
def test_rejects_ambiguous_or_incomplete_json(content: str) -> None:
    rejected(content, "JSON_INVALID")


@pytest.mark.parametrize(
    "content", ["[[[[]]]]", "[" * 12000 + "]" * 12000], ids=["depth-four", "extreme"]
)
def test_rejects_excessive_depth_before_recursive_decoding(content: str) -> None:
    rejected(content, "LIMIT_EXCEEDED")


@pytest.mark.parametrize(
    "content",
    [
        '{"verdict":"pass","findings":[],"\\ud800":"x"}',
        '{"verdict":"pass","findings":[{"behavior":"\\ud800"}]}',
    ],
)
def test_decoded_lone_surrogates_are_rejected_before_schema(content: str) -> None:
    rejected(content, "INPUT_INVALID")


def test_valid_surrogate_pair_and_escaped_json_characters_are_preserved() -> None:
    item = finding(behavior='😺 "quoted" \\backslash\t{[[]]}')
    escaped = json.dumps({"verdict": "changes_requested", "findings": [item]}, ensure_ascii=True)
    assert "\\ud83d\\ude3a" in escaped
    assert parse(escaped).model_dump() == {"verdict": "failed", "findings": [item]}


def test_input_subclasses_cannot_supply_custom_conversion() -> None:
    class CustomText(str):
        def encode(self, *args: Any, **kwargs: Any) -> bytes:
            raise AssertionError("must not invoke custom conversion")

    class CustomBytes(bytes):
        def decode(self, *args: Any, **kwargs: Any) -> str:
            raise AssertionError("must not invoke custom conversion")

    rejected(CustomText(wire()), "INPUT_INVALID")
    rejected(CustomBytes(wire().encode()), "INPUT_INVALID")


@pytest.mark.parametrize(
    "document",
    [
        None,
        [],
        True,
        1,
        "text",
        {},
        {"verdict": "pass"},
        {"findings": []},
        {"verdict": "pass", "findings": [], "summary": "x"},
        {"verdict": "pass", "findings": None},
        {"verdict": "pass", "findings": {}},
        {"verdict": "pass", "findings": "[]"},
        {"verdict": None, "findings": []},
        {"verdict": {}, "findings": []},
        {"verdict": 1, "findings": []},
        {"verdict": "pass", "findings": [None]},
        {"verdict": "pass", "findings": [[]]},
        {"verdict": "pass", "findings": [1]},
    ],
)
def test_requires_exact_top_and_finding_shapes(document: Any) -> None:
    rejected(json.dumps(document), "SCHEMA_INVALID")


@pytest.mark.parametrize("verdict", ["passed", "failed", "PASS", "unknown", " pass", ""])
def test_rejects_other_wire_verdicts(verdict: str) -> None:
    rejected(wire(verdict=verdict), "SCHEMA_INVALID")


@pytest.mark.parametrize(
    "field", ["severity", "file", "line", "behavior", "trigger", "acceptance_ref", "blocking"]
)
def test_every_finding_field_is_required(field: str) -> None:
    item = finding()
    del item[field]
    rejected(wire(item), "SCHEMA_INVALID")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("severity", "HIGH"),
        ("severity", "info"),
        ("severity", None),
        ("file", None),
        ("file", 1),
        ("acceptance_ref", False),
        ("line", True),
        ("line", False),
        ("line", 1.0),
        ("line", "1"),
        ("line", None),
        ("line", 0),
        ("line", -1),
        ("blocking", 0),
        ("blocking", 1),
        ("blocking", "false"),
        ("blocking", None),
        ("behavior", None),
        ("behavior", 0),
        ("behavior", ""),
        ("behavior", " \n\t\r"),
        ("trigger", None),
        ("trigger", False),
        ("trigger", ""),
        ("trigger", "\u2003"),
    ],
)
def test_finding_types_and_existing_enums_are_strict(field: str, value: Any) -> None:
    rejected(wire(finding(**{field: value})), "SCHEMA_INVALID")


@pytest.mark.parametrize("field", ["behavior", "trigger"])
@pytest.mark.parametrize("codepoint", [i for i in range(32) if i not in (9, 10, 13)])
def test_free_text_rejects_disallowed_c0_controls(field: str, codepoint: int) -> None:
    rejected(wire(finding(**{field: "x" + chr(codepoint)})), "SCHEMA_INVALID")


@pytest.mark.parametrize("field", ["behavior", "trigger"])
def test_description_limits_count_decoded_codepoints(field: str) -> None:
    item = finding(**{field: "😺" * 2048})
    assert parse(wire(item)).findings[0].model_dump() == item
    rejected(wire(finding(**{field: "😺" * 2049})), "LIMIT_EXCEEDED")


def test_line_has_a_bounded_strict_integer_representation() -> None:
    assert parse(wire(finding(line=2_147_483_647))).findings[0].line == 2_147_483_647
    rejected(wire(finding(line=2_147_483_648)), "LIMIT_EXCEEDED")


def test_findings_limit_rejects_whole_response() -> None:
    items = [finding(behavior=f"Problem {index}") for index in range(32)]
    assert [f.behavior for f in parse(wire(*items)).findings] == [
        f"Problem {index}" for index in range(32)
    ]
    rejected(wire(*items, finding()), "LIMIT_EXCEEDED")


@pytest.mark.parametrize("as_bytes", [False, True])
def test_total_limit_measures_utf8_bytes_including_json_whitespace(as_bytes: bool) -> None:
    content = wire(finding(behavior="中文😺"))
    content += " " * (65_536 - len(content.encode("utf-8")))
    assert len(content) < 65_536
    assert len(content.encode("utf-8")) == 65_536
    assert parse(content.encode("utf-8") if as_bytes else content).verdict == "failed"
    oversized = content + " "
    rejected(oversized.encode("utf-8") if as_bytes else oversized, "LIMIT_EXCEEDED")


@pytest.mark.parametrize(("field", "limit"), [("file", 4096), ("acceptance_ref", 256)])
def test_reference_length_limits_precede_identifier_schema(field: str, limit: int) -> None:
    item = finding(**{field: "a" * limit})
    scope = {
        "allowed_files" if field == "file" else "allowed_acceptance_refs": frozenset({"a" * limit})
    }
    assert parse(wire(item), **scope).findings[0].model_dump() == item
    rejected(wire(finding(**{field: "a" * (limit + 1)})), "LIMIT_EXCEEDED")


@pytest.mark.parametrize("position", [0, 15, 31])
def test_pass_cannot_hide_a_blocker_at_any_position(position: int) -> None:
    items = [finding(blocking=False) for _ in range(32)]
    items[position]["blocking"] = True
    rejected(wire(*items, verdict="pass"), "VERDICT_CONFLICT")


@pytest.mark.parametrize("severity", ["critical", "high", "medium", "low"])
def test_severity_does_not_override_explicit_nonblocking_or_inconclusive(severity: str) -> None:
    assert (
        parse(wire(finding(severity=severity, blocking=False), verdict="pass")).verdict == "passed"
    )
    assert parse(wire(finding(severity=severity), verdict="inconclusive")).verdict == "inconclusive"


def test_field_shapes_precede_limits_and_typed_limits_precede_reference_syntax() -> None:
    # Each pair separates the final spec's precedence; the overlong ID must not be
    # swallowed by Finding's existing Identifier validator.
    rejected(wire(finding(behavior="x" * 2049, line=True)), "SCHEMA_INVALID")
    rejected(wire(finding(acceptance_ref=" " * 257)), "LIMIT_EXCEEDED")
    rejected(wire(finding(file="../x", behavior="x" * 2049)), "LIMIT_EXCEEDED")
    rejected(wire(finding(acceptance_ref="has space")), "REFERENCE_DENIED")


def test_combined_errors_follow_the_documented_stages() -> None:
    malformed = [finding() for _ in range(33)]
    malformed[-1]["blocking"] = None
    rejected(wire(*malformed), "SCHEMA_INVALID")
    rejected(wire(finding(file="../x", severity="INVALID")), "REFERENCE_DENIED")
    rejected(wire(finding(file="not-allowed.py", severity="INVALID")), "SCHEMA_INVALID")
    rejected(wire(finding(file="not-allowed.py"), verdict="pass"), "REFERENCE_DENIED")
    rejected('{"verdict":"pass","findings":[]}' + " " * 65_536, "LIMIT_EXCEEDED")


@pytest.mark.parametrize(
    "path",
    [
        "src/",
        "src",
        "src/other.py",
        "SRC/export.py",
        "src/*.py",
        "../export.py",
        "/src/export.py",
        "C:/src/export.py",
        "src\\export.py",
        "src/../export.py",
        "src/.git/config",
        "src/.GIT/config",
        "src//export.py",
        "./src/export.py",
        "src/export.py ",
        "src/export.py.",
        "src/%65xport.py",
        "",
    ],
)
def test_model_file_must_be_legal_and_exactly_allowed(path: str) -> None:
    rejected(wire(finding(file=path)), "REFERENCE_DENIED")


@pytest.mark.parametrize(
    "ref", ["", "ACCEPTANCE:csv-v1", "acceptance:csv", "x*", "has space", "x\n"]
)
def test_model_acceptance_ref_is_exact_and_not_cleaned(ref: str) -> None:
    rejected(wire(finding(acceptance_ref=ref)), "REFERENCE_DENIED")


@pytest.mark.parametrize("scope_name", ["allowed_files", "allowed_acceptance_refs"])
def test_empty_scope_is_deny_all_but_allows_zero_findings(scope_name: str) -> None:
    scope = {scope_name: frozenset()}
    assert parse(wire(), **scope).model_dump() == {"verdict": "failed", "findings": []}
    rejected(wire(finding()), "REFERENCE_DENIED", **scope)


@pytest.mark.parametrize("scope_name", ["allowed_files", "allowed_acceptance_refs"])
@pytest.mark.parametrize(
    "value",
    [None, "src/export.py", [], set(), {}, frozenset({1}), frozenset({b"x"}), frozenset({None})],
)
def test_controller_scopes_require_exact_frozensets_of_strings(scope_name: str, value: Any) -> None:
    rejected(wire(), "SCOPE_INVALID", **{scope_name: value})


@pytest.mark.parametrize(
    ("scope_name", "values"),
    [
        ("allowed_files", {""}),
        ("allowed_files", {".."}),
        ("allowed_files", {"src/"}),
        ("allowed_files", {"*.py"}),
        ("allowed_files", {".git/x"}),
        ("allowed_files", {"/tmp/x"}),
        ("allowed_files", {"x" * 4097}),
        ("allowed_files", {"A.py", "a.py"}),
        ("allowed_files", {"ß.py", "ss.py"}),
        ("allowed_files", {"\ud800"}),
        ("allowed_acceptance_refs", {""}),
        ("allowed_acceptance_refs", {"has space"}),
        ("allowed_acceptance_refs", {"x" * 257}),
        ("allowed_acceptance_refs", {"\ud800"}),
    ],
    ids=[
        "empty-file",
        "parent",
        "directory",
        "glob",
        "git",
        "absolute",
        "long-file",
        "case-collision",
        "unicode-case-collision",
        "file-surrogate",
        "empty-ref",
        "space-ref",
        "long-ref",
        "ref-surrogate",
    ],
)
def test_invalid_controller_reference_is_always_scope_error(
    scope_name: str, values: set[str]
) -> None:
    rejected(wire(), "SCOPE_INVALID", **{scope_name: frozenset(values)})


def test_scope_is_checked_before_content_and_not_unicode_normalized() -> None:
    rejected(b"\xff", "SCOPE_INVALID", allowed_files=frozenset({"../x"}))
    item = finding(file="e\u0301.py", acceptance_ref="é")
    scopes = {
        "allowed_files": frozenset({"e\u0301.py"}),
        "allowed_acceptance_refs": frozenset({"é"}),
    }
    assert parse(wire(item), **scopes).findings[0].file == "e\u0301.py"
    rejected(wire(item | {"file": "é.py"}), "REFERENCE_DENIED", **scopes)
    rejected(wire(item | {"acceptance_ref": "e\u0301"}), "REFERENCE_DENIED", **scopes)


@pytest.mark.parametrize(
    "trusted_field",
    [
        "actor",
        "candidate_id",
        "evidence_key",
        "check_evidence_ids",
        "qualification",
        "provenance",
        "author_reasoning_included",
        "completed",
        "source_sha256",
        "input_sha256",
        "policy_sha256",
        "permissions",
        "allowed_files",
        "allowed_acceptance_refs",
        "schema_version",
        "summary",
        "passed_gate",
        "role",
    ],
)
@pytest.mark.parametrize("at_finding", [False, True])
def test_model_cannot_add_authority_or_unknown_fields(trusted_field: str, at_finding: bool) -> None:
    document = {"verdict": "changes_requested", "findings": [finding()]}
    target = document["findings"][0] if at_finding else document
    target[trusted_field] = "MODEL-CANNOT-GRANT-AUTHORITY"
    rejected(json.dumps(document), "SCHEMA_INVALID")


def test_all_failure_categories_are_safe_and_silent(
    capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    canary = "SYNTHETIC-PRIVATE-REVIEW-CANARY"
    cases = [
        (wire(), {"allowed_files": frozenset({"../" + canary})}, "SCOPE_INVALID"),
        ('"' + canary + '\\ud800"', {}, "INPUT_INVALID"),
        (wire(finding(behavior=canary + "x" * 2048)), {}, "LIMIT_EXCEEDED"),
        ('{"' + canary + '":1,"' + canary + '":2}', {}, "JSON_INVALID"),
        (wire(finding(severity=canary)), {}, "SCHEMA_INVALID"),
        (wire(finding(file=canary)), {}, "REFERENCE_DENIED"),
        (wire(finding(behavior=canary), verdict="pass"), {}, "VERDICT_CONFLICT"),
        (
            json.dumps({"verdict": "pass", "findings": [finding(**{canary: "x"})]}),
            {},
            "SCHEMA_INVALID",
        ),
        (
            json.dumps({"verdict": "pass", "findings": [finding(behavior={canary: []})]}),
            {},
            "LIMIT_EXCEEDED",
        ),
    ]
    for content, scope, code in cases:
        with pytest.raises(ReviewOutputError) as caught:
            parse(content, **scope)
        error = caught.value
        assert isinstance(error, ValueError)
        assert error.code == "REVIEW_OUTPUT_" + code
        assert error.args == (error.code,)
        assert str(error) == error.code
        assert canary not in repr(error)
    assert capsys.readouterr() == ("", "")
    assert caplog.records == []


def test_parsing_is_pure_fresh_and_does_not_require_a_real_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Parser crossed a forbidden effect boundary")

    content = wire(finding())
    with monkeypatch.context() as guard:
        for target, name in [
            (builtins, "open"),
            (Path, "open"),
            (Path, "stat"),
            (Path, "mkdir"),
            (socket, "socket"),
            (sqlite3, "connect"),
            (time, "time"),
            (time, "monotonic"),
            (CandidateStore, "__init__"),
        ]:
            guard.setattr(target, name, forbidden)
        first = parse(content)
        second = parse(content)
        assert first.model_dump() == second.model_dump()
        first.findings[0].behavior = "caller mutation"
        first.findings.clear()
        assert second.model_dump() == {"verdict": "failed", "findings": [finding()]}
        assert parse(content).model_dump() == second.model_dump()
    assert content == wire(finding())
    assert FILES == frozenset({"src/export.py"})
    assert ACCEPTANCE == frozenset({"acceptance:csv-v1"})


@pytest.mark.parametrize(
    ("wire_verdict", "stored"),
    [("pass", "passed"), ("changes_requested", "failed"), ("inconclusive", "inconclusive")],
)
def test_content_requires_independently_supplied_trusted_storage_fields(
    case: dict[str, Any], wire_verdict: str, stored: str
) -> None:
    # Existing synthetic controller fixture, real temporary Git/CAS/ledger. This
    # assembly checks the old storage protocol; it is not a production Reviewer.
    candidate = case["store"].freeze(case["workspace"], case["request"])
    check = case["store"].record_check(check_record(candidate), log=b"fixture check")
    trusted = review_record(candidate, [check])
    original = ReviewResult.model_validate(trusted).model_dump_json()
    content = parse(wire(verdict=wire_verdict)).model_dump()
    with pytest.raises(ValidationError):
        ReviewResult.model_validate(content)
    assembled = trusted | content
    result = ReviewResult.model_validate(assembled)
    assert result.verdict == stored
    assert result.model_dump() == assembled
    assert ReviewResult.model_validate(trusted).model_dump_json() == original
    assert case["store"].record_review(assembled, log=b"fixture review")["status"] == stored
