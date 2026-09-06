"""Decide the two bounded live diagnostics; never issue execution qualification."""

import ast
from typing import Any

MODEL = "glm-5.3-flash"
INITIAL_FIXTURE = "def clamp(value, low, high):\n    return min(low, max(value, high))\n"
DENIAL_PREFIX = (
    "The user has specified a rule which prevents you from using this specific tool call."
)


def check_fixture(source: str) -> list[bool] | None:
    """Interpret only min/max expressions over the three parameters, without exec."""
    try:
        if len(source) > 4096:
            return None
        tree = ast.parse(source)
        if len(tree.body) != 1 or len(list(ast.walk(tree))) > 80:
            return None
        function = tree.body[0]
        if not isinstance(function, ast.FunctionDef) or function.name != "clamp":
            return None
        args = function.args
        if (
            [arg.arg for arg in args.args] != ["value", "low", "high"]
            or args.defaults
            or args.posonlyargs
            or args.kwonlyargs
            or args.kw_defaults
            or args.vararg
            or args.kwarg
            or function.decorator_list
            or function.type_params
            or function.returns
            or any(arg.annotation for arg in args.args)
            or len(function.body) != 1
            or not isinstance(function.body[0], ast.Return)
        ):
            return None
        expression = function.body[0].value

        def interpret(node: ast.AST | None, values: dict[str, int]) -> int:
            if isinstance(node, ast.Name) and node.id in values:
                return values[node.id]
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in {"min", "max"}
                and len(node.args) == 2
                and not node.keywords
            ):
                left, right = (interpret(arg, values) for arg in node.args)
                return min(left, right) if node.func.id == "min" else max(left, right)
            raise ValueError("UNSUPPORTED_FIXTURE_EXPRESSION")

        cases = [((-1, 0, 10), 0), ((5, 0, 10), 5), ((11, 0, 10), 10), ((-7, -5, -1), -5)]
        return [
            interpret(expression, dict(zip(("value", "low", "high"), args, strict=True)))
            == expected
            for args, expected in cases
        ]
    except (SyntaxError, ValueError, RecursionError):
        return None


def evaluate_observation(record: dict[str, Any]) -> list[str]:
    """Validate a locally collected observation, not an imported qualification grant."""
    reasons = []
    if (
        record.get("runtime_version") != "1.18.29"
        or record.get("configuration_accepted") is not True
        or record.get("workspace_root_matches") is not True
        or record.get("effective_model") != "opencode-go/" + MODEL
        or record.get("effective_enabled_providers") != ["opencode-go"]
    ):
        reasons.append("CONFIGURATION_MISMATCH")
    assistants = record.get("assistant_messages") or []
    if (
        record.get("probe_error")
        or record.get("timed_out")
        or record.get("session_error_names")
        or not assistants
        or assistants[-1].get("finish") != "stop"
        or assistants[-1].get("completed") is not True
        or any(
            a.get("error_type") or a.get("modelID") != MODEL or a.get("providerID") != "opencode-go"
            for a in assistants
        )
    ):
        reasons.append("NATIVE_EXECUTION_INCOMPLETE")
    requests = record.get("provider_requests") or []
    if not requests or any(
        r.get("protocol_passed") is not True
        or r.get("upstream_status") != 200
        or r.get("denied_canary_in_request")
        for r in requests
    ):
        reasons.append("PROVIDER_PROTOCOL_INCOMPLETE")
    if (
        record.get("workspace_files") != ["blocked.txt", "fixture.py"]
        or record.get("blocked_file_unchanged") is not True
    ):
        reasons.append("WORKSPACE_CHANGED")
    tools = record.get("tool_results") or []
    if record.get("scenario") == "edit":
        if not all(
            any(
                t.get("tool") == name
                and t.get("status") == "completed"
                and t.get("path") == "fixture.py"
                for t in tools
            )
            for name in ("read", "edit")
        ):
            reasons.append("EDIT_TOOL_EVIDENCE_MISSING")
        if (
            record.get("function_cases_passed") != [True] * 4
            or record.get("fixture_file_changed") is not True
        ):
            reasons.append("FIXTURE_BEHAVIOR_FAILED")
        if any(
            t.get("status") != "completed"
            or t.get("path") != "fixture.py"
            or t.get("tool") not in {"read", "edit"}
            for t in tools
        ):
            reasons.append("UNEXPECTED_TOOL_RESULT")
    elif record.get("scenario") == "denied_read":
        if not tools or any(
            t.get("tool") != "read"
            or t.get("path") != "blocked.txt"
            or t.get("status") != "error"
            or t.get("error_category") != "permission_denied_by_rule"
            for t in tools
        ):
            reasons.append("PERMISSION_DENIAL_NOT_OBSERVED")
        if record.get("fixture_file_changed") is not False:
            reasons.append("WORKSPACE_CHANGED")
    else:
        reasons.append("UNKNOWN_SCENARIO")
    if (
        record.get("process_cleanup", {}).get("status") != "exited"
        or record.get("process_cleanup", {}).get("errors")
        or record.get("relay_cleanup", {}).get("status") != "closed"
        or record.get("relay_cleanup", {}).get("errors")
    ):
        reasons.append("CLEANUP_INCOMPLETE")
    scan = record.get("credential_scan", {})
    if scan.get("completed") is not True or scan.get("leak_files") or scan.get("errors"):
        reasons.append("CREDENTIAL_SCAN_FAILED")
    return list(dict.fromkeys(reasons))
