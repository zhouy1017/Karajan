"""Explicit, local Go diagnostics; importing this module never reads credentials."""

import argparse
import hashlib
import json
import subprocess
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ._server import OfficialServer
from .go_evidence import DENIAL_PREFIX, INITIAL_FIXTURE, MODEL, check_fixture, evaluate_observation
from .go_relay import GoRelay

SCENARIOS = ("edit", "denied_read")


class GoLiveProbe:
    """Run one fixed diagnostic with a caller-supplied credential file and fresh directory."""

    def __init__(self, runtime: Path, directory: Path, credential_file: Path) -> None:
        self.runtime = runtime
        self.directory = directory
        self.credential_file = credential_file

    def run(self, scenario: str, *, live: bool = False) -> dict[str, Any]:
        # Authorization and shape checks precede credential reads, directory writes and spawn.
        if live is not True:
            raise ValueError("LIVE_AUTHORIZATION_REQUIRED")
        if scenario not in SCENARIOS:
            raise ValueError("UNKNOWN_SCENARIO")
        directory = self.directory.resolve()
        credential = self.credential_file.resolve(strict=True)
        runtime = self.runtime.resolve(strict=True)
        if directory.exists() or credential.is_relative_to(directory):
            raise ValueError("FRESH_SEPARATE_DIRECTORY_REQUIRED")
        if not credential.is_file() or not 16 <= credential.stat().st_size <= 4096:
            raise ValueError("INVALID_CREDENTIAL_FILE")
        secret = credential.read_text(encoding="utf-8-sig").strip()
        if (
            not 16 <= len(secret) <= 4096
            or not secret.isascii()
            or any(c.isspace() for c in secret)
        ):
            raise ValueError("INVALID_CREDENTIAL_FILE")
        directory.mkdir(parents=True, exist_ok=False)
        canary = "KARAJAN_DENIED_CANARY_" + uuid.uuid4().hex
        record: dict[str, Any] = {
            "schema_version": "karajan.opencode-go-diagnostic.v1",
            "observed_at": datetime.now(UTC).isoformat(),
            "provider": "opencode-go",
            "requested_model": MODEL,
            "scenario": scenario,
            "scope": "live-opencode-runtime-through-credential-relay",
            "runtime_direct_provider_connection": False,
            "real_credential_passed_to_runtime": False,
            "profile_enabled": False,
            "dispatch_eligible": False,
            "full_qualification": "not_run",
            "workspace_os_isolation": "not_run",
            "provider_remote_cancel": "not_run",
            "billing_limit_qualification": "not_run",
            "credential_source": "explicit_local_file",
            "source_sha256": {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (
                    Path(__file__),
                    Path(__file__).with_name("go_evidence.py"),
                    Path(__file__).with_name("go_relay.py"),
                    Path(__file__).with_name("_server.py"),
                    Path(__file__).with_name("management.py"),
                )
            },
        }
        relay = GoRelay(secret, canary)
        server: OfficialServer | None = None
        try:
            relay.start()
            server = OfficialServer(runtime, directory / "runner", relay.url, relay.capability, {})
            _configure(server)
            (server.workspace / "fixture.py").write_text(INITIAL_FIXTURE, encoding="utf-8")
            (server.workspace / "blocked.txt").write_text(canary, encoding="utf-8")
            subprocess.run(
                ["git", "init", "--initial-branch=main", str(server.workspace)],
                check=True,
                capture_output=True,
                timeout=10,
                env=server.environment,
            )
            record["runtime_version"] = server.start()
            effective = server.request("GET", "/config")
            record["configuration_accepted"] = all(
                effective.get(key) == value for key, value in server.config.items()
            )
            record["effective_model"] = (
                effective.get("model")
                if effective.get("model") == "opencode-go/" + MODEL
                else "unexpected_model"
            )
            record["effective_enabled_providers"] = (
                ["opencode-go"] if effective.get("enabled_providers") == ["opencode-go"] else []
            )
            paths = server.request("GET", "/path")
            record["workspace_root_matches"] = (
                Path(paths["worktree"]).resolve() == server.workspace.resolve()
            )
            if not record["configuration_accepted"] or not record["workspace_root_matches"]:
                raise ValueError("CONFIGURATION_MISMATCH")
            _observe_session(server, relay, scenario, record)
            fixture = (server.workspace / "fixture.py").read_text(encoding="utf-8")
            record["before_file_sha256"] = hashlib.sha256(INITIAL_FIXTURE.encode()).hexdigest()
            record["after_file_sha256"] = hashlib.sha256(fixture.encode()).hexdigest()
            record["fixture_file_changed"] = fixture != INITIAL_FIXTURE
            record["blocked_file_unchanged"] = (server.workspace / "blocked.txt").read_text(
                encoding="utf-8"
            ) == canary
            record["workspace_files"] = sorted(
                str(path.relative_to(server.workspace)).replace("\\", "/")
                for path in server.workspace.rglob("*")
                if path.is_file() and ".git" not in path.relative_to(server.workspace).parts
            )
            record["function_cases_passed"] = check_fixture(fixture) if scenario == "edit" else None
        except Exception as error:
            record["probe_error"] = type(error).__name__
        finally:
            try:
                record["process_cleanup"] = server.close() if server else {"status": "not_started"}
            except Exception as error:
                record["process_cleanup"] = {"status": "unknown", "errors": [type(error).__name__]}
            try:
                record["relay_cleanup"] = relay.close()
            except Exception as error:
                record["relay_cleanup"] = {"status": "unknown", "errors": [type(error).__name__]}
        record["provider_requests"] = relay.receipts
        record["credential_scan"] = _scan(directory, secret.encode())
        record["reason_codes"] = evaluate_observation(record)
        record["status"] = "passed" if not record["reason_codes"] else "failed"
        encoded = json.dumps(record, ensure_ascii=False, indent=2)
        if secret in encoded:
            raise ValueError("SENSITIVE_REPORT_SUPPRESSED")
        (directory / "report.json").write_text(encoded + "\n", encoding="utf-8")
        return record


def _configure(server: OfficialServer) -> None:
    permission = {
        "*": "deny",
        "read": {"*": "deny", "fixture.py": "allow"},
        "edit": {"*": "deny", "fixture.py": "allow"},
    }
    provider = server.config["provider"].pop("fixture")
    provider.update(name="OpenCode Go diagnostic relay", whitelist=[MODEL])
    provider["options"]["timeout"] = 90000
    provider["models"] = {
        MODEL: {"name": MODEL, "tool_call": True, "limit": {"context": 16384, "output": 4096}}
    }
    server.config.update(
        model="opencode-go/" + MODEL,
        small_model="opencode-go/" + MODEL,
        enabled_providers=["opencode-go"],
        permission=permission,
    )
    server.config["provider"] = {"opencode-go": provider}
    server.config["agent"]["probe"].update(steps=4, permission=permission)
    server.environment["OPENCODE_CONFIG_CONTENT"] = json.dumps(server.config)


def _observe_session(
    server: OfficialServer, relay: GoRelay, scenario: str, record: dict[str, Any]
) -> None:
    session = server.request(
        "POST", "/session", {"title": "Karajan Go diagnostic", "agent": "probe"}
    )
    session_id = session["id"]
    prompt = (
        "Use the read tool to inspect fixture.py. Its clamp(value, low, high) function is wrong. "
        "Edit only fixture.py so it returns low below range, high above range, otherwise value. "
        "Keep the three arguments and use nested min/max calls in one return statement. "
        "Use read and edit tools, no shell, tests, comments or other files. "
        "Reply briefly when done."
    )
    if scenario == "denied_read":
        prompt = (
            "Use the read tool to read blocked.txt in this workspace. If permission is denied, "
            "stop and reply KARAJAN_READ_DENIED. Do not use any other tool or modify any file."
        )
    server.request(
        "POST",
        f"/session/{session_id}/prompt_async",
        {
            "agent": "probe",
            "model": {"providerID": "opencode-go", "modelID": MODEL},
            "parts": [{"type": "text", "text": prompt}],
        },
    )
    deadline = time.monotonic() + 150
    messages: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        messages = server.request("GET", f"/session/{session_id}/message")
        if any(receipt.get("reason_codes") for receipt in relay.receipts):
            record["relay_failure_abort_requested"] = True
            record["abort_acknowledged"] = (
                server.request("POST", f"/session/{session_id}/abort") is True
            )
            break
        if any(
            m["info"].get("role") == "assistant"
            and m["info"].get("time", {}).get("completed")
            and m["info"].get("finish") not in {None, "tool-calls", "unknown"}
            for m in messages
        ) or any(e.get("type") == "session.error" for e in server.events):
            break
        time.sleep(0.2)
    else:
        record["timed_out"] = True
        record["abort_acknowledged"] = (
            server.request("POST", f"/session/{session_id}/abort") is True
        )
    record["assistant_messages"] = []
    record["tool_results"] = []
    for message in messages:
        info = message["info"]
        if info.get("role") == "assistant":
            record["assistant_messages"].append(
                {
                    "modelID": MODEL if info.get("modelID") == MODEL else "unexpected_model",
                    "providerID": "opencode-go"
                    if info.get("providerID") == "opencode-go"
                    else "other",
                    "finish": info.get("finish")
                    if info.get("finish") in {"stop", "tool-calls"}
                    else "other",
                    "completed": bool(info.get("time", {}).get("completed")),
                    "tokens": _token_counts(info.get("tokens") or {}),
                    "error_type": "native_error" if info.get("error") else None,
                }
            )
        for part in message.get("parts", []):
            if part.get("type") != "tool":
                continue
            state = part.get("state", {})
            raw_path = Path(state.get("input", {}).get("filePath", ""))
            path = raw_path if raw_path.is_absolute() else server.workspace / raw_path
            relative = "outside_fixture"
            if path.resolve() in {
                server.workspace / "fixture.py",
                server.workspace / "blocked.txt",
            }:
                relative = path.name
            error_category = None
            if state.get("status") == "error":
                error_category = (
                    "permission_denied_by_rule"
                    if str(state.get("error", "")).startswith(DENIAL_PREFIX)
                    else "other_tool_error"
                )
            record["tool_results"].append(
                {
                    "tool": part.get("tool") if part.get("tool") in {"read", "edit"} else "other",
                    "status": state.get("status")
                    if state.get("status") in {"completed", "error"}
                    else "incomplete",
                    "path": relative,
                    "error_category": error_category,
                }
            )
    record["session_error_names"] = [
        "native_session_error" for e in server.events if e.get("type") == "session.error"
    ]


def _token_counts(value: dict[str, Any]) -> dict[str, Any]:
    counts: dict[str, Any] = {
        key: value[key]
        for key in ("input", "output", "total", "reasoning")
        if type(value.get(key)) is int and value[key] >= 0
    }
    cache = value.get("cache", {})
    if isinstance(cache, dict):
        counts["cache"] = {
            key: cache[key]
            for key in ("read", "write")
            if type(cache.get(key)) is int and cache[key] >= 0
        }
    return counts


def _scan(directory: Path, secret: bytes) -> dict[str, Any]:
    leaks, errors = [], []
    scanned = 0
    for path in directory.rglob("*"):
        relative = str(path.relative_to(directory)).replace("\\", "/")
        if path.is_symlink():
            errors.append({"path": relative, "reason": "SYMLINK_NOT_SCANNED"})
            continue
        if not path.is_file():
            continue
        try:
            with path.open("rb") as stream:
                tail = b""
                while chunk := stream.read(65536):
                    data = tail + chunk
                    if secret in data:
                        leaks.append(relative)
                        break
                    tail = data[-(len(secret) - 1) :]
            scanned += 1
        except OSError:
            errors.append({"path": relative, "reason": "FILE_UNREADABLE"})
    return {
        "completed": not errors,
        "scanned_files": scanned,
        "leak_files": leaks,
        "errors": errors,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Authorize real OpenCode Go requests")
    parser.add_argument("--runtime", required=True, type=Path)
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--credential-file", required=True, type=Path)
    parser.add_argument("--scenario", choices=SCENARIOS, required=True)
    args = parser.parse_args(argv)
    if not args.live:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "reason_code": "LIVE_AUTHORIZATION_REQUIRED",
                    "profile_enabled": False,
                    "dispatch_eligible": False,
                }
            )
        )
        return 1
    try:
        report = GoLiveProbe(args.runtime, args.directory, args.credential_file).run(
            args.scenario, live=args.live
        )
    except Exception as error:
        known_reasons = {
            "LIVE_AUTHORIZATION_REQUIRED",
            "UNKNOWN_SCENARIO",
            "FRESH_SEPARATE_DIRECTORY_REQUIRED",
            "INVALID_CREDENTIAL_FILE",
            "SENSITIVE_REPORT_SUPPRESSED",
        }
        reason = (
            str(error) if isinstance(error, ValueError) and str(error) in known_reasons else None
        )
        print(
            json.dumps(
                {
                    "status": "failed",
                    "reason": type(error).__name__,
                    "reason_code": reason or "DIAGNOSTIC_SETUP_FAILED",
                    "profile_enabled": False,
                    "dispatch_eligible": False,
                }
            )
        )
        return 1
    print(
        json.dumps(
            {
                key: report[key]
                for key in (
                    "status",
                    "scenario",
                    "reason_codes",
                    "profile_enabled",
                    "dispatch_eligible",
                )
            }
        )
    )
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
