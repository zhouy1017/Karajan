"""A bounded child outlives its direct parent; no network or model service is used."""

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--child", action="store_true")
    arguments = parser.parse_args()
    if not arguments.child:
        # The parent intentionally exits; RunnerHost must still observe the child.
        subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--output",
                str(arguments.output),
                "--child",
            ]
        )
        return 0
    for _ in range(120):
        with arguments.output.open("a", encoding="utf-8") as stream:
            stream.write(f"{os.getpid()} heartbeat\n")
        time.sleep(0.05)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
