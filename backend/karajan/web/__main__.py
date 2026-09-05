"""Launch a loopback workbench with a private, one-use bootstrap file."""

import argparse
import csv
import os
import secrets
import subprocess
from pathlib import Path

import uvicorn

from .app import create_app


def _prepare_state(path: Path) -> Path:
    if path.is_symlink() or path.is_junction():
        raise ValueError("STATE_LINK_REJECTED")
    resolved = path.resolve()
    marker = resolved / "karajan-local-state.v1"
    if resolved.exists():
        if not marker.is_file() or marker.read_text() != "karajan-local-state.v1\n":
            raise ValueError("STATE_DIRECTORY_NOT_RECOGNIZED")
    else:
        resolved.mkdir(parents=True, mode=0o700)
        if os.name == "nt":
            identity = subprocess.run(
                ["whoami", "/user", "/fo", "csv", "/nh"],
                capture_output=True,
                text=True,
                check=True,
                timeout=5,
            )
            sid = next(csv.reader([identity.stdout.strip()]))[1]
            subprocess.run(
                ["icacls", str(resolved), "/inheritance:r", "/grant:r", f"*{sid}:(OI)(CI)F"],
                capture_output=True,
                check=True,
                timeout=5,
            )
        marker.write_text("karajan-local-state.v1\n")
    return resolved


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m karajan.web")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--state-directory", type=Path, required=True)
    serve.add_argument("--project-root", type=Path, action="append", default=[])
    serve.add_argument("--frontend-directory", type=Path)
    serve.add_argument("--port", type=int, default=8765)
    arguments = parser.parse_args()
    try:
        if not 1 <= arguments.port <= 65535:
            raise ValueError("PORT_INVALID")
        state = _prepare_state(arguments.state_directory)
        token = secrets.token_urlsafe(32)
        code_file = state / f"bootstrap-{secrets.token_hex(6)}.txt"
        descriptor = os.open(code_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w") as output:
            output.write(token + "\n")
        app = create_app(
            state,
            origin=f"http://127.0.0.1:{arguments.port}",
            bootstrap_token=token,
            allowed_roots=arguments.project_root,
            frontend_directory=arguments.frontend_directory,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        print(
            "Local workbench setup rejected. Check the dedicated state directory and local paths."
        )
        return 1
    print(f"Workbench: http://127.0.0.1:{arguments.port}", flush=True)
    print(f"One-use local access code file (expires in 10 minutes): {code_file}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=arguments.port, access_log=False, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
