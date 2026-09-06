"""Fixed synthetic input, measured wire facts, and complete Collector evidence."""

import copy
import hashlib
import json
import os
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from karajan.adapters.opencode.go_evidence import INITIAL_FIXTURE
from karajan.adapters.opencode.go_relay import GoQualificationContext
from karajan.candidates import CandidateStore
from karajan.candidates.store import manifest_digest

from .go_probe import source_digest
from .opencode_runtime import StoppedProjection

REFERENCE = b"Clamp contract: return low below range, high above range, otherwise value.\n"
FILES = {
    "reference.md": REFERENCE,
    "src/fixture.py": INITIAL_FIXTURE.encode(),
    "assets/unchanged.bin": b"\x00\xff\x01synthetic-baseline\x00",
    "bin/unchanged": b"#!/bin/sh\nexit 0\n",
}
PROMPTS = {
    "edit": (
        "Use read to inspect /workspace/reference.md, then read /workspace/src/fixture.py. "
        "Fix clamp(value, low, high) according to the reference. Use edit only on "
        "/workspace/src/fixture.py. Keep three arguments and one return with nested min/max "
        "calls. No comments, shell, tests or other files. Reply briefly when done."
    ),
    "denied_read": (
        "Use read to read /workspace/blocked.txt once. If permission is denied, stop and reply "
        "KARAJAN_READ_DENIED. Do not use any other tool or modify any file."
    ),
}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def projection() -> list[dict[str, Any]]:
    return [
        {"path": path, "sha256": sha(FILES[path]), "writable": writable}
        for path, writable in (("reference.md", False), ("src/fixture.py", True))
    ]


@dataclass
class WireRetention:
    """Raw history lives in memory only; no payload or response text is serialized."""

    scenario: str
    previous: list[dict[str, Any]] = field(default_factory=list, repr=False)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def observe(self, payload: dict[str, Any], measured: dict[str, Any]) -> None:
        messages = payload["messages"]
        tool_paths: dict[str, str] = {}
        for message in messages:
            for call in message.get("tool_calls", []):
                function = call.get("function", {})
                if function.get("name") == "read":
                    arguments = json.loads(function["arguments"])
                    tool_paths[call["id"]] = arguments.get("filePath", "")
        observed: set[str] = set()
        for message in messages:
            if message["role"] != "tool":
                continue
            path = tool_paths.get(message.get("tool_call_id", ""), "")
            content = json.dumps(message.get("content", ""), ensure_ascii=False)
            if path == "/workspace/reference.md" and REFERENCE.decode().strip() in content:
                observed.add("reference")
            # Native read adds line numbers. Match every nonempty original line.
            if path == "/workspace/src/fixture.py" and all(
                line.strip() in content for line in INITIAL_FIXTURE.splitlines() if line.strip()
            ):
                observed.add("target")
        self.calls.append(
            {
                "sequence": len(self.calls) + 1,
                "request_digest": measured["request_digest"],
                "message_count": len(messages),
                "tool_message_count": sum(m["role"] == "tool" for m in messages),
                "messages_digest": source_digest({"messages": messages}),
                "initial_input_retained": any(
                    m["role"] == "user" and PROMPTS[self.scenario] in json.dumps(m["content"])
                    for m in messages
                ),
                "prior_messages_retained": messages[: len(self.previous)] == self.previous,
                "reference_tool_result_observed": "reference" in observed,
                "target_tool_result_observed": "target" in observed,
            }
        )
        self.previous = copy.deepcopy(messages)

    def report(self) -> dict[str, Any]:
        return {
            "schema_version": "karajan.projected-wire-retention.v1",
            "source": "measured_final_payload",
            "calls": copy.deepcopy(self.calls),
            "initial_input_retained": bool(self.calls)
            and all(c["initial_input_retained"] for c in self.calls),
            "tool_history_retained": len(self.calls) >= 2
            and self.calls[-1]["tool_message_count"] > 0
            and all(c["prior_messages_retained"] for c in self.calls),
            "reference_input_observed": bool(self.calls)
            and self.calls[-1]["reference_tool_result_observed"],
            "target_input_observed": bool(self.calls)
            and self.calls[-1]["target_tool_result_observed"],
        }


@dataclass(frozen=True)
class ObservedContext(GoQualificationContext):
    retention: WireRetention = field(repr=False)

    def measure(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = super().measure(payload)
        self.retention.observe(payload, result)
        return result


def prepare_baseline(directory: Path) -> tuple[CandidateStore, dict[str, Any]]:
    repository = directory / "baseline"
    repository.mkdir(mode=0o700)
    for name, content in FILES.items():
        path = repository / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        path.chmod(0o755 if name == "bin/unchanged" else 0o644)
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(directory),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "Karajan fixed fixture",
        "GIT_AUTHOR_EMAIL": "fixture@invalid",
        "GIT_COMMITTER_NAME": "Karajan fixed fixture",
        "GIT_COMMITTER_EMAIL": "fixture@invalid",
        "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
        "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
    }

    def git(*args: str) -> str:
        return subprocess.run(
            ["/usr/bin/git", "-C", str(repository), *args],
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()

    git("init", "--quiet")
    git("add", "--", ".")
    git("commit", "--quiet", "-m", "Fixed projected qualification baseline")
    store = CandidateStore(directory / "candidates")
    baseline = store.register_baseline(
        repository,
        repository_identity="fixed-projected-qualification",
        base_sha=git("rev-parse", "HEAD"),
    )
    return store, baseline


def manifest_summary(manifest: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "path": row["path"],
            "mode": row["mode"],
            "blob_sha": row["blob_sha"],
            "sha256": row["artifact"]["sha256"],
            "bytes": row["artifact"]["size"],
        }
        for row in manifest
    ]


def collect_capture(
    directory: Path,
    store: CandidateStore,
    baseline: dict[str, Any],
    captured: StoppedProjection,
    binding: dict[str, Any],
    grant_id: str,
) -> dict[str, Any]:
    spec_digest = binding["probe_spec_digest"]
    actor = {
        "attempt_id": binding["attempt_id"],
        "fence": binding["fence"],
        "profile_id": "qualification-profile",
        "profile_revision": 1,
        "model_family": None,
        "context_id": "projected-context:" + binding["attempt_id"],
        "provenance_ref": "projected-probe:" + spec_digest,
    }
    request = {
        "series_id": "qualification:" + binding["qualification_id"] + ":" + binding["scenario"],
        "baseline_id": baseline["id"],
        "input_sha256": spec_digest,
        "allowed_paths": ["src/fixture.py"],
        "task_class": "T1",
        "authors": [actor],
        "writer": {
            "attempt_id": binding["attempt_id"],
            "fence": binding["fence"],
            "stopped": captured.stop_evidence["local_stop"] == "confirmed",
            "observation_ref": "projected-stop:" + grant_id,
        },
        "policy": {
            "id": "fixed-projected-validation",
            "revision": 1,
            "checks": [
                {
                    "id": "fixture_check",
                    "revision": 1,
                    "argv": [
                        "python3",
                        "-c",
                        "from src.fixture import clamp; "
                        "assert [clamp(0,1,3),clamp(4,1,3),"
                        "clamp(2,1,3),clamp(1,1,1)] == [1,3,2,1]",
                    ],
                    "environment_sha256": binding["runtime_digest"],
                }
            ],
            "review": {
                "revision": 1,
                "environment_sha256": binding["runtime_digest"],
                "approved_reviewers": [],
            },
        },
    }
    descriptor = [asdict(row) for row in captured.projection]
    contents = dict(captured.files)
    candidate = store.freeze_projection(descriptor, contents, request)
    restored = directory / "materialized"
    store.materialize(candidate["id"], restored)
    actual = {
        p.relative_to(restored).as_posix(): p.read_bytes()
        for p in restored.rglob("*")
        if p.is_file()
    }
    old, new = manifest_summary(baseline["manifest"]), manifest_summary(candidate["manifest"])
    old_by_path, new_by_path = ({r["path"]: r for r in rows} for rows in (old, new))
    unchanged = all(
        new_by_path.get(p) == row for p, row in old_by_path.items() if p != "src/fixture.py"
    )
    gate = store.gate(
        candidate["id"],
        current={
            k: candidate[k]
            for k in ("repository_identity", "base_sha", "input_sha256", "policy_sha256")
        },
    )
    matches = (
        actual == FILES | contents and (restored / "bin/unchanged").stat().st_mode & 0o777 == 0o755
    )
    return {
        "status": "passed"
        if unchanged and matches and old_by_path.keys() == new_by_path.keys()
        else "failed",
        "baseline_id": baseline["id"],
        "baseline_tree_sha": baseline["tree_sha"],
        "baseline_manifest_sha256": manifest_digest(baseline["manifest"]),
        "candidate_id": candidate["id"],
        "candidate_revision": candidate["revision"],
        "candidate_tree_sha": candidate["tree_sha"],
        "candidate_manifest_sha256": candidate["manifest_sha256"],
        "projection_digest": source_digest({"projection": descriptor}),
        "captured_files": [
            {"path": p, "sha256": sha(b), "bytes": len(b)} for p, b in captured.files
        ],
        "changed_paths": candidate["changed_paths"],
        "readonly_unchanged": contents["reference.md"] == REFERENCE,
        "outside_projection_unchanged": unchanged,
        "full_baseline_preserved": old_by_path.keys() == new_by_path.keys() and unchanged,
        "materialization_matches": matches,
        "local_stop_confirmed": captured.stop_evidence["local_stop"] == "confirmed",
        "validation_gate": {
            "local_gate_passed": gate["local_gate_passed"],
            "reasons": gate["reasons"],
        },
        "baseline_manifest": old,
        "candidate_manifest": new,
    }
