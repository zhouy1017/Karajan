"""Trusted local control CLI; no Commander generation or runtime launch."""

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

from karajan.projects import ProjectError, ProjectRegistry

from .planning import RunError, RunPlanner


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m karajan.runs")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--projects", type=Path, required=True)
    parser.add_argument("--allowed-root", type=Path, action="append", required=True)
    parser.add_argument("--principal", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list").add_argument("--project-id")
    commands.add_parser("get").add_argument("--run-id", required=True)
    for name in ("create", "approve-plan", "decide-handoff"):
        command = commands.add_parser(name)
        command.add_argument("--input", type=Path, required=True)
        command.add_argument("--command-key", required=True)
        if name != "create":
            command.add_argument("--run-id", required=True)
    arguments = parser.parse_args()
    try:
        if not arguments.projects.is_file():
            raise RunError("PROJECT_DATABASE_REQUIRED")
        projects = ProjectRegistry(arguments.projects, arguments.allowed_root)
        planner = RunPlanner(arguments.database, projects)
        result: Any
        if arguments.command == "list":
            result = planner.list(principal=arguments.principal, project_id=arguments.project_id)
        elif arguments.command == "get":
            result = planner.get(arguments.run_id, principal=arguments.principal)
        else:
            content = arguments.input.read_bytes()
            if len(content) > 1_000_000:
                raise RunError("PLANNING_INPUT_TOO_LARGE")
            request = json.loads(content)
            if arguments.command == "create":
                result = planner.create(
                    request, command_key=arguments.command_key, principal=arguments.principal
                )
            else:
                operation = (
                    planner.approve_plan
                    if arguments.command == "approve-plan"
                    else planner.decide_handoff
                )
                result = operation(
                    arguments.run_id,
                    request,
                    command_key=arguments.command_key,
                    principal=arguments.principal,
                )
        print(json.dumps(result, sort_keys=True))
        return 0
    except (RunError, ProjectError) as error:
        reason = error.code
    except (OSError, ValueError, sqlite3.Error):
        reason = "LOCAL_INPUT_OR_DATABASE_UNAVAILABLE"
    print(json.dumps({"status": "failed", "reason_code": reason, "dispatch_enabled": False}))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
