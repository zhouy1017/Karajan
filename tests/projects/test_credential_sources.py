"""Controller credentials use exact material and a real persistent project owner."""

import hashlib
import json
import os
import pickle
import sqlite3
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from karajan.projects import ProjectError, ProjectRegistry
from karajan.projects.credential_sources import (
    CredentialSourceError,
    CredentialSourceStore,
    LocalKeyFile,
)


@pytest.fixture
def case(tmp_path: Path) -> dict:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "fixture.txt").write_text("fixture\n", encoding="utf-8")
    for arguments in (
        ["init", "--initial-branch=main", str(repository)],
        ["-C", str(repository), "add", "fixture.txt"],
        [
            "-C",
            str(repository),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
    ):
        subprocess.run(["git", *arguments], check=True, capture_output=True)
    projects = ProjectRegistry(tmp_path / "projects.sqlite", [repository])
    project = projects.create(
        {
            "name": "Credential fixture",
            "repository_path": str(repository),
            "base_ref": "main",
            "target_branch": "main",
            "allowed_target_branches": ["main"],
        },
        principal="owner",
        command_key="create-project",
    )
    secret = "synthetic-first-secret-for-tests"
    key_file = tmp_path / "synthetic.key"
    key_file.write_text(secret + "\n", encoding="utf-8")
    with tempfile.TemporaryDirectory(
        prefix="karajan-credential-private-", dir="/tmp" if sys.platform == "linux" else tmp_path
    ) as private_root:
        yield {
            "projects": projects,
            "project_id": project["id"],
            "key_file": key_file,
            "secret": secret,
            "private": Path(private_root) / "private-credential-state",
        }


def service(case: dict) -> CredentialSourceStore:
    return CredentialSourceStore(
        case["projects"],
        sources={(case["project_id"], "secret:go"): LocalKeyFile("local-go", case["key_file"])},
        private_directory=case["private"],
        clock=lambda: 1000.0,
    )


def register(case: dict, **kwargs: object) -> dict:
    return service(case).register(
        case["project_id"], "secret:go", principal="owner", command_key="register", **kwargs
    )


def test_public_registration_reopens_and_rejects_changed_material_with_same_mtime(
    case: dict,
) -> None:
    # DrvFS utime rounds to whole seconds. Choose a representable timestamp before
    # registration so both operating systems genuinely preserve the same mtime.
    os.utime(case["key_file"], ns=(1_600_000_000_000_000_000,) * 2)
    record = register(case)
    assert record == {
        "schema_version": "karajan.credential-generation.v1",
        "project_id": case["project_id"],
        "auth_ref": "secret:go",
        "generation": record["generation"],
        "source": {"kind": "controller_local_key_file", "id": "local-go"},
        "registered_at": 1000.0,
        "previous_generation": None,
    }
    reopened = service(case)
    assert reopened.current(case["project_id"], "secret:go", principal="owner") == record
    material = reopened.resolve_exact(
        case["project_id"], "secret:go", record["generation"], principal="owner"
    )
    assert material.reveal() == case["secret"]
    assert (material.project_id, material.auth_ref, material.generation, material.source_id) == (
        case["project_id"],
        "secret:go",
        record["generation"],
        "local-go",
    )
    assert case["secret"] not in repr(material)
    assert case["secret"] not in str(material)
    before = case["key_file"].stat()
    case["key_file"].write_text("synthetic-other-secret-for-tests\n", encoding="utf-8")
    os.utime(case["key_file"], ns=(before.st_atime_ns, before.st_mtime_ns))
    assert case["key_file"].stat().st_mtime_ns == before.st_mtime_ns
    with pytest.raises(CredentialSourceError, match="^CREDENTIAL_MATERIAL_CHANGED$"):
        service(case).current(case["project_id"], "secret:go", principal="owner")
    with pytest.raises(CredentialSourceError, match="^CREDENTIAL_MATERIAL_CHANGED$"):
        service(case).resolve_exact(
            case["project_id"], "secret:go", record["generation"], principal="owner"
        )


def test_rotation_revocation_and_command_replays_preserve_immutable_history(case: dict) -> None:
    first = register(case)
    store = service(case)
    case["key_file"].write_text("synthetic-rotated-secret-value", encoding="utf-8")
    assert register(case) == first  # Historical command receipt, not current authorization.
    with pytest.raises(CredentialSourceError, match="^CREDENTIAL_GENERATION_CONFLICT$"):
        store.register(case["project_id"], "secret:go", principal="owner", command_key="wrong-cas")
    second = store.register(
        case["project_id"],
        "secret:go",
        expected_generation=first["generation"],
        principal="owner",
        command_key="rotate",
    )
    assert second["generation"] != first["generation"]
    assert second["previous_generation"] == first["generation"]
    assert store.get(case["project_id"], "secret:go", first["generation"], principal="owner") == {
        "record": first,
        "revoked": False,
        "revocation": None,
    }
    with pytest.raises(CredentialSourceError, match="^CREDENTIAL_GENERATION_CHANGED$"):
        store.resolve_exact(case["project_id"], "secret:go", first["generation"], principal="owner")
    revoked = store.revoke(
        case["project_id"],
        "secret:go",
        second["generation"],
        principal="owner",
        command_key="revoke",
    )
    assert revoked["record"] == second
    assert revoked["revoked"] is True
    assert (
        service(case).revoke(
            case["project_id"],
            "secret:go",
            second["generation"],
            principal="owner",
            command_key="revoke",
        )
        == revoked
    )
    for action in (
        lambda: service(case).current(case["project_id"], "secret:go", principal="owner"),
        lambda: service(case).resolve_exact(
            case["project_id"], "secret:go", second["generation"], principal="owner"
        ),
    ):
        with pytest.raises(CredentialSourceError, match="^CREDENTIAL_GENERATION_REVOKED$"):
            action()
    assert (
        store.register(
            case["project_id"],
            "secret:go",
            expected_generation=first["generation"],
            principal="owner",
            command_key="rotate",
        )
        == second
    )
    with pytest.raises(ProjectError, match="^IDEMPOTENCY_CONFLICT$"):
        store.revoke(
            case["project_id"],
            "secret:go",
            first["generation"],
            principal="owner",
            command_key="rotate",
        )


def test_owner_check_precedes_material_read_and_mapping_cannot_be_supplied_in_request(
    case: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = service(case)
    original = Path.open

    def no_credential_read(path, *args, **kwargs):
        if path == case["key_file"]:
            pytest.fail("Unauthorized caller reached credential material")
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", no_credential_read)
        for principal, auth_ref, error, code in (
            ("other", "secret:go", ProjectError, "USER_DECISION_REQUIRED"),
            ("owner", "secret:unknown", CredentialSourceError, "CREDENTIAL_SOURCE_UNCONFIGURED"),
        ):
            with pytest.raises(error, match="^" + code + "$"):
                store.register(
                    case["project_id"], auth_ref, principal=principal, command_key="owner-check"
                )
    with pytest.raises(TypeError):
        store.register(
            case["project_id"],
            "secret:go",
            principal="owner",
            command_key="path-request",
            path=case["key_file"],
        )
    record = register(case)
    for method, arguments in (
        (store.current, (case["project_id"], "secret:go")),
        (store.get, (case["project_id"], "secret:go", record["generation"])),
        (store.resolve_exact, (case["project_id"], "secret:go", record["generation"])),
    ):
        with pytest.raises(ProjectError, match="^USER_DECISION_REQUIRED$"):
            method(*arguments, principal="other")


def test_current_locked_uses_the_held_project_transaction_and_blocks_revoke(case: dict) -> None:
    record = register(case)
    store = service(case)
    attempted = threading.Event()

    def revoke():
        attempted.set()
        return store.revoke(
            case["project_id"],
            "secret:go",
            record["generation"],
            principal="owner",
            command_key="concurrent-revoke",
        )

    with ThreadPoolExecutor(max_workers=1) as pool:
        with case["projects"]._transaction() as db:
            commands = []
            db.set_trace_callback(commands.append)
            assert (
                store.current_locked(db, case["project_id"], "secret:go", principal="owner")
                == record
            )
            future = pool.submit(revoke)
            assert attempted.wait(timeout=2)
            assert future.done() is False
            assert (
                store.current_locked(db, case["project_id"], "secret:go", principal="owner")
                == record
            )
            assert not any(command.startswith("BEGIN") for command in commands)
        assert future.result(timeout=5)["revoked"] is True
    with sqlite3.connect(case["projects"].database) as db:
        with pytest.raises(
            CredentialSourceError, match="^CREDENTIAL_PROJECT_TRANSACTION_REQUIRED$"
        ):
            store.current_locked(db, case["project_id"], "secret:go", principal="owner")


@pytest.mark.parametrize("change", ["source_id", "source_path", "unconfigured"])
def test_current_controller_source_changes_reject_old_generation(case: dict, change: str) -> None:
    record = register(case)
    other = case["key_file"].with_name("other-source.key")
    other.write_bytes(case["key_file"].read_bytes())
    mappings = {
        (case["project_id"], "secret:go"): LocalKeyFile(
            "changed-source" if change == "source_id" else "local-go",
            other if change == "source_path" else case["key_file"],
        )
    }
    changed = CredentialSourceStore(
        case["projects"],
        sources={} if change == "unconfigured" else mappings,
        private_directory=case["private"],
    )
    with pytest.raises(CredentialSourceError):
        changed.current(case["project_id"], "secret:go", principal="owner")
    assert (
        changed.get(case["project_id"], "secret:go", record["generation"], principal="owner")[
            "record"
        ]
        == record
    )


def test_public_database_and_results_contain_no_key_path_key_or_material_digest(case: dict) -> None:
    record = register(case)
    material = service(case).resolve_exact(
        case["project_id"], "secret:go", record["generation"], principal="owner"
    )
    with pytest.raises(CredentialSourceError, match="^CREDENTIAL_SERIALIZATION_FORBIDDEN$"):
        pickle.dumps(material)
    raw = case["key_file"].read_bytes()
    with sqlite3.connect(case["projects"].database) as db:
        public = "\n".join(db.iterdump())
    public += json.dumps(record)
    for forbidden in (
        case["secret"],
        str(case["key_file"]),
        hashlib.sha256(raw).hexdigest(),
        hashlib.sha256(case["secret"].encode()).hexdigest(),
    ):
        assert forbidden not in public
    for path in case["private"].iterdir():
        assert case["secret"].encode() not in path.read_bytes()


def test_private_state_inside_repository_is_rejected_before_any_private_file_write(
    case: dict,
) -> None:
    project = case["projects"].get(case["project_id"])
    private = Path(project["repository"]["root"]) / "unsafe-private"
    with pytest.raises(CredentialSourceError, match="^CREDENTIAL_PRIVATE_STATE_IN_REPOSITORY$"):
        CredentialSourceStore(case["projects"], sources={}, private_directory=private)
    assert not private.exists()


def test_failed_public_commit_leaves_private_orphan_without_automatic_generation_replay(
    case: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = service(case)
    original_connect = sqlite3.connect
    failed = False

    class DroppedCommit(sqlite3.Connection):
        def commit(self):
            nonlocal failed
            if not failed and self.execute("SELECT 1 FROM credential_generations").fetchone():
                failed = True
                raise sqlite3.OperationalError("synthetic public commit loss")
            return super().commit()

    def connecting(database, *args, **kwargs):
        if database == case["projects"].database:
            kwargs["factory"] = DroppedCommit
        return original_connect(database, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", connecting)
        with pytest.raises(CredentialSourceError, match="^CREDENTIAL_STATE_UNAVAILABLE$"):
            store.register(
                case["project_id"], "secret:go", principal="owner", command_key="lost-register"
            )
    assert failed
    with pytest.raises(CredentialSourceError, match="^CREDENTIAL_GENERATION_NOT_FOUND$"):
        service(case).current(case["project_id"], "secret:go", principal="owner")
    with pytest.raises(CredentialSourceError, match="^CREDENTIAL_REGISTRATION_INCOMPLETE$"):
        service(case).register(
            case["project_id"], "secret:go", principal="owner", command_key="lost-register"
        )
    with sqlite3.connect(case["projects"].database) as db:
        assert db.execute("SELECT count(*) FROM credential_generations").fetchone()[0] == 0
    # A separately authorized new command may register; the incomplete command
    # itself remains an immutable orphan and does not acquire that new result.
    record = register(case)
    assert service(case).current(case["project_id"], "secret:go", principal="owner") == record
    with pytest.raises(CredentialSourceError, match="^CREDENTIAL_REGISTRATION_INCOMPLETE$"):
        service(case).register(
            case["project_id"], "secret:go", principal="owner", command_key="lost-register"
        )


def test_concurrent_registration_has_one_generation_and_identical_idempotent_receipts(
    case: dict,
) -> None:
    store = service(case)

    def perform(_):
        return store.register(
            case["project_id"], "secret:go", principal="owner", command_key="same-command"
        )

    with ThreadPoolExecutor(max_workers=6) as pool:
        records = list(pool.map(perform, range(6)))
    assert all(record == records[0] for record in records)
    with sqlite3.connect(case["projects"].database) as db:
        assert db.execute("SELECT count(*) FROM credential_generations").fetchone()[0] == 1


@pytest.mark.parametrize("part", ["material-seal.key", "material-seals.sqlite"])
def test_missing_private_state_never_regenerates_or_accepts_public_metadata(
    case: dict, part: str
) -> None:
    record = register(case)
    missing = case["private"] / part
    missing.unlink()
    with pytest.raises(CredentialSourceError):
        service(case)
    assert not missing.exists()
    with sqlite3.connect(case["projects"].database) as db:
        assert (
            json.loads(db.execute("SELECT record FROM credential_generations").fetchone()[0])
            == record
        )


def test_private_permission_weakening_is_rejected_without_silent_repair(case: dict) -> None:
    register(case)
    private = case["private"]
    if sys.platform == "win32":
        subprocess.run(
            ["icacls", str(private), "/grant", "*S-1-1-0:(OI)(CI)R"],
            check=True,
            capture_output=True,
        )
    else:
        private.chmod(0o755)
    try:
        with pytest.raises(CredentialSourceError, match="^CREDENTIAL_PRIVATE_PERMISSIONS_INVALID$"):
            service(case)
    finally:
        if sys.platform == "win32":
            subprocess.run(
                ["icacls", str(private), "/remove:g", "*S-1-1-0"],
                check=True,
                capture_output=True,
            )
        else:
            private.chmod(0o700)


@pytest.mark.parametrize(
    "raw",
    [b"", b"short", b"a" * 4097, b"a" * 20 + b"\x00", b"a" * 20 + b"\n" + b"b" * 20, b"\xff" * 20],
)
def test_invalid_material_returns_only_a_code_and_no_registration(case: dict, raw: bytes) -> None:
    store = service(case)
    case["key_file"].write_bytes(raw)
    with pytest.raises(CredentialSourceError) as failure:
        store.register(case["project_id"], "secret:go", principal="owner", command_key="invalid")
    assert str(failure.value) in {"CREDENTIAL_MATERIAL_INVALID", "CREDENTIAL_MATERIAL_UNAVAILABLE"}
    with sqlite3.connect(case["projects"].database) as db:
        assert db.execute("SELECT count(*) FROM credential_generations").fetchone()[0] == 0


def test_content_seal_includes_original_bytes_not_only_the_stripped_secret(case: dict) -> None:
    record = register(case)
    case["key_file"].write_text(case["secret"], encoding="utf-8")
    with pytest.raises(CredentialSourceError, match="^CREDENTIAL_MATERIAL_CHANGED$"):
        service(case).resolve_exact(
            case["project_id"], "secret:go", record["generation"], principal="owner"
        )


def test_hardlinked_source_is_rejected(case: dict) -> None:
    linked = case["key_file"].with_name("second-link.key")
    os.link(case["key_file"], linked)
    with pytest.raises(CredentialSourceError, match="^CREDENTIAL_PATH_INVALID$"):
        register(case)
