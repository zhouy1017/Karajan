"""Durable, project-scoped gateway catalog inside the owned project database.

The store reuses ProjectRegistry's transaction, ownership check and idempotency
ledger instead of opening a second store: every command is bound to a
``principal``, every conditional write compares the durable head, and every
revision is append-only so an old reference keeps resolving after a newer
revision exists. Nothing here contacts an upstream or reads a credential.

Every lookup is scoped by ``project_id``. A reference to a connection or
binding owned by another project is therefore not found rather than reported:
the rejection must not disclose whether that identity exists elsewhere.
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
from .models import BindingCreate, ConnectionCreate, discovery_path, registered_origin
from .probe import SessionSecretResolver, observed_catalog, public_observation

REQUIRED_SCHEMA = {
    "projects": ["id", "snapshot"],
    "project_owners": ["project_id", "principal"],
    "commands": ["principal", "key", "digest", "result"],
    "gateway_connections": ["project_id", "id", "revision", "record", "digest"],
    "gateway_connection_current": ["project_id", "id", "revision"],
    "gateway_bindings": ["project_id", "id", "revision", "record", "digest"],
    "gateway_binding_current": ["project_id", "id", "revision"],
    "gateway_catalog_observations": ["observation_id", "project_id", "id", "revision", "record"],
}


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def encoded(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


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
    ) -> None:
        self.projects = projects
        self.resolver = resolver
        self.clock = clock
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

    @contextmanager
    def _owned(self, project_id: str, principal: str) -> Iterator[sqlite3.Connection]:
        """Hold one owned project transaction; the registry owns commit/rollback."""
        identifier(project_id)
        identifier(principal)
        if self.unavailable is not None:
            raise GatewayError(self.unavailable)
        try:
            with self.projects._transaction() as db:
                known = db.execute(
                    "SELECT 1 FROM projects WHERE id=?", (project_id,)
                ).fetchone()
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

    def _record(
        self, db: sqlite3.Connection, table: str, project_id: str, identity: str, revision: int
    ) -> dict[str, Any]:
        row = db.execute(
            f"SELECT record, digest FROM {table} WHERE project_id=? AND id=? AND revision=?",
            (project_id, identity, revision),
        ).fetchone()
        if row is None:
            raise GatewayError("GATEWAY_REVISION_NOT_FOUND")
        record: dict[str, Any] = json.loads(row["record"])
        if digest(record) != row["digest"] or (
            record["project_id"],
            record["id"],
            record["revision"],
        ) != (project_id, identity, revision):
            raise GatewayError("GATEWAY_RECORD_CHANGED")
        return record

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
            return [
                json.loads(row["record"])
                for row in db.execute(
                    "SELECT record FROM gateway_connections WHERE project_id=? "
                    "ORDER BY id, revision",
                    (project_id,),
                )
            ]

    def get_binding(
        self, project_id: str, binding_id: str, revision: int, *, principal: str
    ) -> dict[str, Any]:
        identity, revision = identifier(binding_id), _positive(revision)
        with self._owned(project_id, principal) as db:
            return self._record(db, "gateway_bindings", project_id, identity, revision)

    def list_bindings(self, project_id: str, *, principal: str) -> list[dict[str, Any]]:
        with self._owned(project_id, principal) as db:
            return [
                json.loads(row["record"])
                for row in db.execute(
                    "SELECT record FROM gateway_bindings WHERE project_id=? ORDER BY id, revision",
                    (project_id,),
                )
            ]

    def list_catalog_observations(self, project_id: str, *, principal: str) -> list[dict[str, Any]]:
        with self._owned(project_id, principal) as db:
            return [
                json.loads(row["record"])
                for row in db.execute(
                    "SELECT record FROM gateway_catalog_observations WHERE project_id=? "
                    "ORDER BY rowid",
                    (project_id,),
                )
            ]

    def create_connection(
        self, project_id: str, payload: object, *, principal: str, command_key: str
    ) -> tuple[dict[str, Any], bool]:
        request = _validated(ConnectionCreate, payload)
        identity = identifier(request.connection_id)
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
                self._connection_record(project_id, identity, 1, request, principal),
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
        identity, expected = identifier(connection_id), _positive(expected_revision)
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
                self._connection_record(project_id, identity, expected + 1, request, principal),
                principal,
                command_key,
                request_digest,
            )

    def create_binding(
        self, project_id: str, payload: object, *, principal: str, command_key: str
    ) -> tuple[dict[str, Any], bool]:
        request = _validated(BindingCreate, payload)
        identity = identifier(request.binding_id)
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
                self._binding_record(project_id, identity, 1, request, principal, db=db),
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
        identity, expected = identifier(binding_id), _positive(expected_revision)
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
                self._binding_record(
                    project_id, identity, expected + 1, request, principal, db=db
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
                digest(record),
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
        if not isinstance(connection_id, str) or not connection_id:
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
                "digest": digest(connection),
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

        The observation is appended evidence, not a revision of the connection:
        a probe never mutates the binding it observed, and a visible model never
        becomes execution eligibility.
        """
        identity, revision = identifier(connection_id), _positive(connection_revision)
        request_digest = digest(["catalog.probe", project_id, identity, revision])
        with self._owned(project_id, principal) as db:
            replay = self._replay(db, principal, command_key, request_digest)
            if replay is not None:
                return replay
            connection = self._record(db, "gateway_connections", project_id, identity, revision)
            observed = observed_catalog(
                origin=connection["base_url"],
                catalog_path=connection["discovery_path"],
                project_id=project_id,
                secret_ref=connection["secret_ref"],
                resolver=self.resolver,
            )
            result = {
                "schema_version": "karajan.gateway-catalog-observation.v1",
                "observation_id": str(uuid.uuid4()),
                "project_id": project_id,
                "connection": {
                    "id": connection["id"],
                    "revision": connection["revision"],
                    "digest": digest(connection),
                },
                "observed_by": principal,
                "observed_at": self.clock(),
                "catalog": public_observation(observed),
                "declared_binding_effects": "none",
            }
            db.execute(
                "INSERT INTO gateway_catalog_observations VALUES (?,?,?,?,?)",
                (
                    result["observation_id"],
                    project_id,
                    connection["id"],
                    connection["revision"],
                    encoded(result),
                ),
            )
            db.execute(
                "INSERT INTO commands VALUES (?,?,?,?)",
                (principal, command_key, request_digest, encoded(result)),
            )
            return result
