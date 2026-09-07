"""Trusted local supervisor. No model request or business retry is implemented."""

import os
import subprocess
import sys
import time
from pathlib import Path

from karajan.contracts.probe import AttemptManifest

from ._platform import ProcessGroup, process_identity
from .host import RunnerHost


def supervise(database: Path, start_key: str) -> None:
    import json

    host = RunnerHost(database.parent, existing_only=True)
    with host._connect() as connection:
        row = connection.execute(
            "SELECT * FROM executions WHERE start_key = ?", (start_key,)
        ).fetchone()
    if row is None or row["state"] != "starting":
        return
    identity = process_identity(os.getpid())
    if identity is None:
        return
    group = ProcessGroup(row["nonce"], identity.pid, create=True)
    try:
        with host._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT cancelled, activation FROM executions WHERE start_key=?", (start_key,)
            ).fetchone()
            manifest = AttemptManifest.model_validate_json(row["manifest"])
            control = connection.execute(
                "SELECT * FROM controls WHERE attempt_id=?", (manifest.id,)
            ).fetchone()
            may_launch = (
                not current["cancelled"]
                and control is not None
                and control["dispatch_enabled"]
                and control["fence"] == manifest.fence
                and control["authorization_ref"] == manifest.authorization_ref
                and json.loads(current["activation"])["expires_at"] > time.time()
            )
            connection.execute(
                "UPDATE executions SET supervisor_pid=?, supervisor_birth=?, "
                "containment_ready=1, state=? WHERE start_key=? AND state='starting'",
                (identity.pid, identity.birth, "running" if may_launch else "finished", start_key),
            )
        if not may_launch:
            return
        spec = json.loads(row["spec"])
        with (database.parent / f"{row['nonce']}.tool.log").open("ab") as log:
            process = subprocess.Popen(
                spec["argv"],
                cwd=spec["cwd"],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            runner = process_identity(process.pid)
            if runner is not None:
                with host._connect(existing_only=True) as connection:
                    connection.execute(
                        "UPDATE executions SET runner_pid=?, runner_birth=? WHERE start_key=? "
                        "AND state='running' AND runner_pid IS NULL AND supervisor_pid=? "
                        "AND supervisor_birth=?",
                        (runner.pid, runner.birth, start_key, identity.pid, identity.birth),
                    )
            deadline = time.monotonic() + spec["timeout_seconds"]
            while True:
                with host._connect() as connection:
                    cancelled = connection.execute(
                        "SELECT cancelled FROM executions WHERE start_key=?", (start_key,)
                    ).fetchone()[0]
                if cancelled or time.monotonic() >= deadline:
                    group.terminate()
                    return
                if process.poll() is not None and len(group.members()) <= 1:
                    break
                time.sleep(0.02)
        with host._connect() as connection:
            connection.execute(
                "UPDATE executions SET state='finished', exit_code=? WHERE start_key=?",
                (process.returncode, start_key),
            )
    finally:
        group.close()


if __name__ == "__main__":
    supervise(Path(sys.argv[1]), sys.argv[2])
