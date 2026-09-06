"""Local protocol fixture entry point; accepts no API endpoint or credentials."""

import argparse
import json
from pathlib import Path

from .offline import DeepSeekOfflineProbe
from .protocol import ProtocolError


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m karajan.adapters.deepseek")
    commands = parser.add_subparsers(dest="command", required=True)
    probe = commands.add_parser("probe")
    probe.add_argument("path", type=Path)
    probe.add_argument("--runtime", type=Path, required=True)
    probe.add_argument("--directory", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        report = DeepSeekOfflineProbe(arguments.runtime, arguments.directory).run_file(
            arguments.path
        )
    except (ProtocolError, OSError) as error:
        print(
            json.dumps(
                {
                    "status": "rejected",
                    "reason_codes": [
                        str(error)
                        if isinstance(error, ProtocolError)
                        else "LOCAL_INPUT_OR_STATE_UNAVAILABLE"
                    ],
                    "live_qualification": "not_run",
                    "cash_api_calls": 0,
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
                    "runtime_version",
                    "conditions",
                    "live_qualification",
                    "cash_api_calls",
                    "profile_enabled",
                )
            },
            sort_keys=True,
        )
    )
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
