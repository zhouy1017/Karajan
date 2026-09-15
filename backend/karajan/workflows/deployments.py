"""Durable deployment intent, a real materialised package and a conditional slot.

Deployment is deliberately not a boolean column. It is an ordered sequence of
steps whose side effects are arranged so that an interruption at any point is
recoverable and can never be silently manufactured into a success:

1. **Persist the intent.** The whole authorized command - principal, operation,
   project, conversation, bundle identity and revision, the bundle, manifest,
   compiled and per-file digests, the compiler identity and revision, the
   confirmed preview identity, the target slot, the expected active revision and
   the explicit action - is written to a durable row *before* a single byte is
   copied or any loader runs. A command interrupted later is therefore always
   visible, and always reconcilable against the copy it already recorded.
2. **Materialise the pending package from the verified source bytes.** The bytes
   come from ``WorkflowStore._verified_bundle``, which re-read every file of the
   published revision from disk, re-hashed it and re-compiled it in this call.
   They are never taken from the request, and every managed component is checked
   for a link before the first write.
3. **Reopen and recompile the real package.** ``WorkflowLoader`` reads back the
   files this process just wrote and recompiles them with the module-private
   trusted registry. Readiness is a capability fact produced by that read, not a
   value the intent supplied.
4. **Compare the receipt to the intent, then release the slot.** Only when the
   loader's observed bundle digest, manifest digest, compiled digest and compiler
   revision all equal the frozen ones is the receipt accepted. The receipt is
   written in the same short transaction as the conditional slot update, so a
   slot that names a deployment always carries the receipt that justified it.
5. **Activate conditionally.** The slot update carries the expected active
   revision, so exactly one of two concurrent commands can win and a rollback
   cannot overwrite a newer deployment.

Two facts are kept apart and never merged: the *historical receipt* of what was
loaded when a deployment was activated, and the *current* loaded/readiness state.
A new process re-loads the active package and replaces only the second. The first
is immutable evidence of what happened, not a claim about the present.
"""

import json
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from karajan.projects import ProjectRegistry
from karajan.storage import ExistingStoreError, require_schema

from . import bundle as bundles
from .digests import canonical_json, content_digest
from .errors import WorkflowError, located
from .layout import (
    MANIFEST_PATH,
    WORKFLOW_PATH,
    discard_staging_tree,
    require_segment,
    require_trusted_chain,
)
from .loader import LOADER_IDENTITY, FileIdentity, LoadReceipt, WorkflowLoader
from .store import WorkflowStore

REQUIRED_SCHEMA = {
    "projects": ["id", "snapshot"],
    "project_owners": ["project_id", "principal"],
    "commands": ["principal", "key", "digest", "result"],
    "workflow_bundles": ["project_id", "id", "revision", "record", "digest"],
    "workflow_bundle_current": ["project_id", "id", "revision"],
    "workflow_deployment_intents": ["principal", "key", "digest", "record"],
    "workflow_deployments": ["project_id", "slot", "deployment_id", "record"],
    "workflow_deployment_slots": ["project_id", "slot", "slot_revision"],
}

#: The actions this slice accepts. ``deploy_and_run`` is a different product
#: decision with its own inputs and its own authorization, so it is refused
#: explicitly rather than quietly treated as a plain deployment.
ACTIONS = ("deploy_only", "rollback")
REFUSED_ACTIONS = ("deploy_and_run",)

#: The one slot a project has in this slice. A slot identity is a name, never a
#: path, and it is validated before it is joined to the data root.
DEFAULT_SLOT = "default"

DEPLOY_PAYLOAD_FIELDS = frozenset({"action", "slot", "expected_active_revision", "preview_id"})
ROLLBACK_PAYLOAD_FIELDS = frozenset({"target_deployment_id", "expected_active_revision"})

INTENT_SCHEMA_VERSION = "karajan.workflow-deployment-intent.v1"
DEPLOYMENT_SCHEMA_VERSION = "karajan.workflow-deployment.v1"

#: The steps a command performs, in order. Each name is recorded in the intent as
#: soon as it completes, which is what makes a later reconciliation a read of
#: real completed side effects rather than a guess.
STEPS = ("materialize", "load", "activate")


class DeploymentStore:
    """Persisted deployment intent, real package materialisation and one slot."""

    def __init__(
        self,
        projects: ProjectRegistry,
        conversations: Any,
        store: WorkflowStore,
        root: Path,
        *,
        clock: Callable[[], float] = time.time,
        loader: WorkflowLoader | None = None,
    ) -> None:
        self.projects = projects
        self.conversations = conversations
        self.store = store
        self.root = Path(root)
        self.clock = clock
        self.loader = loader or WorkflowLoader(self.root, clock=clock)
        self.existing_only = projects.existing_only
        self.unavailable: str | None = None
        #: Serializes the read-decide-act part of a command inside this process,
        #: so the copy step and the slot CAS cannot interleave with another
        #: thread's. It is reentrant because a replay re-reads the slot's current
        #: state through the same guard. The durable guarantee is still the
        #: conditional slot update in SQL; this only keeps database transactions
        #: short and the filesystem step single-threaded.
        self._guard = threading.RLock()
        if self.existing_only:
            try:
                require_schema(projects.database, REQUIRED_SCHEMA)
            except ExistingStoreError:
                self.unavailable = "WORKFLOW_DEPLOYMENT_STATE_UNAVAILABLE"
            return
        self.root.mkdir(parents=True, exist_ok=True)
        with self.projects._transaction() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS workflow_deployment_intents ("
                "principal TEXT NOT NULL, key TEXT NOT NULL, digest TEXT NOT NULL, "
                "record TEXT NOT NULL, PRIMARY KEY(principal, key))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS workflow_deployments ("
                "project_id TEXT NOT NULL REFERENCES projects(id), slot TEXT NOT NULL, "
                "deployment_id TEXT NOT NULL, record TEXT NOT NULL, "
                "PRIMARY KEY(project_id, slot, deployment_id))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS workflow_deployment_slots ("
                "project_id TEXT NOT NULL REFERENCES projects(id), slot TEXT NOT NULL, "
                "slot_revision INTEGER NOT NULL, deployment_id TEXT, "
                "loaded_record TEXT, PRIMARY KEY(project_id, slot))"
            )

    # ------------------------------------------------------------------ plumbing

    @contextmanager
    def _owned(self, project_id: str, principal: str) -> Iterator[sqlite3.Connection]:
        with self.store._owned(project_id, principal) as db:
            yield db

    def _replay(
        self, db: sqlite3.Connection, principal: str, key: str, digest: str
    ) -> dict[str, Any] | None:
        return self.store._replay(db, principal, key, digest)

    def _ledger(
        self, db: sqlite3.Connection, principal: str, key: str
    ) -> dict[str, Any] | None:
        row = db.execute(
            "SELECT digest, result FROM commands WHERE principal=? AND key=?", (principal, key)
        ).fetchone()
        return dict(row) if row is not None else None

    def _intent_row(
        self, db: sqlite3.Connection, principal: str, key: str
    ) -> dict[str, Any] | None:
        row = db.execute(
            "SELECT record FROM workflow_deployment_intents WHERE principal=? AND key=?",
            (principal, key),
        ).fetchone()
        return json.loads(row["record"]) if row is not None else None

    # ---------------------------------------------------------------- commands

    def deploy(
        self,
        project_id: str,
        conversation_id: str,
        bundle_id: str,
        payload: Mapping[str, Any],
        *,
        preview_revision: object,
        principal: str,
        command_key: str,
    ) -> tuple[dict[str, Any], bool]:
        """Confirm, materialise, load, then conditionally activate one revision.

        The command is keyed on the fields the caller actually authorized - the
        operation, its subject, the confirmed preview and the expected slot
        revision - so repeating it returns the original result even after the
        slot has moved, while a changed payload under the same key is a conflict.
        Everything else (digests, inventory, compiler identity) is *derived* from
        the real published files when the intent is first written.
        """
        self._require_fields(payload, DEPLOY_PAYLOAD_FIELDS)
        action = self._action(payload.get("action"))
        if action == "rollback":
            raise WorkflowError("WORKFLOW_DEPLOY_ACTION_UNSUPPORTED", fields={"action": action})
        identity = self.store._identity(bundle_id)
        slot = self._slot(payload.get("slot", DEFAULT_SLOT))
        revision = self.store._positive(preview_revision)
        expected = self._expected_active(payload.get("expected_active_revision"))
        preview_id = self._preview_id(payload.get("preview_id"))
        digest = content_digest(
            [
                "workflow.deploy",
                action,
                project_id,
                conversation_id,
                identity,
                revision,
                preview_id,
                slot,
                expected,
            ]
        )
        return self._command(
            project_id=project_id,
            principal=principal,
            command_key=command_key,
            digest=digest,
            build=lambda: self._deploy_intent(
                action=action,
                project_id=project_id,
                conversation_id=conversation_id,
                bundle_id=identity,
                revision=revision,
                preview_id=preview_id,
                slot=slot,
                expected_active=expected,
                principal=principal,
            ),
        )

    def rollback(
        self,
        project_id: str,
        payload: Mapping[str, Any],
        *,
        principal: str,
        command_key: str,
    ) -> tuple[dict[str, Any], bool]:
        """Re-activate an exact historical deployment through the same pipeline.

        A rollback is not a shortcut around the loader. It names a deployment
        that really happened in this project and slot, resolves the bundle
        revision that deployment activated, and then runs the ordinary
        materialise/load/conditionally-activate path against a *new* deployment
        directory. Nothing about the target is rewritten, and a concurrent newer
        deployment still wins the slot.
        """
        self._require_fields(payload, ROLLBACK_PAYLOAD_FIELDS)
        target = require_segment(
            payload.get("target_deployment_id"),
            code="WORKFLOW_DEPLOYMENT_ID_INVALID",
            detail="a deployment identity is one addressable name",
            location="workflow.yaml#/deployment_id",
        )
        expected = self._expected_active(payload.get("expected_active_revision"))
        historical = self._historical_deployment(project_id, target, principal)
        slot = str(historical["slot"])
        digest = content_digest(
            ["workflow.rollback", project_id, slot, target, expected, historical["digest"]]
        )
        return self._command(
            project_id=project_id,
            principal=principal,
            command_key=command_key,
            digest=digest,
            build=lambda: self._rollback_intent(
                project_id=project_id,
                slot=slot,
                target=historical,
                expected_active=expected,
                principal=principal,
            ),
        )

    def _command(
        self,
        *,
        project_id: str,
        principal: str,
        command_key: str,
        digest: str,
        build: Callable[[], dict[str, Any]],
    ) -> tuple[dict[str, Any], bool]:
        """Resolve a replay, else persist one intent and run it to completion.

        The order matters and is the whole recovery contract. A finished command
        is answered from the ledger, so a repeated key never performs a second
        deployment. An *unfinished* command is resumed from its own persisted
        intent, so an interrupted copy, load or activation is reconciled against
        what actually happened rather than being replaced by a new deployment
        that would hide the unknown outcome.

        Every database access is its own short transaction and every decision is
        taken from data already read inside that one transaction. No store read,
        no loader call and no current-state projection happens while a
        transaction is open or while this process's guard is held: this store
        shares one SQLite file with the rest of the control plane, and a nested
        ``BEGIN IMMEDIATE`` would block against itself forever.
        """
        replay: dict[str, Any] | None = None
        stored_intent: dict[str, Any] | None = None
        with self._guard:
            with self._owned(project_id, principal) as db:
                completed = self._ledger(db, principal, command_key)
                if completed is None:
                    stored_intent = self._intent_row(db, principal, command_key)
            if completed is not None:
                if completed["digest"] != digest:
                    raise WorkflowError("WORKFLOW_IDEMPOTENCY_CONFLICT")
                replay = json.loads(completed["result"])
            elif stored_intent is None:
                record = build()
                with self._owned(project_id, principal) as db:
                    again = self._ledger(db, principal, command_key)
                    if again is not None:
                        # Another thread finished the identical command while this
                        # one was freezing its revision: the same key yields
                        # exactly one deployment.
                        if again["digest"] != digest:
                            raise WorkflowError("WORKFLOW_IDEMPOTENCY_CONFLICT")
                        replay = json.loads(again["result"])
                    else:
                        concurrent = self._intent_row(db, principal, command_key)
                        if concurrent is not None:
                            if concurrent.get("command_digest") != digest:
                                raise WorkflowError("WORKFLOW_IDEMPOTENCY_CONFLICT")
                            stored_intent = concurrent
                        else:
                            record["command_digest"] = digest
                            db.execute(
                                "INSERT INTO workflow_deployment_intents VALUES (?,?,?,?)",
                                (principal, command_key, digest, canonical_json(record)),
                            )
                            stored_intent = record
            elif stored_intent.get("command_digest") not in {None, digest}:
                raise WorkflowError("WORKFLOW_IDEMPOTENCY_CONFLICT")
        if replay is not None:
            return self._document(project_id, principal, replay), False
        assert stored_intent is not None
        return self._execute(
            stored_intent, principal=principal, command_key=command_key, digest=digest
        )

    # ------------------------------------------------------------ intent build

    def _deploy_intent(
        self,
        *,
        action: str,
        project_id: str,
        conversation_id: str,
        bundle_id: str,
        revision: int,
        preview_id: str,
        slot: str,
        expected_active: int,
        principal: str,
    ) -> dict[str, Any]:
        """Freeze the real revision and record the intent before any copy.

        Two things are checked before anything is written. The requested revision
        must still be the bundle's *current* revision, and the confirmed preview
        must be the one this process derives from those verified bytes. Either
        check failing means the confirmation no longer describes what would be
        deployed, so it is refused rather than resolved to whatever the head
        happens to be now - a confirmation is of a specific design, and a later
        publication must be confirmed again on its own.
        """
        head = self._current_revision(project_id, bundle_id, principal)
        if head != revision:
            raise WorkflowError("WORKFLOW_PREVIEW_STALE", current_revision=head)
        frozen = self._freeze(project_id, bundle_id, revision, principal, conversation_id)
        if frozen.preview_id != preview_id:
            # The confirmed preview no longer describes these bytes: something
            # changed them between the confirmation and the command.
            raise WorkflowError(
                "WORKFLOW_PREVIEW_STALE",
                fields={"current_preview_id": frozen.preview_id},
            )
        if frozen.conversation_id != conversation_id:
            raise WorkflowError("WORKFLOW_CONVERSATION_MISMATCH")
        return self._intent_record(
            action=action,
            project_id=project_id,
            conversation_id=conversation_id,
            slot=slot,
            deployment_id=uuid.uuid4().hex,
            frozen=frozen,
            expected_active=expected_active,
            principal=principal,
            rollback_of=None,
        )

    def _current_revision(self, project_id: str, bundle_id: str, principal: str) -> int:
        """The bundle's durable head revision, read under the acting principal."""
        with self._owned(project_id, principal) as db:
            row = db.execute(
                "SELECT revision FROM workflow_bundle_current WHERE project_id=? AND id=?",
                (project_id, bundle_id),
            ).fetchone()
        if row is None:
            raise WorkflowError("WORKFLOW_BUNDLE_NOT_FOUND")
        return int(row["revision"])

    def _rollback_intent(
        self,
        *,
        project_id: str,
        slot: str,
        target: Mapping[str, Any],
        expected_active: int,
        principal: str,
    ) -> dict[str, Any]:
        frozen = self._freeze(
            project_id,
            str(target["bundle_id"]),
            int(target["bundle_revision"]),
            principal,
            str(target["conversation_id"]),
        )
        return self._intent_record(
            action="rollback",
            project_id=project_id,
            conversation_id=str(target["conversation_id"]),
            slot=slot,
            deployment_id=uuid.uuid4().hex,
            frozen=frozen,
            expected_active=expected_active,
            principal=principal,
            rollback_of=str(target["deployment_id"]),
        )

    def _intent_record(
        self,
        *,
        action: str,
        project_id: str,
        conversation_id: str,
        slot: str,
        deployment_id: str,
        frozen: "_Frozen",
        expected_active: int,
        principal: str,
        rollback_of: str | None,
    ) -> dict[str, Any]:
        return {
            "schema_version": INTENT_SCHEMA_VERSION,
            "action": action,
            "project_id": project_id,
            "conversation_id": conversation_id,
            "slot": slot,
            "deployment_id": deployment_id,
            "rollback_of": rollback_of,
            "expected_active_revision": expected_active,
            "commanded_by": principal,
            "commanded_at": self.clock(),
            "completed_steps": [],
            "frozen": frozen.as_document(),
        }

    def _freeze(
        self,
        project_id: str,
        bundle_id: str,
        revision: int,
        principal: str,
        conversation_id: str,
    ) -> "_Frozen":
        """Re-read and re-verify one exact revision, then freeze its identity.

        ``_verified_bundle`` is the single place that reads the published files
        from disk, re-hashes them and re-compiles them, so it is the only source
        of the bytes this store will copy. The preview identity is derived from
        that same verified revision, which is what makes a confirmation of an
        out-of-date preview detectable rather than silently accepted.
        """
        record, verified, compiled = self.store._verified_bundle(
            project_id, bundle_id, revision, principal
        )
        preview = self.store.preview(
            project_id, bundle_id, revision, compare_to=None, principal=principal
        )
        return _Frozen(
            project_id=project_id,
            conversation_id=str(record["conversation_id"]),
            bundle_id=bundle_id,
            bundle_revision=revision,
            delivery_kind=str(record["delivery_kind"]),
            bundle_digest=verified.bundle_digest,
            manifest_file_digest=verified.manifest_file_digest,
            compiled_digest=compiled.compiled_digest,
            compiler_identity=compiled.compiler_identity,
            compiler_revision=compiled.compiler_revision,
            files=tuple(
                FileIdentity(item.path, item.digest, len(item.raw))
                for item in sorted(verified.files, key=lambda item: item.path)
            ),
            binding_index={
                reference: dict(resolved)
                for reference, resolved in (record.get("binding_index") or {}).items()
            },
            preview_revision=revision,
            preview_id=str(preview["preview_id"]),
        )

    # -------------------------------------------------------------- execution

    def _execute(
        self,
        intent: dict[str, Any],
        *,
        principal: str,
        command_key: str,
        digest: str,
    ) -> tuple[dict[str, Any], bool]:
        """Run, or resume, one persisted command's remaining steps.

        Every step is idempotent against the durable facts: materialising adopts
        a copy whose bytes are exactly the frozen ones, loading is a pure read of
        real files, and activation is a conditional update guarded by the ledger.
        A resumed command therefore converges on the same deployment rather than
        creating a second one, and an interrupted package is checked rather than
        replaced.

        A failure is *recorded* on the intent before it propagates. The intent
        keeps the steps that really completed, the reason code and the located
        diagnostics, so a later query or restart can reconcile the original
        command instead of being told only that something went wrong.
        """
        project_id = str(intent["project_id"])
        slot = str(intent["slot"])
        deployment_id = str(intent["deployment_id"])
        identity = _Identity(intent["frozen"])
        try:
            self._materialize(
                project_id=project_id, slot=slot, deployment_id=deployment_id, intent=intent
            )
            self._record_step(project_id, principal, command_key, "materialize")
            receipt = self.loader.load(
                project_id=project_id,
                slot=slot,
                deployment_id=deployment_id,
                expected=identity.document(),
                verify_bytes=True,
            )
            self._record_step(project_id, principal, command_key, "load")
            return self._activate(
                intent=intent,
                receipt=receipt,
                principal=principal,
                command_key=command_key,
                digest=digest,
            )
        except WorkflowError as error:
            self._record_failure(project_id, principal, command_key, intent, error.code, error)
            raise
        except (OSError, sqlite3.Error) as error:
            # An ordinary filesystem or database failure is a *known* failure of
            # this command, so it is recorded as one: the caller learns which
            # step it reached. It is deliberately not a guess about an
            # unobserved crash - the process is still here and observed this.
            # The recorded reason is the exception's class name, never its
            # message, so no host path or credential can reach the record.
            self._record_failure(
                project_id,
                principal,
                command_key,
                intent,
                _step_failure_code(error),
                None,
            )
            raise

    def _record_failure(
        self,
        project_id: str,
        principal: str,
        command_key: str,
        intent: dict[str, Any],
        reason_code: str,
        error: WorkflowError | None,
    ) -> None:
        """Persist what actually happened, so the original command stays auditable.

        The outcome is written as ``failed`` with the steps that completed, never
        as ``ready``. A caller that sees this intent knows the exact stage it
        reached, so the recovery decision is made from evidence rather than by
        issuing a replacement deployment that would conceal an unknown outcome.

        The decision and the write happen in **one** transaction, against the
        current durable row. A second attempt of the same command that commits
        while this one is deciding therefore cannot be overwritten by a failure
        recorded from a stale reading, and a failure another attempt already
        wrote is merged rather than erased.

        Raising here is deliberately impossible: a failure to *record* a failure
        must never replace the caller's original error with a second one.
        """
        del intent  # the current durable row is authoritative, not a snapshot
        try:
            with self._owned(project_id, principal) as db:
                # A command that already has a ledger entry really committed. The
                # error that reached this point therefore happened *after* the
                # durable outcome - a lost response, not a failed command - so no
                # failure is recorded against it. Writing one would turn a
                # succeeded deployment into a reported failure.
                if self._ledger(db, principal, command_key) is not None:
                    return
                row = db.execute(
                    "SELECT record FROM workflow_deployment_intents "
                    "WHERE principal=? AND key=?",
                    (principal, command_key),
                ).fetchone()
                if row is None:
                    return
                stored: dict[str, Any] = json.loads(row["record"])
                stored["outcome"] = "failed"
                failure: dict[str, Any] = {
                    "reason_code": reason_code,
                    "at_step": _next_step(stored.get("completed_steps") or []),
                    "recorded_at": self.clock(),
                }
                if error is not None:
                    failure["diagnostics"] = [
                        item.as_document() for item in error.diagnostics
                    ]
                    failure["fields"] = {
                        key: value for key, value in error.fields.items()
                    }
                # A failure another attempt of this same command already recorded
                # is preserved: both observations really happened, and neither is
                # more true than the other.
                history = list(stored.get("failure_history") or [])
                previous = stored.get("failure")
                if previous is not None:
                    history.append(previous)
                stored["failure_history"] = history
                stored["failure"] = failure
                db.execute(
                    "UPDATE workflow_deployment_intents SET record=? "
                    "WHERE principal=? AND key=?",
                    (canonical_json(stored), principal, command_key),
                )
        except (WorkflowError, sqlite3.Error):
            pass

    def pending(self, project_id: str, slot: str, *, principal: str) -> list[dict[str, Any]]:
        """Every persisted command for this slot that has not been activated.

        This is the reconciliation read. It reports the *intents* rather than
        only the completed deployments, because an interrupted command is exactly
        the case an operator has to resolve: whether its package was really
        written, what it already completed, and why it stopped. None of these is
        an entry point for a Run.
        """
        slot = self._slot(slot)
        with self._owned(project_id, principal) as db:
            rows = db.execute(
                "SELECT key, record FROM workflow_deployment_intents "
                "WHERE json_extract(record, '$.project_id')=? "
                "AND json_extract(record, '$.slot')=?",
                (project_id, slot),
            ).fetchall()
            activated = {
                row["deployment_id"]
                for row in db.execute(
                    "SELECT deployment_id FROM workflow_deployments "
                    "WHERE project_id=? AND slot=?",
                    (project_id, slot),
                )
            }
        items: list[dict[str, Any]] = []
        for row in rows:
            record = json.loads(row["record"])
            if str(record["deployment_id"]) in activated:
                continue
            items.append(
                {
                    "command_key": row["key"],
                    "deployment_id": record["deployment_id"],
                    "action": record["action"],
                    "project_id": record["project_id"],
                    "conversation_id": record["conversation_id"],
                    "bundle_id": record["frozen"]["bundle_id"],
                    "bundle_revision": record["frozen"]["bundle_revision"],
                    "bundle_digest": record["frozen"]["bundle_digest"],
                    "compiled_digest": record["frozen"]["compiled_digest"],
                    "command_digest": record.get("command_digest"),
                    "expected_active_revision": record["expected_active_revision"],
                    "commanded_by": record["commanded_by"],
                    "commanded_at": record["commanded_at"],
                    "completed_steps": list(record.get("completed_steps") or []),
                    "outcome": record.get("outcome", "unresolved"),
                    "failure": record.get("failure"),
                    "failure_history": list(record.get("failure_history") or []),
                    "package_present": self.loader.package_root(
                        project_id, slot, str(record["deployment_id"])
                    ).is_dir(),
                    "consumable": False,
                }
            )
        return sorted(items, key=lambda item: (item["commanded_at"], item["deployment_id"]))

    def _mutate_intent(
        self,
        project_id: str,
        principal: str,
        command_key: str,
        change: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any] | None:
        """Apply one change to the *current* durable intent, in one transaction.

        Every intent mutation goes through here. The record is re-read inside the
        transaction that writes it, so a change is applied to what is really
        stored rather than to a snapshot a caller has been holding. Two attempts
        of the same command - a resumed one and a later one, or two threads -
        cannot erase each other's evidence, because each one merges into the
        current row instead of overwriting it from a stale copy.

        Returns the updated record, or ``None`` when the command has no intent.
        """
        with self._owned(project_id, principal) as db:
            row = db.execute(
                "SELECT record FROM workflow_deployment_intents WHERE principal=? AND key=?",
                (principal, command_key),
            ).fetchone()
            if row is None:
                return None
            record: dict[str, Any] = json.loads(row["record"])
            change(record)
            db.execute(
                "UPDATE workflow_deployment_intents SET record=? WHERE principal=? AND key=?",
                (canonical_json(record), principal, command_key),
            )
        return record

    def _record_step(
        self,
        project_id: str,
        principal: str,
        command_key: str,
        step: str,
    ) -> None:
        """Record one completed step against the *current* durable intent."""
        changed: list[dict[str, Any]] = []

        def change(record: dict[str, Any]) -> None:
            completed = list(record.get("completed_steps") or [])
            if step not in completed:
                completed.append(step)
            record["completed_steps"] = completed
            changed.append(record)

        self._mutate_intent(project_id, principal, command_key, change)

    def _materialize(
        self, *, project_id: str, slot: str, deployment_id: str, intent: Mapping[str, Any]
    ) -> None:
        """Copy verified source bytes into this deployment's own pending directory.

        The bytes come from the *published revision directory*, re-read through
        ``read_directory`` - which re-hashes every file and refuses an added,
        removed, replaced or linked entry - and then checked one by one against
        the inventory the intent froze. Nothing is copied from the request and
        nothing is copied from a stale in-memory buffer.

        The destination is named after the deployment rather than given a
        temporary name the process would remove on failure: an interrupted copy
        must leave something a later command can find and check. If it already
        exists, its bytes are read back and compared to the same frozen inventory
        and adopted only when they are exactly equal, so a partial or altered
        copy is refused instead of being reused.
        """
        identity = _Identity(intent["frozen"])
        destination = self.loader.package_root(project_id, slot, deployment_id)
        require_trusted_chain(self.root, destination, levels=3)
        source = self._read_source(project_id, identity, intent)
        if destination.exists():
            self._adopt(destination, project_id=project_id, intent=intent, identity=identity)
            return
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = destination.parent / f".staging-{deployment_id}-{uuid.uuid4().hex}"
        try:
            self._write_tree(staging, source, identity)
            os.rename(staging, destination)
        except FileExistsError:
            # Another thread of this process materialised the same intent first;
            # its bytes are the same frozen bytes, so it is adopted rather than
            # reported as a conflict.
            discard_staging_tree(staging)
            self._adopt(destination, project_id=project_id, intent=intent, identity=identity)
            return
        except BaseException:
            discard_staging_tree(staging)
            raise

    def _read_source(
        self, project_id: str, identity: "_Identity", intent: Mapping[str, Any]
    ) -> "bundles.Bundle":
        """Re-read the published revision and require it to be the frozen one."""
        source_root = self.store._bundle_root(project_id, identity.bundle_id)
        revision_root = source_root / f"revision-{identity.bundle_revision}"
        if not revision_root.is_dir():
            raise WorkflowError("WORKFLOW_BUNDLE_NOT_FOUND")
        verified = bundles.read_directory(
            revision_root,
            managed_root=source_root,
            bundle_id=identity.bundle_id,
            revision=identity.bundle_revision,
            project_id=project_id,
            conversation_id=str(intent["conversation_id"]),
        )
        observed = {item.path: item for item in verified.files}
        recorded = {item.path: item for item in identity.files}
        if set(observed) != set(recorded):
            raise WorkflowError(
                "WORKFLOW_SOURCE_CHANGED",
                fields={
                    "recorded_files": sorted(recorded)[:16],
                    "observed_files": sorted(observed)[:16],
                },
            )
        for path, item in observed.items():
            entry = recorded[path]
            if entry.byte_digest != item.digest or entry.byte_length != len(item.raw):
                raise WorkflowError(
                    "WORKFLOW_SOURCE_CHANGED",
                    diagnostics=[
                        located(
                            "WORKFLOW_SOURCE_CHANGED",
                            path,
                            "the confirmed revision no longer holds the confirmed bytes",
                        )
                    ],
                )
        if (
            verified.bundle_digest != identity.bundle_digest
            or verified.manifest_file_digest != identity.manifest_file_digest
        ):
            raise WorkflowError("WORKFLOW_SOURCE_CHANGED")
        return verified

    def _adopt(
        self,
        destination: Path,
        *,
        project_id: str,
        intent: Mapping[str, Any],
        identity: "_Identity",
    ) -> None:
        """Accept an existing package only when it is byte-for-byte the frozen one."""
        verified = self._read_package(
            destination,
            project_id=project_id,
            bundle_id=identity.bundle_id,
            revision=identity.bundle_revision,
            conversation_id=str(intent["conversation_id"]),
        )
        observed = {item.path: item for item in verified.files}
        recorded = {item.path: item for item in identity.files}
        if set(observed) != set(recorded):
            raise WorkflowError(
                "WORKFLOW_UNCOMMITTED_MATERIALIZATION_MISMATCH",
                fields={
                    "recorded_files": sorted(recorded)[:16],
                    "observed_files": sorted(observed)[:16],
                },
            )
        for path, item in observed.items():
            entry = recorded[path]
            if entry.byte_digest != item.digest or entry.byte_length != len(item.raw):
                raise WorkflowError(
                    "WORKFLOW_UNCOMMITTED_MATERIALIZATION_MISMATCH",
                    diagnostics=[
                        located(
                            "WORKFLOW_UNCOMMITTED_MATERIALIZATION_MISMATCH",
                            path,
                            "an interrupted copy holds different bytes; it is left for inspection",
                        )
                    ],
                )
        if verified.bundle_digest != identity.bundle_digest:
            raise WorkflowError("WORKFLOW_BUNDLE_DIGEST_MISMATCH")
        if verified.manifest_file_digest != identity.manifest_file_digest:
            raise WorkflowError("WORKFLOW_MANIFEST_DIGEST_MISMATCH")

    def _write_tree(
        self, staging: Path, source: "bundles.Bundle", identity: "_Identity"
    ) -> None:
        """Write every verified source file plus the manifest, then make it durable."""
        for item in source.files:
            target = staging.joinpath(*item.path.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            _write_bytes(target, item.raw)
        manifest_bytes = bundles.render_manifest(source.manifest)
        if bundles.byte_digest(manifest_bytes) != identity.manifest_file_digest:
            raise WorkflowError("WORKFLOW_MANIFEST_DIGEST_MISMATCH")
        _write_bytes(staging / MANIFEST_PATH, manifest_bytes)
        _fsync_directory(staging)

    def _read_package(
        self,
        package: Path,
        *,
        project_id: str,
        bundle_id: str,
        revision: int,
        conversation_id: str,
    ) -> bundles.Bundle:
        return bundles.read_directory(
            package,
            managed_root=package.parent,
            bundle_id=bundle_id,
            revision=revision,
            project_id=project_id,
            conversation_id=conversation_id,
        )

    # ------------------------------------------------------------ activation

    def _activate(
        self,
        *,
        intent: Mapping[str, Any],
        receipt: LoadReceipt,
        principal: str,
        command_key: str,
        digest: str,
    ) -> tuple[dict[str, Any], bool]:
        """Publish the deployment and move the slot, in one short transaction.

        Every read this needs is taken from the *same* connection the write
        happens on. Nothing here opens a second transaction, and the loader has
        already finished before the write begins: the store shares one SQLite
        file with the rest of the control plane, so a nested ``BEGIN IMMEDIATE``
        would block against itself.
        """
        project_id = str(intent["project_id"])
        slot = str(intent["slot"])
        expected = int(intent["expected_active_revision"])
        with self._owned(project_id, principal) as db:
            replay = self._replay(db, principal, command_key, digest)
            if replay is not None:
                return replay, False
            delivery_kind = self._delivery_kind(db, project_id, intent)
            row = db.execute(
                "SELECT slot_revision FROM workflow_deployment_slots "
                "WHERE project_id=? AND slot=?",
                (project_id, slot),
            ).fetchone()
            current = int(row["slot_revision"]) if row is not None else 0
            if current != expected:
                # A concurrent command already moved the slot, or the caller
                # confirmed against a slot that has since advanced. The active
                # deployment is left exactly as it is.
                raise WorkflowError("WORKFLOW_SLOT_REVISION_CONFLICT", current_revision=current)
            # The steps this command really completed are read from the durable
            # row *inside this transaction*, not from the caller's snapshot: a
            # checkpoint may have been merged by another attempt of the same
            # command after the snapshot was taken, and the published deployment
            # must describe what is durably recorded. No nested transaction is
            # opened, and no whole-row write from a stale copy is performed.
            completed_steps = self._durable_steps(db, principal, command_key)
            record = self._deployment_record(
                intent=intent,
                receipt=receipt,
                principal=principal,
                delivery_kind=delivery_kind,
                completed_steps=completed_steps,
            )
            db.execute(
                "INSERT INTO workflow_deployments VALUES (?,?,?,?)",
                (project_id, slot, str(intent["deployment_id"]), canonical_json(record)),
            )
            db.execute(
                "INSERT INTO workflow_deployment_slots "
                "(project_id, slot, slot_revision, deployment_id, loaded_record) "
                "VALUES (?,?,?,?,?) "
                "ON CONFLICT(project_id, slot) DO UPDATE SET "
                "slot_revision=excluded.slot_revision, deployment_id=excluded.deployment_id, "
                "loaded_record=excluded.loaded_record",
                (
                    project_id,
                    slot,
                    expected + 1,
                    str(intent["deployment_id"]),
                    canonical_json(self._loaded(record, receipt)),
                ),
            )
            result = self._accepted(record, receipt)
            db.execute(
                "INSERT INTO commands VALUES (?,?,?,?)",
                (principal, command_key, digest, canonical_json(result)),
            )
            # The command is now complete. The completion is written against the
            # row as it is *right now*, inside this same transaction, so any
            # earlier diagnosis this command recorded - by this attempt or by
            # another attempt of the same command - is kept and archived rather
            # than overwritten from a snapshot read before the activation began.
            self._complete_intent(db, principal, command_key, intent)
            return result, True

    @staticmethod
    def _durable_steps(
        db: sqlite3.Connection, principal: str, command_key: str
    ) -> list[str]:
        """The completed steps really recorded for one command, as of now.

        Read on the caller's own connection, so it participates in the caller's
        transaction instead of waiting for a second writer that this same process
        is already holding.
        """
        row = db.execute(
            "SELECT record FROM workflow_deployment_intents WHERE principal=? AND key=?",
            (principal, command_key),
        ).fetchone()
        if row is None:
            return []
        return [str(step) for step in (json.loads(row["record"]).get("completed_steps") or [])]

    @staticmethod
    def _complete_intent(
        db: sqlite3.Connection,
        principal: str,
        command_key: str,
        intent: Mapping[str, Any],
    ) -> None:
        """Mark the command completed, archiving rather than erasing failures.

        Nothing is deleted. The record ends with ``outcome: activated``, the
        deployment it named, and a ``failure_history`` holding every earlier
        attempt that failed at a named step, so the durable history answers both
        "what happened in the end" and "what went wrong on the way" without
        either answer contradicting the other.
        """
        row = db.execute(
            "SELECT record FROM workflow_deployment_intents WHERE principal=? AND key=?",
            (principal, command_key),
        ).fetchone()
        if row is None:
            return
        stored: dict[str, Any] = json.loads(row["record"])
        history = list(stored.get("failure_history") or [])
        previous = stored.get("failure")
        if previous is not None:
            history.append(previous)
        stored.pop("failure", None)
        stored["failure_history"] = history
        stored["outcome"] = "activated"
        already = list(stored.get("completed_steps") or [])
        merged = [*already]
        for step in intent.get("completed_steps") or []:
            if step not in merged:
                merged.append(step)
        stored["completed_steps"] = merged
        stored["deployment_id"] = str(intent["deployment_id"])
        db.execute(
            "UPDATE workflow_deployment_intents SET record=? WHERE principal=? AND key=?",
            (canonical_json(stored), principal, command_key),
        )

    @staticmethod
    def _delivery_kind(db: sqlite3.Connection, project_id: str, intent: Mapping[str, Any]) -> str:
        """The published revision's delivery kind, read on this same connection."""
        identity = _Identity(intent["frozen"])
        row = db.execute(
            "SELECT record, digest FROM workflow_bundles "
            "WHERE project_id=? AND id=? AND revision=?",
            (project_id, identity.bundle_id, identity.bundle_revision),
        ).fetchone()
        if row is None:
            raise WorkflowError("WORKFLOW_BUNDLE_NOT_FOUND")
        record = json.loads(row["record"])
        if record.get("digest") != row["digest"]:
            raise WorkflowError("WORKFLOW_RECORD_CHANGED")
        return str(record["delivery_kind"])

    def _deployment_record(
        self,
        *,
        intent: Mapping[str, Any],
        receipt: LoadReceipt,
        principal: str,
        delivery_kind: str,
        completed_steps: Sequence[str],
    ) -> dict[str, Any]:
        """The immutable deployment record, carrying its historical receipt."""
        record: dict[str, Any] = {
            "schema_version": DEPLOYMENT_SCHEMA_VERSION,
            "deployment_id": intent["deployment_id"],
            "project_id": intent["project_id"],
            "conversation_id": intent["conversation_id"],
            "slot": intent["slot"],
            "action": intent["action"],
            "rollback_of": intent.get("rollback_of"),
            "bundle_id": receipt.bundle_id,
            "bundle_revision": receipt.bundle_revision,
            "revision": receipt.bundle_revision,
            "delivery_kind": delivery_kind,
            "bundle_digest": receipt.bundle_digest,
            "manifest_file_digest": receipt.manifest_file_digest,
            "compiled_digest": receipt.compiled_digest,
            "compiler_identity": receipt.compiler_identity,
            "compiler_revision": receipt.compiler_revision,
            "files": [item.as_document() for item in receipt.files],
            "available_execution_kinds": list(receipt.available_execution_kinds),
            "unavailable_execution_kinds": list(receipt.unavailable_execution_kinds),
            "capability_evidence": list(receipt.capability_evidence),
            "expected_active_revision": intent["expected_active_revision"],
            "binding_index": {
                str(reference): dict(resolved)
                for reference, resolved in (
                    _Identity(intent["frozen"]).binding_index
                ).items()
            },
            "commanded_by": principal,
            "commanded_at": intent["commanded_at"],
            # Read from the durable row inside the activation transaction, so the
            # published record describes what is really recorded rather than the
            # caller's snapshot of it.
            "completed_steps": list(completed_steps),
            "completed_at": self.clock(),
            # The historical receipt is what the activating process really read.
            # It is deliberately separate from the *current* loaded/readiness
            # facts, which live in the slot row and are replaced by whichever
            # process last re-loaded the active package.
            "load_receipt": {**receipt.as_document(), "receipt_digest": receipt.digest},
            "loader_identity": LOADER_IDENTITY,
            "model_calls": 0,
            "runs_started": 0,
            "business_steps_executed": 0,
        }
        record["digest"] = content_digest(
            {key: value for key, value in record.items() if key != "digest"}
        )
        return record

    @staticmethod
    def _loaded(record: Mapping[str, Any], receipt: LoadReceipt) -> dict[str, Any]:
        """The *current* loaded/readiness facts, as of one process's real read."""
        return {
            "state": "ready",
            "deployment_id": record["deployment_id"],
            "revision": record["bundle_revision"],
            "compiled_digest": record["compiled_digest"],
            "loaded_by_process": receipt.process_id,
            "loaded_at": receipt.loaded_at,
            "loader_identity": LOADER_IDENTITY,
            "verify_bytes": receipt.verify_bytes,
            "authorization_re_pending": True,
        }

    def _accepted(self, record: Mapping[str, Any], receipt: LoadReceipt) -> dict[str, Any]:
        """The command result: a ready deployment, with its real receipt."""
        return {
            "deployment": record,
            "slot": {
                "project_id": record["project_id"],
                "slot": record["slot"],
                "slot_revision": int(record["expected_active_revision"]) + 1,
                "deployment_id": record["deployment_id"],
                "bundle_id": record["bundle_id"],
                "bundle_revision": record["bundle_revision"],
                "compiled_digest": record["compiled_digest"],
            },
            "readiness": "ready",
            "loaded": self._loaded(record, receipt),
            "previous_active_revision": record["expected_active_revision"],
            "model_calls": 0,
            "runs_started": 0,
            "business_steps_executed": 0,
            "note": (
                "deploy_only loaded and activated the definition; no Run was created "
                "and no business step was executed"
            ),
        }

    # ------------------------------------------------------------- consumer API

    def accept(
        self, project_id: str, slot: str, *, principal: str
    ) -> "_FrozenDefinition":
        """Acquire the loaded and active definition as an immutable handle.

        Acquisition always re-loads the active package *in this process*, so a
        handle can only be obtained from a definition this process has really
        re-read from disk and re-verified. The handle is a frozen copy of the
        compiled template and its identities; a later deployment or rollback
        changes the slot, not the handle a caller already holds.
        """
        slot = self._slot(slot)
        with self._owned(project_id, principal) as db:
            row = db.execute(
                "SELECT slot_revision, deployment_id FROM workflow_deployment_slots "
                "WHERE project_id=? AND slot=?",
                (project_id, slot),
            ).fetchone()
        if row is None or row["deployment_id"] is None:
            raise WorkflowError("WORKFLOW_SLOT_EMPTY")
        deployment = self._require_record(
            project_id, slot, str(row["deployment_id"]), principal
        )
        intent = self._reconstruction(deployment)
        # One verification produces both the receipt and the definition, so the
        # handle cannot describe a second, separately derived compilation that
        # disagrees with the receipt it publishes beside it.
        verified, compiled = self.loader.verified(
            project_id=project_id,
            slot=slot,
            deployment_id=str(row["deployment_id"]),
            expected=intent,
        )
        evidence = self.loader.capability_evidence(compiled)
        receipt = self.loader.receipt_for(
            project_id=project_id,
            slot=slot,
            deployment_id=str(row["deployment_id"]),
            expected=intent,
            verified=verified,
            compiled=compiled,
            capability_evidence=evidence,
            verify_bytes=False,
        )
        return _FrozenDefinition(
            deployment=deployment,
            slot_revision=int(row["slot_revision"]),
            receipt=receipt,
            definition=compiled.as_document(),
        )

    def _reconstruction(self, deployment: Mapping[str, Any]) -> dict[str, Any]:
        """Rebuild the frozen identity a stored deployment was activated from.

        It is rebuilt from the deployment's own immutable record, not re-derived
        from the bundle head, so a later revision published under the same bundle
        identity cannot change what this deployment is verified against.
        """
        return {
            "project_id": deployment["project_id"],
            "conversation_id": deployment["conversation_id"],
            "bundle_id": deployment["bundle_id"],
            "bundle_revision": deployment["bundle_revision"],
            "delivery_kind": deployment["delivery_kind"],
            "bundle_digest": deployment["bundle_digest"],
            "manifest_file_digest": deployment["manifest_file_digest"],
            "compiled_digest": deployment["compiled_digest"],
            "compiler_identity": deployment["compiler_identity"],
            "compiler_revision": deployment["compiler_revision"],
            "files": list(deployment["files"]),
            "binding_index": dict(deployment.get("binding_index") or {}),
        }

    # ----------------------------------------------------------------- readback

    def status(self, project_id: str, slot: str, *, principal: str) -> dict[str, Any]:
        """The slot's current state, obtained by re-loading the real files here.

        The stored slot row is *not* the answer. Each call re-opens the active
        package in this process so a corrupt, missing or replaced file reports
        ``blocked`` rather than an earlier process's ``ready``.
        """
        slot = self._slot(slot)
        with self._owned(project_id, principal) as db:
            row = db.execute(
                "SELECT slot_revision, deployment_id FROM workflow_deployment_slots "
                "WHERE project_id=? AND slot=?",
                (project_id, slot),
            ).fetchone()
        active_revision = int(row["slot_revision"]) if row is not None else 0
        active_deployment = (
            str(row["deployment_id"]) if row is not None and row["deployment_id"] else None
        )
        document: dict[str, Any] = {
            "project_id": project_id,
            "slot": slot,
            "slot_revision": active_revision,
            "active_deployment_id": active_deployment,
            "loader_identity": LOADER_IDENTITY,
            "current": None,
            "readiness": "empty" if active_deployment is None else "unknown",
            "blocked_reason": None,
            "pending": self.pending(project_id, slot, principal=principal),
        }
        if active_deployment is None:
            return document
        deployment = self._require_record(project_id, slot, active_deployment, principal)
        document["active"] = {
            "deployment_id": deployment["deployment_id"],
            "bundle_id": deployment["bundle_id"],
            "bundle_revision": deployment["bundle_revision"],
            "bundle_digest": deployment["bundle_digest"],
            "compiled_digest": deployment["compiled_digest"],
            "compiler_identity": deployment["compiler_identity"],
            "compiler_revision": deployment["compiler_revision"],
            "activated_at": deployment["completed_at"],
        }
        document["receipt"] = deployment["load_receipt"]
        try:
            receipt = self.loader.load(
                project_id=project_id,
                slot=slot,
                deployment_id=active_deployment,
                expected=self._reconstruction(deployment),
                verify_bytes=False,
            )
        except WorkflowError as error:
            document["readiness"] = "blocked"
            document["blocked_reason"] = error.code
            document["diagnostics"] = [item.as_document() for item in error.diagnostics]
            return document
        document["current"] = {
            "state": "ready",
            "loaded_by_process": receipt.process_id,
            "loaded_at": receipt.loaded_at,
            "verify_bytes": receipt.verify_bytes,
            "files": [item.as_document() for item in receipt.files],
            "available_execution_kinds": list(receipt.available_execution_kinds),
            "capability_evidence": list(receipt.capability_evidence),
        }
        document["readiness"] = "ready"
        return document

    def deployment(
        self, project_id: str, slot: str, deployment_id: str, *, principal: str
    ) -> dict[str, Any]:
        """One immutable deployment record, beside the slot's current facts."""
        slot = self._slot(slot)
        deployment = self._require_record(project_id, slot, deployment_id, principal)
        state = self.status(project_id, slot, principal=principal)
        return {
            "deployment": deployment,
            "historical_receipt": deployment["load_receipt"],
            "current": {
                "slot_revision": state["slot_revision"],
                "is_active": state["active_deployment_id"] == deployment_id,
                "readiness": state["readiness"],
                "blocked_reason": state["blocked_reason"],
                "loaded": state["current"],
            },
        }

    def list_deployments(
        self, project_id: str, slot: str, *, principal: str
    ) -> list[dict[str, Any]]:
        slot = self._slot(slot)
        with self._owned(project_id, principal) as db:
            rows = db.execute(
                "SELECT record FROM workflow_deployments WHERE project_id=? AND slot=?",
                (project_id, slot),
            ).fetchall()
        return [self._validate(json.loads(row["record"])) for row in rows]

    def _require_record(
        self, project_id: str, slot: str, deployment_id: str, principal: str
    ) -> dict[str, Any]:
        identity = require_segment(
            deployment_id,
            code="WORKFLOW_DEPLOYMENT_NOT_FOUND",
            detail="a deployment identity is one addressable name",
            location="workflow.yaml#/deployment_id",
        )
        with self._owned(project_id, principal) as db:
            row = db.execute(
                "SELECT record FROM workflow_deployments "
                "WHERE project_id=? AND slot=? AND deployment_id=?",
                (project_id, slot, identity),
            ).fetchone()
        if row is None:
            raise WorkflowError("WORKFLOW_DEPLOYMENT_NOT_FOUND")
        return self._validate(json.loads(row["record"]))

    def _historical_deployment(
        self, project_id: str, deployment_id: str, principal: str
    ) -> dict[str, Any]:
        """Find one durable deployment of this project by identity alone."""
        with self._owned(project_id, principal) as db:
            row = db.execute(
                "SELECT record FROM workflow_deployments "
                "WHERE project_id=? AND deployment_id=?",
                (project_id, deployment_id),
            ).fetchone()
        if row is None:
            raise WorkflowError("WORKFLOW_DEPLOYMENT_NOT_FOUND")
        return self._validate(json.loads(row["record"]))

    @staticmethod
    def _validate(record: dict[str, Any]) -> dict[str, Any]:
        """Refuse a deployment row whose stored digest no longer describes it."""
        declared = record.get("digest")
        if not isinstance(declared, str) or content_digest(
            {key: value for key, value in record.items() if key != "digest"}
        ) != declared:
            raise WorkflowError("WORKFLOW_DEPLOYMENT_RECORD_CHANGED")
        return record

    def _document(
        self, project_id: str, principal: str, result: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Re-derive a replayed result's *current* facts without changing history.

        The stored ledger entry is the original answer and stays exactly as it
        was written. What a replay adds is the present state of the slot, re-read
        from the real files, under a separate key - so a replay after a later
        deployment reports the original result *and* the fact that it is no
        longer the active one, instead of either rewriting history or pretending
        the old deployment is still in force.
        """
        record = result["deployment"]
        state = self.status(project_id, str(record["slot"]), principal=principal)
        return {
            **result,
            "replayed": True,
            "current": {
                "slot_revision": state["slot_revision"],
                "is_active": state["active_deployment_id"] == record["deployment_id"],
                "readiness": state["readiness"],
                "blocked_reason": state["blocked_reason"],
                "loaded": state["current"],
            },
        }
        return result

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _action(value: object) -> str:
        if isinstance(value, str) and value in REFUSED_ACTIONS:
            raise WorkflowError("WORKFLOW_DEPLOY_ACTION_UNSUPPORTED", fields={"action": value})
        if not isinstance(value, str) or value not in ACTIONS:
            raise WorkflowError(
                "WORKFLOW_DEPLOY_ACTION_INVALID",
                diagnostics=[
                    located(
                        "WORKFLOW_DEPLOY_ACTION_INVALID",
                        WORKFLOW_PATH,
                        "an explicit deploy_only or rollback action is required",
                    )
                ],
            )
        return value

    @staticmethod
    def _slot(value: object) -> str:
        return require_segment(
            value,
            code="WORKFLOW_SLOT_INVALID",
            detail="a slot is one addressable name",
            location="workflow.yaml#/slot",
        )

    @staticmethod
    def _expected_active(value: object) -> int:
        """The slot revision the caller confirmed against; 0 means empty."""
        if type(value) is not int or not 0 <= value <= 1_000_000:
            raise WorkflowError(
                "WORKFLOW_EXPECTED_ACTIVE_INVALID",
                diagnostics=[
                    located(
                        "WORKFLOW_EXPECTED_ACTIVE_INVALID",
                        WORKFLOW_PATH,
                        "the expected active slot revision is required, and 0 means empty",
                    )
                ],
            )
        return value

    @staticmethod
    def _preview_id(value: object) -> str:
        if not isinstance(value, str) or len(value) != 64 or not value.isalnum():
            raise WorkflowError(
                "WORKFLOW_PREVIEW_REQUIRED",
                diagnostics=[
                    located(
                        "WORKFLOW_PREVIEW_REQUIRED",
                        WORKFLOW_PATH,
                        "the confirmed preview identity is required",
                    )
                ],
            )
        return value

    @staticmethod
    def _require_fields(payload: object, allowed: frozenset[str]) -> None:
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
                        f"workflow.yaml#/id={str(name)[:48]}",
                        "unknown field",
                    )
                    for name in unknown
                ],
            )


class _Frozen:
    """The exact identity one command binds, derived from real stored files."""

    __slots__ = (
        "project_id",
        "conversation_id",
        "bundle_id",
        "bundle_revision",
        "delivery_kind",
        "bundle_digest",
        "manifest_file_digest",
        "compiled_digest",
        "compiler_identity",
        "compiler_revision",
        "files",
        "binding_index",
        "preview_revision",
        "preview_id",
    )

    def __init__(
        self,
        *,
        project_id: str,
        conversation_id: str,
        bundle_id: str,
        bundle_revision: int,
        delivery_kind: str,
        bundle_digest: str,
        manifest_file_digest: str,
        compiled_digest: str,
        compiler_identity: str,
        compiler_revision: int,
        files: tuple[FileIdentity, ...],
        binding_index: Mapping[str, Mapping[str, Any]],
        preview_revision: int,
        preview_id: str,
    ) -> None:
        self.project_id = project_id
        self.conversation_id = conversation_id
        self.bundle_id = bundle_id
        self.bundle_revision = bundle_revision
        self.delivery_kind = delivery_kind
        self.bundle_digest = bundle_digest
        self.manifest_file_digest = manifest_file_digest
        self.compiled_digest = compiled_digest
        self.compiler_identity = compiler_identity
        self.compiler_revision = compiler_revision
        self.files = files
        self.binding_index = binding_index
        self.preview_revision = preview_revision
        self.preview_id = preview_id

    def as_document(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "conversation_id": self.conversation_id,
            "bundle_id": self.bundle_id,
            "bundle_revision": self.bundle_revision,
            "delivery_kind": self.delivery_kind,
            "bundle_digest": self.bundle_digest,
            "manifest_file_digest": self.manifest_file_digest,
            # The recorded file inventory, not the manifest document. The loader
            # compares an inventory; the manifest bytes are re-derived from the
            # verified source bytes and checked against their own digest when the
            # package is written.
            "files": [item.as_document() for item in self.files],
            "binding_index": {
                reference: dict(resolved) for reference, resolved in self.binding_index.items()
            },
            "compiled_digest": self.compiled_digest,
            "compiler_identity": self.compiler_identity,
            "compiler_revision": self.compiler_revision,
            "preview_revision": self.preview_revision,
            "preview_id": self.preview_id,
        }


class _Identity:
    """The frozen identity as read back from a persisted intent."""

    def __init__(self, frozen: Mapping[str, Any]) -> None:
        self.project_id = str(frozen["project_id"])
        self.conversation_id = str(frozen["conversation_id"])
        self.bundle_id = str(frozen["bundle_id"])
        self.bundle_revision = int(frozen["bundle_revision"])
        self.delivery_kind = str(frozen["delivery_kind"])
        self.bundle_digest = str(frozen["bundle_digest"])
        self.manifest_file_digest = str(frozen["manifest_file_digest"])
        self.compiled_digest = str(frozen["compiled_digest"])
        self.compiler_identity = str(frozen["compiler_identity"])
        self.compiler_revision = int(frozen["compiler_revision"])
        self.files = tuple(
            FileIdentity(
                str(item["path"]), str(item["byte_digest"]), int(item["byte_length"])
            )
            for item in frozen["files"]
        )
        self.binding_index = {
            str(reference): dict(resolved)
            for reference, resolved in (frozen.get("binding_index") or {}).items()
        }
        self.preview_revision = int(frozen.get("preview_revision", 0))
        self.preview_id = str(frozen.get("preview_id", ""))

    def document(self) -> dict[str, Any]:
        """The identity document the loader verifies a package against."""
        return {
            "project_id": self.project_id,
            "conversation_id": self.conversation_id,
            "bundle_id": self.bundle_id,
            "bundle_revision": self.bundle_revision,
            "delivery_kind": self.delivery_kind,
            "bundle_digest": self.bundle_digest,
            "manifest_file_digest": self.manifest_file_digest,
            "files": [item.as_document() for item in self.files],
            "binding_index": self.binding_index,
            "compiled_digest": self.compiled_digest,
            "compiler_identity": self.compiler_identity,
            "compiler_revision": self.compiler_revision,
        }


class _FrozenDefinition:
    """An immutable handle on one loaded, active definition.

    The handle is built once, from a real read of the active package in the
    process that acquired it, and it is never written to again. Every access
    returns a fresh deep copy, so a consumer that mutates what it received -
    including a nested list or mapping - cannot alter the handle, the copy
    another consumer receives, or the digest the handle published.
    """

    def __init__(
        self,
        *,
        deployment: Mapping[str, Any],
        slot_revision: int,
        receipt: LoadReceipt,
        definition: Mapping[str, Any],
    ) -> None:
        self._deployment = _deep(dict(deployment))
        self._slot_revision = slot_revision
        # The receipt's digest is a property, not a field of ``as_document``, so
        # it is added here exactly once and from the same object whose document
        # was read: deriving it separately is how a handle ends up with a digest
        # that describes something other than what it carries.
        self._receipt = {**receipt.as_document(), "receipt_digest": receipt.digest}
        self._definition = _deep(dict(definition))
        self._document = {
            "schema_version": "karajan.workflow-frozen-definition.v1",
            "loader_identity": LOADER_IDENTITY,
            "deployment": {
                "deployment_id": self._deployment["deployment_id"],
                "slot": self._deployment["slot"],
                "action": self._deployment["action"],
                "bundle_id": self._deployment["bundle_id"],
                "bundle_revision": self._deployment["bundle_revision"],
                "bundle_digest": self._deployment["bundle_digest"],
                "manifest_file_digest": self._deployment["manifest_file_digest"],
                "compiled_digest": self._deployment["compiled_digest"],
                "compiler_identity": self._deployment["compiler_identity"],
                "compiler_revision": self._deployment["compiler_revision"],
                "delivery_kind": self._deployment["delivery_kind"],
                "files": self._deployment["files"],
                "available_execution_kinds": self._deployment["available_execution_kinds"],
                "capability_evidence": self._deployment["capability_evidence"],
            },
            "slot": {
                "slot": self._deployment["slot"],
                "slot_revision": slot_revision,
                "active_deployment_id": self._deployment["deployment_id"],
            },
            "acquired_load": {
                "state": "ready",
                "loaded_by_process": self._receipt["process_id"],
                "loaded_at": self._receipt["loaded_at"],
                "verify_bytes": self._receipt["verify_bytes"],
                "loader_identity": self._receipt["loader_identity"],
                "receipt_digest": self._receipt["receipt_digest"],
            },
            "definition": self._definition,
            "execution": {
                "deploy_only": True,
                "runs_started": 0,
                "business_steps_executed": 0,
                "note": "a definition handle for #178; acquiring it executes nothing",
            },
        }
        self._digest = content_digest(self._document)

    @property
    def digest(self) -> str:
        """The identity of this handle, fixed when it was acquired."""
        return self._digest

    def as_document(self) -> dict[str, Any]:
        """A fresh deep copy; mutating it cannot change this handle."""
        return {**_deep(self._document), "definition_digest": self._digest}


def _deep(value: Any) -> Any:
    """A deep copy through canonical JSON, so no container is shared."""
    return json.loads(canonical_json(value))


def _next_step(completed: list[str]) -> str:
    """The step a failure occurred at, derived from what really completed."""
    for step in STEPS:
        if step not in completed:
            return step
    return "activate"


#: Exception class names that may appear in a recorded reason code. The record is
#: a durable, caller-visible document, so only this fixed vocabulary is allowed
#: into it: an arbitrary class name from a third-party exception is not.
_RECORDABLE_FAILURES = frozenset(
    {
        "OSError",
        "IOError",
        "PermissionError",
        "FileNotFoundError",
        "FileExistsError",
        "NotADirectoryError",
        "IsADirectoryError",
        "UnicodeError",
        "OperationalError",
        "IntegrityError",
        "DatabaseError",
    }
)


def _step_failure_code(error: BaseException) -> str:
    """A stable, content-free reason code for an ordinary step failure.

    The class name is used *only* when it is one this module recognises, so a
    record can never carry an unexpected third-party identifier. Nothing from the
    exception's message, arguments or traceback is used, so no host path, project
    identifier or credential can reach the durable record or the response.
    """
    name = type(error).__name__
    if name not in _RECORDABLE_FAILURES:
        name = "IOError" if isinstance(error, OSError) else "UnexpectedStepFailure"
    return f"WORKFLOW_STEP_FAILED_{name}".upper()


def _write_bytes(path: Path, data: bytes) -> None:
    """Write one file completely, then make it durable before it is referenced."""
    with open(path, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


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
