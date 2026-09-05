"""Prepare fresh, synthetic CLI inputs; do not start processes or access credentials."""

import argparse
import json
import sys
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    arguments = parser.parse_args()
    directory = arguments.directory.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    workspace = directory / "workspace"
    workspace.mkdir()
    root = Path(__file__).resolve().parents[2]
    source = json.loads((root / "examples/probes/fixture-passed.json").read_text(encoding="utf-8"))
    attempt = source["attempt"]
    documents = {
        "prepare.json": {
            "start_key": "runnerhost-demo-start",
            "manifest": attempt,
            "process": {
                "argv": [
                    sys.executable,
                    str(Path(__file__).with_name("fixture.py").resolve()),
                    "--output",
                    str(workspace / "heartbeat.txt"),
                ],
                "cwd": str(workspace),
                "timeout_seconds": 5,
            },
        },
        "control.json": {
            "attempt_id": attempt["id"],
            "fence": attempt["fence"],
            "authorization_ref": attempt["authorization_ref"],
            "dispatch_enabled": True,
        },
        "activation.json": {
            "id": "runnerhost-demo-activation",
            "attempt_id": attempt["id"],
            "fence": attempt["fence"],
            "authorization_ref": attempt["authorization_ref"],
            "budget_ref": attempt["budget_ref"],
            "expires_at": time.time() + 300,
        },
    }
    for name, document in documents.items():
        (directory / name).write_text(json.dumps(document, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "directory": str(directory),
                "attempt_id": attempt["id"],
                "start_key": "runnerhost-demo-start",
                "live_qualified": False,
                "note": "Synthetic inputs only; no runtime or model has been started.",
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
