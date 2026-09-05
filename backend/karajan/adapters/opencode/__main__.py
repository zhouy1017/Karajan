"""Run a local fixture; a zero exit code is not execution qualification."""

import argparse
import json
import subprocess
from pathlib import Path

from .probe import SCENARIOS, OpenCodeProbe


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m karajan.adapters.opencode")
    parser.add_argument("--runtime", required=True, type=Path)
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), default="tool_loop")
    parser.add_argument("--tamper", choices=["model", "permission", "endpoint"])
    arguments = parser.parse_args()
    try:
        report = OpenCodeProbe(arguments.runtime, arguments.directory).run(
            arguments.scenario, tamper=arguments.tamper
        )
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "reason": type(error).__name__,
                    "live_qualified": False,
                    "profile_enabled": False,
                }
            )
        )
        return 1
    print(
        json.dumps(
            {
                "status": report.status,
                "runtime_version": report.runtime_version,
                "qualification_scope": report.qualification_scope,
                "live_qualified": False,
                "profile_enabled": False,
                "qualification_decision": "rejected",
                "reason_codes": report.reason_codes,
                "qualification_reason_codes": report.qualification_reason_codes,
                "capabilities": report.capabilities,
                "evidence_directory": str(arguments.directory.resolve()),
            }
        )
    )
    if report.status == "rejected":
        return 1
    return 2 if report.status in {"runtime_error", "unknown"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
