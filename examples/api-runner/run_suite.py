"""Replay synthetic HTTP faults through the pinned official OpenCode runtime."""

import argparse
import json
import subprocess
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    executable = "opencode.exe"
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument(
        "--runtime",
        type=Path,
        default=root / "runtimes/opencode/node_modules/opencode-ai/bin" / executable,
    )
    arguments = parser.parse_args()
    destination = arguments.directory.resolve()
    destination.mkdir(parents=True, exist_ok=False)
    cases = [
        (name, None, 2 if name in {"timeout_once", "admission_limit", "cleanup_fault"} else 0)
        for name in (
            "tool_loop",
            "rate_limit_once",
            "disconnect_once",
            "timeout_once",
            "cancel_stream",
            "admission_limit",
            "cleanup_fault",
        )
    ]
    cases += [("tool_loop", tamper, 1) for tamper in ("model", "permission", "endpoint")]
    outcomes = []
    for scenario, tamper, expected in cases:
        case = f"tamper-{tamper}" if tamper else scenario
        command = [
            sys.executable,
            "-m",
            "karajan.adapters.opencode",
            "--runtime",
            str(arguments.runtime),
            "--directory",
            str(destination / case),
            "--scenario",
            scenario,
        ]
        if tamper:
            command += ["--tamper", tamper]
        result = subprocess.run(command, text=True, capture_output=True, timeout=35, check=False)
        outcome = {
            "case": case,
            "exit_code": result.returncode,
            "expected_exit_code": expected,
            "observation": json.loads(result.stdout),
        }
        outcomes.append(outcome)
        print(json.dumps(outcome))
    summary = {
        "live_qualified": False,
        "profile_enabled": False,
        "qualification_decision": "rejected",
        "cases": outcomes,
    }
    (destination / "index.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return int(any(item["exit_code"] != item["expected_exit_code"] for item in outcomes))


if __name__ == "__main__":
    raise SystemExit(main())
