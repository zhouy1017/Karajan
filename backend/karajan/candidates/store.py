"""A trusted file Collector and append-only local candidate ledger.

Worker Git metadata is never opened by Git. Only register_baseline accepts a
trusted, registered repository; freeze reads its separate workspace as files.
"""

import hashlib
import json
import os
import sqlite3
import stat
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from .models import CheckResult, CurrentContext, Freeze, ReviewResult

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
MAX_ENTRIES = 10000


class CandidateError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    except (ValueError, TypeError, UnicodeError):
        raise CandidateError("INPUT_INVALID") from None


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def timestamp() -> str:
    return datetime.now(UTC).isoformat()


def manifest_digest(manifest: list[dict[str, Any]]) -> str:
    return digest(
        [
            {key: value for key, value in entry.items() if key != "artifact"}
            | {"sha256": entry["artifact"]["sha256"], "size": entry["artifact"]["size"]}
            for entry in manifest
        ]
    )


def relative_path(value: str, *, directory_allowed: bool = False) -> None:
    parts = (value[:-1] if directory_allowed and value.endswith("/") else value).split("/")
    if (
        not value
        or len(value) > 4096
        or any(not char.isprintable() or char in '\\:*?"<>|' for char in value)
        or any(
            not part or part in {".", ".."} or part.lower() == ".git" or part.rstrip(". ") != part
            for part in parts
        )
    ):
        raise CandidateError("PATH_INVALID")


class CandidateStore:
    def __init__(self, state_directory: Path) -> None:
        self.directory = Path(state_directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.objects = self.directory / "artifacts"
        self.objects.mkdir(exist_ok=True)
        self.git_directory = self.directory / "objects.git"
        if not self.git_directory.exists():
            self._git(["init", "--bare", "--object-format=sha1", str(self.git_directory)])
        with self._connection() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS baselines (id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS candidates (
                    id TEXT PRIMARY KEY, series_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    data TEXT NOT NULL, UNIQUE(series_id, revision));
                CREATE TABLE IF NOT EXISTS evidence (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                    evidence_key TEXT UNIQUE NOT NULL, candidate_id TEXT NOT NULL,
                    kind TEXT NOT NULL, subject TEXT NOT NULL, data TEXT NOT NULL);
            """)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.directory / "candidates.sqlite", timeout=10)
        try:
            connection.execute("PRAGMA synchronous=FULL")
            with connection:
                yield connection
        finally:
            connection.close()

    def _git(
        self, args: list[str], data: bytes | None = None, *, repository: Path | None = None
    ) -> bytes:
        env = {
            key: value
            for key, value in os.environ.items()
            if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}
        }
        env.update(
            {
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "0",
            }
        )
        command = [
            "git",
            "--no-replace-objects",
            "-c",
            "core.hooksPath=" + str(self.directory / "no-hooks"),
            "-c",
            "core.fsmonitor=false",
            "-c",
            "protocol.allow=never",
        ]
        if repository is not None:
            command += ["--git-dir=" + str(repository)]
        try:
            result = subprocess.run(
                command + args, input=data, capture_output=True, env=env, timeout=30
            )
        except (OSError, subprocess.TimeoutExpired):
            raise CandidateError("GIT_UNAVAILABLE") from None
        if result.returncode:
            raise CandidateError("GIT_OPERATION_FAILED")
        return result.stdout.strip() if data is None else result.stdout

    def _artifact(self, content: bytes) -> dict[str, Any]:
        if len(content) > MAX_FILE_BYTES:
            raise CandidateError("SNAPSHOT_LIMIT_EXCEEDED")
        sha = hashlib.sha256(content).hexdigest()
        target = self.objects / sha
        reference = {"sha256": sha, "size": len(content), "path": str(target)}
        if target.exists() and not self._available(reference):
            raise CandidateError("ARTIFACT_UNAVAILABLE")
        if not target.exists():
            temporary = self.objects / ("temporary-" + str(uuid4()))
            with temporary.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        return reference

    def _available(self, reference: dict[str, Any] | None) -> bool:
        if reference is None:
            return False
        try:
            content = (self.objects / reference["sha256"]).read_bytes()
        except OSError:
            return False
        return bool(
            len(content) == reference["size"]
            and hashlib.sha256(content).hexdigest() == reference["sha256"]
        )

    def register_baseline(
        self, trusted_repository: Path, *, repository_identity: str, base_sha: str
    ) -> dict[str, Any]:
        source = Path(trusted_repository).resolve() / ".git"
        canonical([repository_identity, base_sha])
        if (
            not source.is_dir()
            or len(base_sha) != 40
            or any(c not in "0123456789abcdef" for c in base_sha)
        ):
            raise CandidateError("BASELINE_INVALID")
        resolved = self._git(
            ["rev-parse", "--verify", base_sha + "^{commit}"], repository=source
        ).decode()
        entries = self._git(["ls-tree", "-rz", "--full-tree", resolved], repository=source)
        manifest: list[dict[str, Any]] = []
        total_bytes = 0
        for entry in entries.split(b"\0"):
            if not entry:
                continue
            metadata, name = entry.split(b"\t", 1)
            mode, kind, oid = metadata.decode().split()
            if kind != "blob" or mode not in {"100644", "100755"}:
                raise CandidateError("BASELINE_FILE_TYPE_UNSUPPORTED")
            relative_path(name.decode("utf-8"))
            size = int(self._git(["cat-file", "-s", oid], repository=source))
            total_bytes += size
            if (
                size > MAX_FILE_BYTES
                or total_bytes > MAX_SNAPSHOT_BYTES
                or len(manifest) >= MAX_ENTRIES
            ):
                raise CandidateError("SNAPSHOT_LIMIT_EXCEEDED")
            content = self._git(["cat-file", "blob", oid], b"", repository=source)
            manifest.append(
                {
                    "path": name.decode("utf-8"),
                    "mode": mode,
                    "blob_sha": oid,
                    "artifact": self._artifact(content),
                }
            )
        record = {
            "repository_identity": repository_identity,
            "base_sha": resolved,
            "tree_sha": self._git(["rev-parse", resolved + "^{tree}"], repository=source).decode(),
            "manifest": manifest,
        }
        record["id"] = digest(
            {
                "repository_identity": repository_identity,
                "base_sha": resolved,
                "tree_sha": record["tree_sha"],
                "manifest_sha256": manifest_digest(manifest),
            }
        )
        with self._connection() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO baselines VALUES (?, ?)",
                (record["id"], canonical(record).decode()),
            )
        return record

    def _tree(self, manifest: list[dict[str, Any]]) -> str:
        def build(prefix: str) -> str:
            entries = []
            directories: set[str] = set()
            for entry in manifest:
                if not entry["path"].startswith(prefix):
                    continue
                suffix = entry["path"][len(prefix) :]
                if "/" in suffix:
                    directories.add(suffix.split("/", 1)[0])
                else:
                    entries.append(
                        f"{entry['mode']} blob {entry['blob_sha']}\t{suffix}".encode() + b"\0"
                    )
            for name in sorted(directories):
                entries.append(f"040000 tree {build(prefix + name + '/')}\t{name}".encode() + b"\0")
            return (
                self._git(["mktree", "-z"], b"".join(entries), repository=self.git_directory)
                .decode()
                .strip()
            )

        return build("")

    def freeze(self, workspace: Path, request: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._freeze(workspace, request)
        except (OSError, ValueError):
            raise CandidateError("WORKSPACE_UNAVAILABLE") from None

    def _freeze(self, workspace: Path, request: dict[str, Any]) -> dict[str, Any]:
        canonical(request)
        try:
            spec = Freeze.model_validate(request)
        except ValidationError:
            raise CandidateError("FREEZE_INPUT_INVALID") from None
        for allowed in spec.allowed_paths:
            relative_path(allowed, directory_allowed=True)
        check_names = [check.id for check in spec.policy.checks]
        reviewer_names = [
            (item.profile_id, item.profile_revision)
            for item in spec.policy.review.approved_reviewers
        ]
        if len(check_names) != len(set(check_names)) or len(reviewer_names) != len(
            set(reviewer_names)
        ):
            raise CandidateError("POLICY_IDENTITY_AMBIGUOUS")
        if not spec.writer.stopped or not any(
            author.attempt_id == spec.writer.attempt_id and author.fence == spec.writer.fence
            for author in spec.authors
        ):
            raise CandidateError("WRITER_STOP_NOT_CONFIRMED")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT data FROM baselines WHERE id=?", (spec.baseline_id,)
            ).fetchone()
        if row is None:
            raise CandidateError("BASELINE_NOT_FOUND")
        baseline = json.loads(row[0])
        modes = {entry["path"]: entry["mode"] for entry in baseline["manifest"]}
        manifest = []
        root_info = Path(workspace).lstat()
        if (
            stat.S_ISLNK(root_info.st_mode)
            or getattr(root_info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise CandidateError("WORKSPACE_LINK_UNSUPPORTED")
        root = Path(workspace).resolve()
        if (
            root == self.directory
            or root in self.directory.parents
            or self.directory in root.parents
        ):
            raise CandidateError("CONTROL_STORAGE_OVERLAP")
        pending = [root]
        files = []
        total_bytes = 0
        entry_count = 0
        while pending:
            directory = pending.pop()
            for path in sorted(directory.iterdir()):
                if directory == root and path.name.lower() == ".git":
                    continue
                entry_count += 1
                if entry_count > MAX_ENTRIES:
                    raise CandidateError("SNAPSHOT_LIMIT_EXCEEDED")
                info = path.lstat()
                if (
                    stat.S_ISLNK(info.st_mode)
                    or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                ):
                    raise CandidateError("WORKSPACE_LINK_UNSUPPORTED")
                if stat.S_ISDIR(info.st_mode):
                    pending.append(path)
                elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    total_bytes += info.st_size
                    if info.st_size > MAX_FILE_BYTES or total_bytes > MAX_SNAPSHOT_BYTES:
                        raise CandidateError("SNAPSHOT_LIMIT_EXCEEDED")
                    files.append(path)
                else:
                    raise CandidateError("WORKSPACE_FILE_TYPE_UNSUPPORTED")
        for path in sorted(files):
            relative = path.relative_to(root).as_posix()
            relative_path(relative)
            content = path.read_bytes()
            mode = modes.get(relative, "100644")
            if os.name != "nt":
                mode = "100755" if path.stat().st_mode & stat.S_IXUSR else "100644"
            oid = (
                self._git(["hash-object", "-w", "--stdin"], content, repository=self.git_directory)
                .decode()
                .strip()
            )
            manifest.append(
                {
                    "path": relative,
                    "mode": mode,
                    "blob_sha": oid,
                    "artifact": self._artifact(content),
                }
            )
        before = {
            entry["path"]: (entry["mode"], entry["blob_sha"]) for entry in baseline["manifest"]
        }
        after = {entry["path"]: (entry["mode"], entry["blob_sha"]) for entry in manifest}
        changes = sorted(
            path for path in before.keys() | after.keys() if before.get(path) != after.get(path)
        )
        for path in changes:
            if not any(
                path == allowed or (allowed.endswith("/") and path.startswith(allowed))
                for allowed in spec.allowed_paths
            ):
                raise CandidateError("PATH_OUTSIDE_AUTHORIZATION")
        tree = self._tree(manifest)
        content_identity = {
            "repository_identity": baseline["repository_identity"],
            "base_sha": baseline["base_sha"],
            "tree_sha": tree,
            "input_sha256": spec.input_sha256,
        }
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT data FROM candidates WHERE series_id=? ORDER BY revision DESC LIMIT 1",
                (spec.series_id,),
            ).fetchone()
            previous = json.loads(row[0]) if row else None
            if (
                previous
                and previous["content_sha256"] == digest(content_identity)
                and previous["request"] == spec.model_dump()
            ):
                return dict(previous)
            record = {
                "schema_version": "karajan.candidate.v1",
                "id": str(uuid4()),
                "series_id": spec.series_id,
                "revision": (previous["revision"] if previous else 0) + 1,
                **content_identity,
                "content_sha256": digest(content_identity),
                "manifest_sha256": manifest_digest(manifest),
                "manifest": manifest,
                "changed_paths": changes,
                "request": spec.model_dump(),
                "policy_sha256": digest(spec.policy.model_dump()),
                "frozen_at": timestamp(),
            }
            connection.execute(
                "INSERT INTO candidates VALUES (?, ?, ?, ?)",
                (record["id"], spec.series_id, record["revision"], canonical(record).decode()),
            )
        return record

    def get(self, candidate_id: str) -> dict[str, Any]:
        canonical(candidate_id)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT data FROM candidates WHERE id=?", (candidate_id,)
            ).fetchone()
        if row is None:
            raise CandidateError("CANDIDATE_NOT_FOUND")
        result: dict[str, Any] = json.loads(row[0])
        return result

    def materialize(self, candidate_id: str, destination: Path) -> dict[str, Any]:
        candidate = self.get(candidate_id)
        target = Path(destination).resolve()
        if (
            target == self.directory
            or target in self.directory.parents
            or self.directory in target.parents
        ):
            raise CandidateError("CONTROL_STORAGE_OVERLAP")
        if target.exists():
            raise CandidateError("DESTINATION_EXISTS")
        if not all(self._available(entry["artifact"]) for entry in candidate["manifest"]):
            raise CandidateError("ARTIFACT_UNAVAILABLE")
        target.mkdir(parents=True, exist_ok=False)
        for entry in candidate["manifest"]:
            output = target / entry["path"]
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open("xb") as stream:
                stream.write((self.objects / entry["artifact"]["sha256"]).read_bytes())
            if os.name != "nt":
                output.chmod(0o755 if entry["mode"] == "100755" else 0o644)
        return {
            "candidate_id": candidate_id,
            "content_sha256": candidate["content_sha256"],
            "directory": str(target),
        }

    def gate(self, candidate_id: str, *, current: dict[str, Any]) -> dict[str, Any]:
        canonical(current)
        try:
            CurrentContext.model_validate(current)
        except ValidationError:
            raise CandidateError("GATE_CONTEXT_INVALID") from None
        candidate = self.get(candidate_id)
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT data FROM evidence WHERE candidate_id=? ORDER BY sequence", (candidate_id,)
            ).fetchall()
            latest = connection.execute(
                "SELECT MAX(revision) FROM candidates WHERE series_id=?", (candidate["series_id"],)
            ).fetchone()[0]
        evidence = [json.loads(row[0]) for row in rows]
        reasons = []
        context_changed = any(candidate[key] != value for key, value in current.items())
        if context_changed:
            reasons.append("CURRENT_CONTEXT_CHANGED")
        superseded = latest != candidate["revision"]
        if superseded:
            reasons.append("CANDIDATE_SUPERSEDED")
        if not all(self._available(entry["artifact"]) for entry in candidate["manifest"]):
            reasons.append("ARTIFACT_UNAVAILABLE")
        for item in evidence:
            item["effective_status"] = (
                "invalidated" if context_changed or superseded else item["status"]
            )
            if not self._available(item["log"]):
                item["effective_status"] = "unavailable"
        check_ids = []
        for check in candidate["request"]["policy"]["checks"]:
            matching = [
                item
                for item in evidence
                if item["kind"] == "check" and item["input"]["check_id"] == check["id"]
            ]
            if matching:
                check_ids.append(matching[-1]["id"])
            if not matching:
                reasons.append("CHECK_EVIDENCE_MISSING:" + check["id"])
            elif matching[-1]["effective_status"] != "passed":
                reasons.append("CHECK_NOT_PASSED:" + check["id"])
                if (
                    matching[-1]["effective_status"] == "unavailable"
                    and "ARTIFACT_UNAVAILABLE" not in reasons
                ):
                    reasons.append("ARTIFACT_UNAVAILABLE")
        reviews = [item for item in evidence if item["kind"] == "review"]
        if reviews and sorted(reviews[-1]["input"]["check_evidence_ids"]) != sorted(check_ids):
            reviews[-1]["effective_status"] = "invalidated"
            reasons.append("REVIEW_CHECK_SET_CHANGED")
        if not reviews:
            reasons.append("REVIEW_EVIDENCE_MISSING")
        elif reviews[-1]["effective_status"] != "passed":
            reasons.append("REVIEW_NOT_PASSED")
            if (
                reviews[-1]["effective_status"] == "unavailable"
                and "ARTIFACT_UNAVAILABLE" not in reasons
            ):
                reasons.append("ARTIFACT_UNAVAILABLE")
        return {
            "schema_version": "karajan.candidate-gate.v1",
            "candidate_id": candidate_id,
            "local_gate_passed": not reasons,
            "delivery_eligible": False,
            "live_qualification": "not_run",
            "reasons": reasons,
            "evidence": evidence,
        }

    def record_check(self, request: dict[str, Any], *, log: bytes | None) -> dict[str, Any]:
        canonical(request)
        try:
            spec = CheckResult.model_validate(request)
        except ValidationError:
            raise CandidateError("CHECK_INPUT_INVALID") from None
        candidate = self.get(spec.candidate_id)
        status = "passed"
        if spec.outcome != "completed" or spec.exit_code is None:
            status = "inconclusive"
        elif spec.exit_code != 0:
            status = "failed"
        elif not log:
            status = "unavailable"
        reasons = []
        expected = next(
            (
                check
                for check in candidate["request"]["policy"]["checks"]
                if check["id"] == spec.check_id
            ),
            None,
        )
        if (
            spec.policy_sha256 != candidate["policy_sha256"]
            or spec.input_sha256 != candidate["input_sha256"]
            or expected is None
            or expected["revision"] != spec.check_revision
            or expected["environment_sha256"] != spec.environment_sha256
        ):
            status = "invalidated"
            reasons.append("EVIDENCE_BINDING_MISMATCH")
        record = {
            "id": str(uuid4()),
            "kind": "check",
            "input": spec.model_dump(),
            "status": status,
            "reasons": reasons,
            "log": self._artifact(log) if log is not None else None,
            "recorded_at": timestamp(),
        }
        return self._save_evidence(record, spec.check_id)

    def record_review(self, request: dict[str, Any], *, log: bytes | None) -> dict[str, Any]:
        canonical(request)
        try:
            spec = ReviewResult.model_validate(request)
        except ValidationError:
            raise CandidateError("REVIEW_INPUT_INVALID") from None
        candidate = self.get(spec.candidate_id)
        review_policy = candidate["request"]["policy"]["review"]
        qualifier = next(
            (
                item
                for item in review_policy["approved_reviewers"]
                if item["profile_id"] == spec.actor.profile_id
                and item["profile_revision"] == spec.actor.profile_revision
            ),
            None,
        )
        reasons = []
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT data FROM evidence WHERE candidate_id=? AND kind='check' ORDER BY sequence",
                (spec.candidate_id,),
            ).fetchall()
        checks = {json.loads(row[0])["input"]["check_id"]: json.loads(row[0]) for row in rows}
        required = candidate["request"]["policy"]["checks"]
        prior_checks_valid = all(
            check["id"] in checks
            and checks[check["id"]]["status"] == "passed"
            and self._available(checks[check["id"]]["log"])
            for check in required
        )
        required_ids = [checks[check["id"]]["id"] for check in required if check["id"] in checks]
        if (
            spec.policy_sha256 != candidate["policy_sha256"]
            or spec.input_sha256 != candidate["input_sha256"]
            or spec.environment_sha256 != review_policy["environment_sha256"]
            or spec.review_revision != review_policy["revision"]
            or not prior_checks_valid
            or sorted(required_ids) != sorted(spec.check_evidence_ids)
        ):
            reasons.append("EVIDENCE_BINDING_MISMATCH")
        if (
            qualifier is None
            or qualifier["model_family"] != spec.actor.model_family
            or spec.author_reasoning_included
            or any(
                author["attempt_id"] == spec.actor.attempt_id
                or author["context_id"] == spec.actor.context_id
                for author in candidate["request"]["authors"]
            )
        ):
            reasons.append("REVIEWER_NOT_INDEPENDENT_OR_QUALIFIED")
        families = [author["model_family"] for author in candidate["request"]["authors"]]
        if candidate["request"]["task_class"] == "T3" and (
            not spec.actor.model_family
            or any(not family or family == spec.actor.model_family for family in families)
        ):
            reasons.append("T3_FAMILY_INDEPENDENCE_UNPROVEN")
        status = str(spec.verdict)
        if any(finding.blocking for finding in spec.findings):
            status = "failed"
        elif not log and status == "passed":
            status = "unavailable"
        if reasons:
            status = "invalidated"
        record = {
            "id": str(uuid4()),
            "kind": "review",
            "input": spec.model_dump(),
            "status": status,
            "reasons": reasons,
            "log": self._artifact(log) if log is not None else None,
            "recorded_at": timestamp(),
        }
        return self._save_evidence(record, "review")

    def _save_evidence(self, record: dict[str, Any], subject: str) -> dict[str, Any]:
        request = record["input"]
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT data FROM evidence WHERE evidence_key=?", (request["evidence_key"],)
            ).fetchone()
            if row:
                previous: dict[str, Any] = json.loads(row[0])
                if (
                    previous["kind"] != record["kind"]
                    or previous["input"] != request
                    or previous["log"] != record["log"]
                ):
                    raise CandidateError("EVIDENCE_KEY_CONFLICT")
                return previous
            connection.execute(
                "INSERT INTO evidence (id, evidence_key, candidate_id, kind, subject, data) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    record["id"],
                    request["evidence_key"],
                    request["candidate_id"],
                    record["kind"],
                    subject,
                    canonical(record).decode(),
                ),
            )
        return record
