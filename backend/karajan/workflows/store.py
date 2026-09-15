"""Durable, project-scoped workflow bundle revisions and deterministic edits.

The store writes real bytes before it publishes anything. A revision is created
by materialising the complete bundle into its own directory, verifying every
stored file against the manifest the controller derived from those same bytes,
and only then committing the revision row and its command receipt in one
transaction. A partially written directory therefore never becomes a visible
revision, and a revision that is visible is always complete.

Boundaries that are structural rather than documented:

* Nothing read back comes from a stored preview. ``compiled`` re-reads the files
  from disk, re-verifies their digests and re-compiles, and compares that result
  to the durable revision record. A restart, a second process or a damaged or
  replaced file therefore produces a mismatch instead of a new answer under an
  old revision identity.
* A direct table or file edit calls no model, because the whole path is
  arithmetic: bytes, digests and the pure compiler. A text instruction is
  persisted as authoring input with an explicit ``pending_generation`` state and
  never fabricates a generated result.
* Ownership and idempotency reuse the project registry's ledger, so there is no
  second authentication path and no second command table. Every read takes the
  acting principal, and a bundle's conversation is fixed when it is created.
"""

import json
import os
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol

from karajan.conversations import ConversationError
from karajan.projects import ProjectError, ProjectRegistry
from karajan.storage import ExistingStoreError, require_schema

from . import bundle as bundles
from . import yamls
from .compiler import (
    MANIFEST_SCHEMA_VERSION,
    CompiledWorkflow,
    compile_workflow,
    diff,
    projection,
)
from .digests import canonical_json, content_digest
from .errors import WorkflowError, located
from .layout import (
    MANIFEST_PATH,
    WORKFLOW_PATH,
    BundlePaths,
    discard_staging_tree,
    require_trusted_chain,
)
from .registry import kind_for, registered_refs

REQUIRED_SCHEMA = {
    "projects": ["id", "snapshot"],
    "project_owners": ["project_id", "principal"],
    "commands": ["principal", "key", "digest", "result"],
    "workflow_bundles": ["project_id", "conversation_id", "id", "revision", "record", "digest"],
    "workflow_bundle_current": ["project_id", "id", "revision", "conversation_id"],
    "workflow_bundle_files": [
        "project_id",
        "id",
        "revision",
        "path",
        "byte_digest",
        "byte_length",
    ],
    "workflow_authoring_inputs": ["project_id", "id", "revision", "record"],
}

BUNDLE_SCHEMA_VERSION = "karajan.workflow-bundle-revision.v1"
AUTHORING_SCHEMA_VERSION = "karajan.workflow-authoring-input.v1"

#: Authoring states. Only ``generated`` may ever carry a generated configuration;
#: a text instruction is always ``pending_generation``.
AUTHORING_STATES = ("pending_generation", "generated", "rejected")

BUNDLE_PAYLOAD_FIELDS = frozenset({"files", "delivery_kind", "references"})
EDIT_PAYLOAD_FIELDS = frozenset({"edits"})
AUTHORING_PAYLOAD_FIELDS = frozenset({"instruction", "base_revision"})


class GatewayBindingResolver(Protocol):
    """A trusted read of one exact gateway binding revision.

    Implemented by the gateway catalog store. It never probes, never resolves a
    credential and never grants eligibility; it answers only whether this project
    owns that exact binding and connection revision.
    """

    def get_binding(
        self, project_id: str, binding_id: str, revision: int, *, principal: str
    ) -> dict[str, Any]: ...

    def get_connection(
        self, project_id: str, connection_id: str, revision: int, *, principal: str
    ) -> dict[str, Any]: ...


class WorkflowStore:
    def __init__(
        self,
        projects: ProjectRegistry,
        conversations: Any,
        root: Path,
        *,
        gateway: GatewayBindingResolver | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.projects = projects
        self.conversations = conversations
        self.gateway = gateway
        self.root = Path(root)
        self.clock = clock
        self.existing_only = projects.existing_only
        self.unavailable: str | None = None
        if self.existing_only:
            try:
                require_schema(projects.database, REQUIRED_SCHEMA)
            except ExistingStoreError:
                self.unavailable = "WORKFLOW_STATE_UNAVAILABLE"
            return
        self.root.mkdir(parents=True, exist_ok=True)
        with self.projects._transaction() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS workflow_bundles ("
                "project_id TEXT NOT NULL REFERENCES projects(id), "
                "conversation_id TEXT NOT NULL, id TEXT NOT NULL, revision INTEGER NOT NULL, "
                "record TEXT NOT NULL, digest TEXT NOT NULL, "
                "PRIMARY KEY(project_id, id, revision))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS workflow_bundle_current ("
                "project_id TEXT NOT NULL REFERENCES projects(id), id TEXT NOT NULL, "
                "conversation_id TEXT NOT NULL, revision INTEGER NOT NULL, "
                "PRIMARY KEY(project_id, id))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS workflow_bundle_files ("
                "project_id TEXT NOT NULL, id TEXT NOT NULL, revision INTEGER NOT NULL, "
                "path TEXT NOT NULL, byte_digest TEXT NOT NULL, byte_length INTEGER NOT NULL, "
                "PRIMARY KEY(project_id, id, revision, path))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS workflow_authoring_inputs ("
                "project_id TEXT NOT NULL REFERENCES projects(id), id TEXT NOT NULL, "
                "revision INTEGER NOT NULL, record TEXT NOT NULL, "
                "PRIMARY KEY(project_id, id, revision))"
            )

    # ---------------------------------------------------------------- plumbing

    @contextmanager
    def _owned(self, project_id: str, principal: str) -> Iterator[sqlite3.Connection]:
        if self.unavailable is not None:
            raise WorkflowError(self.unavailable)
        if (
            not isinstance(project_id, str)
            or not 1 <= len(project_id) <= 256
            or any(character.isspace() for character in project_id)
        ):
            raise WorkflowError("WORKFLOW_PROJECT_NOT_FOUND")
        if (
            not isinstance(principal, str)
            or not 1 <= len(principal) <= 256
            or any(character.isspace() for character in principal)
        ):
            raise WorkflowError("WORKFLOW_PRINCIPAL_INVALID")
        try:
            with self.projects._transaction() as db:
                known = db.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone()
                if known is None:
                    raise WorkflowError("WORKFLOW_PROJECT_NOT_FOUND")
                self.projects._require_owner(db, project_id, principal)
                yield db
        except ProjectError as error:
            # USER_DECISION_REQUIRED means "not this principal's project"; it is
            # reported as a missing record so another project's identity cannot
            # be probed through the error code.
            code = (
                "WORKFLOW_PROJECT_NOT_FOUND"
                if error.code in {"USER_DECISION_REQUIRED", "PROJECT_OWNER_UNRESOLVED"}
                else error.code
            )
            raise WorkflowError(code, current_revision=error.current_revision) from None
        except (OSError, sqlite3.Error):
            # A transaction failure after the bytes were written leaves an
            # unreferenced directory, which is inert: no revision names it.
            raise WorkflowError("WORKFLOW_STATE_UNAVAILABLE") from None

    def _replay(
        self, db: sqlite3.Connection, principal: str, key: str, digest: str
    ) -> dict[str, Any] | None:
        try:
            return self.projects._replay(db, principal, key, digest)
        except ProjectError as error:
            raise WorkflowError(error.code) from None

    def _bundle_root(self, project_id: str, bundle_id: str) -> Path:
        """The one managed directory for a bundle identity; never caller-named."""
        return self.root / project_id / bundle_id

    def _trusted_root(self) -> Path:
        """The managed data root, created once and then only ever validated.

        The root is resolved once at construction time, so a link planted later
        cannot change what "inside the managed tree" means for this process.
        """
        return self.root

    def _require_trusted_chain(self, destination: Path) -> None:
        """Validate every existing component before anything is created below it.

        Shared with the deployment store, so a bundle revision and a deployment
        directory are validated by one implementation rather than by two that
        could drift apart.
        """
        require_trusted_chain(self._trusted_root(), destination, levels=3)

    def _reconcile_materialized(
        self,
        destination: Path,
        *,
        files: Mapping[str, bytes],
        bundle_id: str,
        revision: int,
        project_id: str,
        conversation_id: str,
    ) -> bundles.Bundle | None:
        """Recover from a revision directory an interrupted attempt left behind.

        A crash between materialising the tree and committing the revision row
        leaves a complete directory no row references. That directory is inert,
        but it must not permanently block this revision. It is therefore re-read
        and verified: when its bytes are exactly the bytes this attempt is
        publishing, it is adopted; when they are anything else, the orphan is
        refused rather than silently reused or replaced.

        This never follows a link and never removes a directory it cannot fully
        account for, so recovery cannot itself become the way bytes escape or
        disappear.
        """
        if not destination.exists():
            return None
        verified = bundles.read_directory(
            destination,
            managed_root=self._bundle_root(project_id, bundle_id),
            bundle_id=bundle_id,
            revision=revision,
            project_id=project_id,
            conversation_id=conversation_id,
        )
        expected = {path: bundles.byte_digest(raw) for path, raw in files.items()}
        actual = {item.path: item.digest for item in verified.files}
        if actual != expected:
            raise WorkflowError(
                "WORKFLOW_UNCOMMITTED_MATERIALIZATION_MISMATCH",
                diagnostics=[
                    located(
                        "WORKFLOW_UNCOMMITTED_MATERIALIZATION_MISMATCH",
                        WORKFLOW_PATH,
                        "an uncommitted revision directory holds different bytes; "
                        "it is left untouched for inspection",
                    )
                ],
            )
        return verified

    # ------------------------------------------------------------- provisioning

    def create_bundle(
        self,
        project_id: str,
        conversation_id: str,
        bundle_id: str,
        payload: Mapping[str, Any],
        *,
        principal: str,
        command_key: str,
    ) -> tuple[dict[str, Any], bool]:
        """Publish revision 1 from real supplied files."""
        identity = self._identity(bundle_id)
        self._require_fields(payload, BUNDLE_PAYLOAD_FIELDS)
        self._require_conversation(project_id, conversation_id)
        files = bundles.prepare_files(self._declared_files(payload))
        delivery_kind = self._delivery_kind(payload.get("delivery_kind"))
        compiled = self._compile_files(
            project_id,
            principal,
            files,
            delivery_kind=delivery_kind,
            bundle_id=identity,
            revision=1,
        )
        request_digest = content_digest(
            [
                "workflow.bundle.create",
                project_id,
                conversation_id,
                identity,
                1,
                _content_index(files),
            ]
        )
        return self._publish(
            project_id=project_id,
            conversation_id=conversation_id,
            bundle_id=identity,
            revision=1,
            files=files,
            delivery_kind=delivery_kind,
            compiled=compiled,
            principal=principal,
            command_key=command_key,
            request_digest=request_digest,
            expected_revision=None,
        )

    def revise_bundle(
        self,
        project_id: str,
        conversation_id: str,
        bundle_id: str,
        payload: Mapping[str, Any],
        *,
        expected_revision: object,
        principal: str,
        command_key: str,
    ) -> tuple[dict[str, Any], bool]:
        """Publish a new immutable revision; the previous revision is retained."""
        identity = self._identity(bundle_id)
        expected = self._positive(expected_revision)
        self._require_fields(payload, BUNDLE_PAYLOAD_FIELDS)
        # The bundle must already exist and the conversation must be the one that
        # owns it. The expected revision is *not* looked up as a record: a stale
        # or absent revision is a conflict against the durable head, not a
        # missing record, and reporting the head is what lets the caller recover.
        with self._owned(project_id, principal) as db:
            self._require_existing(db, project_id, identity)
            self._require_fixed_conversation(db, project_id, identity, conversation_id)
        self._require_conversation(project_id, conversation_id)
        files = bundles.prepare_files(self._declared_files(payload))
        delivery_kind = self._delivery_kind(payload.get("delivery_kind"))
        compiled = self._compile_files(
            project_id,
            principal,
            files,
            delivery_kind=delivery_kind,
            bundle_id=identity,
            revision=expected + 1,
        )
        request_digest = content_digest(
            [
                "workflow.bundle.revise",
                project_id,
                conversation_id,
                identity,
                expected,
                _content_index(files),
            ]
        )
        return self._publish(
            project_id=project_id,
            conversation_id=conversation_id,
            bundle_id=identity,
            revision=expected + 1,
            files=files,
            delivery_kind=delivery_kind,
            compiled=compiled,
            principal=principal,
            command_key=command_key,
            request_digest=request_digest,
            expected_revision=expected,
        )

    def _publish(
        self,
        *,
        project_id: str,
        conversation_id: str,
        bundle_id: str,
        revision: int,
        files: Mapping[str, bytes],
        delivery_kind: str,
        compiled: CompiledWorkflow,
        principal: str,
        command_key: str,
        request_digest: str,
        expected_revision: int | None,
        finalize: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Write bytes atomically, verify them, then commit the revision row.

        Idempotency is resolved before the conditional write, so a repeated
        command returns its original result even after the head has advanced,
        while a genuinely late write still loses the CAS.

        ``finalize`` adds the command-specific fields a caller sees (for example
        the edits an edit command applied). The finalized document is what is
        persisted in the command ledger, so a replay returns the exact same
        complete result rather than the bare revision record.
        """
        with self._owned(project_id, principal) as db:
            replay = self._replay(db, principal, command_key, request_digest)
            if replay is not None:
                return replay, False
            # The owning conversation is decided *before* any file is written.
            # An identity claimed by an earlier authoring input already belongs
            # to that conversation, so creating a revision from another one is
            # refused here rather than after its bytes are on disk.
            self._require_fixed_conversation(db, project_id, bundle_id, conversation_id)
            head_row = db.execute(
                "SELECT revision FROM workflow_bundle_current WHERE project_id=? AND id=?",
                (project_id, bundle_id),
            ).fetchone()
            head = int(head_row["revision"]) if head_row is not None else None
            if expected_revision is None:
                if head is not None:
                    raise WorkflowError("WORKFLOW_BUNDLE_EXISTS")
            else:
                if head is None:
                    raise WorkflowError("WORKFLOW_BUNDLE_NOT_FOUND")
                if head != expected_revision:
                    raise WorkflowError("WORKFLOW_REVISION_CONFLICT", current_revision=head)
            record = self._write_revision(
                project_id=project_id,
                conversation_id=conversation_id,
                bundle_id=bundle_id,
                revision=revision,
                files=files,
                delivery_kind=delivery_kind,
                compiled=compiled,
                principal=principal,
            )
            db.execute(
                "INSERT INTO workflow_bundles VALUES (?,?,?,?,?,?)",
                (
                    project_id,
                    conversation_id,
                    bundle_id,
                    revision,
                    canonical_json(record),
                    record["digest"],
                ),
            )
            for item in record["files"]:
                db.execute(
                    "INSERT INTO workflow_bundle_files VALUES (?,?,?,?,?,?)",
                    (
                        project_id,
                        bundle_id,
                        revision,
                        item["path"],
                        item["byte_digest"],
                        item["byte_length"],
                    ),
                )
            db.execute(
                "INSERT INTO workflow_bundle_current (project_id, id, conversation_id, revision) "
                "VALUES (?,?,?,?) "
                "ON CONFLICT(project_id,id) DO UPDATE SET "
                "revision=excluded.revision, conversation_id=excluded.conversation_id",
                (project_id, bundle_id, conversation_id, revision),
            )
            result = finalize(record) if finalize is not None else record
            db.execute(
                "INSERT INTO commands VALUES (?,?,?,?)",
                (principal, command_key, request_digest, canonical_json(result)),
            )
            return result, True

    def _write_revision(
        self,
        *,
        project_id: str,
        conversation_id: str,
        bundle_id: str,
        revision: int,
        files: Mapping[str, bytes],
        delivery_kind: str,
        compiled: CompiledWorkflow,
        principal: str,
    ) -> dict[str, Any]:
        """Materialise one revision and read it back before it is referenced."""
        managed = self._bundle_root(project_id, bundle_id)
        destination = managed / f"revision-{revision}"
        # The whole controlled chain is validated before the first write, so a
        # pre-existing link cannot redirect these bytes anywhere.
        self._require_trusted_chain(managed)
        manifest = bundles.manifest_document(
            bundle_id=bundle_id,
            revision=revision,
            files=files,
            references={
                "workflow_id": compiled.workflow_id,
                "workflow_revision": compiled.revision,
                "input_contract_ref": compiled.input_contract_ref,
                "roles": dict(sorted(compiled.roles.items())),
                "execution_kinds": sorted(
                    set(compiled.available_execution_kinds)
                    | set(compiled.unavailable_execution_kinds)
                ),
                "contracts": sorted(
                    {step.output_contract_ref for step in compiled.steps}
                    | {compiled.input_contract_ref}
                ),
            },
            delivery_kind=delivery_kind,
        )
        bundles.adopt_manifest(
            manifest,
            bundle_id=bundle_id,
            revision=revision,
            files=files,
            delivery_kind=delivery_kind,
        )
        verified = self._materialize(
            managed=managed,
            destination=destination,
            files=files,
            manifest=manifest,
            bundle_id=bundle_id,
            revision=revision,
            project_id=project_id,
            conversation_id=conversation_id,
        )
        recompiled = bundles.compile_bundle(
            verified, binding_index=compiled.binding_index
        )
        if recompiled.compiled_digest != compiled.compiled_digest:
            raise WorkflowError(
                "WORKFLOW_COMPILE_UNSTABLE",
                diagnostics=[
                    located(
                        "WORKFLOW_COMPILE_UNSTABLE",
                        WORKFLOW_PATH,
                        "the stored bytes do not compile to the published template",
                    )
                ],
            )
        return self._revision_record(
            project_id=project_id,
            conversation_id=conversation_id,
            bundle_id=bundle_id,
            revision=revision,
            delivery_kind=delivery_kind,
            compiled=compiled,
            verified=verified,
            principal=principal,
        )

    def _materialize(
        self,
        *,
        managed: Path,
        destination: Path,
        files: Mapping[str, bytes],
        manifest: Mapping[str, Any],
        bundle_id: str,
        revision: int,
        project_id: str,
        conversation_id: str,
    ) -> bundles.Bundle:
        """Write the tree under a staging name, then verify what was written.

        A crash before the rename leaves an inert staging directory; a crash
        after it leaves a complete revision directory that no row references yet.
        Both are recoverable, and neither is ever silently replaced by different
        bytes.
        """
        if destination.exists():
            adopted = self._reconcile_materialized(
                destination,
                files=files,
                bundle_id=bundle_id,
                revision=revision,
                project_id=project_id,
                conversation_id=conversation_id,
            )
            if adopted is None:
                raise WorkflowError("WORKFLOW_REVISION_ALREADY_MATERIALIZED")
            return adopted
        managed.mkdir(parents=True, exist_ok=True)
        staging = self._new_staging(managed, revision)
        try:
            self._write_tree(staging, files, manifest)
            os.rename(staging, destination)
        except BaseException:
            _discard(staging)
            raise
        return bundles.read_directory(
            destination,
            managed_root=managed,
            bundle_id=bundle_id,
            revision=revision,
            project_id=project_id,
            conversation_id=conversation_id,
        )

    def _new_staging(self, managed: Path, revision: int) -> Path:
        """A staging name that cannot collide with another attempt's."""
        return managed / f".staging-{revision}-{uuid.uuid4().hex}"

    def _revision_record(
        self,
        *,
        project_id: str,
        conversation_id: str,
        bundle_id: str,
        revision: int,
        delivery_kind: str,
        compiled: CompiledWorkflow,
        verified: bundles.Bundle,
        principal: str,
    ) -> dict[str, Any]:
        record: dict[str, Any] = {
            "schema_version": BUNDLE_SCHEMA_VERSION,
            "project_id": project_id,
            "conversation_id": conversation_id,
            "id": bundle_id,
            "revision": revision,
            "delivery_kind": delivery_kind,
            "manifest": dict(verified.manifest),
            "files": [
                {
                    "path": item.path,
                    "byte_digest": item.digest,
                    "byte_length": len(item.raw),
                }
                for item in verified.files
            ],
            "bundle_digest": verified.bundle_digest,
            "manifest_file_digest": verified.manifest_file_digest,
            "compiled_digest": compiled.compiled_digest,
            "compiler_revision": compiled.compiler_revision,
            "compiler_identity": compiled.compiler_identity,
            "role_digest": compiled.role_digest,
            "binding_index": {
                reference: dict(resolved)
                for reference, resolved in compiled.binding_index.items()
            },
            "readiness": compiled.readiness,
            "executable": compiled.executable,
            "available_execution_kinds": list(compiled.available_execution_kinds),
            "unavailable_execution_kinds": list(compiled.unavailable_execution_kinds),
            "predicate": {
                "compile": "read_files_and_recompile",
                "preview": "same_compiled_digest",
            },
            "recorded_by": principal,
            "recorded_at": self.clock(),
            "activation_allowed": False,
            "dispatch_eligible": False,
            "model_calls": 0,
        }
        record["digest"] = content_digest(
            {key: value for key, value in record.items() if key != "digest"}
        )
        return record

    def _write_tree(
        self, staging: Path, files: Mapping[str, bytes], manifest: Mapping[str, Any]
    ) -> None:
        """Write every declared file plus the manifest, then make it durable."""
        for relative in sorted(files):
            segments = relative.split("/")
            target = staging
            for segment in segments[:-1]:
                target = target / segment
                target.mkdir(parents=True, exist_ok=True)
            _write_bytes(target / segments[-1], files[relative])
        _write_bytes(staging / MANIFEST_PATH, bundles.render_manifest(manifest))
        self._fsync_directory(staging)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        try:
            descriptor = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            os.close(descriptor)

    def _compile_files(
        self,
        project_id: str,
        principal: str,
        files: Mapping[str, bytes],
        *,
        delivery_kind: str,
        bundle_id: str,
        revision: int,
        binding_index: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> CompiledWorkflow:
        """Compile before anything is written, so a rejection writes nothing.

        ``binding_index`` is supplied when an existing revision's resolved
        bindings must be preserved: a structural edit recompiles the same
        references, so it reuses the identities already recorded for this bundle
        instead of re-resolving them against a catalog that may have moved on.
        """
        documents = {
            path: yamls.load(raw.decode("utf-8"), file=path)
            for path, raw in files.items()
            if path != MANIFEST_PATH
        }
        workflow = documents.get(WORKFLOW_PATH)
        if not isinstance(workflow, Mapping):
            raise WorkflowError(
                "WORKFLOW_SCHEMA_INVALID",
                diagnostics=[
                    located("WORKFLOW_SCHEMA_INVALID", WORKFLOW_PATH, "expected a mapping")
                ],
            )
        roles = {
            path: document
            for path, document in documents.items()
            if path.startswith("roles/")
        }
        if binding_index is None:
            binding_index = self._binding_index(project_id, principal, workflow)
        provisional_digest = content_digest(
            {path: bundles.byte_digest(raw) for path, raw in sorted(files.items())}
        )
        compiled = compile_workflow(
            {"schema_version": MANIFEST_SCHEMA_VERSION, "bundle_id": bundle_id},
            workflow,
            roles,
            bundle_digest=provisional_digest,
            role_digest=content_digest(
                {path: content_digest(roles[path]) for path in sorted(roles)}
            ),
            template_digests={
                path: bundles.byte_digest(raw)
                for path, raw in sorted(files.items())
                if path.startswith("templates/")
            },
            binding_index=binding_index,
        )
        if compiled.delivery_kind != delivery_kind:
            raise WorkflowError(
                "WORKFLOW_DELIVERY_KIND_MISMATCH",
                diagnostics=[
                    located(
                        "WORKFLOW_DELIVERY_KIND_MISMATCH",
                        f"{WORKFLOW_PATH}#/delivery_kind",
                        "the declared and compiled delivery kinds disagree",
                    )
                ],
            )
        return compiled

    def _binding_index(
        self, project_id: str, principal: str, workflow: Mapping[str, Any]
    ) -> dict[str, Mapping[str, Any]]:
        """Resolve every ``binding:`` reference to one exact owned revision.

        A binding reference is resolved by exact revision through the gateway
        catalog, scoped to this project, and its connection revision and digest
        are recorded. Nothing here probes the upstream or resolves a credential,
        so a resolved binding is still a declared, unqualified draft.
        """
        declared = workflow.get("bindings")
        if declared is None:
            return {}
        if not isinstance(declared, Mapping):
            raise WorkflowError(
                "WORKFLOW_SCHEMA_INVALID",
                diagnostics=[
                    located(
                        "WORKFLOW_SCHEMA_INVALID",
                        f"{WORKFLOW_PATH}#/bindings",
                        "expected a mapping",
                    )
                ],
            )
        index: dict[str, Mapping[str, Any]] = {}
        for role_name, reference in declared.items():
            if not isinstance(reference, str) or not reference.startswith("binding:"):
                continue
            binding_id, revision = _parse_binding_ref(reference)
            if self.gateway is None:
                raise WorkflowError(
                    "WORKFLOW_GATEWAY_BINDING_UNAVAILABLE",
                    diagnostics=[
                        located(
                            "WORKFLOW_GATEWAY_BINDING_UNAVAILABLE",
                            f"{WORKFLOW_PATH}#/bindings/id={role_name}",
                            "no gateway catalog is configured for this deployment",
                        )
                    ],
                )
            binding = self.gateway.get_binding(
                project_id, binding_id, revision, principal=principal
            )
            connection = self.gateway.get_connection(
                project_id,
                str(binding["connection"]["id"]),
                int(binding["connection"]["revision"]),
                principal=principal,
            )
            if connection["digest"] != binding["connection"]["digest"]:
                raise WorkflowError(
                    "WORKFLOW_GATEWAY_BINDING_CHANGED",
                    diagnostics=[
                        located(
                            "WORKFLOW_GATEWAY_BINDING_CHANGED",
                            f"{WORKFLOW_PATH}#/bindings/id={role_name}",
                            "the pinned connection revision no longer matches",
                        )
                    ],
                )
            index[reference] = {
                "binding": {
                    "id": binding["id"],
                    "revision": binding["revision"],
                    "digest": binding["digest"],
                },
                "connection": {
                    "id": connection["id"],
                    "revision": connection["revision"],
                    "digest": connection["digest"],
                },
                "execution_eligible": False,
                "verified": False,
                "qualification_scope": "declared_binding_only",
                "probe_performed": False,
                "credential_resolved": False,
                "draft_only": True,
            }
        return index

    # ---------------------------------------------------------------- readback

    def _record(
        self, db: sqlite3.Connection, project_id: str, bundle_id: str, revision: int
    ) -> dict[str, Any]:
        row = db.execute(
            "SELECT record, digest FROM workflow_bundles "
            "WHERE project_id=? AND id=? AND revision=?",
            (project_id, bundle_id, revision),
        ).fetchone()
        if row is None:
            raise WorkflowError("WORKFLOW_BUNDLE_NOT_FOUND")
        record: dict[str, Any] = json.loads(row["record"])
        declared = record.get("digest")
        if not isinstance(declared, str) or declared != row["digest"]:
            raise WorkflowError("WORKFLOW_RECORD_CHANGED")
        if (
            content_digest({key: value for key, value in record.items() if key != "digest"})
            != declared
        ):
            raise WorkflowError("WORKFLOW_RECORD_CHANGED")
        if (record["project_id"], record["id"], record["revision"]) != (
            project_id,
            bundle_id,
            revision,
        ):
            raise WorkflowError("WORKFLOW_RECORD_CHANGED")
        return record

    def _require_existing(
        self, db: sqlite3.Connection, project_id: str, bundle_id: str
    ) -> None:
        row = db.execute(
            "SELECT revision FROM workflow_bundle_current WHERE project_id=? AND id=?",
            (project_id, bundle_id),
        ).fetchone()
        if row is None:
            raise WorkflowError("WORKFLOW_BUNDLE_NOT_FOUND")

    def _require_fixed_conversation(
        self, db: sqlite3.Connection, project_id: str, bundle_id: str, conversation_id: str
    ) -> None:
        """Require the conversation that already owns this bundle identity.

        Ownership is established by whichever command claims the identity first,
        and it is claimed by *both* kinds of command: a text authoring input and
        a published revision. Consulting only the revision table would let one
        conversation file design text first, then let a different conversation
        create the bundle under it — after which the original conversation's own
        input would be refused. Both records are therefore consulted, so the
        identity has exactly one owner from its first use.
        """
        row = db.execute(
            "SELECT conversation_id FROM workflow_bundle_current WHERE project_id=? AND id=?",
            (project_id, bundle_id),
        ).fetchone()
        if row is not None and row["conversation_id"] != conversation_id:
            raise WorkflowError("WORKFLOW_CONVERSATION_MISMATCH")
        claimed = db.execute(
            "SELECT record FROM workflow_authoring_inputs "
            "WHERE project_id=? AND id=? ORDER BY revision LIMIT 1",
            (project_id, bundle_id),
        ).fetchone()
        if claimed is not None and (
            json.loads(claimed["record"]).get("conversation_id") != conversation_id
        ):
            raise WorkflowError("WORKFLOW_CONVERSATION_MISMATCH")

    def _verified_bundle(
        self, project_id: str, bundle_id: str, revision: int, principal: str
    ) -> tuple[dict[str, Any], bundles.Bundle, CompiledWorkflow]:
        """Re-read and re-verify one revision, under the acting principal.

        The comparison against the durable record is the point: files that were
        replaced or edited after publication produce a mismatch with the recorded
        bundle, compiled and per-file digests instead of a new preview being
        served under an unchanged revision identity.
        """
        with self._owned(project_id, principal) as db:
            record = self._record(db, project_id, bundle_id, revision)
        root = self._bundle_root(project_id, bundle_id) / f"revision-{revision}"
        verified = bundles.read_directory(
            root,
            managed_root=self._bundle_root(project_id, bundle_id),
            bundle_id=bundle_id,
            revision=revision,
            project_id=project_id,
            conversation_id=str(record["conversation_id"]),
        )
        if verified.bundle_digest != record["bundle_digest"]:
            raise WorkflowError("WORKFLOW_BUNDLE_DIGEST_MISMATCH")
        if verified.manifest_file_digest != record["manifest_file_digest"]:
            raise WorkflowError("WORKFLOW_MANIFEST_DIGEST_MISMATCH")
        recorded_files = {item["path"]: item for item in record["files"]}
        if set(recorded_files) != {item.path for item in verified.files}:
            raise WorkflowError("WORKFLOW_FILE_INVENTORY_MISMATCH")
        for item in verified.files:
            entry = recorded_files[item.path]
            if entry["byte_digest"] != item.digest or entry["byte_length"] != len(item.raw):
                raise WorkflowError("WORKFLOW_FILE_DIGEST_MISMATCH")
        compiled = bundles.compile_bundle(
            verified, binding_index=record.get("binding_index") or {}
        )
        if compiled.compiled_digest != record["compiled_digest"]:
            raise WorkflowError(
                "WORKFLOW_COMPILE_MISMATCH",
                fields={"stored_compiled_digest": record["compiled_digest"]},
            )
        if compiled.compiler_revision != record["compiler_revision"]:
            raise WorkflowError("WORKFLOW_COMPILER_REVISION_MISMATCH")
        return record, verified, compiled

    def get_bundle(
        self, project_id: str, bundle_id: str, revision: int, *, principal: str
    ) -> dict[str, Any]:
        """Read one exact revision, re-verifying it against the files on disk."""
        identity, number = self._identity(bundle_id), self._positive(revision)
        record, verified, compiled = self._verified_bundle(
            project_id, identity, number, principal
        )
        document = dict(record)
        document["readback"] = {
            "source": "verified_files",
            "manifest": dict(verified.manifest),
            "files": [
                {
                    "path": item.path,
                    "byte_digest": item.digest,
                    "byte_length": len(item.raw),
                    "content": item.text,
                }
                for item in verified.files
            ],
            "bundle_digest": verified.bundle_digest,
            "manifest_file_digest": verified.manifest_file_digest,
            "compiled_digest": compiled.compiled_digest,
            "compiler_identity": compiled.compiler_identity,
            "compiler_revision": compiled.compiler_revision,
            "verified": True,
        }
        return document

    def list_bundles(self, project_id: str, *, principal: str) -> list[dict[str, Any]]:
        with self._owned(project_id, principal) as db:
            return [
                {
                    "id": row["id"],
                    "revision": row["revision"],
                    "conversation_id": row["conversation_id"],
                    "digest": row["digest"],
                }
                for row in db.execute(
                    "SELECT id, revision, conversation_id, digest FROM workflow_bundles "
                    "WHERE project_id=? ORDER BY id, revision",
                    (project_id,),
                )
            ]

    def revision_record(
        self, project_id: str, bundle_id: str, revision: int, *, principal: str
    ) -> dict[str, Any]:
        identity, number = self._identity(bundle_id), self._positive(revision)
        with self._owned(project_id, principal) as db:
            return self._record(db, project_id, identity, number)

    def compiled_bundle(
        self, project_id: str, bundle_id: str, revision: int, *, principal: str
    ) -> CompiledWorkflow:
        """The verified compiler result for #177's trusted loader.

        Returned only after the files were re-read and re-compiled and the result
        matched the durable revision record, so a loader never consumes a
        template this process has not actually verified in this process.
        """
        identity, number = self._identity(bundle_id), self._positive(revision)
        _, _, compiled = self._verified_bundle(project_id, identity, number, principal)
        return compiled

    # ---------------------------------------------------------------- preview

    def preview(
        self,
        project_id: str,
        bundle_id: str,
        revision: int,
        *,
        compare_to: int | None,
        principal: str,
    ) -> dict[str, Any]:
        """The same-source projection: graph, Mermaid, table and diff.

        Every field is derived from one compiled result of one verified revision,
        so the diagram and the table cannot describe a different version than the
        files they are shown beside. The read is scoped to the acting principal.
        """
        identity, number = self._identity(bundle_id), self._positive(revision)
        record, _, compiled = self._verified_bundle(project_id, identity, number, principal)
        document = projection(compiled)
        left: CompiledWorkflow | None = None
        if compare_to is not None:
            previous_number = self._positive(compare_to)
            _, _, left = self._verified_bundle(
                project_id, identity, previous_number, principal
            )
        document["diff"] = diff(left, compiled)
        document["project_id"] = project_id
        document["bundle_id"] = identity
        document["revision"] = number
        document["conversation_id"] = record["conversation_id"]
        document["immutable_revision_record"] = {
            "digest": record["digest"],
            "bundle_digest": record["bundle_digest"],
            "compiled_digest": record["compiled_digest"],
            "compiler_revision": record["compiler_revision"],
        }
        document["preview_id"] = content_digest(
            [
                "workflow.preview",
                project_id,
                identity,
                number,
                record["bundle_digest"],
                record["compiled_digest"],
                record["compiler_revision"],
            ]
        )
        return document

    # ------------------------------------------------------------ direct edits

    def edit(
        self,
        project_id: str,
        conversation_id: str,
        bundle_id: str,
        payload: Mapping[str, Any],
        *,
        expected_revision: object,
        principal: str,
        command_key: str,
    ) -> tuple[dict[str, Any], bool]:
        """Apply a structural table edit and write a new revision deterministically.

        The whole path is arithmetic on the stored bytes: no model, no network, no
        shell. The response carries ``model_calls: 0`` so a caller can assert the
        property instead of trusting it.

        Idempotency is resolved before the conditional write: a repeated command
        returns its original result even after the head advanced, while a genuine
        late write still loses the CAS.
        """
        identity = self._identity(bundle_id)
        expected = self._positive(expected_revision)
        self._require_fields(payload, EDIT_PAYLOAD_FIELDS)
        self._require_conversation(project_id, conversation_id)
        edits = self._edits(payload)
        request_digest = content_digest(
            ["workflow.bundle.edit", project_id, conversation_id, identity, expected, edits]
        )
        # First short transaction: resolve a replay, or decide the CAS outcome.
        with self._owned(project_id, principal) as db:
            replay = self._replay(db, principal, command_key, request_digest)
            if replay is not None:
                return replay, False
            record = self._record(db, project_id, identity, expected)
            self._require_fixed_conversation(db, project_id, identity, conversation_id)
            head_row = db.execute(
                "SELECT revision FROM workflow_bundle_current WHERE project_id=? AND id=?",
                (project_id, identity),
            ).fetchone()
            head = int(head_row["revision"]) if head_row is not None else None
            if head != expected:
                raise WorkflowError("WORKFLOW_REVISION_CONFLICT", current_revision=head)
        _, verified, _ = self._verified_bundle(project_id, identity, expected, principal)
        files = {item.path: item.raw for item in verified.files if item.path != MANIFEST_PATH}
        workflow_document = yamls.load(files[WORKFLOW_PATH].decode("utf-8"), file=WORKFLOW_PATH)
        edited, applied_edits = self._apply_edits(workflow_document, edits)
        files[WORKFLOW_PATH] = (yamls.dump(edited) + "\n").encode("utf-8")
        compiled = self._compile_files(
            project_id,
            principal,
            files,
            delivery_kind=str(record["delivery_kind"]),
            bundle_id=identity,
            revision=expected + 1,
            binding_index=record.get("binding_index") or {},
        )
        # The command-specific fields are added inside ``finalize`` so the exact
        # document a caller receives is what the ledger stores: a replay then
        # returns the same complete result, not a bare revision record.
        return self._publish(
            project_id=project_id,
            conversation_id=conversation_id,
            bundle_id=identity,
            revision=expected + 1,
            files=files,
            delivery_kind=str(record["delivery_kind"]),
            compiled=compiled,
            principal=principal,
            command_key=command_key,
            request_digest=request_digest,
            expected_revision=expected,
            finalize=lambda created: {
                **created,
                "applied_edits": applied_edits,
                "model_calls": 0,
                "edit_path": "deterministic_structural",
            },
        )

    def _edits(self, payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        declared = payload.get("edits")
        if not isinstance(declared, list) or not declared or len(declared) > 256:
            raise WorkflowError(
                "WORKFLOW_EDIT_INVALID",
                diagnostics=[located("WORKFLOW_EDIT_INVALID", WORKFLOW_PATH, "edits are required")],
            )
        edits: list[dict[str, Any]] = []
        for index, entry in enumerate(declared):
            if not isinstance(entry, Mapping):
                raise WorkflowError(
                    "WORKFLOW_EDIT_INVALID",
                    diagnostics=[
                        located("WORKFLOW_EDIT_INVALID", f"{WORKFLOW_PATH}#/edits/{index}", "shape")
                    ],
                )
            unknown = sorted(set(entry) - _EDIT_FIELDS)
            if unknown:
                # A silently ignored field would let a caller believe an edit was
                # applied when nothing read it.
                raise WorkflowError(
                    "WORKFLOW_EDIT_INVALID",
                    diagnostics=[
                        located(
                            "WORKFLOW_EDIT_INVALID",
                            f"{WORKFLOW_PATH}#/edits/{index}/id={name}",
                            "unknown edit field",
                        )
                        for name in unknown
                    ],
                )
            operation = entry.get("operation")
            step_id = entry.get("step_id")
            if operation not in _EDIT_OPERATIONS:
                raise WorkflowError(
                    "WORKFLOW_EDIT_UNSUPPORTED",
                    diagnostics=[
                        located(
                            "WORKFLOW_EDIT_UNSUPPORTED",
                            f"{WORKFLOW_PATH}#/edits/{index}/operation",
                            "unsupported edit operation",
                        )
                    ],
                )
            if not isinstance(step_id, str) or not step_id:
                raise WorkflowError(
                    "WORKFLOW_EDIT_INVALID",
                    diagnostics=[
                        located(
                            "WORKFLOW_EDIT_INVALID",
                            f"{WORKFLOW_PATH}#/edits/{index}/step_id",
                            "a step identity is required",
                        )
                    ],
                )
            edits.append(dict(entry))
        return edits

    def _apply_edits(
        self, workflow: Any, edits: Sequence[Mapping[str, Any]]
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        if not isinstance(workflow, dict) or not isinstance(workflow.get("steps"), list):
            raise WorkflowError(
                "WORKFLOW_SCHEMA_INVALID",
                diagnostics=[
                    located("WORKFLOW_SCHEMA_INVALID", WORKFLOW_PATH, "steps are required")
                ],
            )
        steps: list[dict[str, Any]] = [dict(step) for step in workflow["steps"]]
        index: dict[str, dict[str, Any]] = {str(step.get("id")): step for step in steps}
        applied: list[dict[str, Any]] = []
        for entry in edits:
            operation = str(entry["operation"])
            step_id = str(entry["step_id"])
            if operation == "add_step":
                if step_id in index:
                    raise WorkflowError(
                        "WORKFLOW_STEP_ID_DUPLICATE",
                        diagnostics=[
                            located(
                                "WORKFLOW_STEP_ID_DUPLICATE",
                                f"{WORKFLOW_PATH}#/steps/id={step_id}",
                                "step identity already exists",
                                step_id=step_id,
                            )
                        ],
                    )
                step = self._new_step(entry, step_id)
                steps.append(step)
                index[step_id] = step
                applied.append({"operation": operation, "step_id": step_id})
                continue
            existing_step = index.get(step_id)
            if existing_step is None:
                raise WorkflowError(
                    "WORKFLOW_STEP_NOT_FOUND",
                    diagnostics=[
                        located(
                            "WORKFLOW_STEP_NOT_FOUND",
                            f"{WORKFLOW_PATH}#/steps/id={step_id}",
                            "the edited step is not defined",
                            step_id=step_id,
                        )
                    ],
                )
            if operation == "remove_step":
                steps = [item for item in steps if item.get("id") != step_id]
                index.pop(step_id, None)
            elif operation == "set_dependency":
                dependency = _edit_text(entry.get("depends_on"), "depends_on")
                current = [str(item) for item in existing_step.get("depends_on", [])]
                if dependency not in current:
                    current.append(dependency)
                existing_step["depends_on"] = sorted(current)
            elif operation == "remove_dependency":
                dependency = _edit_text(entry.get("depends_on"), "depends_on")
                existing_step["depends_on"] = sorted(
                    item
                    for item in (str(value) for value in existing_step.get("depends_on", []))
                    if item != dependency
                )
            elif operation == "set_role":
                existing_step["role"] = _edit_text(entry.get("role"), "role")
            elif operation == "set_required":
                required = entry.get("required")
                if not isinstance(required, bool):
                    raise WorkflowError(
                        "WORKFLOW_EDIT_INVALID",
                        diagnostics=[
                            located(
                                "WORKFLOW_EDIT_INVALID",
                                f"{WORKFLOW_PATH}#/edits/id={step_id}/required",
                                "required must be a boolean",
                                step_id=step_id,
                            )
                        ],
                    )
                existing_step["required"] = required
            elif operation == "set_step_input":
                name = _edit_text(entry.get("name"), "name")
                if "value" not in entry:
                    raise WorkflowError(
                        "WORKFLOW_EDIT_INVALID",
                        diagnostics=[
                            located(
                                "WORKFLOW_EDIT_INVALID",
                                f"{WORKFLOW_PATH}#/edits/id={step_id}/value",
                                "a value is required",
                                step_id=step_id,
                            )
                        ],
                    )
                inputs = dict(existing_step.get("inputs", {}))
                inputs[name] = entry["value"]
                existing_step["inputs"] = dict(sorted(inputs.items()))
            applied.append({"operation": operation, "step_id": step_id})
        document = dict(workflow)
        document["steps"] = steps
        return document, applied

    @staticmethod
    def _new_step(entry: Mapping[str, Any], step_id: str) -> dict[str, Any]:
        for field in ("execution_kind", "output_contract"):
            if not isinstance(entry.get(field), str):
                raise WorkflowError(
                    "WORKFLOW_EDIT_INVALID",
                    diagnostics=[
                        located(
                            "WORKFLOW_EDIT_INVALID",
                            f"{WORKFLOW_PATH}#/edits/id={step_id}/{field}",
                            "a new step declares its kind and output contract",
                            step_id=step_id,
                        )
                    ],
                )
        step: dict[str, Any] = {
            "id": step_id,
            "execution_kind": entry["execution_kind"],
            "output_contract": entry["output_contract"],
        }
        for field in ("role", "inputs", "depends_on", "required", "condition", "join"):
            if field in entry:
                step[field] = entry[field]
        return step

    # --------------------------------------------------------- text authoring

    def authoring(
        self,
        project_id: str,
        conversation_id: str,
        bundle_id: str,
        payload: Mapping[str, Any],
        *,
        principal: str,
        command_key: str,
    ) -> tuple[dict[str, Any], bool]:
        """Persist a text instruction as authoring input in a pending state.

        The instruction is stored verbatim and its state is explicit. Nothing in
        this path produces a configuration, so a saved sentence can never be
        mistaken for a generated result (docs/architecture/09 §1).
        """
        identity = self._identity(bundle_id)
        self._require_fields(payload, AUTHORING_PAYLOAD_FIELDS)
        self._require_conversation(project_id, conversation_id)
        instruction = payload.get("instruction")
        if not isinstance(instruction, str) or not 1 <= len(instruction) <= 8_000:
            raise WorkflowError(
                "WORKFLOW_AUTHORING_INVALID",
                diagnostics=[
                    located(
                        "WORKFLOW_AUTHORING_INVALID",
                        f"{WORKFLOW_PATH}#/instruction",
                        "an instruction of bounded length is required",
                    )
                ],
            )
        base_revision = payload.get("base_revision")
        digest = content_digest(
            [
                "workflow.authoring",
                project_id,
                conversation_id,
                identity,
                base_revision,
                instruction,
            ]
        )
        with self._owned(project_id, principal) as db:
            replay = self._replay(db, principal, command_key, digest)
            if replay is not None:
                return replay, False
            # The owning conversation is fixed by whichever command established
            # this bundle identity. A later instruction from a different
            # conversation in the same project is refused here, before a base
            # revision is even consulted, so no text can be filed under another
            # conversation's bundle.
            self._require_fixed_conversation(db, project_id, identity, conversation_id)
            if base_revision is not None:
                resolved_base = self._positive(base_revision)
                self._record(db, project_id, identity, resolved_base)
            claimed = db.execute(
                "SELECT record FROM workflow_authoring_inputs "
                "WHERE project_id=? AND id=? ORDER BY revision LIMIT 1",
                (project_id, identity),
            ).fetchone()
            if claimed is not None and (
                json.loads(claimed["record"]).get("conversation_id") != conversation_id
            ):
                raise WorkflowError("WORKFLOW_CONVERSATION_MISMATCH")
            next_revision = (
                int(
                    db.execute(
                        "SELECT COALESCE(MAX(revision), 0) FROM workflow_authoring_inputs "
                        "WHERE project_id=? AND id=?",
                        (project_id, identity),
                    ).fetchone()[0]
                )
                + 1
            )
            item: dict[str, Any] = {
                "schema_version": AUTHORING_SCHEMA_VERSION,
                "project_id": project_id,
                "conversation_id": conversation_id,
                "bundle_id": identity,
                "revision": next_revision,
                "base_revision": base_revision,
                "instruction": instruction,
                "state": "pending_generation",
                "generated_configuration": None,
                "generated_by": None,
                "model_calls": 0,
                "recorded_by": principal,
                "recorded_at": self.clock(),
                "note": "authoring input only; no configuration is generated here",
            }
            db.execute(
                "INSERT INTO workflow_authoring_inputs VALUES (?,?,?,?)",
                (project_id, identity, next_revision, canonical_json(item)),
            )
            db.execute(
                "INSERT INTO commands VALUES (?,?,?,?)",
                (principal, command_key, digest, canonical_json(item)),
            )
            return item, True

    def list_authoring_inputs(
        self, project_id: str, bundle_id: str, *, principal: str
    ) -> list[dict[str, Any]]:
        identity = self._identity(bundle_id)
        with self._owned(project_id, principal) as db:
            return [
                json.loads(row["record"])
                for row in db.execute(
                    "SELECT record FROM workflow_authoring_inputs "
                    "WHERE project_id=? AND id=? ORDER BY revision",
                    (project_id, identity),
                )
            ]

    # ------------------------------------------------------------ kind catalog

    def catalog(self, project_id: str, *, principal: str) -> dict[str, Any]:
        """The trusted registry, including which revisions have no adapter."""
        with self._owned(project_id, principal):
            pass
        kinds: list[dict[str, Any]] = []
        for reference in registered_refs():
            kind = kind_for(reference)
            kinds.append(
                {
                    "execution_kind_ref": kind.ref,
                    "available": kind.available,
                    "requires_role": kind.requires_role,
                    "deterministic": kind.deterministic,
                    "side_effects": kind.side_effects,
                    "declared_inputs": [
                        {"name": item.name, "type": item.type, "required": item.required}
                        for item in kind.inputs
                    ],
                    "declared_outputs": [
                        {
                            "contract_ref": item.contract_ref,
                            "kind": item.kind,
                            "required": item.required,
                        }
                        for item in kind.outputs
                    ],
                    "required_capabilities": list(kind.required_capabilities),
                    "capability_evidence_refs": list(kind.capability_evidence_refs),
                    "reason_codes": list(kind.reason_codes),
                }
            )
        return {
            "kinds": kinds,
            "registry": "trusted_module_registry",
            "caller_supplied_modules": False,
        }

    # ----------------------------------------------------------------- helpers

    @staticmethod
    def _identity(value: object) -> str:
        """A bundle identity is one addressable path segment, not a path."""
        if not isinstance(value, str) or not 1 <= len(value) <= 96:
            raise WorkflowError("WORKFLOW_BUNDLE_ID_INVALID")
        try:
            BundlePaths.validate(value)
        except WorkflowError:
            # A bundle identity is an identity, not a file path: a value that
            # cannot be a path segment is reported as an invalid identity rather
            # than as a bad path, so the caller sees the field it supplied.
            raise WorkflowError(
                "WORKFLOW_BUNDLE_ID_INVALID",
                diagnostics=[
                    located(
                        "WORKFLOW_BUNDLE_ID_INVALID",
                        BUNDLE_ID_LOCATION,
                        "a bundle identity is a single addressable segment",
                    )
                ],
            ) from None
        if "/" in value or "\\" in value or value.startswith("."):
            raise WorkflowError(
                "WORKFLOW_BUNDLE_ID_INVALID",
                diagnostics=[
                    located(
                        "WORKFLOW_BUNDLE_ID_INVALID",
                        BUNDLE_ID_LOCATION,
                        "a bundle identity is a single addressable segment",
                    )
                ],
            )
        return value

    @staticmethod
    def _positive(value: object) -> int:
        if type(value) is not int or not 1 <= value <= 1_000_000:
            raise WorkflowError("WORKFLOW_REVISION_INVALID")
        return value

    @staticmethod
    def _delivery_kind(value: object) -> str:
        if not isinstance(value, str) or value not in {"report", "patch", "pr"}:
            raise WorkflowError(
                "WORKFLOW_DELIVERY_KIND_INVALID",
                diagnostics=[
                    located(
                        "WORKFLOW_DELIVERY_KIND_INVALID",
                        f"{WORKFLOW_PATH}#/delivery_kind",
                        "delivery_kind must be report, patch or pr",
                    )
                ],
            )
        return value

    @staticmethod
    def _require_fields(payload: object, allowed: frozenset[str]) -> None:
        """Refuse an unknown top-level field instead of silently ignoring it."""
        if not isinstance(payload, Mapping):
            raise WorkflowError(
                "WORKFLOW_SCHEMA_INVALID",
                diagnostics=[
                    located("WORKFLOW_SCHEMA_INVALID", WORKFLOW_PATH, "expected an object")
                ],
            )
        unknown = sorted(
            name for name in payload if not isinstance(name, str) or name not in allowed
        )
        if unknown:
            raise WorkflowError(
                "WORKFLOW_SCHEMA_INVALID",
                diagnostics=[
                    located(
                        "WORKFLOW_SCHEMA_INVALID",
                        f"{WORKFLOW_PATH}#/id={str(name)[:48]}",
                        "unknown field",
                    )
                    for name in unknown
                ],
            )

    @staticmethod
    def _declared_files(payload: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
        declared = payload.get("files")
        if not isinstance(declared, Sequence) or isinstance(declared, (str, bytes)):
            raise WorkflowError(
                "WORKFLOW_FILE_MISSING",
                diagnostics=[
                    located(
                        "WORKFLOW_FILE_MISSING",
                        WORKFLOW_PATH,
                        "files must be a list of path and content",
                    )
                ],
            )
        return declared

    def _require_conversation(self, project_id: str, conversation_id: str) -> None:
        """Constrain every write to this project's own conversation identity."""
        if (
            not isinstance(conversation_id, str)
            or not 1 <= len(conversation_id) <= 256
            or any(character.isspace() for character in conversation_id)
        ):
            raise WorkflowError("WORKFLOW_CONVERSATION_INVALID")
        try:
            owner = self.conversations.conversation_project(conversation_id)
        except ConversationError:
            raise WorkflowError("WORKFLOW_CONVERSATION_NOT_FOUND") from None
        if owner != project_id:
            # A cross-project conversation is reported exactly like an unknown
            # one, so a caller cannot probe another project's conversation ids.
            raise WorkflowError("WORKFLOW_CONVERSATION_NOT_FOUND")


BUNDLE_ID_LOCATION = "workflow.yaml#/bundle_id"

_EDIT_OPERATIONS = frozenset(
    {
        "set_dependency",
        "remove_dependency",
        "set_role",
        "set_required",
        "add_step",
        "remove_step",
        "set_step_input",
    }
)

_EDIT_FIELDS = frozenset(
    {
        "operation",
        "step_id",
        "depends_on",
        "role",
        "required",
        "name",
        "value",
        "execution_kind",
        "output_contract",
        "inputs",
        "condition",
        "join",
    }
)


def _edit_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 96:
        raise WorkflowError(
            "WORKFLOW_EDIT_INVALID",
            diagnostics=[
                located(
                    "WORKFLOW_EDIT_INVALID",
                    f"{WORKFLOW_PATH}#/edits/{field}",
                    "a bounded text value is required",
                )
            ],
        )
    return value


def _parse_binding_ref(reference: str) -> tuple[str, int]:
    """Parse ``binding:<binding_id>@<revision>``, refusing a floating reference."""
    body = reference[len("binding:") :]
    if "@" not in body:
        raise WorkflowError(
            "WORKFLOW_REFERENCE_NOT_PINNED",
            diagnostics=[
                located(
                    "WORKFLOW_REFERENCE_NOT_PINNED",
                    f"{WORKFLOW_PATH}#/bindings",
                    "a gateway binding reference names an exact revision",
                )
            ],
        )
    binding_id, _, revision = body.rpartition("@")
    if not binding_id or revision.casefold() in {"latest", "head", "*", "next"}:
        raise WorkflowError(
            "WORKFLOW_REFERENCE_NOT_PINNED",
            diagnostics=[
                located(
                    "WORKFLOW_REFERENCE_NOT_PINNED",
                    f"{WORKFLOW_PATH}#/bindings",
                    "a latest reference is never resolved at compile or load time",
                )
            ],
        )
    try:
        number = int(revision)
    except ValueError:
        raise WorkflowError(
            "WORKFLOW_REFERENCE_INVALID",
            diagnostics=[
                located(
                    "WORKFLOW_REFERENCE_INVALID",
                    f"{WORKFLOW_PATH}#/bindings",
                    "the binding revision is not a number",
                )
            ],
        ) from None
    if number < 1:
        raise WorkflowError("WORKFLOW_REFERENCE_INVALID")
    BundlePaths.validate(binding_id)
    if "/" in binding_id or "\\" in binding_id:
        raise WorkflowError("WORKFLOW_REFERENCE_INVALID")
    return binding_id, number


def _content_index(files: Mapping[str, bytes]) -> dict[str, str]:
    """The identity a command is keyed on: path plus the digest of its bytes."""
    return {path: bundles.byte_digest(raw) for path, raw in sorted(files.items())}


def _write_bytes(path: Path, data: bytes) -> None:
    """Write one file completely, then make it durable before it is referenced."""
    with open(path, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _discard(path: Path) -> None:
    """Remove a staging directory this process just created."""
    discard_staging_tree(path)
