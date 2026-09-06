"""Controller-owned local credential generations, with no material in the project DB.

Only constructor configuration supplies file paths. The private directory contains
a random HMAC key and material seals, never a copied provider key. POSIX requires
owner-only modes; Windows requires an owner/admin/SYSTEM-only DACL. This protects
against other local users, not the trusted controller account or host administrator.
WSL must use native Linux storage for this directory, not unverifiable DrvFS modes.
The directory and its parents must remain controller-managed; this is not a
hostile-filesystem or same-account sandbox.

Private registration commits before public registration. A private orphan fails
closed on replay; it is never silently promoted or retried as a fresh generation.
SQLite transactions do not lock external files. Each consumer checks the actual
bytes again. Returned Python strings cannot be revoked or reliably erased; callers
must keep them out of logs and recheck the generation before recording evidence.
"""

import ctypes
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sqlite3
import stat
import sys
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .publication import digest
from .registry import ProjectRegistry, encoded, identifier


class CredentialSourceError(ValueError):
    @property
    def code(self) -> str:
        return str(self)


@dataclass(frozen=True, slots=True)
class LocalKeyFile:
    """Trusted controller configuration; never deserialize this from an HTTP request."""

    source_id: str
    path: Path = field(repr=False)


@dataclass(frozen=True, slots=True, repr=False)
class ResolvedCredential:
    """An in-memory exact-generation result; reveal only at the trusted relay boundary."""

    project_id: str
    auth_ref: str
    generation: str
    source_id: str
    _secret: str = field(repr=False)

    def reveal(self) -> str:
        return self._secret

    def __repr__(self) -> str:
        return "ResolvedCredential(<redacted>)"

    def __str__(self) -> str:
        return repr(self)

    def __getstate__(self) -> object:
        raise CredentialSourceError("CREDENTIAL_SERIALIZATION_FORBIDDEN")


def _plain(path: Path, *, directory: bool = False) -> os.stat_result:
    try:
        info = path.lstat()
        if (
            stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
            or (not stat.S_ISDIR(info.st_mode) if directory else not stat.S_ISREG(info.st_mode))
            or (not directory and info.st_nlink != 1)
        ):
            raise CredentialSourceError("CREDENTIAL_PATH_INVALID")
        return info
    except OSError:
        raise CredentialSourceError("CREDENTIAL_PATH_INVALID") from None


def _ancestors(path: Path) -> None:
    for parent in path.parents:
        _plain(parent, directory=True)


def _windows_private(path: Path, *, directory: bool) -> None:
    """Read the actual DACL; do not confuse Windows chmod bits with access control."""
    if sys.platform != "win32":
        raise CredentialSourceError("CREDENTIAL_PRIVATE_PLATFORM_UNSUPPORTED")
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    pointer = ctypes.c_void_p
    advapi.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(pointer),
        ctypes.POINTER(pointer),
        ctypes.POINTER(pointer),
        ctypes.POINTER(pointer),
        ctypes.POINTER(pointer),
    ]
    advapi.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi.ConvertSidToStringSidW.argtypes = [pointer, ctypes.POINTER(wintypes.LPWSTR)]
    advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        pointer,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(pointer)]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    advapi.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        pointer,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi.GetTokenInformation.restype = wintypes.BOOL
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [pointer]
    kernel.LocalFree.restype = pointer
    token, descriptor, owner = pointer(), pointer(), pointer()

    def sid_text(sid: Any) -> str:
        text = wintypes.LPWSTR()
        if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise CredentialSourceError("CREDENTIAL_PRIVATE_PERMISSIONS_INVALID")
        try:
            return str(text.value)
        finally:
            kernel.LocalFree(text)

    try:
        if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
            raise CredentialSourceError("CREDENTIAL_PRIVATE_PERMISSIONS_INVALID")
        size = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        if not 1 <= size.value <= 65536:
            raise CredentialSourceError("CREDENTIAL_PRIVATE_PERMISSIONS_INVALID")
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi.GetTokenInformation(token, 1, buffer, size.value, ctypes.byref(size)):
            raise CredentialSourceError("CREDENTIAL_PRIVATE_PERMISSIONS_INVALID")
        principal_sid = sid_text(ctypes.cast(buffer, ctypes.POINTER(pointer))[0])
        if advapi.GetNamedSecurityInfoW(
            str(path), 1, 5, ctypes.byref(owner), None, None, None, ctypes.byref(descriptor)
        ):
            raise CredentialSourceError("CREDENTIAL_PRIVATE_PERMISSIONS_INVALID")
        if sid_text(owner) != principal_sid:
            raise CredentialSourceError("CREDENTIAL_PRIVATE_PERMISSIONS_INVALID")
        text = wintypes.LPWSTR()
        if not advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            descriptor, 1, 4, ctypes.byref(text), None
        ):
            raise CredentialSourceError("CREDENTIAL_PRIVATE_PERMISSIONS_INVALID")
        try:
            sddl = str(text.value)
        finally:
            kernel.LocalFree(text)
        aces = [value.split(";") for value in re.findall(r"\(([^()]*)\)", sddl)]
        allowed = {principal_sid, "OW", "SY", "BA", "S-1-5-18", "S-1-5-32-544", "S-1-3-4"}
        if (
            not aces
            or directory
            and not sddl.startswith("D:P")
            or any(len(ace) != 6 or ace[0] != "A" or ace[5] not in allowed for ace in aces)
        ):
            raise CredentialSourceError("CREDENTIAL_PRIVATE_PERMISSIONS_INVALID")
    finally:
        if descriptor:
            kernel.LocalFree(descriptor)
        if token:
            kernel.CloseHandle(token)


def _private(path: Path, *, directory: bool = False) -> None:
    info = _plain(path, directory=directory)
    _ancestors(path)
    if sys.platform == "win32":
        _windows_private(path, directory=directory)
    else:
        if os.name != "posix":
            raise CredentialSourceError("CREDENTIAL_PRIVATE_PLATFORM_UNSUPPORTED")
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise CredentialSourceError("CREDENTIAL_PRIVATE_PERMISSIONS_INVALID")


def _new_file(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


class CredentialSourceStore:
    def __init__(
        self,
        projects: ProjectRegistry,
        *,
        sources: Mapping[tuple[str, str], LocalKeyFile],
        private_directory: Path,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.projects = projects
        self.clock = clock
        self._sources: dict[tuple[str, str], LocalKeyFile] = {}
        for identity, source in sources.items():
            if (
                not isinstance(identity, tuple)
                or len(identity) != 2
                or not isinstance(source, LocalKeyFile)
                or not isinstance(source.path, Path)
            ):
                raise CredentialSourceError("CREDENTIAL_SOURCE_CONFIGURATION_INVALID")
            self._sources[(identifier(identity[0]), identifier(identity[1]))] = LocalKeyFile(
                identifier(source.source_id), source.path.absolute()
            )
        self._directory = private_directory.absolute()
        self._seed_file = self._directory / "material-seal.key"
        self._private_db = self._directory / "material-seals.sqlite"
        try:
            with projects._transaction() as db:
                self._outside_repositories(db)
            _ancestors(self._directory)
            new = not self._directory.exists() and not self._directory.is_symlink()
            if new:
                self._directory.mkdir(mode=0o700)
            _private(self._directory, directory=True)
            if new:
                _new_file(self._seed_file, secrets.token_bytes(32))
                _new_file(self._private_db, b"")
                with self._materials(write=True) as private:
                    private.execute(
                        "CREATE TABLE material_seals (generation TEXT PRIMARY KEY, "
                        "principal TEXT NOT NULL, command_key TEXT NOT NULL, "
                        "request_digest TEXT NOT NULL, seal TEXT NOT NULL, "
                        "UNIQUE(principal,command_key))"
                    )
            else:
                self._seed()
                with self._materials() as private:
                    private.execute("SELECT generation FROM material_seals LIMIT 1")
            with projects._transaction() as db:
                self._outside_repositories(db)
                db.execute(
                    "CREATE TABLE IF NOT EXISTS credential_generations ("
                    "generation TEXT PRIMARY KEY, "
                    "project_id TEXT NOT NULL REFERENCES projects(id), "
                    "auth_ref TEXT NOT NULL, record TEXT NOT NULL, digest TEXT NOT NULL)"
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS credential_current ("
                    "project_id TEXT NOT NULL REFERENCES projects(id), auth_ref TEXT NOT NULL, "
                    "generation TEXT NOT NULL REFERENCES credential_generations(generation), "
                    "PRIMARY KEY(project_id,auth_ref))"
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS credential_revocations ("
                    "generation TEXT PRIMARY KEY REFERENCES credential_generations(generation), "
                    "record TEXT NOT NULL)"
                )
        except (OSError, sqlite3.Error):
            raise CredentialSourceError("CREDENTIAL_PRIVATE_STATE_INVALID") from None

    def _outside_repositories(self, db: sqlite3.Connection) -> None:
        for row in db.execute("SELECT snapshot FROM projects"):
            repository = Path(json.loads(row["snapshot"])["repository"]["root"]).resolve()
            if self._directory.is_relative_to(repository):
                raise CredentialSourceError("CREDENTIAL_PRIVATE_STATE_IN_REPOSITORY")

    @contextmanager
    def _materials(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        _private(self._directory, directory=True)
        _private(self._private_db)
        db = sqlite3.connect(
            self._private_db if write else self._private_db.as_uri() + "?mode=ro",
            uri=not write,
            isolation_level=None,
            timeout=10,
        )
        db.row_factory = sqlite3.Row
        try:
            if write:
                db.execute("PRAGMA synchronous=FULL")
                db.execute("BEGIN IMMEDIATE")
            yield db
            if write:
                db.commit()
        except BaseException:
            if write:
                db.rollback()
            raise
        finally:
            db.close()

    def _seed(self) -> bytes:
        _private(self._directory, directory=True)
        _private(self._seed_file)
        seed = self._seed_file.read_bytes()
        if len(seed) != 32:
            raise CredentialSourceError("CREDENTIAL_PRIVATE_STATE_INVALID")
        return seed

    @contextmanager
    def _owned(self, project_id: str, principal: str) -> Iterator[sqlite3.Connection]:
        identifier(project_id)
        identifier(principal)
        try:
            with self.projects._transaction() as db:
                self.projects._require_owner(db, project_id, principal)
                self._outside_repositories(db)
                yield db
        except (OSError, sqlite3.Error):
            raise CredentialSourceError("CREDENTIAL_STATE_UNAVAILABLE") from None

    def _now(self) -> float:
        value = self.clock()
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise CredentialSourceError("CREDENTIAL_CLOCK_INVALID")
        return float(value)

    def _source(self, project_id: str, auth_ref: str) -> LocalKeyFile:
        source = self._sources.get((project_id, auth_ref))
        if source is None:
            raise CredentialSourceError("CREDENTIAL_SOURCE_UNCONFIGURED")
        return source

    def _read(self, source: LocalKeyFile) -> tuple[bytes, str]:
        try:
            _ancestors(source.path)
            original = _plain(source.path)
            with source.path.open("rb") as stream:
                before = os.fstat(stream.fileno())
                raw = stream.read(4097)
                after = os.fstat(stream.fileno())
            current = _plain(source.path)
            if (
                (before.st_dev, before.st_ino) != (original.st_dev, original.st_ino)
                or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
                or (before.st_mtime_ns, before.st_ctime_ns, before.st_size)
                != (after.st_mtime_ns, after.st_ctime_ns, after.st_size)
                or not 16 <= len(raw) <= 4096
            ):
                raise CredentialSourceError("CREDENTIAL_MATERIAL_INVALID")
            secret = raw.decode("utf-8-sig").strip()
            if (
                not 16 <= len(secret) <= 4096
                or not secret.isascii()
                or not secret.isprintable()
                or any(character.isspace() for character in secret)
            ):
                raise CredentialSourceError("CREDENTIAL_MATERIAL_INVALID")
            return raw, secret
        except (OSError, UnicodeError):
            raise CredentialSourceError("CREDENTIAL_MATERIAL_UNAVAILABLE") from None

    def _seal(self, record: dict[str, Any], source: LocalKeyFile, raw: bytes) -> str:
        context = encoded([record, str(source.path)]).encode()
        return hmac.new(self._seed(), context + b"\0" + raw, hashlib.sha256).hexdigest()

    def _record(
        self, db: sqlite3.Connection, project_id: str, auth_ref: str, generation: str
    ) -> dict[str, Any]:
        row = db.execute(
            "SELECT record,digest FROM credential_generations WHERE generation=? "
            "AND project_id=? AND auth_ref=?",
            (generation, project_id, auth_ref),
        ).fetchone()
        if row is None:
            raise CredentialSourceError("CREDENTIAL_GENERATION_NOT_FOUND")
        record: dict[str, Any] = json.loads(row["record"])
        if digest(record) != row["digest"] or (
            record["project_id"],
            record["auth_ref"],
            record["generation"],
        ) != (project_id, auth_ref, generation):
            raise CredentialSourceError("CREDENTIAL_RECORD_CHANGED")
        return record

    def _current_id(self, db: sqlite3.Connection, project_id: str, auth_ref: str) -> str | None:
        row = db.execute(
            "SELECT generation FROM credential_current WHERE project_id=? AND auth_ref=?",
            (project_id, auth_ref),
        ).fetchone()
        return str(row["generation"]) if row is not None else None

    def register(
        self,
        project_id: str,
        auth_ref: str,
        *,
        principal: str,
        command_key: str,
        expected_generation: str | None = None,
    ) -> dict[str, Any]:
        auth_ref = identifier(auth_ref)
        if expected_generation is not None:
            identifier(expected_generation)
        request_digest = digest(["credential_register", project_id, auth_ref, expected_generation])
        with self._owned(project_id, principal) as db:
            replay = self.projects._replay(db, principal, command_key, request_digest)
            if replay is not None:
                return replay
            with self._materials() as private:
                orphan = private.execute(
                    "SELECT request_digest FROM material_seals WHERE principal=? AND command_key=?",
                    (principal, command_key),
                ).fetchone()
            if orphan is not None:
                reason = "CREDENTIAL_REGISTRATION_INCOMPLETE"
                if orphan["request_digest"] != request_digest:
                    reason = "IDEMPOTENCY_CONFLICT"
                raise CredentialSourceError(reason)
            if self._current_id(db, project_id, auth_ref) != expected_generation:
                raise CredentialSourceError("CREDENTIAL_GENERATION_CONFLICT")
            source = self._source(project_id, auth_ref)
            raw, _ = self._read(source)
            record = {
                "schema_version": "karajan.credential-generation.v1",
                "project_id": project_id,
                "auth_ref": auth_ref,
                "generation": str(uuid.uuid4()),
                "source": {"kind": "controller_local_key_file", "id": source.source_id},
                "registered_at": self._now(),
                "previous_generation": expected_generation,
            }
            seal = self._seal(record, source, raw)
            with self._materials(write=True) as private:
                private.execute(
                    "INSERT INTO material_seals VALUES (?,?,?,?,?)",
                    (record["generation"], principal, command_key, request_digest, seal),
                )
            db.execute(
                "INSERT INTO credential_generations VALUES (?,?,?,?,?)",
                (record["generation"], project_id, auth_ref, encoded(record), digest(record)),
            )
            db.execute(
                "INSERT INTO credential_current VALUES (?,?,?) ON CONFLICT(project_id,auth_ref) "
                "DO UPDATE SET generation=excluded.generation",
                (project_id, auth_ref, record["generation"]),
            )
            db.execute(
                "INSERT INTO commands VALUES (?,?,?,?)",
                (principal, command_key, request_digest, encoded(record)),
            )
            return record

    def _checked(
        self,
        db: sqlite3.Connection,
        project_id: str,
        auth_ref: str,
        *,
        expected_generation: str | None = None,
    ) -> tuple[dict[str, Any], str]:
        generation = self._current_id(db, project_id, auth_ref)
        if generation is None:
            raise CredentialSourceError("CREDENTIAL_GENERATION_NOT_FOUND")
        if expected_generation is not None and generation != expected_generation:
            raise CredentialSourceError("CREDENTIAL_GENERATION_CHANGED")
        record = self._record(db, project_id, auth_ref, generation)
        if (
            db.execute(
                "SELECT 1 FROM credential_revocations WHERE generation=?", (generation,)
            ).fetchone()
            is not None
        ):
            raise CredentialSourceError("CREDENTIAL_GENERATION_REVOKED")
        source = self._source(project_id, auth_ref)
        if record["source"] != {"kind": "controller_local_key_file", "id": source.source_id}:
            raise CredentialSourceError("CREDENTIAL_SOURCE_CHANGED")
        raw, secret = self._read(source)
        seal = self._seal(record, source, raw)
        with self._materials() as private:
            observed = private.execute(
                "SELECT seal FROM material_seals WHERE generation=?", (generation,)
            ).fetchone()
        if observed is None:
            raise CredentialSourceError("CREDENTIAL_PRIVATE_RECORD_MISSING")
        if not hmac.compare_digest(observed["seal"], seal):
            raise CredentialSourceError("CREDENTIAL_MATERIAL_CHANGED")
        return record, secret

    def current_locked(
        self, db: sqlite3.Connection, project_id: str, auth_ref: str, *, principal: str
    ) -> dict[str, Any]:
        """Read within the caller's ProjectRegistry BEGIN IMMEDIATE; never nest BEGIN.

        The controller owns this internal port. Public records stay stable while
        its transaction is held. File reads are fresh observations, not file locks.
        """
        identifier(project_id)
        identifier(auth_ref)
        identifier(principal)
        try:
            databases = db.execute("PRAGMA database_list").fetchall()
            if not db.in_transaction or not any(
                row[1] == "main" and Path(row[2]).resolve() == self.projects.database.resolve()
                for row in databases
            ):
                raise CredentialSourceError("CREDENTIAL_PROJECT_TRANSACTION_REQUIRED")
            self.projects._require_owner(db, project_id, principal)
            self._outside_repositories(db)
            return self._checked(db, project_id, auth_ref)[0]
        except (OSError, sqlite3.Error):
            raise CredentialSourceError("CREDENTIAL_STATE_UNAVAILABLE") from None

    def current(self, project_id: str, auth_ref: str, *, principal: str) -> dict[str, Any]:
        with self._owned(project_id, principal) as db:
            return self.current_locked(db, project_id, auth_ref, principal=principal)

    def resolve_exact(
        self, project_id: str, auth_ref: str, generation: str, *, principal: str
    ) -> ResolvedCredential:
        identifier(auth_ref)
        identifier(generation)
        with self._owned(project_id, principal) as db:
            record, secret = self._checked(db, project_id, auth_ref, expected_generation=generation)
        return ResolvedCredential(project_id, auth_ref, generation, record["source"]["id"], secret)

    def _view(
        self, db: sqlite3.Connection, project_id: str, auth_ref: str, generation: str
    ) -> dict[str, Any]:
        record = self._record(db, project_id, auth_ref, generation)
        row = db.execute(
            "SELECT record FROM credential_revocations WHERE generation=?", (generation,)
        ).fetchone()
        return {
            "record": record,
            "revoked": row is not None,
            "revocation": json.loads(row["record"]) if row else None,
        }

    def get(
        self, project_id: str, auth_ref: str, generation: str, *, principal: str
    ) -> dict[str, Any]:
        identifier(auth_ref)
        identifier(generation)
        with self._owned(project_id, principal) as db:
            return self._view(db, project_id, auth_ref, generation)

    def revoke(
        self, project_id: str, auth_ref: str, generation: str, *, principal: str, command_key: str
    ) -> dict[str, Any]:
        identifier(auth_ref)
        identifier(generation)
        request_digest = digest(["credential_revoke", project_id, auth_ref, generation])
        with self._owned(project_id, principal) as db:
            replay = self.projects._replay(db, principal, command_key, request_digest)
            if replay is not None:
                return replay
            view = self._view(db, project_id, auth_ref, generation)
            if not view["revoked"]:
                revocation = {
                    "project_id": project_id,
                    "auth_ref": auth_ref,
                    "generation": generation,
                    "revoked_by": principal,
                    "revoked_at": self._now(),
                }
                db.execute(
                    "INSERT INTO credential_revocations VALUES (?,?)",
                    (generation, encoded(revocation)),
                )
            result = self._view(db, project_id, auth_ref, generation)
            db.execute(
                "INSERT INTO commands VALUES (?,?,?,?)",
                (principal, command_key, request_digest, encoded(result)),
            )
            return result
