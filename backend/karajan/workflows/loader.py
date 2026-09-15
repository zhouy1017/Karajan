"""The trusted loader: reopen real pending files and recompile them here.

A deployment is not an ``active = true`` flag. Between the durable intent and the
slot switch there is a real read: this module opens the materialised package
directory, verifies that its inventory and per-file identities are exactly the
ones the intent froze, recompiles the bytes with the *module-private* trusted
registry, and refuses to report readiness unless every referenced execution kind
resolves there to a real, callable adapter.

Three properties are structural rather than documented:

* A request cannot inject a loader. The registry is module state, the compiler is
  the same pure function the publish path used, and nothing here imports, execs
  or evaluates anything from the package. A package can therefore only *name*
  kinds that already exist in this build.
* Readiness is a capability fact, never a template fact. A template that names
  ``agent_task@1`` compiles into a reviewable document, but its kind has no
  adapter in this build, so this loader can never report it ready — no matter
  what the request says.
* The loader never calls the adapter. Readiness is "the capability is present and
  callable here"; invoking it is execution, which is #178's authority, not a
  deployment's. A deploy-only confirmation runs no business step.

``verify_bytes`` selects how much is re-read. A first load re-hashes the stored
bytes. A re-load of an already verified package (a fresh process, a re-read of
the active slot, a rollback target) treats the bytes as immutable and verifies
the stronger thing instead: the complete (path, byte_digest, byte_length)
inventory, the recorded bundle and manifest identities and the recompiled
digest. Both are reported honestly, so no caller can claim a byte-level re-read
it did not perform.
"""

import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import bundle as bundles
from .compiler import CompiledWorkflow
from .digests import content_digest
from .errors import WorkflowError, located
from .layout import WORKFLOW_PATH
from .registry import kind_for

LOADER_IDENTITY = "karajan.workflow-loader.v1"

#: The directory a deployment's package is materialised into, below its own
#: immutable deployment directory. The name is fixed here so the loader and the
#: deployment store cannot disagree about where the package is.
PACKAGE_DIRECTORY = "pending"

#: Names the materialising writer must never report as verified content.
_NEVER_INVENTORIED = frozenset({".staging"})


@dataclass(frozen=True, slots=True)
class FileIdentity:
    """One package file's path and stored-bytes identity."""

    path: str
    byte_digest: str
    byte_length: int

    def as_document(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "byte_digest": self.byte_digest,
            "byte_length": self.byte_length,
        }


@dataclass(frozen=True, slots=True)
class LoadReceipt:
    """What the loader actually observed, in the process that observed it.

    The receipt is the immutable historical fact. It records the process that
    performed the load, because "this package is loaded" is a statement about a
    running process and not about the files alone: a new process must load again
    rather than inherit this receipt.
    """

    project_id: str
    slot: str
    deployment_id: str
    bundle_id: str
    bundle_revision: int
    bundle_digest: str
    manifest_file_digest: str
    compiled_digest: str
    compiler_identity: str
    compiler_revision: int
    files: tuple[FileIdentity, ...]
    available_execution_kinds: tuple[str, ...]
    unavailable_execution_kinds: tuple[str, ...]
    capability_evidence: tuple[str, ...]
    verify_bytes: bool
    loaded_at: float
    process_id: int
    loader_identity: str = LOADER_IDENTITY

    def as_document(self) -> dict[str, Any]:
        """The immutable receipt, without its own digest field."""
        return {
            "schema_version": "karajan.workflow-load-receipt.v1",
            "loader_identity": self.loader_identity,
            "project_id": self.project_id,
            "slot": self.slot,
            "deployment_id": self.deployment_id,
            "bundle_id": self.bundle_id,
            "bundle_revision": self.bundle_revision,
            "bundle_digest": self.bundle_digest,
            "manifest_file_digest": self.manifest_file_digest,
            "compiled_digest": self.compiled_digest,
            "compiler_identity": self.compiler_identity,
            "compiler_revision": self.compiler_revision,
            "files": [item.as_document() for item in self.files],
            "available_execution_kinds": list(self.available_execution_kinds),
            "unavailable_execution_kinds": list(self.unavailable_execution_kinds),
            "capability_evidence": list(self.capability_evidence),
            "verify_bytes": self.verify_bytes,
            "loaded_at": self.loaded_at,
            "process_id": self.process_id,
        }

    @property
    def digest(self) -> str:
        return content_digest(self.as_document())


class WorkflowLoader:
    """Reopen a materialised package and recompile it with the trusted registry.

    The loader holds no cache. Every call reads the files again in the calling
    process, which is what makes a fresh service process a real re-verification
    rather than a lookup of an earlier answer.
    """

    def __init__(
        self,
        root: Path,
        *,
        clock: Callable[[], float] = time.time,
        process_id: Callable[[], int] = os.getpid,
    ) -> None:
        self.root = Path(root)
        self.clock = clock
        self.process_id = process_id

    def package_root(self, project_id: str, slot: str, deployment_id: str) -> Path:
        """The one managed package directory for a deployment; never caller-named."""
        return self.root / project_id / slot / deployment_id / PACKAGE_DIRECTORY

    def load(
        self,
        *,
        project_id: str,
        slot: str,
        deployment_id: str,
        expected: Mapping[str, Any],
        verify_bytes: bool,
    ) -> LoadReceipt:
        """Read one package and prove it is the one the intent froze.

        ``expected`` is the identity the durable intent recorded: the bundle and
        manifest digests, the compiled digest and compiler revision, the exact
        file inventory and the binding identities the revision was published
        against. Every field is compared; a package that differs in any of them
        is refused rather than loaded under the wrong identity.
        """
        package = self.package_root(project_id, slot, deployment_id)
        verified, compiled = self._verify(
            package,
            project_id=project_id,
            bundle_id=str(expected["bundle_id"]),
            revision=int(expected["bundle_revision"]),
            expected=expected,
        )
        evidence = self.capability_evidence(compiled)
        return self.receipt_for(
            project_id=project_id,
            slot=slot,
            deployment_id=deployment_id,
            expected=expected,
            verified=verified,
            compiled=compiled,
            capability_evidence=evidence,
            verify_bytes=verify_bytes,
        )

    def capability_evidence(self, compiled: CompiledWorkflow) -> tuple[str, ...]:
        """Resolve every referenced kind, or refuse to call the package ready."""
        return self._require_capabilities(compiled)

    def receipt_for(
        self,
        *,
        project_id: str,
        slot: str,
        deployment_id: str,
        expected: Mapping[str, Any],
        verified: bundles.Bundle,
        compiled: CompiledWorkflow,
        capability_evidence: tuple[str, ...],
        verify_bytes: bool,
    ) -> LoadReceipt:
        """Describe one already verified read, without reading the files again.

        A caller that needs both the receipt and the definition uses this after a
        single :meth:`verified` call, so the two describe the same read.
        """
        return LoadReceipt(
            project_id=project_id,
            slot=slot,
            deployment_id=deployment_id,
            bundle_id=str(expected["bundle_id"]),
            bundle_revision=int(expected["bundle_revision"]),
            bundle_digest=verified.bundle_digest,
            manifest_file_digest=verified.manifest_file_digest,
            compiled_digest=compiled.compiled_digest,
            compiler_identity=compiled.compiler_identity,
            compiler_revision=compiled.compiler_revision,
            files=tuple(
                FileIdentity(item.path, item.digest, len(item.raw)) for item in verified.files
            ),
            available_execution_kinds=tuple(compiled.available_execution_kinds),
            unavailable_execution_kinds=tuple(compiled.unavailable_execution_kinds),
            capability_evidence=capability_evidence,
            verify_bytes=verify_bytes,
            loaded_at=self.clock(),
            process_id=self.process_id(),
        )

    def verified(
        self,
        *,
        project_id: str,
        slot: str,
        deployment_id: str,
        expected: Mapping[str, Any],
    ) -> tuple[bundles.Bundle, CompiledWorkflow]:
        """Both verified results of one read, so a caller repeats no check.

        A consumer handle needs the compiled definition, and it needs to be the
        *same* verified result the receipt describes. Returning both from one
        verification is what makes that true: the definition cannot be a second,
        separately derived compilation that quietly disagrees with the receipt.
        """
        package = self.package_root(project_id, slot, deployment_id)
        return self._verify(
            package,
            project_id=project_id,
            bundle_id=str(expected["bundle_id"]),
            revision=int(expected["bundle_revision"]),
            expected=expected,
        )

    def _verify(
        self,
        package: Path,
        *,
        project_id: str,
        bundle_id: str,
        revision: int,
        expected: Mapping[str, Any],
    ) -> tuple[bundles.Bundle, CompiledWorkflow]:
        """Read, inventory-check, recompile and identity-check one package."""
        verified = self._read_package(
            package,
            managed=package.parent,
            project_id=project_id,
            bundle_id=bundle_id,
            revision=revision,
            expected=expected,
        )
        if verified.bundle_digest != expected["bundle_digest"]:
            raise WorkflowError("WORKFLOW_BUNDLE_DIGEST_MISMATCH")
        if verified.manifest_file_digest != expected["manifest_file_digest"]:
            raise WorkflowError("WORKFLOW_MANIFEST_DIGEST_MISMATCH")
        compiled = bundles.compile_bundle(
            verified, binding_index=expected.get("binding_index") or {}
        )
        # The compiler identity and revision are compared *independently* of the
        # compiled digest. The compiled document does not itself carry the
        # compiler revision, so a compiler revision change can leave the digest
        # unchanged; comparing the digest alone would then accept a template
        # produced by a compiler this build is not the one recorded for.
        if compiled.compiler_identity != str(expected["compiler_identity"]):
            raise WorkflowError(
                "WORKFLOW_COMPILER_REVISION_MISMATCH",
                fields={
                    "recorded_compiler_identity": str(expected["compiler_identity"]),
                    "current_compiler_identity": compiled.compiler_identity,
                },
            )
        if int(compiled.compiler_revision) != int(expected["compiler_revision"]):
            raise WorkflowError(
                "WORKFLOW_COMPILER_REVISION_MISMATCH",
                fields={
                    "recorded_compiler_revision": int(expected["compiler_revision"]),
                    "current_compiler_revision": int(compiled.compiler_revision),
                },
            )
        if compiled.compiled_digest != expected["compiled_digest"]:
            # Same compiler, different result: the bytes on disk no longer
            # compile to the template that was published under this identity.
            raise WorkflowError(
                "WORKFLOW_COMPILE_MISMATCH",
                fields={"stored_compiled_digest": str(expected["compiled_digest"])},
            )
        return verified, compiled

    # ------------------------------------------------------------------ internals

    def _read_package(
        self,
        package: Path,
        *,
        managed: Path,
        project_id: str,
        bundle_id: str,
        revision: int,
        expected: Mapping[str, Any],
    ) -> bundles.Bundle:
        """Read one package directory and check its whole recorded inventory.

        ``read_directory`` compares the declared inventory with what is really on
        disk and re-hashes every file it reads, so an added, removed, replaced or
        linked entry is a refusal. The recorded identities are then compared field
        by field against what the intent froze: the same path, the same digest and
        the same length for every single file, with no extra and no missing entry.
        """
        if not package.is_dir():
            raise WorkflowError(
                "WORKFLOW_PACKAGE_MISSING",
                diagnostics=[
                    located(
                        "WORKFLOW_PACKAGE_MISSING",
                        WORKFLOW_PATH,
                        "the materialised package is not present",
                    )
                ],
            )
        verified = bundles.read_directory(
            package,
            managed_root=managed,
            bundle_id=bundle_id,
            revision=revision,
            project_id=project_id,
            conversation_id=str(expected.get("conversation_id") or ""),
        )
        recorded = {
            str(item["path"]): item
            for item in expected.get("files") or ()
            if isinstance(item, Mapping)
        }
        observed = {item.path: item for item in verified.files}
        if set(recorded) != set(observed):
            raise WorkflowError(
                "WORKFLOW_FILE_INVENTORY_MISMATCH",
                fields={
                    "recorded_files": sorted(recorded)[:16],
                    "observed_files": sorted(observed)[:16],
                },
            )
        for path, item in observed.items():
            entry = recorded[path]
            if entry.get("byte_digest") != item.digest or int(entry.get("byte_length", -1)) != len(
                item.raw
            ):
                raise WorkflowError(
                    "WORKFLOW_FILE_DIGEST_MISMATCH",
                    diagnostics=[
                        located(
                            "WORKFLOW_FILE_DIGEST_MISMATCH",
                            path,
                            "stored bytes no longer match the recorded identity",
                        )
                    ],
                )
        return verified

    def _require_capabilities(self, compiled: CompiledWorkflow) -> tuple[str, ...]:
        """Resolve every referenced kind through the trusted registry.

        Positive readiness requires that each referenced revision resolves to a
        kind that this build really has an adapter for, that the adapter is a
        callable, and that the kind itself reports itself available. A kind that
        is registered but unimplemented — every model kind in this slice — leaves
        this method raising, so a template cannot be reported ready because a
        request, a manifest or a fixture said so.
        """
        referenced = sorted(
            {step.execution_kind_ref for step in compiled.steps}
            | {
                str(name)
                for expansion in compiled.expansions
                for name in expansion.get("execution_kinds") or ()
            }
        )
        unavailable: list[str] = []
        evidence: list[str] = []
        for reference in referenced:
            kind = kind_for(reference, location="workflow.yaml#/steps")
            if kind.adapter is None or not callable(kind.adapter) or not kind.available:
                unavailable.append(reference)
                continue
            evidence.extend(kind.capability_evidence_refs)
        if unavailable:
            raise WorkflowError(
                "WORKFLOW_CAPABILITY_UNAVAILABLE",
                fields={"unavailable_execution_kinds": unavailable[:16]},
                diagnostics=[
                    located(
                        "WORKFLOW_CAPABILITY_UNAVAILABLE",
                        WORKFLOW_PATH,
                        f"{reference} is registered without a callable adapter in this build",
                    )
                    for reference in unavailable[:16]
                ],
            )
        return tuple(sorted(set(evidence)))


def inventory_of(bundle: bundles.Bundle) -> list[dict[str, Any]]:
    """The recorded file inventory of one verified bundle, in a stable order."""
    return [
        {
            "path": item.path,
            "byte_digest": item.digest,
            "byte_length": len(item.raw),
        }
        for item in sorted(bundle.files, key=lambda item: item.path)
    ]


__all__ = [
    "LOADER_IDENTITY",
    "PACKAGE_DIRECTORY",
    "FileIdentity",
    "LoadReceipt",
    "WorkflowLoader",
    "inventory_of",
]
