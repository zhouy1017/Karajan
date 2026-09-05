"""Explicit local probe commands; this entry point never enables a model account."""

import argparse
import json
import os
from dataclasses import asdict
from pathlib import Path

from karajan.contracts.probe import AttemptManifest

from .host import Activation, ProbeCrash, ProcessSpec, RunnerHost


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m karajan.execution")
    parser.add_argument("--state", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("document", type=Path)
    control = commands.add_parser("control")
    control.add_argument("document", type=Path)
    start = commands.add_parser("start")
    start.add_argument("prepared_id")
    start.add_argument("activation_file", type=Path)
    start.add_argument(
        "--crash-at", choices=["after_accept", "before_spawn", "after_spawn", "after_ack"]
    )
    inspect = commands.add_parser("inspect")
    inspect.add_argument("attempt_id")
    cancel = commands.add_parser("cancel")
    cancel.add_argument("attempt_id")
    cancel.add_argument("cancel_key")
    cancel.add_argument("--wait-seconds", type=float, default=3.0)
    commands.add_parser("reconcile")
    arguments = parser.parse_args()
    host = RunnerHost(arguments.state)
    code = 0
    data: object
    try:
        if arguments.command == "prepare":
            document = json.loads(arguments.document.read_text(encoding="utf-8"))
            if set(document) != {"start_key", "manifest", "process"}:
                raise ValueError("Invalid preparation fields.")
            process = document["process"]
            if not {"argv", "cwd"} <= set(process) <= {"argv", "cwd", "timeout_seconds"}:
                raise ValueError("Invalid process fields.")
            if not isinstance(process["argv"], list):
                raise ValueError("Argument vector must be a JSON array.")
            spec = ProcessSpec(
                tuple(process["argv"]), Path(process["cwd"]), process.get("timeout_seconds", 30.0)
            )
            data = asdict(
                host.prepare(
                    AttemptManifest.model_validate_json(json.dumps(document["manifest"])),
                    document["start_key"],
                    spec,
                )
            )
        elif arguments.command == "control":
            host.set_control(**json.loads(arguments.document.read_text(encoding="utf-8")))
            data = {"control_updated": True}
        elif arguments.command == "start":
            activation = Activation(
                **json.loads(arguments.activation_file.read_text(encoding="utf-8"))
            )
            data = asdict(
                host.start(arguments.prepared_id, activation, crash_at=arguments.crash_at)
            )
        elif arguments.command == "inspect":
            data = asdict(host.inspect(arguments.attempt_id))
        elif arguments.command == "cancel":
            cancellation = host.cancel(
                arguments.attempt_id, arguments.cancel_key, timeout_seconds=arguments.wait_seconds
            )
            data = asdict(cancellation)
            code = 0 if cancellation.status == "confirmed" else 2
        else:
            data = [asdict(snapshot) for snapshot in host.reconcile()]
    except ProbeCrash:
        os._exit(91)
    except (ValueError, KeyError, TypeError, OSError):
        print(json.dumps({"error": "PROBE_COMMAND_REJECTED", "live_qualified": False}))
        return 1
    print(json.dumps({"scope": "local_process_probe", "live_qualified": False, "data": data}))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
