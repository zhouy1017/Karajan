"""One-shot controller for Issue #107's fixed official Go Reviewer suite.

This is an operator artifact, not product code.  It deliberately has no endpoint,
transport, prompt, verdict, session, grant, or credential CLI argument.  The
provider credential is only reached through CredentialSourceStore's configured
LocalKeyFile at the trusted relay boundary.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from karajan.adapters.opencode.go_context import GoRequestAccounting
from karajan.adapters.opencode.go_journal import GoCallJournal
from karajan.projects import ProjectRegistry
from karajan.projects.credential_sources import CredentialSourceStore, LocalKeyFile
from karajan.projects.go_reviewer_suite import FixedGoReviewerSuite
from karajan.projects.publication import digest
from karajan.projects.qualification import ProfileQualificationStore, QualificationError

ROOT = Path(__file__).resolve().parents[2]

SUITE = {"id": "opencode-go-readonly-review-linux", "revision": 1}
PRINCIPAL = "issue107-controller"
COMMAND = "issue107-official-go-reviewer-20260907-attempt2"


def sha(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def paths(private_root: Path) -> dict[str, Path]:
    return {
        "state": private_root / "projects.sqlite",
        "journal": private_root / "journal.sqlite",
        "credentials": private_root / "credential-private",
        "work": private_root / "reviewer-work",
        "repo": private_root / "fixed-project-repository",
        "runtime": Path(
            "/mnt/c/Users/Chooo/Playground/Karajan/.cache/go-linux-runtime/package/bin/opencode"
        ),
        "tokenizer": Path(
            "/mnt/c/Users/Chooo/Playground/Karajan/.cache/go-task-execution/.cache/go-context-artifacts"
        ),
        "key": Path("/mnt/c/Users/Chooo/Playground/Karajan/opencodego.key.txt"),
    }


def source_summary(source: dict[str, Any]) -> dict[str, Any]:
    runtime = source["runtime_source"]
    return {
        "source_digest": digest(source),
        "origin": source["observation_origin"],
        "scope": source["qualification_scope"],
        "suite_ref": source["suite_ref"],
        "runtime": {
            key: runtime[key]
            for key in (
                "execution_path",
                "runtime_version",
                "artifact_sha256",
                "machine",
                "kernel_release",
            )
        },
        "runtime_digest": source["runtime_digest"],
        "probe_spec_digest": source["probe_spec_digest"],
        "parser_revision": source["probe_spec"]["parser_revision"],
        "context": source["probe_spec"]["context"],
        "source_sha256": source["runtime_source"]["source_sha256"],
        "tokenizer": source["runtime_source"]["accounting_source"],
    }


def reviewer_configuration() -> tuple[dict[str, Any], dict[str, Any]]:
    document = json.loads(
        (ROOT / "examples/projects/offline-configuration.json").read_text(encoding="utf-8")
    )
    original = document["resources"]["profiles"][0]
    reviewer = copy.deepcopy(original)
    reviewer["id"] = reviewer["profile"]["id"] = "readonly-reviewer-107"
    reviewer["profile"]["auth_ref"] = "go-reviewer-secret-107"
    reviewer["profile"]["required_permissions"] = ["read"]
    reviewer["profile"]["binding"].update(
        model_id="glm-5.3-flash",
        runtime_kind="opencode-go-isolated",
        runtime_version="1.18.29",
        auth_mode="api_key",
        native_settings={"suite_ref": SUITE},
    )
    account = document["resources"]["accounts"][0]
    account.update(provider_id="opencode-go", secret_ref="go-reviewer-secret-107")
    channel = document["resources"]["channels"][0]
    channel.update(
        account_id=account["id"], billing_path="subscription_only", approved_data_destination=True
    )
    reviewer["profile"]["binding"].update(
        account_id=account["id"], channel_id=channel["id"], billing_path="subscription_only"
    )
    reviewer["capability_evidence"] = [
        {**row, "profile_digest": digest(reviewer["profile"]), "runtime_version": "1.18.29"}
        for row in reviewer["capability_evidence"]
    ]
    document["resources"]["profiles"].append(reviewer)
    document["approved_profile_refs"].append({"id": reviewer["id"], "revision": 1})
    return document, reviewer


def fixed_private_repository(path: Path) -> Path:
    """A controller-created identity root; the observer itself accepts no repository path."""
    if not path.exists():
        path.mkdir(mode=0o700, parents=True)
        (path / "README.md").write_text("fixed Issue 107 controller workspace\n", encoding="utf-8")
        for command in (
            ("git", "init", "--initial-branch=dev", str(path)),
            ("git", "-C", str(path), "add", "README.md"),
            (
                "git",
                "-C",
                str(path),
                "-c",
                "user.name=Issue107",
                "-c",
                "user.email=issue107@invalid",
                "commit",
                "-qm",
                "fixed-controller-workspace",
            ),
        ):
            subprocess.run(
                command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
    return path.resolve()


def open_controller(
    private_root: Path,
) -> tuple[ProfileQualificationStore, str, dict[str, Any], GoCallJournal]:
    private_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    p = paths(private_root)
    p["repo"] = fixed_private_repository(p["repo"])
    p["work"].mkdir(mode=0o700, parents=True, exist_ok=True)
    registry = ProjectRegistry(p["state"], [p["repo"]])
    project = registry.create(
        {
            "name": "Issue 107 fixed Reviewer qualification",
            "repository_path": str(p["repo"]),
            "base_ref": "HEAD",
            "target_branch": "dev",
            "allowed_target_branches": ["dev"],
        },
        command_key="issue107-create-project",
        principal=PRINCIPAL,
    )
    current = registry.get(project["id"])
    if current["configuration"]["revision"] == 0:
        configuration, reviewer = reviewer_configuration()
        preview = registry.preview_configuration(
            project["id"], configuration, command_key="issue107-config-preview", principal=PRINCIPAL
        )
        registry.apply_configuration(
            project["id"],
            preview["preview_id"],
            expected_revision=current["revision"],
            command_key="issue107-config-apply",
            principal=PRINCIPAL,
        )
    else:
        configured = registry.get_configuration(project["id"])["configuration"]
        reviewer = next(
            row
            for row in configured["resources"]["profiles"]
            if row["id"] == "readonly-reviewer-107"
        )
    credentials = CredentialSourceStore(
        registry,
        sources={
            (project["id"], "go-reviewer-secret-107"): LocalKeyFile("issue107-root-key", p["key"])
        },
        private_directory=p["credentials"],
    )
    credentials.register(
        project["id"],
        "go-reviewer-secret-107",
        principal=PRINCIPAL,
        command_key="issue107-generation",
    )
    accounting = GoRequestAccounting(p["tokenizer"])
    journal = GoCallJournal(p["journal"])
    suite = FixedGoReviewerSuite(p["runtime"], p["work"], journal, accounting=accounting)
    return (
        ProfileQualificationStore(registry, credentials=credentials, reviewer_suite=suite),
        project["id"],
        reviewer,
        journal,
    )


def scenario_summary(row: dict[str, Any]) -> dict[str, Any]:
    observation = row.get("observation", {})
    journal = observation.get("journal", {})
    final = observation.get("native_final") or {}
    parsed = observation.get("parsed_review") or {}
    return {
        "scenario": row["scenario"],
        "attempt_sha256": hashlib.sha256(row["attempt_id"].encode()).hexdigest(),
        "grant_sha256": hashlib.sha256(row["grant_id"].encode()).hexdigest(),
        "status": row["status"],
        "reason_codes": row["reason_codes"],
        "request_count": journal.get("request_count"),
        "call_states": [call.get("state") for call in journal.get("calls", [])],
        "usage": [call.get("outcome", {}).get("usage") for call in journal.get("calls", [])],
        "final": {
            key: final.get(key) for key in ("finish", "completed", "text_sha256", "text_bytes")
        },
        "parsed": {"verdict": parsed.get("verdict"), "findings": len(parsed.get("findings", []))},
        "readonly_unchanged": observation.get("readonly", {}).get("unchanged"),
        "local_stop": observation.get("native_cleanup", {}).get("local_stop"),
        "canary_in_retention": any(
            item.get("denied_canary_present")
            for item in observation.get("retention", {}).get("requests", [])
        ),
    }


def run(private_root: Path, report: Path) -> None:
    store, project_id, reviewer, journal = open_controller(private_root)
    source = store.reviewer_suite.source()  # type: ignore[union-attr]
    if source["observation_origin"] != "official_go":
        raise RuntimeError("official source is unavailable")
    record = store.qualify_runtime_tools(
        project_id,
        {"id": reviewer["id"], "revision": 1},
        principal=PRINCIPAL,
        command_key=COMMAND,
        suite_ref=SUITE,
        validity_seconds=600,
    )
    start = store.get_command_start(project_id, COMMAND, principal=PRINCIPAL)

    def counts() -> dict[str, int | None]:
        result: dict[str, int | None] = {}
        for row in start["binding"]["execution_start"]["scenarios"]:
            try:
                result[row["grant_id"]] = journal.snapshot(row["grant_id"])["request_count"]
            except Exception:
                result[row["grant_id"]] = None
        return result

    before = counts()
    replay = store.qualify_runtime_tools(
        project_id,
        {"id": reviewer["id"], "revision": 1},
        principal=PRINCIPAL,
        command_key=COMMAND,
        suite_ref=SUITE,
        validity_seconds=600,
    )
    after = counts()
    facts: dict[str, Any] | None = None
    fact_error: str | None = None
    if record["status"] == "passed":
        try:
            facts = store.facts_for_profile(
                project_id, reviewer, principal=PRINCIPAL, scope="readonly_reviewer_tools"
            )
        except QualificationError as error:
            fact_error = error.code
    revocation = store.revoke(
        project_id, record["id"], principal=PRINCIPAL, reason="issue107-revoke-negative"
    )
    revoked_error = None
    try:
        store.facts_for_profile(
            project_id, reviewer, principal=PRINCIPAL, scope="readonly_reviewer_tools"
        )
    except QualificationError as error:
        revoked_error = error.code
    readback = store.get(project_id, record["id"], principal=PRINCIPAL)
    output = {
        "schema_version": "karajan.issue107-official-evidence.v1",
        "issue": 107,
        "command": COMMAND,
        "project_sha256": hashlib.sha256(project_id.encode()).hexdigest(),
        "record_sha256": sha(record),
        "record_status": record["status"],
        "source": source_summary(source),
        "start": {
            "completed": start["completed"],
            "scenario_order": [
                row["scenario"] for row in start["binding"]["execution_start"]["scenarios"]
            ],
            "max_requests": 6,
            "max_total_requests": 18,
            "expires_minus_started": start["binding"]["execution_start"]["expires_at"]
            - start["binding"]["execution_start"]["started_at"],
        },
        "scenarios": [scenario_summary(row) for row in record["observation"].get("scenarios", [])],
        "replay": {
            "same_record": replay == record,
            "before": {
                hashlib.sha256(key.encode()).hexdigest(): value for key, value in before.items()
            },
            "after": {
                hashlib.sha256(key.encode()).hexdigest(): value for key, value in after.items()
            },
            "zero_new_requests": before == after,
        },
        "facts_before_revoke": None
        if facts is None
        else {
            "roles": facts["facts"]["roles"],
            "tools": facts["facts"]["tools"],
            "scope": facts["executor_scope"],
            "dispatch_eligible": facts["dispatch_eligible"],
        },
        "facts_before_revoke_error": fact_error,
        "revoke": {
            "reason": revocation["reason"],
            "current_consumer_error": revoked_error,
            "history_readable": readback["record"] == record,
            "provider_remote_stop": record["observation"].get("provider_remote_stop"),
        },
        "limitations": [
            "No Reviewer Task, Candidate Review, Review Evidence, capacity reservation,"
            " or quality gate was run.",
            "The fixed fixture Plan / ApprovedReviewerBindings consumer is not run"
            " by this controller script.",
        ],
    }
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def preflight(private_root: Path, report: Path) -> None:
    private_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    p = paths(private_root)
    accounting = GoRequestAccounting(p["tokenizer"])
    journal = GoCallJournal(p["journal"])
    suite = FixedGoReviewerSuite(p["runtime"], p["work"], journal, accounting=accounting)
    source = suite.source()
    report.write_text(
        json.dumps(
            {
                "schema_version": "karajan.issue107-preflight.v1",
                "official_effect_started": False,
                "source": source_summary(source),
                "assets": {
                    "runtime_sha256": hashlib.file_digest(
                        p["runtime"].open("rb"), "sha256"
                    ).hexdigest(),
                    "tokenizer_source": accounting.source(),
                },
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def inspect(private_root: Path, report: Path) -> None:
    """History-only readback: never calls qualify_runtime_tools or resolves a credential."""
    store, project_id, reviewer, journal = open_controller(private_root)
    start = store.get_command_start(project_id, COMMAND, principal=PRINCIPAL)
    record = (
        store.get(project_id, start["id"], principal=PRINCIPAL)["record"]
        if start["completed"]
        else None
    )
    scenarios = (
        []
        if record is None
        else [scenario_summary(row) for row in record.get("observation", {}).get("scenarios", [])]
    )
    report.write_text(
        json.dumps(
            {
                "schema_version": "karajan.issue107-history-readback.v1",
                "official_effect_started": True,
                "start": {
                    "completed": start["completed"],
                    "id_sha256": hashlib.sha256(start["id"].encode()).hexdigest(),
                    "scenario_order": [
                        row["scenario"] for row in start["binding"]["execution_start"]["scenarios"]
                    ],
                },
                "record": None
                if record is None
                else {
                    "status": record["status"],
                    "reason_codes": record["reason_codes"],
                    "record_sha256": sha(record),
                },
                "scenarios": scenarios,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("preflight", "run", "inspect"))
    parser.add_argument("--private-root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if args.mode == "preflight":
        preflight(args.private_root, args.report)
    elif args.mode == "run":
        run(args.private_root, args.report)
    else:
        inspect(args.private_root, args.report)


if __name__ == "__main__":
    main()
