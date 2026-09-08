"""Prepare the #153 demo and optionally run one independent Commander qualification.

The live path is deliberately explicit.  Without ``--live`` this command only
validates public paths and prints the bounded action it would take; it never
opens the credential file or contacts a provider.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


SCOPE = "commander_planning.v1"
AUTH_REF = "secret:go-commander"
SOURCE_ID = "opencode-go"
SUITE_REF = {"id": "opencode-go-commander-planning-linux", "revision": 2}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _asset_root(root: Path) -> Path:
    for candidate in (root, *root.parents):
        if (candidate / ".cache" / "go-linux-runtime").exists():
            return candidate
    return root


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="allow credential/provider work")
    parser.add_argument(
        "--runtime",
        type=Path,
        default=None,
        help="fixed Linux Go runtime executable",
    )
    parser.add_argument("--tokenizer-directory", type=Path, default=None)
    parser.add_argument("--credential-file", type=Path, default=None)
    parser.add_argument(
        "--directory",
        type=Path,
        default=None,
        help="new, dedicated demo state directory",
    )
    return parser


def _defaults(root: Path) -> dict[str, Path]:
    assets = _asset_root(root)
    return {
        "runtime": assets / ".cache/go-linux-runtime/package/bin/opencode",
        "tokenizer": assets / ".cache/go-context-artifacts",
        "credential": assets / "opencodego.key.txt",
        "directory": root / ".cache/business-planning-demo-live",
    }


def _public_paths(values: dict[str, Path]) -> dict[str, str]:
    return {
        "runtime": str(values["runtime"].resolve()),
        "tokenizer_directory": str(values["tokenizer"].resolve()),
        "credential_file": str(values["credential"].resolve()),
        "directory": str(values["directory"].resolve()),
    }


def _validate_nonsecret(values: dict[str, Path]) -> list[str]:
    errors: list[str] = []
    if not values["runtime"].is_file():
        errors.append("RUNTIME_NOT_FOUND")
    if not values["tokenizer"].is_dir():
        errors.append("TOKENIZER_DIRECTORY_NOT_FOUND")
    if values["directory"].exists() or values["directory"].is_symlink():
        errors.append("NEW_DEDICATED_DIRECTORY_REQUIRED")
    # Deliberately do not stat or read credential_file on the non-live path.
    return errors


def _git(repository: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repository), *args], check=True, capture_output=True)


def _seed_repository(root: Path) -> Path:
    repository = root / "repository"
    repository.mkdir(mode=0o700)
    (repository / "greeting.py").write_text(
        'def greet(name: str) -> str:\n'
        '    """Return Guest for an empty name and preserve a named greeting."""\n'
        '    return f"Hello, {name or \'Guest\'}!"\n',
        encoding="utf-8",
    )
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "add", "greeting.py")
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "-c",
            "user.name=Karajan Demo",
            "-c",
            "user.email=karajan-demo@example.invalid",
            "commit",
            "-qm",
            "seed greeting demo",
        ],
        check=True,
        capture_output=True,
    )
    return repository


def _configuration() -> dict[str, Any]:
    source = _repo_root() / "examples/projects/offline-configuration.json"
    configuration = json.loads(source.read_text(encoding="utf-8"))
    registration = configuration["resources"]["profiles"][0]
    profile = registration["profile"]
    registration["id"] = profile["id"] = "commander"
    profile["auth_ref"] = AUTH_REF
    profile["required_permissions"] = []
    profile["binding"].update(
        runtime_kind="opencode-go-isolated",
        runtime_version="1.18.29",
        model_id="glm-5.3-flash",
        auth_mode="api_key",
        native_settings={"suite_ref": SUITE_REF},
    )
    configuration["resources"]["accounts"][0].update(
        provider_id=SOURCE_ID, secret_ref=AUTH_REF
    )
    configuration["approved_profile_refs"] = [{"id": "commander", "revision": 1}]
    configuration["rulebook"]["revision"] = 2
    for refs in configuration["rulebook"]["profile_groups"].values():
        for ref in refs:
            ref["id"] = "commander"
    return configuration


def _live(values: dict[str, Path]) -> dict[str, Any]:
    root = values["directory"].resolve()
    if root.exists() or root.is_symlink():
        raise ValueError("NEW_DEDICATED_DIRECTORY_REQUIRED")
    credential = values["credential"].resolve()
    if not credential.is_file():
        raise ValueError("CREDENTIAL_FILE_NOT_FOUND")
    if not 16 <= credential.stat().st_size <= 4096:
        raise ValueError("CREDENTIAL_INPUT_INVALID")
    root.mkdir(mode=0o700)
    repository = _seed_repository(root)

    sys.path.insert(0, str(_repo_root() / "backend"))
    from karajan.adapters.opencode.go_journal import GoCallJournal
    from karajan.orchestration.go_commander_qualification import (
        CommanderCredentialSource,
        CommanderQualificationSettings,
        open_go_commander_qualification_store,
        qualify_commander_planning,
        write_commander_qualification_settings,
    )
    from karajan.projects import ProjectRegistry
    from karajan.projects.credential_sources import CredentialSourceStore, LocalKeyFile

    projects = ProjectRegistry(root / "projects.sqlite", [root])
    project = projects.create(
        {
            "name": "#153 Commander planning demo",
            "repository_path": str(repository),
            "base_ref": "main",
            "target_branch": "main",
            "allowed_target_branches": ["main"],
        },
        command_key="create-demo-project",
        principal="owner",
    )
    configuration = _configuration()
    preview = projects.preview_configuration(
        project["id"], configuration, principal="owner", command_key="preview-demo-config"
    )
    projects.apply_configuration(
        project["id"],
        preview["preview_id"],
        expected_revision=1,
        principal="owner",
        command_key="apply-demo-config",
    )

    private = root / "credential-private"
    control = (root / "control").absolute()
    journal_path = root / "commander-journal.sqlite"
    work_root = root / "commander-work"
    settings = CommanderQualificationSettings(
        values["runtime"].resolve(),
        values["tokenizer"].resolve(),
        private,
        (CommanderCredentialSource(project["id"], AUTH_REF, SOURCE_ID, credential),),
        journal_path=journal_path,
        work_root=work_root,
    )
    write_commander_qualification_settings(control, settings)
    credentials = CredentialSourceStore(
        projects,
        sources={(project["id"], AUTH_REF): LocalKeyFile(SOURCE_ID, credential)},
        private_directory=private,
    )
    generation = credentials.register(
        project["id"], AUTH_REF, principal="owner", command_key="register-commander-credential"
    )
    store = open_go_commander_qualification_store(projects, control_directory=control)
    record = qualify_commander_planning(
        store,
        project["id"],
        {"id": "commander", "revision": 1},
        principal="owner",
        command_key="qualify-commander-planning",
        validity_seconds=3600,
    )
    start = store.get_command_start(
        project["id"], "qualify-commander-planning", principal="owner"
    )
    return {
        "schema_version": "karajan.business-planning-demo.v1",
        "scope": SCOPE,
        "status": record.get("status", "unknown"),
        "reason_codes": record.get("reason_codes", []),
        "project_id": project["id"],
        "repository": str(repository),
        "control_directory": str(control),
        "journal": str(journal_path),
        "work_root": str(work_root),
        "secret_ref": AUTH_REF,
        "source": {"provider_id": SOURCE_ID, "profile_id": "commander", "revision": 1},
        "credential_generation": generation.get("generation"),
        "start": {
            "id": start.get("id"),
            "completed": start.get("completed"),
            "request_counts": [
                row.get("request_count") for row in start.get("binding", {}).get("execution_start", {}).get("scenarios", [])
            ],
        },
        "dispatch_eligible": False,
        "business_execution": "owned by web planning factory",
    }


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    defaults = _defaults(_repo_root())
    values = {
        "runtime": args.runtime or defaults["runtime"],
        "tokenizer": args.tokenizer_directory or defaults["tokenizer"],
        "credential": args.credential_file or defaults["credential"],
        "directory": args.directory or defaults["directory"],
    }
    errors = _validate_nonsecret(values)
    if errors:
        print(json.dumps({"status": "invalid", "errors": errors, "paths": _public_paths(values)}))
        return 2
    if not args.live:
        print(json.dumps({
            "status": "not_run",
            "reason": "EXPLICIT_LIVE_REQUIRED",
            "scope": SCOPE,
            "paths": _public_paths(values),
            "credential_file_checked": False,
            "provider_called": False,
            "business_execution": "owned by web planning factory",
        }))
        return 0
    try:
        result = _live(values)
    except Exception as error:  # keep secret material out of the CLI result
        print(json.dumps({"status": "failed", "error_type": type(error).__name__}))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
