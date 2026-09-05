import argparse
import json
from pathlib import Path

from .replay import replay_file


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m karajan.adapters.codex")
    commands = parser.add_subparsers(dest="command", required=True)
    replay = commands.add_parser("replay", help="Inspect an offline native-protocol replay.")
    replay.add_argument("file", type=Path)
    arguments = parser.parse_args()
    report = replay_file(arguments.file)
    print(json.dumps(report, sort_keys=True, ensure_ascii=True))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
