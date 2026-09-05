"""The deliberately narrow offline qualification entry point."""

import argparse
import json
from pathlib import Path

from karajan.contracts.probe import inspect_probe_file


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m karajan")
    commands = parser.add_subparsers(dest="command", required=True)
    probe = commands.add_parser("probe", help="Validate an offline probe document.")
    probe.add_argument("file", type=Path)
    arguments = parser.parse_args()
    report = inspect_probe_file(arguments.file)
    print(json.dumps(report, ensure_ascii=True, sort_keys=True))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
