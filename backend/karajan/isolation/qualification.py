"""A scoped evidence check, explicitly incapable of enabling model runtime tools."""

import hashlib
import json
import re
from typing import Any

REQUIRED_CHECKS = frozenset(
    {
        "workspace_read_write",
        "protected_files",
        "symlink_escape",
        "host_proc",
        "environment",
        "inherited_fds",
        "capabilities",
        "wsl_interop",
        "network_endpoints",
        "git_remote",
        "process_cancel",
        "candidate_collection",
        "runtime_binding",
    }
)
BINDING_FIELDS = frozenset(
    {
        "attempt_id",
        "fence",
        "profile_id",
        "profile_revision",
        "profile_digest",
        "runtime_kind",
        "runtime_version",
        "execution_path",
    }
)


def validate_binding(binding: Any) -> None:
    if not isinstance(binding, dict) or set(binding) != BINDING_FIELDS:
        raise ValueError("INVALID_CANARY_BINDING")
    for key in ("attempt_id", "profile_id"):
        value = binding[key]
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 256
            or any(character.isspace() for character in value)
        ):
            raise ValueError("INVALID_CANARY_BINDING")
    for key in ("fence", "profile_revision"):
        if type(binding[key]) is not int or not 0 < binding[key] < 2**63:
            raise ValueError("INVALID_CANARY_BINDING")
    if (
        binding["runtime_kind"] != "python-canary"
        or binding["execution_path"] != "unshare-chroot-v1"
        or not isinstance(binding["profile_digest"], str)
        or re.fullmatch(r"[0-9a-f]{64}", binding["profile_digest"]) is None
        or not isinstance(binding["runtime_version"], str)
        or re.fullmatch(r"3\.12\.[0-9]+", binding["runtime_version"]) is None
    ):
        raise ValueError("INVALID_CANARY_BINDING")


def require_qualified(
    report: dict[str, Any],
    exact_binding: dict[str, Any],
    *,
    scope: str = "runtime_tools",
) -> dict[str, Any]:
    if scope != "fixed_python_canary":
        raise ValueError("RUNTIME_TOOLS_NOT_QUALIFIED")
    validate_binding(exact_binding)
    if report.get("binding") != exact_binding:
        raise ValueError("BINDING_MISMATCH")
    checks = report.get("checks", [])
    if (
        report.get("schema_version") != "karajan.isolation.report.v1"
        or report.get("qualification_scope") != scope
        or report.get("status") != "passed"
        or report.get("dispatch_eligible") is not False
        or report.get("runtime_tools_status") != "not_run"
        or len(checks) != len(REQUIRED_CHECKS)
        or {check.get("id") for check in checks} != REQUIRED_CHECKS
        or any(check.get("status") != "passed" or not check.get("evidence") for check in checks)
    ):
        raise ValueError("CANARY_EVIDENCE_INCOMPLETE")
    return {
        "scope": scope,
        "dispatch_eligible": False,
        "report_digest": hashlib.sha256(json.dumps(report, sort_keys=True).encode()).hexdigest(),
    }
