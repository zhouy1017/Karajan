"""No live-provider command or endpoint option is exposed by this offline CLI."""

import argparse
import json
from pathlib import Path

from .probe import run_probe
from .store import CapacityError


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m karajan.capacity")
    commands = parser.add_subparsers(dest="command", required=True)
    probe = commands.add_parser("probe")
    probe.add_argument("case", type=Path)
    probe.add_argument("--directory", required=True, type=Path)
    args = parser.parse_args()
    try:
        report = run_probe(args.case, args.directory)
    except (CapacityError, OSError) as error:
        print(
            json.dumps(
                {
                    "status": "rejected",
                    "reason": str(error)
                    if isinstance(error, CapacityError)
                    else "LOCAL_INPUT_OR_STATE_UNAVAILABLE",
                    "live_qualification": "not_run",
                    "cash_api_calls": 0,
                }
            )
        )
        return 2
    print(
        json.dumps(
            {
                key: report[key]
                for key in (
                    "case_status",
                    "receiver_count",
                    "blocked_request_receiver_count",
                    "live_qualification",
                    "cash_api_calls",
                )
            }
        )
    )
    return 0 if report["case_status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
