"""Replay a supplied Claude fragment without starting Claude or reading authentication."""

import argparse
import json
from pathlib import Path

from .replay import replay_file


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m karajan.adapters.claude")
    commands = parser.add_subparsers(dest="command", required=True)
    replay = commands.add_parser("replay")
    replay.add_argument("path", type=Path)
    arguments = parser.parse_args()
    report = replay_file(arguments.path)
    print(json.dumps(report, ensure_ascii=True, sort_keys=True))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
