"""Durable, project-scoped gateway catalog inside the owned project database.

The store reuses ProjectRegistry's transaction, ownership check and idempotency
ledger instead of opening a second store: every command is bound to a
``principal``, every conditional write compares the durable head, and every
revision is append-only so an old reference keeps resolving after a newer
revision exists. Nothing here contacts an upstream or reads a credential except
inside :meth:`GatewayCatalogStore.probe_catalog`.

Every lookup is scoped by ``project_id``. A reference to a connection or
binding owned by another project is therefore not found rather than reported:
the rejection must not disclose whether that identity exists elsewhere.

Two boundaries are structural rather than documented:

* Every public object carries its own canonical content digest. The digest
  covers the record *excluding* the digest field, so it cannot be
  self-referential, and it is recomputed and compared on every read and list.
* A catalog probe never holds a project write transaction across credential
  resolution or network I/O. Blocking work happens between two short
  transactions around a durable claim, so an unreachable upstream in one
  project cannot stall another project's catalog.
"""

import hashlib
import json
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from pydantic import BaseModel, ValidationError

from karajan.projects import ProjectError, ProjectRegistry
from karajan.storage import ExistingStoreError, require_schema

from .errors import GatewayError
from .models import (
    BindingCreate,
    ConnectionCreate,
    addressable,
    discovery_path,
    registered_origin,
)
from .probe import CatalogObservation, SessionSecretResolver, observed_catalog, public_observation

#: A claim older than this is treated as interrupted and reconciled as unknown.
#: It is never silently retried under the same command key.
PROBE_CLAIM_TTL_SECONDS = 300.0

#: Schema of the reservation marker written when a probe claims its command key.
#: It is deliberately distinct from a settled observation: a marker means the
#: key is held but no outcome exists yet, so it must never be replayed as a
#: result. Terminal documents use the observation schema below.
PENDING_SCHEMA_VERSION = "karajan.gateway-probe-pending.v1"
OBSERVATION_SCHEMA_VERSION = "karajan.gateway-catalog-observation.v1"

REQUIRED_SCHEMA = {
    "projects": ["id", "snapshot"],
    "project_owners": ["project_id", "principal"],
    "commands": ["principal", "key", "digest", "result"],
    "gateway_connections": ["project_id", "id", "revision", "record", "digest"],
    "gateway_connection_current": ["project_id", "id", "revision"],
    "gateway_bindings": ["project_id", "id", "revision", "record", "digest"],
    "gateway_binding_current": ["project_id", "id", "revision"],
    "gateway_catalog_observations": ["observation_id", "project_id", "id", "revision", "record"],
    "gateway_probe_claims": [
        "principal",
        "command_key",
        "request_digest",
        "project_id",
        "connection_id",
        "connection_revision",
        "connection_digest",
        "state",
        "started_at",
        "completed_at",
        "observation_id",
    ],
}


def digest(value: Any) -> str:
    """Canonical content digest; never raises a raw serialization error."""
    try:
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()
    except (TypeError, ValueError, RecursionError):
        raise GatewayError("GATEWAY_CONTENT_NOT_SERIALIZABLE") from None


def encoded(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise GatewayError("GATEWAY_CONTENT_NOT_SERIALIZABLE") from None


def content_digest(record: dict[str, Any]) -> str:
    """Digest a record's own content, excluding the digest field itself."""
    return digest({key: value for key, value in record.items() if key != "digest"})


def sealed(record: dict[str, Any]) -> dict[str, Any]:
    """Return the record with its canonical content digest attached."""
    return {**record, "digest": content_digest(record)}


def identifier(value: object) -> str:
    """Accept exactly the identifier shape the persistence layer can round-trip."""
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 256
        or any(character.isspace() for character in value)
        or any(character < " " or character == "\x7f" for character in value)
    ):
        raise GatewayError("GATEWAY_IDENTIFIER_INVALID")
    return value


def addressable_identifier(value: object) -> str:
    """Require an identity that a single URL path segment can address."""
    if not addressable(value):
        raise GatewayError("GATEWAY_IDENTIFIER_NOT_ADDRESSABLE")
    return str(value)


def _positive(value: object) -> int:
    if type(value) is not int or not 1 <= value <= 1_000_000:
        raise GatewayError("GATEWAY_REVISION_INVALID")
    return value


def _validated[MODEL: BaseModel](model: type[MODEL], payload: object) -> MODEL:
    try:
        return model.model_validate(payload)
    except ValidationError:
        raise GatewayError("GATEWAY_INPUT_INVALID") from None


class GatewayCatalogStore:
    def __init__(
        self,
        projects: ProjectRegistry,
        *,
        resolver: SessionSecretResolver | None = None,
        clock: Callable[[], float] = time.time,
        claim_ttl_seconds: float = PROBE_CLAIM_TTL_SECONDS,
    ) -> None:
        self.projects = projects
        self.resolver = resolver
        self.clock = clock
        self.claim_ttl_seconds = claim_ttl_seconds
        # A legacy deployment opened existing-only never provisioned this
        # catalog. Constructing the web application must keep working, so the
        # missing capability is reported per request instead of inventing
        # tables in a database this build does not own.
        self.unavailable: str | None = None
        if projects.existing_only:
            try:
                require_schema(projects.database, REQUIRED_SCHEMA)
            except ExistingStoreError:
                self.unavailable = "GATEWAY_STATE_UNAVAILABLE"
            return
        with projects._transaction() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS gateway_connections ("
                "project_id TEXT NOT NULL REFERENCES projects(id), id TEXT NOT NULL, "
                "revision INTEGER NOT NULL, record TEXT NOT NULL, digest TEXT NOT NULL, "
                "PRIMARY KEY(project_id, id, revision))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS gateway_connection_current ("
                "project_id TEXT NOT NULL REFERENCES projects(id), id TEXT NOT NULL, "
                "revision INTEGER NOT NULL, PRIMARY KEY(project_id, id))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS gateway_bindings ("
                "project_id TEXT NOT NULL REFERENCES projects(id), id TEXT NOT NULL, "
                "revision INTEGER NOT NULL, record TEXT NOT NULL, digest TEXT NOT NULL, "
                "PRIMARY KEY(project_id, id, revision))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS gateway_binding_current ("
                "project_id TEXT NOT NULL REFERENCES projects(id), id TEXT NOT NULL, "
                "revision INTEGER NOT NULL, PRIMARY KEY(project_id, id))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS gateway_catalog_observations ("
                "observation_id TEXT PRIMARY KEY, "
                "project_id TEXT NOT NULL REFERENCES projects(id), id TEXT NOT NULL, "
                "revision INTEGER NOT NULL, record TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS gateway_probe_claims ("
                "principal TEXT NOT NULL, command_key TEXT NOT NULL, "
                "request_digest TEXT NOT NULL, "
                "project_id TEXT NOT NULL REFERENCES projects(id), "
                "connection_id TEXT NOT NULL, connection_revision INTEGER NOT NULL, "
                "connection_digest TEXT NOT NULL, state TEXT NOT NULL, "
                "started_at REAL NOT NULL, completed_at REAL, observation_id TEXT, "
                "PRIMARY KEY(principal, command_key))"
            )

    @contextmanager
    def _owned(self, project_id: str, principal: str) -> Iterator[sqlite3.Connection]:
        """Hold one owned project transaction; the registry owns commit/rollback."""
        identifier(project_id)
        identifier(principal)
        if self.unavailable is not None:
            raise GatewayError(self.unavailable)
        try:
            with self.projects._transaction() as db:
                known = db.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone()
                if known is None:
                    raise GatewayError("GATEWAY_PROJECT_NOT_FOUND")
                self.projects._require_owner(db, project_id, principal)
                yield db
        except ProjectError as error:
            raise GatewayError(error.code, current_revision=error.current_revision) from None
        except (OSError, sqlite3.Error):
            raise GatewayError("GATEWAY_STATE_UNAVAILABLE") from None

    def _replay(
        self, db: sqlite3.Connection, principal: str, command_key: str, request_digest: str
    ) -> dict[str, Any] | None:
        try:
            return self.projects._replay(db, principal, command_key, request_digest)
        except ProjectError as error:
            raise GatewayError(error.code) from None

    def _terminal_receipt(
        self, db: sqlite3.Connection, principal: str, command_key: str
    ) -> dict[str, Any] | None:
        """Read a settled receipt, rejecting a still-pending reservation marker.

        A pending marker means the key is held but the outcome is unknown, so it
        must never be served as a result to a caller.
        """
        row = db.execute(
            "SELECT result FROM commands WHERE principal=? AND key=?", (principal, command_key)
        ).fetchone()
        if row is None:
            return None
        result: dict[str, Any] = json.loads(row["result"])
        if result.get("schema_version") == PENDING_SCHEMA_VERSION:
            return None
        return result

    def _verified(self, raw: str, stored_digest: str) -> dict[str, Any]:
        """Recompute and compare a record's own digest, including its own field."""
        record: dict[str, Any] = json.loads(raw)
        declared = record.get("digest")
        if not isinstance(declared, str) or declared != stored_digest:
            raise GatewayError("GATEWAY_RECORD_CHANGED")
        if content_digest(record) != declared:
            raise GatewayError("GATEWAY_RECORD_CHANGED")
        return record

    def _record(
        self, db: sqlite3.Connection, table: str, project_id: str, identity: str, revision: int
    ) -> dict[str, Any]:
        row = db.execute(
            f"SELECT record, digest FROM {table} WHERE project_id=? AND id=? AND revision=?",
            (project_id, identity, revision),
        ).fetchone()
        if row is None:
            raise GatewayError("GATEWAY_REVISION_NOT_FOUND")
        record = self._verified(row["record"], row["digest"])
        if (record["project_id"], record["id"], record["revision"]) != (
            project_id,
            identity,
            revision,
        ):
            raise GatewayError("GATEWAY_RECORD_CHANGED")
        return record

    def _listed(self, db: sqlite3.Connection, table: str, project_id: str) -> list[dict[str, Any]]:
        """List with the same integrity check a single read performs."""
        if "observations" in table:
            return [
                json.loads(row["record"])
                for row in db.execute(
                    "SELECT record FROM gateway_catalog_observations WHERE project_id=? "
                    "ORDER BY rowid",
                    (project_id,),
                )
            ]
        return [
            self._verified(row["record"], row["digest"])
            for row in db.execute(
                f"SELECT record, digest FROM {table} WHERE project_id=? ORDER BY id, revision",
                (project_id,),
            )
        ]

    def _head(
        self, db: sqlite3.Connection, table: str, project_id: str, identity: str
    ) -> int | None:
        row = db.execute(
            f"SELECT revision FROM {table} WHERE project_id=? AND id=?", (project_id, identity)
        ).fetchone()
        return int(row["revision"]) if row is not None else None

    def get_connection(
        self, project_id: str, connection_id: str, revision: int, *, principal: str
    ) -> dict[str, Any]:
        """Read one exact revision; an older reference keeps resolving."""
        identity, revision = identifier(connection_id), _positive(revision)
        with self._owned(project_id, principal) as db:
            return self._record(db, "gateway_connections", project_id, identity, revision)

    def list_connections(self, project_id: str, *, principal: str) -> list[dict[str, Any]]:
        with self._owned(project_id, principal) as db:
            return self._listed(db, "gateway_connections", project_id)

    def get_binding(
        self, project_id: str, binding_id: str, revision: int, *, principal: str
    ) -> dict[str, Any]:
        identity, revision = identifier(binding_id), _positive(revision)
        with self._owned(project_id, principal) as db:
            return self._record(db, "gateway_bindings", project_id, identity, revision)

    def list_bindings(self, project_id: str, *, principal: str) -> list[dict[str, Any]]:
        with self._owned(project_id, principal) as db:
            return self._listed(db, "gateway_bindings", project_id)

    def list_catalog_observations(self, project_id: str, *, principal: str) -> list[dict[str, Any]]:
        with self._owned(project_id, principal) as db:
            return self._listed(db, "gateway_catalog_observations", project_id)

    def list_probe_claims(self, project_id: str, *, principal: str) -> list[dict[str, Any]]:
        with self._owned(project_id, principal) as db:
            return self._claims(db, project_id)

    def _claims(self, db: sqlite3.Connection, project_id: str) -> list[dict[str, Any]]:
        """Surface every started probe so interrupted work stays reconcilable."""
        now = self.clock()
        return [
            {
                "command_key": row["command_key"],
                "state": row["state"],
                "connection": {
                    "id": row["connection_id"],
                    "revision": row["connection_revision"],
                    "digest": row["connection_digest"],
                },
                "started_at": row["started_at"],
                "completed_at": row["completed_at"],
                "observation_id": row["observation_id"],
                "expired": row["state"] == "in_progress"
                and now - row["started_at"] > self.claim_ttl_seconds,
            }
            for row in db.execute(
                "SELECT * FROM gateway_probe_claims WHERE project_id=? ORDER BY rowid",
                (project_id,),
            )
        ]

    def create_connection(
        self, project_id: str, payload: object, *, principal: str, command_key: str
    ) -> tuple[dict[str, Any], bool]:
        request = _validated(ConnectionCreate, payload)
        identity = addressable_identifier(request.connection_id)
        request_digest = digest(["connection.create", project_id, request.model_dump(mode="json")])
        with self._owned(project_id, principal) as db:
            replay = self._replay(db, principal, command_key, request_digest)
            if replay is not None:
                return replay, False
            if self._head(db, "gateway_connection_current", project_id, identity) is not None:
                raise GatewayError("GATEWAY_CONNECTION_EXISTS")
            return self._commit(
                db,
                "gateway_connections",
                "gateway_connection_current",
                sealed(self._connection_record(project_id, identity, 1, request, principal)),
                principal,
                command_key,
                request_digest,
            )

    def revise_connection(
        self,
        project_id: str,
        connection_id: str,
        payload: object,
        *,
        expected_revision: object,
        principal: str,
        command_key: str,
    ) -> tuple[dict[str, Any], bool]:
        """Publish a new immutable revision; the previous revision is retained."""
        identity, expected = addressable_identifier(connection_id), _positive(expected_revision)
        request = _validated(ConnectionCreate, payload)
        if identity != request.connection_id:
            raise GatewayError("GATEWAY_IDENTITY_MISMATCH")
        request_digest = digest(
            ["connection.revise", project_id, identity, expected, request.model_dump(mode="json")]
        )
        with self._owned(project_id, principal) as db:
            replay = self._replay(db, principal, command_key, request_digest)
            if replay is not None:
                return replay, False
            head = self._head(db, "gateway_connection_current", project_id, identity)
            if head is None:
                raise GatewayError("GATEWAY_CONNECTION_NOT_FOUND")
            if head != expected:
                raise GatewayError("GATEWAY_REVISION_CONFLICT", current_revision=head)
            return self._commit(
                db,
                "gateway_connections",
                "gateway_connection_current",
                sealed(
                    self._connection_record(
                        project_id, identity, expected + 1, request, principal
                    )
                ),
                principal,
                command_key,
                request_digest,
            )

    def create_binding(
        self, project_id: str, payload: object, *, principal: str, command_key: str
    ) -> tuple[dict[str, Any], bool]:
        request = _validated(BindingCreate, payload)
        identity = addressable_identifier(request.binding_id)
        request_digest = digest(["binding.create", project_id, request.model_dump(mode="json")])
        with self._owned(project_id, principal) as db:
            replay = self._replay(db, principal, command_key, request_digest)
            if replay is not None:
                return replay, False
            if self._head(db, "gateway_binding_current", project_id, identity) is not None:
                raise GatewayError("GATEWAY_BINDING_EXISTS")
            return self._commit(
                db,
                "gateway_bindings",
                "gateway_binding_current",
                sealed(self._binding_record(project_id, identity, 1, request, principal, db=db)),
                principal,
                command_key,
                request_digest,
            )

    def revise_binding(
        self,
        project_id: str,
        binding_id: str,
        payload: object,
        *,
        expected_revision: object,
        principal: str,
        command_key: str,
    ) -> tuple[dict[str, Any], bool]:
        identity, expected = addressable_identifier(binding_id), _positive(expected_revision)
        request = _validated(BindingCreate, payload)
        if identity != request.binding_id:
            raise GatewayError("GATEWAY_IDENTITY_MISMATCH")
        request_digest = digest(
            ["binding.revise", project_id, identity, expected, request.model_dump(mode="json")]
        )
        with self._owned(project_id, principal) as db:
            replay = self._replay(db, principal, command_key, request_digest)
            if replay is not None:
                return replay, False
            head = self._head(db, "gateway_binding_current", project_id, identity)
            if head is None:
                raise GatewayError("GATEWAY_BINDING_NOT_FOUND")
            if head != expected:
                raise GatewayError("GATEWAY_REVISION_CONFLICT", current_revision=head)
            return self._commit(
                db,
                "gateway_bindings",
                "gateway_binding_current",
                sealed(
                    self._binding_record(
                        project_id, identity, expected + 1, request, principal, db=db
                    )
                ),
                principal,
                command_key,
                request_digest,
            )

    def _commit(
        self,
        db: sqlite3.Connection,
        table: str,
        current_table: str,
        record: dict[str, Any],
        principal: str,
        command_key: str,
        request_digest: str,
    ) -> tuple[dict[str, Any], bool]:
        """Append the immutable revision and its command receipt together."""
        db.execute(
            f"INSERT INTO {table} VALUES (?,?,?,?,?)",
            (
                record["project_id"],
                record["id"],
                record["revision"],
                encoded(record),
                record["digest"],
            ),
        )
        db.execute(
            f"INSERT INTO {current_table} VALUES (?,?,?) "
            "ON CONFLICT(project_id,id) DO UPDATE SET revision=excluded.revision",
            (record["project_id"], record["id"], record["revision"]),
        )
        db.execute(
            "INSERT INTO commands VALUES (?,?,?,?)",
            (principal, command_key, request_digest, encoded(record)),
        )
        return record, True

    def _connection_record(
        self,
        project_id: str,
        identity: str,
        revision: int,
        request: ConnectionCreate,
        principal: str,
    ) -> dict[str, Any]:
        origin = registered_origin(
            request.base_url, remote_data_destination=request.remote_data_destination
        )
        statement = request.model_dump(mode="json")
        family = statement["protocol"]["family"]
        return {
            "schema_version": "karajan.gateway-connection.v1",
            "project_id": project_id,
            "id": identity,
            "revision": revision,
            "base_url": origin,
            "protocol": statement["protocol"],
            # Derived from the negotiated family, never accepted from a caller.
            "discovery_path": discovery_path(family),
            "secret_ref": statement["secret_ref"],
            "credential_material": "not_accepted",
            "remote_data_destination": statement["remote_data_destination"],
            "request_transformation": statement["request_transformation"],
            "capability_evidence_refs": statement["capability_evidence_refs"],
            "recorded_by": principal,
            "recorded_at": self.clock(),
            "declared_identity": True,
            "verification_evidence": None,
            "verified": False,
            "connection_tested": False,
            "activation_allowed": False,
            # A connection is not a model source and never dispatches work.
            "execution_eligible": False,
        }

    def _binding_connection(
        self,
        db: sqlite3.Connection,
        project_id: str,
        connection_id: str,
        connection_revision: int,
    ) -> dict[str, Any]:
        if not addressable(connection_id):
            raise GatewayError("GATEWAY_INPUT_INVALID")
        if self._head(db, "gateway_connection_current", project_id, connection_id) is None:
            raise GatewayError("GATEWAY_CONNECTION_NOT_FOUND")
        return self._record(
            db, "gateway_connections", project_id, connection_id, connection_revision
        )

    def _binding_record(
        self,
        project_id: str,
        identity: str,
        revision: int,
        request: BindingCreate,
        principal: str,
        *,
        db: sqlite3.Connection,
    ) -> dict[str, Any]:
        connection = self._binding_connection(
            db, project_id, request.connection_id, request.connection_revision
        )
        statement = request.model_dump(mode="json")
        return {
            "schema_version": "karajan.gateway-model-binding.v1",
            "project_id": project_id,
            "id": identity,
            "revision": revision,
            "connection": {
                "id": connection["id"],
                "revision": connection["revision"],
                # The pinned connection's own canonical digest, verified above.
                "digest": connection["digest"],
                "base_url": connection["base_url"],
                "protocol": connection["protocol"],
                "secret_ref": connection["secret_ref"],
            },
            "model_alias": statement["model_alias"],
            "declared": statement["declared"],
            "request_transformation": statement["request_transformation"],
            "recorded_by": principal,
            "recorded_at": self.clock(),
            "alias_is_source_identity": False,
            "declared_identity": True,
            "verification_evidence": None,
            "verified": False,
            "provider_identity_observed": "unknown",
            "account_identity_observed": "unknown",
            "billing_channel_observed": "unknown",
            "execution_eligible": False,
            "dispatch_eligible": False,
            "live_qualified": False,
            "qualification_scope": "declared_binding_only",
        }

    def probe_catalog(
        self,
        project_id: str,
        connection_id: str,
        connection_revision: int,
        *,
        principal: str,
        command_key: str,
    ) -> dict[str, Any]:
        """Read the registered catalog once and persist a structured observation.

        Blocking work (credential resolution and the HTTP read) deliberately runs
        *between* two short transactions, so no project write lock is held across
        network I/O.

        The command key is reserved in the project's shared ``commands`` ledger
        at claim time, not only in a probe-local table. Without that, a different
        command using the same principal and key while a probe is in flight would
        find the ledger empty, write its own receipt, and either fail or be
        silently replaced when the probe finished. Reserving the key makes that
        concurrent command an ordinary idempotency conflict.
        """
        identity, revision = addressable_identifier(connection_id), _positive(connection_revision)
        request_digest = digest(["catalog.probe", project_id, identity, revision])
        claim, replay = self._open_claim(
            project_id, identity, revision, principal, command_key, request_digest
        )
        if replay is not None:
            return replay
        assert claim is not None
        observed = observed_catalog(
            origin=claim["connection"]["base_url"],
            catalog_path=claim["connection"]["discovery_path"],
            project_id=project_id,
            secret_ref=claim["connection"]["secret_ref"],
            resolver=self.resolver,
        )
        return self._close_claim(claim, project_id, observed, principal)

    def _pending_document(
        self,
        project_id: str,
        connection: dict[str, Any],
        principal: str,
        code: str,
    ) -> dict[str, Any]:
        """An explicitly unfinished probe: no observation exists to report yet.

        It carries no ``observation_id``, because inventing one would present
        unpersisted state as a completed observation.
        """
        observed = CatalogObservation(
            status="probe_in_progress",
            reason_codes=[code],
            requested_origin=connection["base_url"],
            requested_path=connection["discovery_path"],
            # Unknown, not zero: the request may or may not have been sent.
            requests_sent=None,
        )
        return {
            "schema_version": PENDING_SCHEMA_VERSION,
            "state": "pending",
            "project_id": project_id,
            "connection": {
                "id": connection["id"],
                "revision": connection["revision"],
                "digest": connection["digest"],
            },
            "requested_by": principal,
            "requested_at": self.clock(),
            "catalog": public_observation(observed),
            "observation_available": False,
            "declared_binding_effects": "none",
            "retry_with_new_command_key": True,
        }

    def _open_claim(
        self,
        project_id: str,
        connection_id: str,
        connection_revision: int,
        principal: str,
        command_key: str,
        request_digest: str,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Reserve the command key and the probe claim in one transaction.

        Returns ``(claim, None)`` when this caller owns the new probe, or
        ``(None, document)`` when the key already has a stable answer: a finished
        observation, a still-in-progress notice, or a reconciled unknown outcome.
        """
        with self._owned(project_id, principal) as db:
            replay = self._replay(db, principal, command_key, request_digest)
            pending_marker = (
                replay is not None
                and replay.get("schema_version") == PENDING_SCHEMA_VERSION
            )
            if replay is not None and not pending_marker:
                # A terminal receipt: a settled observation, or an unknown
                # outcome already reconciled under this key.
                return None, replay
            connection = self._record(
                db, "gateway_connections", project_id, connection_id, connection_revision
            )
            existing = db.execute(
                "SELECT * FROM gateway_probe_claims WHERE principal=? AND command_key=?",
                (principal, command_key),
            ).fetchone()
            if existing is None:
                if pending_marker:
                    # The key is reserved but its claim is gone: the reservation
                    # cannot be trusted, and reusing it would resend a probe.
                    raise GatewayError("GATEWAY_PROBE_CLAIM_LOST")
            elif existing["request_digest"] != request_digest:
                raise GatewayError("IDEMPOTENCY_CONFLICT")
            elif existing["state"] == "in_progress":
                if self.clock() - existing["started_at"] <= self.claim_ttl_seconds:
                    return None, self._pending_document(
                        project_id, connection, principal, "PROBE_IN_PROGRESS"
                    )
                # The claim outlived its TTL: the outcome is genuinely unknown.
                # It is recorded once, replacing the pending reservation, and is
                # never resent under this key. A deliberate retry uses a new key.
                result = self._interrupted_observation(
                    project_id, connection, principal, existing["observation_id"]
                )
                db.execute(
                    "UPDATE gateway_probe_claims SET state='abandoned', completed_at=?, "
                    "observation_id=? WHERE principal=? AND command_key=?",
                    (self.clock(), result["observation_id"], principal, command_key),
                )
                self._store_observation(db, result, principal, command_key, request_digest)
                return None, result
            elif existing["state"] == "completed":
                # A completed claim must have a terminal receipt, which the
                # branch above would already have returned.
                raise GatewayError("GATEWAY_PROBE_RECEIPT_MISSING")
            else:
                # Already reconciled as abandoned; recover its recorded outcome.
                settled = self._terminal_receipt(db, principal, command_key)
                if settled is None:
                    raise GatewayError("GATEWAY_PROBE_RECEIPT_MISSING")
                return None, settled
            if not pending_marker:
                # Reserve the shared key here so a concurrent command cannot take
                # it while the network read is in flight.
                db.execute(
                    "INSERT INTO commands VALUES (?,?,?,?)",
                    (
                        principal,
                        command_key,
                        request_digest,
                        encoded(
                            self._pending_document(
                                project_id, connection, principal, "PROBE_IN_PROGRESS"
                            )
                        ),
                    ),
                )
            db.execute(
                "INSERT INTO gateway_probe_claims VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    principal,
                    command_key,
                    request_digest,
                    project_id,
                    connection["id"],
                    connection["revision"],
                    connection["digest"],
                    "in_progress",
                    self.clock(),
                    None,
                    None,
                ),
            )
            return {
                "principal": principal,
                "command_key": command_key,
                "request_digest": request_digest,
                "project_id": project_id,
                "connection": connection,
            }, None

    def _close_claim(
        self,
        claim: dict[str, Any],
        project_id: str,
        observed: CatalogObservation,
        principal: str,
    ) -> dict[str, Any]:
        """Atomically finalize the observation, its receipt and the claim state."""
        with self._owned(project_id, principal) as db:
            row = db.execute(
                "SELECT state, request_digest FROM gateway_probe_claims "
                "WHERE principal=? AND command_key=?",
                (principal, claim["command_key"]),
            ).fetchone()
            if row is None or row["state"] != "in_progress":
                # Another actor reconciled this key while the read was in flight.
                # Its durable answer wins; the observed result is discarded.
                settled = db.execute(
                    "SELECT result FROM commands WHERE principal=? AND key=?",
                    (principal, claim["command_key"]),
                ).fetchone()
                if settled is None:
                    raise GatewayError("GATEWAY_PROBE_CLAIM_LOST")
                return dict(json.loads(settled["result"]))
            if row["request_digest"] != claim["request_digest"]:
                raise GatewayError("IDEMPOTENCY_CONFLICT")
            result = self._observation(project_id, claim["connection"], observed, principal)
            self._store_observation(
                db, result, principal, claim["command_key"], claim["request_digest"]
            )
            db.execute(
                "UPDATE gateway_probe_claims SET state='completed', completed_at=?, "
                "observation_id=? WHERE principal=? AND command_key=?",
                (self.clock(), result["observation_id"], principal, claim["command_key"]),
            )
            return result

    def _observation(
        self,
        project_id: str,
        connection: dict[str, Any],
        observed: CatalogObservation,
        principal: str,
    ) -> dict[str, Any]:
        return sealed(
            {
                "schema_version": OBSERVATION_SCHEMA_VERSION,
                "state": "completed",
                "observation_id": str(uuid.uuid4()),
                "project_id": project_id,
                "connection": {
                    "id": connection["id"],
                    "revision": connection["revision"],
                    "digest": connection["digest"],
                },
                "observed_by": principal,
                "observed_at": self.clock(),
                "catalog": public_observation(observed),
                "declared_binding_effects": "none",
            }
        )

    def _interrupted_observation(
        self,
        project_id: str,
        connection: dict[str, Any],
        principal: str,
        previous_observation_id: str | None,
    ) -> dict[str, Any]:
        """Record a started-but-unfinished probe without asserting what happened.

        ``requests_sent`` stays unknown rather than becoming zero: the process may
        have completed the upstream read without persisting its result.
        """
        observed = CatalogObservation(
            status="outcome_unknown",
            reason_codes=["PROBE_INTERRUPTED_OUTCOME_UNKNOWN"],
            requested_origin=connection["base_url"],
            requested_path=connection["discovery_path"],
            requests_sent=None,
        )
        return sealed(
            {
                "schema_version": OBSERVATION_SCHEMA_VERSION,
                "state": "unresolved",
                "observation_id": previous_observation_id or str(uuid.uuid4()),
                "project_id": project_id,
                "connection": {
                    "id": connection["id"],
                    "revision": connection["revision"],
                    "digest": connection["digest"],
                },
                "observed_by": principal,
                "observed_at": self.clock(),
                "catalog": public_observation(observed),
                "declared_binding_effects": "none",
            }
        )

    def _store_observation(
        self,
        db: sqlite3.Connection,
        result: dict[str, Any],
        principal: str,
        command_key: str,
        request_digest: str,
    ) -> None:
        payload = encoded(result)
        db.execute(
            "INSERT INTO gateway_catalog_observations VALUES (?,?,?,?,?)",
            (
                result["observation_id"],
                result["project_id"],
                result["connection"]["id"],
                result["connection"]["revision"],
                payload,
            ),
        )
        # The key was reserved as a pending marker; finalizing replaces it.
        db.execute(
            "INSERT INTO commands VALUES (?,?,?,?) "
            "ON CONFLICT(principal,key) DO UPDATE SET digest=excluded.digest, "
            "result=excluded.result",
            (principal, command_key, request_digest, payload),
        )
