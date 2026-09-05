"""Run the documented public API against a new, disposable local Git repository."""

import argparse
import hashlib
import json
import platform
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from karajan.projects import ProjectError, ProjectRegistry


def git(repository: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(repository), *arguments],
        capture_output=True,
        check=True,
        timeout=10,
    )


def fingerprint(repository: Path) -> dict[str, str]:
    return {
        str(path.relative_to(repository)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(repository.rglob("*"))
        if path.is_file()
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    arguments = parser.parse_args()
    directory = arguments.directory.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    repository = directory / "repositories" / "example"
    repository.mkdir(parents=True)
    state = directory / "control-state"
    state.mkdir()
    git(repository, "init", "--initial-branch=main")
    (repository / "example.txt").write_text("fixture before registration\n", encoding="utf-8")
    git(repository, "add", "example.txt")
    git(
        repository,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-m",
        "fixture",
    )
    before = fingerprint(repository)
    request = {
        "name": "Offline fixture",
        "repository_path": str(repository),
        "base_ref": "main",
        "target_branch": "main",
        "allowed_target_branches": ["main"],
    }
    service = ProjectRegistry(state / "projects.sqlite", [repository.parent])
    created = service.create(request, command_key="create", principal="fixture-owner")
    replay = service.create(request, command_key="create", principal="fixture-owner")
    source = Path(__file__).with_name("offline-configuration.json")
    configuration = json.loads(source.read_text(encoding="utf-8"))
    preview = service.preview_configuration(
        created["id"], configuration, command_key="preview", principal="fixture-owner"
    )
    applied = service.apply_configuration(
        created["id"],
        preview["preview_id"],
        expected_revision=1,
        command_key="apply",
        principal="fixture-owner",
    )
    task = {
        "role": "worker",
        "readiness": "T0",
        "complexity": "T1",
        "risk": "critical",
        "approved_profile_refs": configuration["approved_profile_refs"],
    }
    waiting = service.evaluate_task(created["id"], task)
    critical = service.evaluate_task(created["id"], {**task, "readiness": "ready"})
    canary = "FAKE-REGISTRY-CREDENTIAL-CANARY"
    rejected = service.preview_configuration(
        created["id"],
        {**configuration, "api_key": canary},
        command_key="credential-preview",
        principal="fixture-owner",
    )
    rejection = None
    try:
        service.apply_configuration(
            created["id"],
            rejected["preview_id"],
            expected_revision=2,
            command_key="credential-apply",
            principal="fixture-owner",
        )
    except ProjectError as error:
        rejection = error.code
    exported = service.get_configuration(created["id"])
    after = fingerprint(repository)
    conditions = {
        "same_command_same_result": replay == created,
        "configuration_round_trip": exported["configuration"] == configuration,
        "preview_is_offline_valid": preview["status"] == "offline_valid",
        "t0_blocked": waiting["reason_codes"] == ["TASK_NOT_READY"],
        "critical_promoted_to_t3": critical["effective_class"] == "T3"
        and critical["rule_id"] == "critical-worker",
        "credential_not_storable": rejected["can_apply"] is False
        and rejection == "CONFIGURATION_NOT_STORABLE",
        "credential_not_exported": canary not in json.dumps(exported),
        "credential_not_persisted": canary.encode() not in (state / "projects.sqlite").read_bytes(),
        "rejected_apply_keeps_revision": service.get(created["id"]) == applied,
        "repository_files_unchanged": before == after,
    }
    report = {
        "schema_version": "karajan.project-registration-probe.v1",
        "status": "passed" if all(conditions.values()) else "failed",
        "observed_at": datetime.now(UTC).isoformat(),
        "os": platform.system(),
        "python_version": platform.python_version(),
        "qualification_scope": "offline_configuration",
        "live_qualified": False,
        "cash_api_status": "not_run",
        "configuration_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "implementation_sha256": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(
                (Path(__file__).resolve().parents[2] / "backend/karajan/projects").glob("*.py")
            )
        },
        "resolved_base_sha": created["repository"]["base_sha"],
        "repository_file_hashes_before": before,
        "repository_file_hashes_after": after,
        "conditions": conditions,
        "preview": preview,
        "t0_outcome": waiting,
        "critical_outcome": critical,
        "credential_rejection": rejection,
        "limitations": [
            "Uses real local Git and SQLite with fixture model/evidence references.",
            "No model starts; HTTP authentication, OS isolation "
            "and live qualification are untested.",
            "Temporary paths are omitted; the provided directory retains original artifacts.",
        ],
    }
    output = json.dumps(report, indent=2) + "\n"
    (directory / "report.json").write_text(output, encoding="utf-8", newline="\n")
    print(output, end="")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
