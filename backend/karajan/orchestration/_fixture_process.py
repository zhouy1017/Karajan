"""Fixed, network-free synthetic process. This is not an agent or a sandbox."""

import json
import subprocess
import sys
import time
from pathlib import Path


def main() -> None:
    paths = json.loads(sys.argv[2])
    expected = b"print('fixture candidate')\n"
    operation = sys.argv[1]
    log = Path(sys.argv[3]) if len(sys.argv) > 3 else None
    if operation == "infrastructure_failure":
        print("synthetic fixed fixture infrastructure failure", flush=True)
        raise SystemExit(75)
    elif operation in {"write", "write_wait"}:
        for relative in paths:
            target = Path(relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("ab") as stream:
                stream.write(expected)
        print("fixed fixture writer completed", flush=True)
        if operation == "write_wait":
            subprocess.Popen([sys.executable, "-I", __file__, "heartbeat", json.dumps(paths)])
    elif operation == "heartbeat":
        while True:
            with Path(paths[0]).open("ab") as stream:
                stream.write(b"# fixture heartbeat\n")
            time.sleep(0.02)
    elif operation in {"check", "review"}:
        variant = sys.argv[4]
        passed = variant not in {"fail", "failed"} and all(
            Path(relative).read_bytes() == expected for relative in paths
        )
        verdict = "inconclusive" if variant == "inconclusive" else "passed" if passed else "failed"
        observation = {
            "operation": operation,
            "verdict": verdict,
            "synthetic": True,
            "files": paths,
            "author_reasoning_included": False,
        }
        content = json.dumps(observation).encode() + b"\n"
        if log is not None and variant != "missing_log":
            log.write_bytes(content)
        print(content.decode(), flush=True)
        if not passed:
            raise SystemExit(1)
    else:
        raise ValueError("Unsupported fixture operation")


if __name__ == "__main__":
    main()
