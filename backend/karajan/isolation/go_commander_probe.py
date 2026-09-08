"""Bounded no-tools native Commander qualification observation.

This is intentionally an observer, not a Planning executor: its only inputs are
the sealed grant, fixed source and the controller-held credential.  A local HTTP
peer can exercise the exact native wire in tests, but is marked fixture and can
never be promoted by the producer or the current-facts reader.
"""
import hashlib
import json
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Literal, cast

import httpx

from karajan.adapters.opencode.go_context import GoRequestAccounting
from karajan.adapters.opencode.go_relay import (
    GoCommanderQualificationContext,
    GoRelay,
    GoRelayAuthorization,
)
from karajan.projects.go_commander_suite import LIMITS, probe_spec
from karajan.routing.compiler import digest
from karajan.runs.planning_output import PlanningOutputError, parse_planning_output

from ._go_reviewer_evidence import select_final, terminal
from ._opencode_inner import configuration
from .go_projected_probe import _validate_runtime
from .go_task import _cleanup_relay_socket_root, _relay_socket_root, _RelaySocketRoot
from .opencode_runtime import IsolatedOpenCode

_ANCHOR = b"commander-inline-only\n"
_PROJECTION = [
    {"path": "inline-only", "sha256": hashlib.sha256(_ANCHOR).hexdigest(), "writable": False}
]


def _context(accounting: GoRequestAccounting, spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_sha256": digest(accounting.source()),
        **{
            key: spec["limits"][key]
            for key in (
                "approved_input_tokens",
                "reserved_output_tokens",
                "operating_context_tokens",
                "fixed_margin",
                "ratio_margin_basis_points",
            )
        },
    }


def _prompt(scenario: str, spec: dict[str, Any]) -> str:
    case = spec["cases"][scenario]
    objective = case["input"]["objective"]
    if not isinstance(objective, str):
        raise ValueError("COMMANDER_PROBE_SPEC_INVALID")
    # The expected solution remains verifier-private. The model receives complete
    # requirements and constraints, rather than a JSON object it can copy.
    return (
        objective
        + "\nReturn exactly one JSON object, no markdown.\n"
        + json.dumps(case["input"], sort_keys=True, separators=(",", ":"))
    )


def _semantically_valid(plan: dict[str, Any], spec: dict[str, Any], scenario: str) -> bool:
    """Check the capability claim, not merely that PlanV2 accepts the JSON."""
    expected = spec["cases"][scenario]["expected_plan"]
    try:
        authorization = plan["authorization"]
        tasks = {task["id"]: task for task in plan["tasks"]}
        required = {"inspect-contract", "design-change", "verify-contract"}
        return (
            plan == expected
            and authorization["tools"] == []
            and authorization["read_paths"] == ["inline"]
            and authorization["write_paths"] == []
            and authorization["delivery"] == "none"
            and set(tasks) == required
            and tasks["inspect-contract"]["depends_on"] == []
            and tasks["design-change"]["depends_on"] == ["inspect-contract"]
            and tasks["verify-contract"]["depends_on"] == ["design-change"]
            and all(task["tools"] == [] for task in tasks.values())
            and all(
                set(["design_reasoning", "structured_plan_output"])
                <= set(task["required_capabilities"])
                for task in tasks.values()
            )
        )
    except (KeyError, TypeError):
        return False


def commander_runtime_source(runtime: Path, accounting: GoRequestAccounting) -> dict[str, Any]:
    """Same complete source descriptor used before grant creation and at execution."""
    from .go_probe import go_runtime_source

    source = go_runtime_source(runtime)
    root = Path(__file__).parents[1]
    for relative in (
        "isolation/go_commander_probe.py",
        "projects/go_commander_suite.py",
        "runs/planning_output.py",
        "adapters/opencode/go_context.py",
    ):
        path = root / relative
        source["source_sha256"][relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    spec = probe_spec(accounting.source())
    settings = configuration("<local-capability>", projection=_PROJECTION, no_tools=True)
    source.update(
        execution_path="linux-unshare-chroot-opencode-go-commander-planning-v2",
        accounting_source=accounting.source(),
        probe_spec=spec,
        probe_spec_digest=digest(spec),
        native_settings=settings,
        native_settings_digest=digest(settings),
    )
    return source


def _final(messages: list[dict[str, Any]], session: str, prompt: str) -> dict[str, Any]:
    final, tools = select_final(messages, session, prompt)
    if tools:
        raise ValueError("COMMANDER_NATIVE_TOOL_OBSERVED")
    return final


def observe_go_commander_probe(
    runtime: Path,
    directory: Path,
    secret: str,
    authorization: GoRelayAuthorization,
    *,
    scenario: str,
    accounting: GoRequestAccounting,
    current_guard: Callable[[], AbstractContextManager[None]],
    client_factory: Callable[[], httpx.Client] | None = None,
) -> dict[str, Any]:
    """Execute one fresh sealed scene through native OpenCode and the original Journal."""
    if scenario not in ("legal_plan", "denied_tool"):
        raise ValueError("FIXED_SCENARIO_REQUIRED")
    initial = authorization.journal.authenticate_grant(
        authorization.grant_id, capability=authorization.capability, binding=authorization.binding
    )
    source = commander_runtime_source(runtime, accounting)
    spec = source["probe_spec"]
    binding = initial["binding"]
    if (
        binding.get("schema_version") != "karajan.go-commander-qualification-grant.v1"
        or binding.get("runtime_digest") != digest(source)
        or binding.get("probe_spec_digest") != digest(spec)
        or binding.get("scenario") != scenario
        or binding.get("context") != _context(accounting, spec)
        or binding.get("max_requests") != LIMITS["max_requests_per_scene"]
        or initial["state"] != "active"
        or initial["request_count"] != 0
    ):
        raise ValueError("COMMANDER_GRANT_SOURCE_MISMATCH")
    if directory.exists() or directory.is_symlink():
        raise ValueError("NEW_CONTROLLER_DIRECTORY_REQUIRED")
    directory.mkdir(parents=True, mode=0o700)
    canary = "KARAJAN_COMMANDER_DENIED_CANARY_" + authorization.grant_id
    context = GoCommanderQualificationContext(
        accounting=accounting,
        probe_spec_digest=digest(spec),
        scenario=cast(Literal["legal_plan", "denied_tool"], scenario),
        source_sha256=digest(accounting.source()),
        approved_input_tokens=LIMITS["approved_input_tokens"],
        reserved_output_tokens=LIMITS["reserved_output_tokens"],
        operating_context_tokens=LIMITS["operating_context_tokens"],
        fixed_margin=LIMITS["fixed_margin"],
        ratio_margin_basis_points=LIMITS["ratio_margin_basis_points"],
    )
    relay = GoRelay(
        secret,
        canary,
        authorization=authorization,
        context=context,
        send_guard=current_guard,
        client_factory=client_factory,
    )
    native: IsolatedOpenCode | None = None
    root: _RelaySocketRoot | None = None
    reasons: list[str] = []
    prompt = _prompt(scenario, spec)
    record: dict[str, Any] = {
        "schema_version": "karajan.go-commander-observation.v1",
        "scenario": scenario,
        "observation_origin": "http_fixture" if client_factory else "official_go",
        "runtime_source": source,
        "runtime_digest": digest(source),
        "probe_spec_digest": digest(spec),
        "grant_id": authorization.grant_id,
        "requests": [],
        "journal": {},
        "native_final": None,
        "session": None,
        "provider_remote_stop": "unknown",
        "runtime_tools_status": "not_run",
        "dispatch_eligible": False,
    }
    try:
        root = _relay_socket_root()
        socket = root.path / "inference.sock"
        relay.start(unix_socket=socket)
        native = IsolatedOpenCode(
            runtime,
            directory / "native",
            socket,
            relay.capability,
            projection=_PROJECTION,
            no_tools=True,
        )
        (native.workspace / "inline-only").write_bytes(_ANCHOR)
        with current_guard():
            record["runtime"] = native.start()
        _validate_runtime(record["runtime"])
        config = native.request("GET", "/config")
        expected = source["native_settings"]
        if any(
            config.get(key) != expected[key]
            for key in ("permission", "model", "plugin", "mcp", "lsp", "formatter")
        ):
            raise ValueError("EFFECTIVE_CONFIGURATION_MISMATCH")
        projection = native.readonly_projection_observation()
        if len(projection) != 1 or not projection[0]["readonly"]:
            raise ValueError("COMMANDER_PROJECTION_NOT_READONLY")
        session = native.request(
            "POST", "/session", {"title": "Fixed Commander plan", "agent": "probe"}
        )
        if native.request("GET", f"/session/{session['id']}/message") != []:
            raise ValueError("NEW_EMPTY_SESSION_REQUIRED")
        record["session"] = {
            "id": session["id"],
            "initial_messages_sha256": digest({"messages": []}),
        }
        with current_guard():
            native.request(
                "POST",
                f"/session/{session['id']}/prompt_async",
                {
                    "agent": "probe",
                    "model": {"providerID": "opencode-go", "modelID": "glm-5.3-flash"},
                    "parts": [{"type": "text", "text": prompt}],
                },
            )
        deadline = time.monotonic() + LIMITS["max_seconds_per_scene"]
        messages: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            messages = native.request("GET", f"/session/{session['id']}/message")
            if any(row["reason_codes"] for row in relay.receipts):
                raise ValueError("RELAY_REJECTED_REQUEST")
            if terminal(messages):
                break
            time.sleep(0.1)
        else:
            raise ValueError("NATIVE_EXECUTION_TIMEOUT")
        if any(
            value in json.dumps(messages)
            for value in (secret, canary, relay.capability, authorization.capability)
        ):
            raise ValueError("SENSITIVE_NATIVE_OUTPUT")
        final = _final(messages, session["id"], prompt)
        if len(final["text"].encode()) > spec["evidence_limits"]["final_output_bytes"]:
            raise ValueError("COMMANDER_OUTPUT_LIMIT_EXCEEDED")
        plan = parse_planning_output(final["text"], version="v2").model_dump(mode="json")
        if not _semantically_valid(plan, spec, scenario):
            raise ValueError("FIXED_PLAN_SEMANTICS_MISMATCH")
        record["native_final"], record["parsed_plan"] = final, plan
    except PlanningOutputError as error:
        reasons.append(error.code)
    except Exception as error:
        reasons.append(
            str(error)
            if str(error).startswith(
                ("NATIVE_", "RELAY_", "COMMANDER_", "EFFECTIVE_", "NEW_", "FIXED_", "SENSITIVE_")
            )
            else "COMMANDER_PROBE_EXECUTION_FAILED"
        )
    finally:
        try:
            authorization.journal.revoke_grant(authorization.grant_id)
        except Exception:
            reasons.append("GRANT_REVOCATION_FAILED")
        try:
            record["native_cleanup"] = (
                native.capture_projection().stop_evidence
                if native
                else {"local_stop": "not_started"}
            )
        except Exception:
            reasons.append("NATIVE_STOP_UNKNOWN")
        try:
            record["relay_cleanup"] = relay.close()
            if record["relay_cleanup"]["status"] == "closed":
                _cleanup_relay_socket_root(root)
        except Exception:
            reasons.append("RELAY_STOP_UNKNOWN")
    record["requests"] = relay.receipts
    try:
        record["journal"] = authorization.journal.snapshot(authorization.grant_id)
    except Exception:
        reasons.append("JOURNAL_READ_FAILED")
    calls = record["journal"].get("calls", [])
    if (
        not calls
        or len(calls) != len(record["requests"])
        or any(
            c.get("state") != "response_received" or not c.get("outcome", {}).get("protocol_passed")
            for c in calls
        )
    ):
        reasons.append("PROVIDER_PROTOCOL_INCOMPLETE")
    if (
        record.get("native_cleanup", {}).get("local_stop") != "confirmed"
        or record.get("relay_cleanup", {}).get("status") != "closed"
    ):
        reasons.append("LOCAL_CLEANUP_INCOMPLETE")
    record["reason_codes"] = list(dict.fromkeys(reasons))
    record["status"] = "failed" if reasons else "passed"
    return record
