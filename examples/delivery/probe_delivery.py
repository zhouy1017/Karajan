"""Real local Git + persistent PR test double, without external credentials or API calls."""

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from karajan.delivery import DeliveryCoordinator, LocalGitRemote, RemoteUnknown


class FilePullRequests:
    execution_scope = "offline_fixture"

    def __init__(self, path: Path, remote: LocalGitRemote) -> None:
        self.path, self.remote = path, remote

    def lookup(self, binding: dict[str, Any]) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        pr = json.loads(self.path.read_text())
        pr["head_sha"] = self.remote.inspect(binding)["head_sha"]
        return [pr]

    def publish(self, binding: dict[str, Any], existing_id: str | None) -> dict[str, Any]:
        if existing_id is not None:
            return self.lookup(binding)[0]
        if self.path.exists():
            raise AssertionError("The probe must never create a second PR")
        pr = {
            "id": "offline-pr-1",
            **{
                key: binding[key]
                for key in ("run_id", "repository_id", "managed_branch", "base_branch")
            },
            "head_sha": binding["commit_sha"],
            "state": "open",
            "merged": False,
            "ci_sha": binding["commit_sha"],
            "ci_status": "pending",
        }
        self.path.write_text(json.dumps(pr), encoding="utf-8")
        raise RemoteUnknown("Fixture response lost after persistent PR creation")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    directory = Path(tempfile.mkdtemp(prefix="karajan-delivery-probe-"))
    source = directory / "trusted"
    source.mkdir()
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}
    }
    environment.update(
        GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0"
    )

    def git(*arguments: str) -> str:
        return subprocess.run(
            [
                "git",
                "-C",
                str(source),
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "-c",
                "commit.gpgsign=false",
                *arguments,
            ],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
            timeout=20,
        ).stdout.strip()

    git("init", "-q", "--initial-branch=main")
    (source / "app.txt").write_bytes(b"base\n")
    git("add", ".")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    remote_path = directory / "remote.git"
    git("clone", "--bare", str(source), str(remote_path))
    (source / "app.txt").write_bytes(b"candidate\n")
    git("add", ".")
    git("commit", "-qm", "candidate")
    binding = {
        "run_id": "offline-run-1",
        "delivery_revision": 1,
        "repository_id": "offline-repo",
        "managed_branch": "codex/karajan-offline-run-1",
        "base_branch": "main",
        "tested_base_sha": base,
        "candidate_id": "fixture-candidate",
        "content_sha256": hashlib.sha256(b"candidate\n").hexdigest(),
        "tree_sha": git("rev-parse", "HEAD^{tree}"),
        "commit_sha": git("rev-parse", "HEAD"),
        "authorization_sha256": "a" * 64,
        "evidence_sha256": "b" * 64,
        "verification_ref": "fixture-verification",
        "expected_old_sha": None,
        "require_ci": True,
    }
    receipts: dict[str, dict[str, Any]] = {}
    remote = LocalGitRemote(source, remote_path, repository_id="offline-repo")
    pr_file = directory / "pr.json"

    def reopen() -> DeliveryCoordinator:
        return DeliveryCoordinator(
            directory / "delivery.sqlite",
            git_remote=remote,
            pr_service=FilePullRequests(pr_file, remote),
            verification_reader=receipts.__getitem__,
            mode="offline_fixture",
        )

    coordinator = reopen()
    intent = coordinator.plan(binding, command_key="plan", principal="controller")
    receipts[binding["verification_ref"]] = {
        "receipt_ref": binding["verification_ref"],
        "binding_sha256": intent["binding_sha256"],
        "authority_revision": "offline-scripted-authority",
        "decision": "allow",
        "provenance": "fixture",
    }
    transitions = [intent]
    transitions.append(coordinator.advance(intent["id"], principal="controller"))
    transitions.append(coordinator.advance(intent["id"], principal="controller"))
    coordinator = reopen()
    transitions.append(coordinator.advance(intent["id"], principal="controller"))
    pr = json.loads(pr_file.read_text())
    pr.update(ci_status="success", ci_sha=base)
    pr_file.write_text(json.dumps(pr), encoding="utf-8")
    transitions.append(coordinator.advance(intent["id"], principal="controller"))
    pr["ci_sha"] = binding["commit_sha"]
    pr_file.write_text(json.dumps(pr), encoding="utf-8")
    transitions.append(coordinator.advance(intent["id"], principal="controller"))
    states = [item["state"] for item in transitions]
    expected = ["planned", "pushed", "reconciling", "awaiting_ci", "awaiting_ci", "delivered"]
    assert states == expected
    assert transitions[-1]["pr"]["id"] == "offline-pr-1"
    assert transitions[-1]["merge"]["merged"] is False
    report = {
        "schema_version": "karajan.delivery-probe.v1",
        "result": "passed",
        "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "execution_scope": "offline_fixture",
        "live_qualification": "not_run",
        "cash_api_calls": 0,
        "model_calls": 0,
        "external_pr_calls": 0,
        "real_local_git": True,
        "pr_double": "persistent_local_json",
        "verification": "scripted_fixture_receipt_not_real_candidate_review",
        "states": states,
        "transitions": transitions,
        "remote": remote.inspect(binding),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps({"result": report["result"], "states": states, "live_qualification": "not_run"})
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
