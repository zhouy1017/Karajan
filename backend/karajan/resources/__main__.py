"""Standalone evidence command; no changes to Karajan's other CLI entry points."""

import argparse
import json
from pathlib import Path

from .local_probe import run_demo


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m karajan.resources")
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="Run local fake-provider resource probes.")
    demo.add_argument("--scenario", type=Path, required=True)
    demo.add_argument("--directory", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        report = run_demo(arguments.scenario, arguments.directory)
    except (OSError, ValueError, TypeError) as error:
        report = {
            "schema_version": "karajan.resource_probe.report.v1",
            "status": "failed",
            "reason_code": type(error).__name__,
            "live_qualified": False,
            "cash_api_enabled": False,
        }
    print(json.dumps(report, ensure_ascii=True, sort_keys=True))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
