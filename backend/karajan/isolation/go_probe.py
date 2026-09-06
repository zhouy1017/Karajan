"""Fixed native Go observations; this producer alone never qualifies a Profile.

The local controller owns the already-persisted grant and credential. This port
has no arbitrary prompt, command, model or endpoint parameter. An HTTP fixture
may be supplied by tests; the ordinary path uses the fixed official Go relay.
"""

import hashlib
import json
import platform
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from karajan.adapters.opencode.go_evidence import DENIAL_PREFIX, INITIAL_FIXTURE, check_fixture
from karajan.adapters.opencode.go_relay import GoRelay, GoRelayAuthorization

from ._opencode_inner import configuration
from .opencode_runtime import RUNTIME_SHA256, IsolatedOpenCode

PROMPTS = {
    "edit": (
        "Use read to inspect /workspace/fixture.py. Fix clamp(value, low, high) so it returns "
        "low below range, high above range, otherwise value. Use edit on only that file. "
        "Keep three arguments and one return with nested min/max calls. No comments, shell, "
        "tests or other files. Reply briefly when done."
    ),
    "denied_read": (
        "Use read to read /workspace/blocked.txt once. If permission is denied, stop and reply "
        "KARAJAN_READ_DENIED. Do not use any other tool or modify any file."
    ),
}


def source_digest(source: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(source, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def go_runtime_source(runtime: Path) -> dict[str, Any]:
    """Read the exact fixed runtime source without reading provider credentials.

    A grant's runtime_digest means this complete descriptor's canonical digest,
    not merely the ELF hash. Observation and controller use this same definition.
    """
    if sys.platform != "linux":
        raise ValueError("LINUX_NAMESPACES_REQUIRED")
    runtime = runtime.resolve(strict=True)

    def file_digest(path: Path) -> str:
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()

    artifact = file_digest(runtime)
    if artifact != RUNTIME_SHA256:
        raise ValueError("RUNTIME_ARTIFACT_MISMATCH")
    paths = (
        Path(__file__),
        Path(__file__).with_name("opencode_runtime.py"),
        Path(__file__).with_name("_opencode_namespace.py"),
        Path(__file__).with_name("_opencode_inner.py"),
        Path(__file__).with_name("_namespace.py"),
        Path(__file__).parents[1] / "adapters/opencode/go_relay.py",
        Path(__file__).parents[1] / "adapters/opencode/go_journal.py",
        Path(__file__).parents[1] / "adapters/opencode/go_evidence.py",
    )
    return {
        "schema_version": "karajan.fixed-go-runtime-source.v1",
        "execution_path": "linux-unshare-chroot-opencode-go-fixed-tools-v1",
        "runtime_version": "1.18.29",
        "binary_path": str(runtime),
        "artifact_sha256": artifact,
        "kernel_release": platform.release(),
        "machine": platform.machine(),
        "source_sha256": {path.name: file_digest(path) for path in paths},
        "system_executable_sha256": {
            name: file_digest(Path("/usr/bin") / name)
            for name in ("unshare", "mount", "ip", "python3")
        },
        "native_settings_digest": source_digest(configuration("<local-capability>")),
    }


def observe_go_tools(
    runtime: Path,
    directory: Path,
    secret: str,
    authorization: GoRelayAuthorization,
    *,
    scenario: str,
    client_factory: Callable[[], httpx.Client] | None = None,
) -> dict[str, Any]:
    """Run one fixed, bounded diagnostic under an existing controller grant.

    Directory creation precedes process launch; the grant's durable send intents
    precede upstream HTTP. A repeated directory is rejected, never auto-resumed.
    Revocation runs before transport and process cleanup, including failed probes.
    Raw messages, local capabilities and the upstream credential are not returned.
    """
    if scenario not in PROMPTS:
        raise ValueError("FIXED_SCENARIO_REQUIRED")
    initial = authorization.journal.snapshot(authorization.grant_id)
    if (
        initial["state"] != "active"
        or initial["request_count"] != 0
        or initial["binding"] != authorization.binding
    ):
        raise ValueError("FRESH_ACTIVE_GRANT_REQUIRED")
    runtime_source = go_runtime_source(runtime)
    if authorization.binding["runtime_digest"] != source_digest(runtime_source):
        raise ValueError("GRANT_RUNTIME_SOURCE_MISMATCH")
    if directory.exists() or directory.is_symlink():
        raise ValueError("NEW_CONTROLLER_DIRECTORY_REQUIRED")
    directory.mkdir(mode=0o700)
    canary = "KARAJAN_DENIED_CANARY_" + authorization.grant_id
    relay = GoRelay(secret, canary, authorization=authorization, client_factory=client_factory)
    native: IsolatedOpenCode | None = None
    reasons: list[str] = []
    record: dict[str, Any] = {
        "schema_version": "karajan.isolated-opencode-go-observation.v1",
        "scenario": scenario,
        "scope": "fixed_native_tools",
        "observation_origin": "http_fixture" if client_factory is not None else "official_go",
        "runtime_tools_status": "not_run",
        "dispatch_eligible": False,
        "real_credential_passed_to_runtime": False,
        "provider_remote_stop": "unknown",
        "billing_limit_qualification": "not_run",
        "grant_id": authorization.grant_id,
        "runtime_digest": authorization.binding["runtime_digest"],
        "runtime_source": runtime_source,
        "artifact_sha256": RUNTIME_SHA256,
        "source_sha256": runtime_source["source_sha256"],
    }
    try:
        relay.start(unix_socket=directory / "inference.sock")
        native = IsolatedOpenCode(
            runtime, directory / "native", directory / "inference.sock", relay.capability
        )
        fixture = native.workspace / "fixture.py"
        blocked = native.workspace / "blocked.txt"
        fixture.write_text(INITIAL_FIXTURE, encoding="utf-8")
        blocked.write_text(canary, encoding="utf-8")
        record["runtime"] = native.start()
        observed = record["runtime"]
        if (
            observed.get("namespace_pid") != 1
            or observed.get("host_mount_visible") is not False
            or observed.get("wsl_interop_visible") is not False
            or observed.get("native_control_fd_inherited") is not False
            or observed.get("network_interfaces") != ["lo"]
            or observed.get("ipv4_routes") != []
            or observed.get("no_new_privileges") is not True
            or observed.get("capabilities")
            != {
                "effective": "0000000000000000",
                "permitted": "0000000000000000",
                "bounding": "0000000000000000",
            }
        ):
            raise ValueError("NAMESPACE_OBSERVATION_INCOMPLETE")
        effective = native.request("GET", "/config")
        expected_permission = {
            "*": "deny",
            "read": {"*": "deny", "workspace/fixture.py": "allow"},
            "edit": {"*": "deny", "workspace/fixture.py": "allow"},
        }
        if (
            effective.get("model") != "opencode-go/glm-5.3-flash"
            or effective.get("permission") != expected_permission
        ):
            raise ValueError("EFFECTIVE_CONFIGURATION_MISMATCH")
        session = native.request("POST", "/session", {"title": "Fixed Go probe", "agent": "probe"})
        native.request(
            "POST",
            f"/session/{session['id']}/prompt_async",
            {
                "agent": "probe",
                "model": {"providerID": "opencode-go", "modelID": "glm-5.3-flash"},
                "parts": [{"type": "text", "text": PROMPTS[scenario]}],
            },
        )
        messages: list[dict[str, Any]] = []
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline:
            messages = native.request("GET", f"/session/{session['id']}/message")
            if any(receipt["reason_codes"] for receipt in relay.receipts):
                reasons.append("RELAY_REJECTED_REQUEST")
                break
            if any(
                message["info"].get("role") == "assistant"
                and (
                    message["info"].get("error")
                    or (
                        message["info"].get("time", {}).get("completed")
                        and message["info"].get("finish") not in {None, "tool-calls", "unknown"}
                    )
                )
                for message in messages
            ):
                break
            time.sleep(0.1)
        else:
            reasons.append("NATIVE_EXECUTION_TIMEOUT")
        raw = json.dumps(messages)
        if any(
            value in raw for value in (secret, relay.capability, authorization.capability, canary)
        ):
            reasons.append("SENSITIVE_NATIVE_OUTPUT")
        record.update(_message_facts(messages))
        content = fixture.read_text(encoding="utf-8")
        record["fixture_changed"] = content != INITIAL_FIXTURE
        record["fixture_cases"] = check_fixture(content) if scenario == "edit" else None
        record["blocked_file_unchanged"] = blocked.read_text(encoding="utf-8") == canary
        record["workspace_files"] = sorted(path.name for path in native.workspace.iterdir())
        _validate_observation(record, reasons)
    except Exception as error:
        # Runtime/provider text is untrusted and can contain credentials.
        record["error_type"] = type(error).__name__
        reasons.append("PROBE_EXECUTION_FAILED")
    finally:
        try:
            authorization.journal.revoke_grant(authorization.grant_id)
        except Exception:
            reasons.append("GRANT_REVOCATION_FAILED")
        try:
            record["native_cleanup"] = native.close() if native is not None else {}
        except Exception:
            record["native_cleanup"] = {"local_stop": "unknown", "remote_stop": "unknown"}
        try:
            record["relay_cleanup"] = relay.close()
        except Exception:
            record["relay_cleanup"] = {"status": "unknown"}
    record["requests"] = relay.receipts
    try:
        record["journal"] = authorization.journal.snapshot(authorization.grant_id)
    except Exception:
        record["journal"] = {}
        reasons.append("JOURNAL_READ_FAILED")
    calls = record["journal"].get("calls", [])
    if (
        not calls
        or any(
            call.get("state") != "response_received"
            or not (call.get("outcome") or {}).get("protocol_passed")
            for call in calls
        )
        or len(calls) != len(record["requests"])
        or any(not request["protocol_passed"] for request in record["requests"])
    ):
        reasons.append("PROVIDER_PROTOCOL_INCOMPLETE")
    if (
        record["native_cleanup"].get("local_stop") != "confirmed"
        or record["relay_cleanup"].get("status") != "closed"
        or record["journal"].get("state") != "revoked"
    ):
        reasons.append("LOCAL_CLEANUP_INCOMPLETE")
    record["reason_codes"] = list(dict.fromkeys(reasons))
    record["status"] = "failed" if reasons else "passed"
    if any(
        value in json.dumps(record)
        for value in (secret, relay.capability, authorization.capability)
    ):
        raise ValueError("SENSITIVE_REPORT_SUPPRESSED")
    return record


def _message_facts(messages: list[dict[str, Any]]) -> dict[str, Any]:
    assistants, tools = [], []
    for message in messages:
        info = message["info"]
        if info.get("role") == "assistant":
            assistants.append(
                {
                    "model_matches": info.get("modelID") == "glm-5.3-flash"
                    and info.get("providerID") == "opencode-go",
                    "completed": bool(info.get("time", {}).get("completed")),
                    "stopped": info.get("finish") == "stop",
                    "error": bool(info.get("error")),
                }
            )
        for part in message.get("parts", []):
            if part.get("type") != "tool":
                continue
            state = part.get("state", {})
            path = state.get("input", {}).get("filePath")
            tools.append(
                {
                    "tool": part.get("tool") if part.get("tool") in {"read", "edit"} else "other",
                    "status": state.get("status")
                    if state.get("status") in {"completed", "error"}
                    else "incomplete",
                    "path": path.removeprefix("/workspace/")
                    if path
                    in {
                        "/workspace/fixture.py",
                        "/workspace/blocked.txt",
                        "fixture.py",
                        "blocked.txt",
                    }
                    else "outside_fixture",
                    "permission_denied": str(state.get("error", "")).startswith(DENIAL_PREFIX),
                }
            )
    return {"assistants": assistants, "tools": tools}


def _validate_observation(record: dict[str, Any], reasons: list[str]) -> None:
    assistants, tools = record["assistants"], record["tools"]
    if (
        not assistants
        or not assistants[-1]["completed"]
        or not assistants[-1]["stopped"]
        or any(not assistant["model_matches"] or assistant["error"] for assistant in assistants)
    ):
        reasons.append("NATIVE_EXECUTION_INCOMPLETE")
    if (
        record["workspace_files"] != ["blocked.txt", "fixture.py"]
        or not record["blocked_file_unchanged"]
    ):
        reasons.append("WORKSPACE_CHANGED")
    if record["scenario"] == "edit":
        if (
            record["fixture_cases"] != [True] * 4
            or not record["fixture_changed"]
            or not all(any(tool["tool"] == name for tool in tools) for name in ("read", "edit"))
            or any(tool["status"] != "completed" or tool["path"] != "fixture.py" for tool in tools)
        ):
            reasons.append("NATIVE_EDIT_NOT_OBSERVED")
    elif (
        not tools
        or record["fixture_changed"]
        or any(
            tool["tool"] != "read"
            or tool["path"] != "blocked.txt"
            or tool["status"] != "error"
            or not tool["permission_denied"]
            for tool in tools
        )
    ):
        reasons.append("NATIVE_DENIAL_NOT_OBSERVED")
