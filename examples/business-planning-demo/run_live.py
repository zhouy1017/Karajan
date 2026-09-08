"""Prepare the #153 demo and optionally run one independent Commander qualification.

The live path is deliberately explicit.  Without ``--live`` this command only
validates public paths and prints the bounded action it would take; it never
opens the credential file or contacts a provider.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

SCOPE = "commander_planning.v1"
AUTH_REF = "secret:go-commander"
SOURCE_ID = "opencode-go"
SUITE_REF = {"id": "opencode-go-commander-planning-linux", "revision": 2}
ACCOUNT_ID = "opencode-go"
CHANNEL_ID = "opencode-go-channel"
POOL_ID = "opencode-go-requests"
POLICY_ID = "business-planning-demo"


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
    with (repository / "greeting.py").open("w", encoding="utf-8", newline="\n") as seed:
        seed.write("def greet(name: str) -> str:\n")
        seed.write('    return f"Hello, {name}!"\n')
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
    registration["model_family"] = "glm-5.3-flash"
    profile["auth_ref"] = AUTH_REF
    profile["required_permissions"] = []
    profile["binding"].update(
        runtime_kind="opencode-go-isolated",
        runtime_version="1.18.29",
        model_id="glm-5.3-flash",
        auth_mode="api_key",
        account_id=ACCOUNT_ID,
        channel_id=CHANNEL_ID,
        native_settings={"suite_ref": SUITE_REF},
    )
    account = configuration["resources"]["accounts"][0]
    account.update(id=ACCOUNT_ID, provider_id=SOURCE_ID, secret_ref=AUTH_REF)
    configuration["resources"]["channels"][0].update(id=CHANNEL_ID, account_id=ACCOUNT_ID)
    pool = configuration["resources"]["quota_pools"][0]
    pool.update(
        id=POOL_ID,
        account_id=ACCOUNT_ID,
        unit="requests",
        limit=None,
        observation_state="unknown",
    )
    registration["quota_pool_refs"] = [POOL_ID]
    configuration["resources"]["capacity_policies"] = [
        {
            "account_id": ACCOUNT_ID,
            "conservative_mode": {
                "enabled": True,
                "max_local_active_attempts": 1,
                "max_attempt_duration_seconds": 300,
                "observation_max_age_seconds": 600,
                "cooldown_seconds": 60,
            },
        }
    ]
    # This demo is a planning-only surface.  Keep the rulebook finite and do
    # not expose worker, review, or autonomous tool routes.
    configuration["rulebook"]["profile_groups"] = {
        "commander_qualified": [{"id": "commander", "revision": 1}]
    }
    configuration["rulebook"]["rules"] = [
        {
            "id": "lead-planning",
            "priority": 100,
            "when": {"role": "commander", "purpose": "lead"},
            "eligible_groups": ["commander_qualified"],
            "capabilities_all": ["design_reasoning", "structured_plan_output"],
            "handoff": "explicit_checkpoint_record",
            "reroute": "propose_checkpoint_handoff_require_user_decision",
        }
    ]
    # These are owner configuration declarations.  They are intentionally
    # unknown until the public #147 qualification produces current facts.
    registration["capability_evidence"] = [
        {
            "capability": capability,
            "status": "not_run",
            "profile_digest": None,
            "runtime_version": None,
            "evidence_ref": None,
            "provenance": None,
        }
        for capability in ("design_reasoning", "structured_plan_output")
    ]
    configuration["approved_profile_refs"] = [{"id": "commander", "revision": 1}]
    configuration["rulebook"]["revision"] = 2
    for refs in configuration["rulebook"]["profile_groups"].values():
        for ref in refs:
            ref["id"] = "commander"
    return configuration


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _execution_policy(project: dict[str, Any], tokenizer_directory: Path) -> dict[str, Any]:
    from karajan.adapters.opencode.go_context import GoRequestAccounting
    from karajan.routing.compiler import digest

    accounting = GoRequestAccounting(tokenizer_directory)
    validation_source = _repo_root() / "tests" / "web" / "test_planning_demo_setup.py"
    environment = {"id": "python-candidate", "revision": 1}
    return {
        "schema_version": "karajan.execution-policy.v2",
        "id": POLICY_ID,
        "revision": 1,
        "configuration_digest": project["configuration"]["digest"],
        "constraints": {
            "profile_refs": [{"id": "commander", "revision": 1}],
            "channel_ids": [CHANNEL_ID],
            "tools": [],
            "data_destinations": ["controller"],
            "required_capabilities": ["design_reasoning", "structured_plan_output"],
            "min_isolation": "tool_sandboxed",
        },
        "risk_policy": {
            "id": "business-planning-risk",
            "revision": 1,
            "mapping": {"standard": "T1", "critical": "T3"},
            "path_floors": [],
        },
        "channel_destinations": {CHANNEL_ID: "controller"},
        "tool_policy": {"id": "no-autonomous-tools", "revision": 1, "tool_permissions": {}},
        "context_policy": {
            "id": "go-reference-context",
            "revision": 1,
            "input_accounting": "explicit_approved_upper_bound",
            "reserved_output_tokens": 4096,
            "measurement": {
                "method": "reference_tokenizer_estimate",
                "source_sha256": digest(accounting.source()),
                "fixed_margin": 2048,
                "ratio_margin_basis_points": 1000,
            },
        },
        "max_context_tokens": 16384,
        "validation": {
            "id": "controlled-python-candidate",
            "revision": 1,
            "checks": [
                {
                    "id": "demo-tests",
                    "revision": 1,
                    "argv": ["python", "-m", "pytest", "tests/web/test_planning_demo_setup.py"],
                    "environment_ref": environment,
                    "timeout_seconds": 300,
                }
            ],
            "environments": [
                {
                    **environment,
                    "runtime_kind": "controlled-python-candidate",
                    "platform": "windows_x64",
                    "source_sha256": _sha256(validation_source),
                    "filesystem": "candidate_copy",
                    "network": "none",
                    "env": {"PYTHONUTF8": "1"},
                    "max_log_bytes": 262144,
                }
            ],
            "review": {
                "id": "independent_review",
                "revision": 1,
                "environment_ref": environment,
                "context_policy": "candidate_and_acceptance_only",
                "independence_policy": "existing_candidate_independence_v1",
            },
        },
    }


def _register_capacity(bootstrap: Any) -> dict[str, Any]:
    from karajan.capacity import CapacityStore

    capacity = CapacityStore(bootstrap.capacity_database, existing_only=True)
    pool = capacity.register_pool(
        {
            "id": POOL_ID,
            "account_id": ACCOUNT_ID,
            "kind": "service",
            "unit": "requests",
            "window_kind": "unknown",
        },
        command_key="register-demo-capacity-pool",
    )
    profile = capacity.register_profile(
        {"id": "commander", "revision": 1, "account_id": ACCOUNT_ID, "pool_ids": [POOL_ID]},
        command_key="register-demo-capacity-profile",
    )
    policy = capacity.activate_policy(
        {
            "account_id": ACCOUNT_ID,
            "max_active_attempts": 1,
            "max_attempt_duration_seconds": 300,
            "observation_max_age_seconds": 600,
            "require_official_observation": False,
            "safety_margin": {},
            "lead_reserve": {},
            "lead_reserved_slots": 0,
            "conservative_mode": {
                "enabled": True,
                "max_local_active_attempts": 1,
                "max_attempt_duration_seconds": 300,
                "observation_max_age_seconds": 600,
                "cooldown_seconds": 60,
            },
        },
        expected_revision=0,
        command_key="register-demo-capacity-policy",
    )
    return {"pool": pool, "profile": profile, "policy": policy, "observation": "unknown"}


def _live(values: dict[str, Path], *, qualifier: Any | None = None) -> dict[str, Any]:
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
    from karajan.orchestration.go_commander_qualification import (
        CommanderCredentialSource,
        CommanderQualificationSettings,
        open_go_commander_qualification_store,
        qualify_commander_planning,
        write_commander_qualification_settings,
    )
    from karajan.orchestration.planning_bootstrap import provision_planning_bootstrap
    from karajan.projects import ProjectRegistry
    from karajan.projects.credential_sources import CredentialSourceStore, LocalKeyFile

    planning_control = (root / "control").absolute()
    planning_state = (root / "planning-state").absolute()
    bootstrap = provision_planning_bootstrap(planning_control, planning_state, (repository,))
    projects = ProjectRegistry(bootstrap.projects_database, bootstrap.allowed_roots)
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
    project = projects.get(project["id"])
    execution_policy = projects.register_execution_policy(
        project["id"],
        _execution_policy(project, values["tokenizer"].resolve()),
        principal="owner",
        command_key="register-demo-execution-policy",
    )
    capacity_registration = _register_capacity(bootstrap)

    private = root / "credential-private"
    control = planning_control
    journal_path = root / "commander-journal.sqlite"
    work_root = root / "commander-work"
    work_root.mkdir(mode=0o700)
    journal_path.touch(mode=0o600)
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
    record = (qualifier or qualify_commander_planning)(
        store,
        project["id"],
        {"id": "commander", "revision": 1},
        principal="owner",
        command_key="qualify-commander-planning",
        validity_seconds=3600,
    )
    start = store.get_command_start(project["id"], "qualify-commander-planning", principal="owner")
    scenarios = start.get("binding", {}).get("execution_start", {}).get("scenarios", [])
    return {
        "schema_version": "karajan.business-planning-demo.v1",
        "scope": SCOPE,
        "status": record.get("status", "unknown"),
        "reason_codes": record.get("reason_codes", []),
        "project_id": project["id"],
        "repository": str(repository),
        "control_directory": str(control),
        "planning_control_directory": str(planning_control),
        "planning_state_directory": str(planning_state),
        "journal": str(journal_path),
        "work_root": str(work_root),
        "secret_ref": AUTH_REF,
        "source": {"provider_id": SOURCE_ID, "profile_id": "commander", "revision": 1},
        "execution_policy": {
            "id": execution_policy["id"],
            "revision": execution_policy["revision"],
            "digest": execution_policy["digest"],
            "schema_version": execution_policy["schema_version"],
        },
        "capacity": {
            "account_id": ACCOUNT_ID,
            "profile_id": "commander",
            "pool_id": POOL_ID,
            "policy_revision": capacity_registration["policy"]["revision"],
            "observation": "unknown",
        },
        "credential_generation": generation.get("generation"),
        "start": {
            "id": start.get("id"),
            "completed": start.get("completed"),
            "request_counts": [row.get("request_count") for row in scenarios],
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
        print(
            json.dumps(
                {
                    "status": "not_run",
                    "reason": "EXPLICIT_LIVE_REQUIRED",
                    "scope": SCOPE,
                    "paths": _public_paths(values),
                    "credential_file_checked": False,
                    "provider_called": False,
                    "business_execution": "owned by web planning factory",
                }
            )
        )
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
