"""Explicit fixed Go diagnostic; never import evidence as Profile qualification."""

import argparse
import hashlib
import json
import time
import uuid
from pathlib import Path

from karajan.adapters.opencode.go_journal import GoCallJournal
from karajan.adapters.opencode.go_relay import GoRelayAuthorization
from karajan.isolation.go_probe import go_runtime_source, observe_go_tools, source_digest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--credential-file", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--scenario", choices=["edit", "denied_read"], required=True)
    args = parser.parse_args()
    if args.live is not True:
        print(json.dumps({"status": "not_run", "reason": "LIVE_AUTHORIZATION_REQUIRED"}))
        return 1
    try:
        directory = args.directory.absolute()
        if directory.exists() or directory.is_symlink():
            raise ValueError("NEW_DIAGNOSTIC_DIRECTORY_REQUIRED")
        source = go_runtime_source(args.runtime)
        credential = args.credential_file.resolve(strict=True)
        if (
            credential.is_relative_to(directory)
            or not credential.is_file()
            or not 16 <= credential.stat().st_size <= 4096
        ):
            raise ValueError("CREDENTIAL_FILE_INVALID")
        secret = credential.read_text(encoding="utf-8-sig").strip()
        if (
            not 16 <= len(secret) <= 4096
            or not secret.isascii()
            or any(character.isspace() for character in secret)
        ):
            raise ValueError("CREDENTIAL_FILE_INVALID")
        directory.mkdir(mode=0o700)
        identity = uuid.uuid4().hex
        binding = {
            "qualification_id": "diagnostic:" + identity,
            "attempt_id": identity,
            "fence": 1,
            "profile_digest": hashlib.sha256(b"non-registered-fixed-go-diagnostic").hexdigest(),
            "runtime_digest": source_digest(source),
            "channel": "opencode-go",
            "model": "glm-5.3-flash",
            "auth_generation": "diagnostic:" + identity,
            "expires_at": time.time() + 240,
            "max_requests": 6,
        }
        start = {
            "binding": binding,
            "runtime_source": source,
            "registered_profile": False,
            "credential_source": "explicit-local-file-diagnostic",
            "started_at": time.time(),
            "scenario": args.scenario,
            "entrypoint_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        }
        # Persist the run and fixed grant identity before creating a relay or child.
        start_encoded = json.dumps(start, indent=2)
        if secret in start_encoded:
            raise ValueError("SENSITIVE_START_SUPPRESSED")
        (directory / "start.json").write_text(start_encoded + "\n")
        journal = GoCallJournal(directory / "journal.sqlite")
        try:
            grant = journal.create_grant(binding, grant_id=identity)
            result = observe_go_tools(
                args.runtime,
                directory / "run",
                secret,
                GoRelayAuthorization(journal, identity, binding, grant["capability"]),
                scenario=args.scenario,
            )
        finally:
            # Also covers an observer preflight exception or a lost create return.
            # Failure here reaches the failed result below; it never claims stop.
            journal.revoke_grant(identity)
        result.update(registered_profile=False, observed_at=time.time())
        encoded = json.dumps(result, indent=2)
        if secret in encoded or grant["capability"] in encoded:
            raise ValueError("SENSITIVE_REPORT_SUPPRESSED")
        (directory / "report.json").write_text(encoded + "\n")
        print(
            json.dumps(
                {
                    "status": result["status"],
                    "scenario": args.scenario,
                    "reason_codes": result["reason_codes"],
                    "requests": len(result["requests"]),
                    "statuses": [request["upstream_status"] for request in result["requests"]],
                    "local_stop": result["native_cleanup"].get("local_stop"),
                    "report": "report.json",
                }
            )
        )
        return 0 if result["status"] == "passed" else 1
    except Exception as error:
        # Provider/native exception messages and caller paths can contain secrets.
        print(json.dumps({"status": "failed", "error_type": type(error).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
