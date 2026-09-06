"""An explicit local-only fixture runtime, never production admission evidence."""

import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any


class LocalFixtureRunner:
    def __init__(
        self,
        root: Path,
        *,
        check_outcome: str = "pass",
        review_verdict: str = "passed",
        worker_behavior: str = "complete",
    ) -> None:
        if check_outcome not in {"pass", "fail", "missing_log"} or review_verdict not in {
            "passed",
            "failed",
            "inconclusive",
        }:
            raise ValueError("FIXTURE_VARIANT_INVALID")
        if worker_behavior not in {"complete", "wait", "infrastructure_failure"}:
            raise ValueError("FIXTURE_VARIANT_INVALID")
        if root.is_symlink() or not root.is_dir():
            raise ValueError("FIXTURE_ROOT_INVALID")
        self.root = root.resolve()
        self.script = Path(__file__).with_name("_fixture_process.py").resolve()
        self.check_outcome = check_outcome
        self.review_verdict = review_verdict
        self.worker_behavior = worker_behavior

    def identity(self) -> str:
        material = {
            "runtime": "fixed-local-fixture-v1",
            "root": str(self.root),
            "python": sys.version,
            "executable": sys.executable,
            "script_sha256": hashlib.sha256(self.script.read_bytes()).hexdigest(),
            "check_outcome": self.check_outcome,
            "review_verdict": self.review_verdict,
            "worker_behavior": self.worker_behavior,
        }
        return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()

    def accepts(self, profile: dict[str, Any], repository: Path) -> bool:
        binding = profile["binding"]
        return bool(
            repository.resolve().is_relative_to(self.root)
            and binding["runtime_kind"] == "fixture-runtime"
            and binding["runtime_version"] == "1"
            and binding["model_id"] == "fixture-model"
            and binding["auth_mode"] == "none"
            and binding["billing_path"] == "subscription_only"
            and binding["native_settings"] == {}
        )

    def workspace(self, attempt_id: str) -> Path:
        return self.root / "workspaces" / attempt_id

    def safe_path(self, path: Path) -> bool:
        if not path.is_relative_to(self.root) or not path.resolve().is_relative_to(self.root):
            return False
        for candidate in (path, *path.parents):
            if candidate.is_relative_to(self.root):
                try:
                    info = candidate.lstat()
                except FileNotFoundError:
                    continue
                if (
                    stat.S_ISLNK(info.st_mode)
                    or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                ):
                    return False
        return True

    def materialize(self, baseline: dict[str, Any], destination: Path) -> None:
        if not destination.is_relative_to(self.root) or destination.exists():
            raise ValueError("FIXTURE_WORKSPACE_NOT_NEW")
        destination.mkdir(parents=True)
        for entry in baseline["manifest"]:
            content = Path(entry["artifact"]["path"]).read_bytes()
            if hashlib.sha256(content).hexdigest() != entry["artifact"]["sha256"]:
                raise ValueError("BASELINE_ARTIFACT_CHANGED")
            target = destination / entry["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            if os.name != "nt":
                target.chmod(0o755 if entry["mode"] == "100755" else 0o644)

    def log_path(self, attempt_id: str) -> Path:
        return self.root / "observations" / (attempt_id + ".json")

    def argv(
        self, operation: str, paths: list[str], attempt_id: str | None = None
    ) -> tuple[str, ...]:
        mode = (
            "write_wait" if operation == "write" and self.worker_behavior == "wait" else operation
        )
        if operation == "write" and self.worker_behavior == "infrastructure_failure":
            mode = "infrastructure_failure"
        argv: tuple[str, ...] = (sys.executable, "-I", str(self.script), mode, json.dumps(paths))
        if operation in {"check", "review"}:
            argv += (str(self.log_path(attempt_id)) if attempt_id else "{observation_log}",)
            argv += (self.check_outcome if operation == "check" else self.review_verdict,)
        return argv

    def read_log(self, attempt_id: str) -> bytes | None:
        path = self.log_path(attempt_id)
        return path.read_bytes() if path.is_file() else None

    def policy(
        self, paths: list[str], checks: list[str], reviewers: list[dict[str, Any]]
    ) -> dict[str, Any]:
        if set(checks) != {"tests", "independent_review"}:
            raise ValueError("FIXTURE_CHECK_POLICY_UNSUPPORTED")
        environment = hashlib.sha256((sys.version + str(sys.executable)).encode()).hexdigest()
        return {
            "id": "fixed-local-fixture",
            "revision": 1,
            "checks": [
                {
                    "id": "tests",
                    "revision": 1,
                    "argv": list(self.argv("check", paths)),
                    "environment_sha256": environment,
                }
            ],
            "review": {
                "revision": 1,
                "environment_sha256": environment,
                "approved_reviewers": [
                    {
                        "profile_id": reviewer["id"],
                        "profile_revision": reviewer["revision"],
                        "model_family": reviewer["model_family"],
                        "qualification_ref": "fixture:fixed-runtime-not-live-qualified",
                    }
                    for reviewer in reviewers
                ],
            },
        }
