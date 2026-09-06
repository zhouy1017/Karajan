"""Credential-free local bare Git adapter for explicit offline fixtures only."""

import os
import subprocess
from pathlib import Path
from typing import Any

from .errors import DeliveryError, RemoteUnknown


class LocalGitRemote:
    execution_scope = "offline_fixture"

    def __init__(self, trusted_repository: Path, bare_remote: Path, *, repository_id: str) -> None:
        self.source = Path(trusted_repository).resolve() / ".git"
        self.remote = Path(bare_remote).resolve()
        self.repository_id = repository_id
        if not self.source.is_dir() or not self.remote.is_dir():
            raise DeliveryError("LOCAL_REPOSITORY_REQUIRED")
        if self._run(self.remote, ["rev-parse", "--is-bare-repository"]) != "true":
            raise DeliveryError("LOCAL_BARE_REMOTE_REQUIRED")

    def _run(
        self, repository: Path, arguments: list[str], *, missing_allowed: bool = False
    ) -> str | None:
        environment = {
            key: value
            for key, value in os.environ.items()
            if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}
        }
        environment.update(
            {
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "0",
            }
        )
        command = [
            "git",
            "--no-replace-objects",
            "--git-dir=" + str(repository),
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "credential.helper=",
            "-c",
            "http.proxy=",
            "-c",
            "protocol.allow=never",
            "-c",
            "protocol.file.allow=always",
            *arguments,
        ]
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, env=environment, timeout=20
            )
        except (OSError, subprocess.TimeoutExpired):
            raise RemoteUnknown("GIT_RESULT_UNKNOWN") from None
        if result.returncode == 0:
            return result.stdout.strip()
        if missing_allowed and result.returncode == 1:
            return None
        raise RemoteUnknown("GIT_RESULT_UNKNOWN")

    def inspect(self, binding: dict[str, Any]) -> dict[str, Any]:
        if binding["repository_id"] != self.repository_id:
            raise DeliveryError("REMOTE_IDENTITY_MISMATCH")

        def head(branch: str) -> str | None:
            ref = "refs/heads/" + branch
            if (
                self._run(
                    self.remote, ["show-ref", "--verify", "--quiet", ref], missing_allowed=True
                )
                is None
            ):
                return None
            return self._run(self.remote, ["rev-parse", "--verify", ref])

        return {
            "repository_id": self.repository_id,
            "head_sha": head(binding["managed_branch"]),
            "base_sha": head(binding["base_branch"]),
        }

    def validate(self, binding: dict[str, Any]) -> dict[str, Any]:
        observation = self.inspect(binding)
        if observation["base_sha"] != binding["tested_base_sha"]:
            raise DeliveryError("TESTED_BASE_CHANGED")
        if observation["head_sha"] != binding["expected_old_sha"]:
            raise DeliveryError("REMOTE_HEAD_CHANGED")
        if (
            self._run(self.source, ["rev-parse", "--verify", binding["commit_sha"] + "^{tree}"])
            != binding["tree_sha"]
        ):
            raise DeliveryError("CANDIDATE_COMMIT_MISMATCH")
        expected = binding["expected_old_sha"] or ""
        if (
            expected
            and self._run(
                self.source,
                ["merge-base", "--is-ancestor", expected, binding["commit_sha"]],
                missing_allowed=True,
            )
            is None
        ):
            raise DeliveryError("NON_FAST_FORWARD_FORBIDDEN")
        return observation

    def push(self, binding: dict[str, Any]) -> dict[str, Any]:
        self.validate(binding)
        ref = "refs/heads/" + binding["managed_branch"]
        expected = binding["expected_old_sha"] or ""
        self._run(
            self.source,
            [
                "push",
                "--porcelain",
                "--receive-pack=git -c core.hooksPath=/dev/null "
                "-c core.fsmonitor=false -c credential.helper= receive-pack",
                "--force-with-lease=" + ref + ":" + expected,
                str(self.remote),
                binding["commit_sha"] + ":" + ref,
            ],
        )
        return self.inspect(binding)
