"""Real local Git/process probe; reviewer input is explicitly a fixture."""

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from karajan.candidates import CandidateError, CandidateStore


def git(directory: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(directory), *arguments], capture_output=True, check=True, timeout=20
    )
    return result.stdout.decode().strip()


def context(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        key: candidate[key]
        for key in ("repository_identity", "base_sha", "input_sha256", "policy_sha256")
    }


def probe(directory: Path, input_file: Path) -> dict[str, Any]:
    if directory.exists():
        raise CandidateError("PROBE_DIRECTORY_EXISTS")
    request = json.loads(input_file.read_text(encoding="utf-8"))
    directory.mkdir(parents=True)
    repository = directory / "trusted"
    repository.mkdir()
    git(repository, "init", "-q")
    git(repository, "config", "user.name", "Karajan offline fixture")
    git(repository, "config", "user.email", "fixture@example.invalid")
    git(repository, "config", "core.autocrlf", "false")
    (repository / "app.py").write_bytes(b"print('base')\n")
    git(repository, "add", "app.py")
    git(repository, "commit", "-qm", "fixed fixture baseline")
    base_sha = git(repository, "rev-parse", "HEAD")
    store = CandidateStore(directory / "state")
    baseline = store.register_baseline(
        repository, repository_identity="fixture-repository", base_sha=base_sha
    )
    request["baseline_id"] = baseline["id"]
    request["policy"]["checks"][0]["argv"][0] = sys.executable
    request["policy"]["checks"][0]["environment_sha256"] = hashlib.sha256(
        json.dumps(
            {"python": sys.version, "platform": platform.platform()}, sort_keys=True
        ).encode()
    ).hexdigest()
    workspace = directory / "worker"
    shutil.copytree(repository, workspace)
    (workspace / "app.py").write_bytes(b"print('candidate')\n")
    git(workspace, "add", "app.py")
    expected_tree = git(workspace, "write-tree")
    # The Collector must remain usable even when worker config cannot be parsed.
    (workspace / ".git" / "config").write_bytes(b"INVALID WORKER CONFIG\x00")
    candidate = store.freeze(workspace, request)

    def check(value: dict[str, Any], key: str) -> dict[str, Any]:
        destination = directory / key
        store.materialize(value["id"], destination)
        result = subprocess.run(
            request["policy"]["checks"][0]["argv"],
            cwd=destination,
            capture_output=True,
            timeout=10,
            env={
                name: value
                for name, value in os.environ.items()
                if name.upper() in {"SYSTEMROOT", "PATH", "TEMP", "TMP"}
            },
        )
        return store.record_check(
            {
                "evidence_key": key,
                "candidate_id": value["id"],
                "policy_sha256": value["policy_sha256"],
                "input_sha256": value["input_sha256"],
                "environment_sha256": request["policy"]["checks"][0]["environment_sha256"],
                "check_id": "content-check",
                "check_revision": 1,
                "executor_ref": "probe:python-process",
                "exit_code": result.returncode,
                "outcome": "completed",
                "observation_ref": "probe:" + key,
                "provenance": "trusted_observation",
            },
            log=result.stdout + result.stderr,
        )

    passed_check = check(candidate, "passing-check")
    review = store.record_review(
        {
            "evidence_key": "fixture-review",
            "candidate_id": candidate["id"],
            "policy_sha256": candidate["policy_sha256"],
            "input_sha256": candidate["input_sha256"],
            "environment_sha256": request["policy"]["review"]["environment_sha256"],
            "review_revision": 1,
            "check_evidence_ids": [passed_check["id"]],
            "actor": {
                "attempt_id": "fixture-review-attempt",
                "fence": 1,
                "profile_id": "fixture-fast",
                "profile_revision": 1,
                "model_family": "fixture-family-a",
                "context_id": "fixture-fresh-context",
                "provenance_ref": "fixture:context",
            },
            "author_reasoning_included": False,
            "verdict": "passed",
            "findings": [],
            "observation_ref": "fixture:review-result",
            "provenance": "fixture",
        },
        log=b"Explicit fixture review: no model invoked.\n",
    )
    initially_passed = store.gate(candidate["id"], current=context(candidate))
    (workspace / "app.py").write_bytes(b"print('broken')\n")
    changed = store.freeze(workspace, request)
    failed_check = check(changed, "failing-check")
    after_change = store.gate(candidate["id"], current=context(candidate))
    new_gate = store.gate(changed["id"], current=context(changed))
    source_root = Path(__file__).resolve().parents[2]
    source_files = [
        *sorted((source_root / "backend/karajan/candidates").glob("*.py")),
        Path(__file__).resolve(),
    ]
    conditions = {
        "real_git_tree_matches": candidate["tree_sha"] == expected_tree,
        "actual_check_process_passed": passed_check["status"] == "passed",
        "fixture_review_only": review["input"]["provenance"] == "fixture",
        "local_gate_initially_passed": initially_passed["local_gate_passed"],
        "new_candidate_created": changed["revision"] == candidate["revision"] + 1,
        "old_gate_invalidated": "CANDIDATE_SUPERSEDED" in after_change["reasons"],
        "actual_check_process_failed": failed_check["status"] == "failed",
        "new_gate_blocked": not new_gate["local_gate_passed"],
        "no_production_delivery_claim": not initially_passed["delivery_eligible"],
        "trusted_baseline_unchanged": git(repository, "rev-parse", "HEAD") == base_sha
        and not git(repository, "status", "--porcelain"),
    }
    return {
        "schema_version": "karajan.candidate-probe.v1",
        "case_id": "real-git-local-validation",
        "recorded_at": datetime.now(UTC).isoformat(),
        "os": platform.platform(),
        "isolation": "local_guarded",
        "status": "passed" if all(conditions.values()) else "failed",
        "conditions": conditions,
        "input_sha256": hashlib.sha256(input_file.read_bytes()).hexdigest(),
        "source_sha256": {
            str(path.relative_to(source_root)).replace("\\", "/"): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in source_files
        },
        "candidate": candidate,
        "changed_candidate": changed,
        "passing_check": passed_check,
        "fixture_review": review,
        "failing_check": failed_check,
        "initial_gate": initially_passed,
        "old_gate_after_change": after_change,
        "new_gate": new_gate,
        "model_calls": 0,
        "cash_api_calls": 0,
        "live_qualification": "not_run",
        "limitations": [
            "Review and writer-stop/qualification attestations are fixtures.",
            "Only fixed local fixture code was executed; no OS sandbox qualification.",
            "No Agent scheduling, RunnerHost integration or delivery activation.",
            "No GitHub PR or CI integration.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--input", type=Path, default=Path(__file__).with_name("freeze-input.json"))
    args = parser.parse_args()
    try:
        result = probe(args.directory.resolve(), args.input)
        encoded = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
        (args.directory / "report.json").write_text(encoded, encoding="utf-8", newline="\n")
        print(encoded, end="")
        return 0 if result["status"] == "passed" else 1
    except CandidateError as error:
        print(json.dumps({"status": "failed", "reason": error.code}))
    except (OSError, ValueError, subprocess.SubprocessError):
        print(json.dumps({"status": "failed", "reason": "PROBE_INPUT_OR_ENVIRONMENT_INVALID"}))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
