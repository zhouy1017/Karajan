"""Explicit temporary-root isolation probe; no arbitrary command or secret inputs."""

import argparse
import json
from pathlib import Path

from .probe import run_probe


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m karajan.isolation")
    commands = parser.add_subparsers(dest="command", required=True)
    probe = commands.add_parser("probe")
    probe.add_argument("--spec", type=Path, required=True)
    probe.add_argument("--directory", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        spec = json.loads(arguments.spec.read_text(encoding="utf-8"))
        report = run_probe(spec, arguments.directory)
    except (OSError, ValueError, TypeError):
        report = {
            "schema_version": "karajan.isolation.report.v1",
            "status": "failed",
            "reason_codes": ["PROBE_INPUT_REJECTED"],
            "dispatch_eligible": False,
            "runtime_tools_status": "not_run",
        }
    print(json.dumps(report, sort_keys=True, ensure_ascii=True))
    return 0 if report["status"] == "passed" else 2 if report["status"] == "unsupported" else 1


if __name__ == "__main__":
    raise SystemExit(main())
