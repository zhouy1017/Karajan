"""Fixed projected Go qualification: accounted wire, native tools, stopped Collector.

This producer accepts a controller's exact v2 qualification grant, never Task
authority. Its small synthetic observation is not maximum-context qualification,
review evidence, a budget bound, or permission to execute arbitrary projects.
"""

import hashlib
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from karajan.adapters.opencode.go_context import GoRequestAccounting
from karajan.adapters.opencode.go_evidence import DENIAL_PREFIX, INITIAL_FIXTURE, check_fixture
from karajan.adapters.opencode.go_relay import GoRelay, GoRelayAuthorization

from ._go_projected_evidence import (
    FILES,
    PROMPTS,
    ObservedContext,
    WireRetention,
    collect_capture,
    prepare_baseline,
    projection,
    sha,
)
from ._opencode_inner import configuration
from .go_probe import go_runtime_source, source_digest
from .opencode_runtime import RUNTIME_SHA256, IsolatedOpenCode


def projected_probe_spec(accounting: GoRequestAccounting) -> dict[str, Any]:
    """Fresh fixed, content-free specification used by controller and observer."""
    return {
        "schema_version": "karajan.go-projected-probe-spec.v1",
        "id": "opencode-go-projected-read-edit-capture-linux",
        "revision": 1,
        "model": "glm-5.3-flash",
        "scenarios": ["edit", "denied_read"],
        "max_requests": 6,
        "context": {
            "source_sha256": source_digest(accounting.source()),
            "approved_input_tokens": 12288,
            "reserved_output_tokens": 4096,
            "operating_context_tokens": 16384,
            "fixed_margin": 2048,
            "ratio_margin_basis_points": 2000,
        },
        "projection": projection(),
        "prompt_sha256": {scenario: sha(text.encode()) for scenario, text in PROMPTS.items()},
        "baseline_manifest": [
            {
                "path": path,
                "mode": "100755" if path == "bin/unchanged" else "100644",
                "sha256": sha(content),
                "bytes": len(content),
                "blob_sha": hashlib.sha1(
                    b"blob " + str(len(content)).encode() + b"\0" + content
                ).hexdigest(),
            }
            for path, content in sorted(FILES.items())
        ],
    }


def projected_runtime_source(runtime: Path, accounting: GoRequestAccounting) -> dict[str, Any]:
    """The complete implementation and fixed artifact identity of this producer."""
    source = go_runtime_source(runtime)
    source["execution_path"] = "linux-unshare-chroot-opencode-go-projected-capture-v1"
    root = Path(__file__).parents[1]
    for path in (
        Path(__file__),
        Path(__file__).with_name("_go_projected_evidence.py"),
        root / "candidates/store.py",
        root / "candidates/models.py",
        root / "candidates/_projection.py",
        root / "adapters/opencode/go_context.py",
    ):
        # Directory-qualified keys avoid collisions between independent store/models modules.
        source["source_sha256"][path.relative_to(root).as_posix()] = sha(path.read_bytes())
    source["system_executable_sha256"]["git"] = sha(Path("/usr/bin/git").read_bytes())
    spec = projected_probe_spec(accounting)
    source.update(
        probe_spec=spec,
        probe_spec_digest=source_digest(spec),
        accounting_source=accounting.source(),
    )
    source["native_settings_digest"] = source_digest(
        configuration("<local-capability>", projection=projection())
    )
    return source


def observe_go_projected_tools(
    runtime: Path,
    directory: Path,
    secret: str,
    authorization: GoRelayAuthorization,
    *,
    scenario: str,
    accounting: GoRequestAccounting,
    client_factory: Callable[[], httpx.Client] | None = None,
) -> dict[str, Any]:
    """Observe one fixed scenario once; all transport sends retain Journal admission.

    Read-only preflight is not an atomic check-to-start permission. Revocation
    racing process startup still prevents every later HTTP send at begin_call.
    An early rejected foreign grant is never revoked by this producer.
    """
    if scenario not in PROMPTS:
        raise ValueError("FIXED_SCENARIO_REQUIRED")
    initial = authorization.journal.authenticate_grant(
        authorization.grant_id, capability=authorization.capability, binding=authorization.binding
    )
    binding = initial["binding"]
    if binding.get("schema_version") != "karajan.go-qualification-grant.v2":
        raise ValueError("PROJECTED_QUALIFICATION_GRANT_REQUIRED")
    source = projected_runtime_source(runtime, accounting)
    spec = source["probe_spec"]
    if (
        binding["runtime_digest"] != source_digest(source)
        or binding["probe_spec_digest"] != source_digest(spec)
        or binding["scenario"] != scenario
        or binding["context"] != spec["context"]
        or binding["max_requests"] != spec["max_requests"]
    ):
        raise ValueError("PROJECTED_GRANT_SOURCE_MISMATCH")
    if initial["state"] != "active" or initial["request_count"] != 0:
        raise ValueError("FRESH_ACTIVE_GRANT_REQUIRED")
    if directory.exists() or directory.is_symlink():
        raise ValueError("NEW_CONTROLLER_DIRECTORY_REQUIRED")
    directory.mkdir(mode=0o700)
    canary = "KARAJAN_PROJECTED_DENIED_CANARY_" + authorization.grant_id
    (directory / "canary.txt").write_text(canary)
    retention = WireRetention(scenario)
    context = ObservedContext(
        accounting=accounting,
        probe_spec_digest=source_digest(spec),
        scenario="edit" if scenario == "edit" else "denied_read",
        retention=retention,
        **spec["context"],
    )
    relay = GoRelay(
        secret, canary, authorization=authorization, context=context, client_factory=client_factory
    )
    native: IsolatedOpenCode | None = None
    reasons: list[str] = []
    record: dict[str, Any] = {
        "schema_version": "karajan.projected-opencode-go-observation.v1",
        "scope": "projected_native_tools_fixture" if client_factory else "projected_native_tools",
        "scenario": scenario,
        "grant_id": authorization.grant_id,
        "observation_origin": "http_fixture" if client_factory else "official_go",
        "runtime_tools_status": "not_run",
        "dispatch_eligible": False,
        "real_credential_passed_to_runtime": False,
        "provider_remote_stop": "unknown",
        "billing_limit_qualification": "not_run",
        "runtime_source": source,
        "runtime_digest": source_digest(source),
        "artifact_sha256": RUNTIME_SHA256,
        "source_sha256": source["source_sha256"],
        "probe_spec": spec,
        "probe_spec_digest": source_digest(spec),
    }
    try:
        store, baseline = prepare_baseline(directory)
        relay.start(unix_socket=directory / "inference.sock")
        native = IsolatedOpenCode(
            runtime,
            directory / "native",
            directory / "inference.sock",
            relay.capability,
            projection=spec["projection"],
        )
        for row in spec["projection"]:
            path = native.workspace / row["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(FILES[row["path"]])
        record["runtime"] = native.start()
        _validate_runtime(record["runtime"])
        config = native.request("GET", "/config")
        expected = configuration("<local-capability>", projection=projection())
        if (
            config.get("permission") != expected["permission"]
            or config.get("model") != expected["model"]
        ):
            raise ValueError("EFFECTIVE_CONFIGURATION_MISMATCH")
        record["effective_configuration_matches"] = True
        session = native.request(
            "POST", "/session", {"title": "Fixed projected Go", "agent": "probe"}
        )
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
            if any(r["reason_codes"] for r in relay.receipts):
                reasons.append("RELAY_REJECTED_REQUEST")
                break
            if any(
                m["info"].get("role") == "assistant"
                and (
                    m["info"].get("error")
                    or (
                        m["info"].get("time", {}).get("completed")
                        and m["info"].get("finish") not in {None, "tool-calls", "unknown"}
                    )
                )
                for m in messages
            ):
                break
            time.sleep(0.1)
        else:
            reasons.append("NATIVE_EXECUTION_TIMEOUT")
        if any(
            value in json.dumps(messages)
            for value in (secret, canary, relay.capability, authorization.capability)
        ):
            reasons.append("SENSITIVE_NATIVE_OUTPUT")
        record.update(_messages(messages))
        # Revoke before any stop/capture cleanup. Subsequent relay attempts cannot send.
        authorization.journal.revoke_grant(authorization.grant_id)
        captured = native.capture_projection()
        record["native_cleanup"] = captured.stop_evidence
        record["capture"] = collect_capture(
            directory, store, baseline, captured, binding, authorization.grant_id
        )
        contents = dict(captured.files)
        record["fixture_changed"] = contents["src/fixture.py"] != INITIAL_FIXTURE.encode()
        record["fixture_cases"] = (
            check_fixture(contents["src/fixture.py"].decode()) if scenario == "edit" else None
        )
        _validate_tools(record, reasons)
    except Exception as error:
        record["error_type"] = type(error).__name__
        reasons.append("PROJECTED_PROBE_EXECUTION_FAILED")
    finally:
        try:
            current = authorization.journal.snapshot(authorization.grant_id)
            if current["binding"] != binding:
                raise ValueError("GRANT_CLEANUP_BINDING_MISMATCH")
            authorization.journal.revoke_grant(authorization.grant_id)
        except Exception:
            reasons.append("GRANT_REVOCATION_FAILED")
        try:
            record.setdefault("native_cleanup", native.close() if native else {})
        except Exception:
            record["native_cleanup"] = {"local_stop": "unknown"}
        try:
            record["relay_cleanup"] = relay.close()
        except Exception:
            record["relay_cleanup"] = {"status": "unknown"}
    record["requests"] = relay.receipts
    record["retention"] = retention.report()
    try:
        record["journal"] = authorization.journal.snapshot(authorization.grant_id)
    except Exception:
        record["journal"] = {}
        reasons.append("JOURNAL_READ_FAILED")
    _validate_final(record, reasons)
    record["reason_codes"] = list(dict.fromkeys(reasons))
    record["status"] = "failed" if reasons else "passed"
    if any(
        value in json.dumps(record)
        for value in (secret, canary, relay.capability, authorization.capability)
    ):
        raise ValueError("SENSITIVE_REPORT_SUPPRESSED")
    return record


def _validate_runtime(observed: dict[str, Any]) -> None:
    if (
        observed.get("namespace_pid") != 1
        or observed.get("host_mount_visible") is not False
        or observed.get("wsl_interop_visible") is not False
        or observed.get("native_control_fd_inherited") is not False
        or observed.get("network_interfaces") != ["lo"]
        or observed.get("ipv4_routes") != []
        or observed.get("no_new_privileges") is not True
        or observed.get("capabilities")
        != {k: "0000000000000000" for k in ("effective", "permitted", "bounding")}
    ):
        raise ValueError("NAMESPACE_OBSERVATION_INCOMPLETE")


def _messages(messages: list[dict[str, Any]]) -> dict[str, Any]:
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
            paths = {
                "/workspace/" + p: p for p in ("reference.md", "src/fixture.py", "blocked.txt")
            }
            tools.append(
                {
                    "tool": part.get("tool") if part.get("tool") in {"read", "edit"} else "other",
                    "path": paths.get(path, "outside_fixture"),
                    "status": state.get("status")
                    if state.get("status") in {"completed", "error"}
                    else "incomplete",
                    "permission_denied": str(state.get("error", "")).startswith(DENIAL_PREFIX),
                }
            )
    return {"assistants": assistants, "tools": tools}


def _validate_tools(record: dict[str, Any], reasons: list[str]) -> None:
    assistants, tools = record["assistants"], record["tools"]
    if (
        not assistants
        or not assistants[-1]["completed"]
        or not assistants[-1]["stopped"]
        or any(not a["model_matches"] or a["error"] for a in assistants)
    ):
        reasons.append("NATIVE_EXECUTION_INCOMPLETE")
    if record["scenario"] == "edit":
        if (
            record["fixture_cases"] != [True] * 4
            or not record["fixture_changed"]
            or any(t["status"] != "completed" for t in tools)
            or not all(
                any(t["tool"] == tool and t["path"] == path for t in tools)
                for tool, path in (
                    ("read", "reference.md"),
                    ("read", "src/fixture.py"),
                    ("edit", "src/fixture.py"),
                )
            )
            or any(
                (t["tool"], t["path"])
                not in {
                    ("read", "reference.md"),
                    ("read", "src/fixture.py"),
                    ("edit", "src/fixture.py"),
                }
                for t in tools
            )
        ):
            reasons.append("NATIVE_PROJECTED_EDIT_NOT_OBSERVED")
    elif (
        not tools
        or record["fixture_changed"]
        or any(
            t["tool"] != "read"
            or t["path"] != "blocked.txt"
            or t["status"] != "error"
            or not t["permission_denied"]
            for t in tools
        )
    ):
        reasons.append("NATIVE_PROJECTED_DENIAL_NOT_OBSERVED")


def _validate_final(record: dict[str, Any], reasons: list[str]) -> None:
    calls = record["journal"].get("calls", [])
    requests = record["requests"]
    wire = record["retention"]
    if (
        not calls
        or len(calls) != len(requests)
        or len(calls) != len(wire["calls"])
        or any(
            c.get("state") != "response_received"
            or not (c.get("outcome") or {}).get("protocol_passed")
            for c in calls
        )
        or any(not r["protocol_passed"] or r["reason_codes"] for r in requests)
    ):
        reasons.append("PROVIDER_PROTOCOL_INCOMPLETE")
    if (
        not wire["initial_input_retained"]
        or not wire["tool_history_retained"]
        or (
            record["scenario"] == "edit"
            and (not wire["reference_input_observed"] or not wire["target_input_observed"])
        )
    ):
        reasons.append("MEASURED_WIRE_RETENTION_INCOMPLETE")
    if len(calls) == len(requests) == len(wire["calls"]) and any(
        c.get("request_context") != r.get("request_context")
        or c.get("request_context", {}).get("request_digest") != w["request_digest"]
        or c.get("call_id") != r.get("journal_call_id")
        for c, r, w in zip(calls, requests, wire["calls"], strict=True)
    ):
        reasons.append("MEASURED_WIRE_BINDING_MISMATCH")
    if (
        record["native_cleanup"].get("local_stop") != "confirmed"
        or record["relay_cleanup"].get("status") != "closed"
        or record["journal"].get("state") != "revoked"
    ):
        reasons.append("LOCAL_CLEANUP_INCOMPLETE")
    capture = record.get("capture", {})
    expected_changes = ["src/fixture.py"] if record["scenario"] == "edit" else []
    if (
        capture.get("status") != "passed"
        or capture.get("changed_paths") != expected_changes
        or capture.get("validation_gate")
        != {
            "local_gate_passed": False,
            "reasons": ["CHECK_EVIDENCE_MISSING:fixture_check", "REVIEW_EVIDENCE_MISSING"],
        }
    ):
        reasons.append("CANDIDATE_CAPTURE_INCOMPLETE")
