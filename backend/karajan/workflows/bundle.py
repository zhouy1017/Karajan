"""Read one real bundle directory into bytes, and render its manifest.

A bundle is a directory of real files. Everything downstream — the compiled
digest, the preview, the diagram and the diff — is derived from those bytes, so
a restart re-reads and re-hashes the files instead of trusting a stored preview.

Three identities are computed and kept separate:

* each declared file's byte digest, over the exact stored bytes;
* ``bundle_digest``, over the canonical manifest content, which lists the sorted
  per-file digests and the fixed references. The digest field is excluded from
  its own computation, so no published digest is self-referential, and the
  manifest never lists itself: its own byte hash is reported as
  ``manifest_file_digest`` beside the record rather than inside the document
  whose bytes it describes;
* ``compiled_digest``, produced by the compiler over the parameterised template
  and never over a concrete Run input.

Reading a directory is closed-world: the declared inventory must equal the files
actually present, so an extra file, an undeclared link or a planted reparse
point is a rejection rather than something silently tolerated beside a verified
revision.
"""

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import yamls
from .compiler import (
    MANIFEST_SCHEMA_VERSION,
    SCHEMA_VERSION,
    CompiledWorkflow,
    compile_workflow,
)
from .digests import byte_digest, canonical_json, content_digest
from .errors import WorkflowError, located
from .layout import (
    MANIFEST_PATH,
    MAXIMUM_FILES,
    REQUIRED_PATHS,
    ROLES_PREFIX,
    TEMPLATES_PREFIX,
    WORKFLOW_PATH,
    BundlePaths,
    is_reparse_point,
)

MAXIMUM_FILE_BYTES = 1_048_576
MAXIMUM_TOTAL_BYTES = 8_388_608


def _encode_content(content: str, relative: str) -> bytes:
    """Encode declared text, refusing text that is not storable UTF-8.

    JSON can carry an unpaired surrogate (``"\\ud800"``), which Python accepts as
    a ``str`` but cannot encode as UTF-8. Left unhandled it would raise an
    uncaught ``UnicodeEncodeError`` — an internal error rather than a rejection —
    so it is refused here, before anything is written, with a located reason that
    does not echo the offending value.
    """
    try:
        raw = content.encode("utf-8")
    except UnicodeEncodeError:
        raise WorkflowError(
            "WORKFLOW_CONTENT_NOT_ENCODABLE",
            diagnostics=[
                located(
                    "WORKFLOW_CONTENT_NOT_ENCODABLE",
                    relative,
                    "content is not storable utf-8 text",
                )
            ],
        ) from None
    if len(raw) > MAXIMUM_FILE_BYTES:
        raise WorkflowError("WORKFLOW_CONTENT_TOO_LARGE")
    return raw

#: Manifest fields this schema version accepts. An unknown field is refused, so
#: a bundle cannot carry an instruction the compiler never reads.
MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "bundle_id",
        "revision",
        "files",
        "references",
        "delivery_kind",
        "bundle_digest",
    }
)

REFERENCE_FIELDS = frozenset(
    {
        "workflow_id",
        "workflow_revision",
        "input_contract_ref",
        "roles",
        "execution_kinds",
        "contracts",
    }
)

ENTRY_FIELDS = frozenset({"path", "byte_digest", "byte_length"})

#: Re-exported so a caller can hash bytes through this module's public surface
#: without depending on the private digest module.
__all__ = [
    "Bundle",
    "BundleFile",
    "adopt_manifest",
    "byte_digest",
    "bundle_digest",
    "compile_bundle",
    "declared_inventory",
    "manifest_document",
    "prepare_files",
    "read_directory",
    "render_manifest",
    "validate_manifest",
]


@dataclass(frozen=True, slots=True)
class BundleFile:
    path: str
    raw: bytes
    digest: str

    @property
    def text(self) -> str:
        return self.raw.decode("utf-8")


@dataclass(frozen=True, slots=True)
class Bundle:
    """One verified bundle revision: real bytes plus their separate identities."""

    bundle_id: str
    revision: int
    project_id: str
    conversation_id: str
    manifest: Mapping[str, Any]
    files: tuple[BundleFile, ...]
    bundle_digest: str
    manifest_file_digest: str

    def file(self, path: str) -> BundleFile:
        for item in self.files:
            if item.path == path:
                return item
        raise WorkflowError(
            "WORKFLOW_FILE_MISSING",
            diagnostics=[located("WORKFLOW_FILE_MISSING", path, "not part of this bundle")],
        )

    def documents(self) -> dict[str, Any]:
        """Parse every declared YAML document into plain Python containers."""
        return {
            item.path: yamls.load(item.text, file=item.path) for item in self.files
        }

    @property
    def byte_digests(self) -> dict[str, str]:
        return {item.path: item.digest for item in self.files}


def manifest_document(
    *,
    bundle_id: str,
    revision: int,
    files: Mapping[str, bytes],
    references: Mapping[str, Any],
    delivery_kind: str,
) -> dict[str, Any]:
    """Render the canonical manifest for a set of concrete file bytes.

    The manifest is derived, never supplied: a caller declares content and the
    controller writes the manifest describing exactly what it stored, so a
    declared digest can never disagree with the bytes beside it. ``files``
    excludes the manifest itself, which is what keeps the document free of a
    self-reference.
    """
    listed = [
        {
            "path": path,
            "byte_digest": byte_digest(files[path]),
            "byte_length": len(files[path]),
        }
        for path in sorted(files)
        if path != MANIFEST_PATH
    ]
    body: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "bundle_id": bundle_id,
        "revision": revision,
        "delivery_kind": delivery_kind,
        "files": listed,
        "references": dict(references),
    }
    body["bundle_digest"] = bundle_digest(body)
    return body


def bundle_digest(manifest: Mapping[str, Any]) -> str:
    """Digest the manifest content, excluding its own digest field."""
    return content_digest(
        {key: value for key, value in manifest.items() if key != "bundle_digest"}
    )


def render_manifest(manifest: Mapping[str, Any]) -> bytes:
    """The exact bytes written to ``manifest.json``: canonical, one trailing LF."""
    return (canonical_json(manifest) + "\n").encode("utf-8")


def adopt_manifest(
    manifest: Mapping[str, Any],
    *,
    bundle_id: str,
    revision: int,
    files: Mapping[str, bytes],
    delivery_kind: str,
) -> None:
    """Check that one manifest describes the bytes it will be written beside.

    The store writes the files first and then the manifest, so the manifest must
    be checked against the bytes actually being written rather than rebuilt from
    them: rebuilding would hash the manifest into its own input once its bytes
    are part of ``files``, which is exactly the self-reference this format
    avoids. Every declared digest and length is compared here instead.
    """
    if manifest.get("bundle_id") != bundle_id or manifest.get("revision") != revision:
        raise WorkflowError(
            "WORKFLOW_BUNDLE_IDENTITY_MISMATCH",
            diagnostics=[
                located(
                    "WORKFLOW_BUNDLE_IDENTITY_MISMATCH",
                    MANIFEST_PATH,
                    "the manifest does not describe the requested revision",
                )
            ],
        )
    if manifest.get("delivery_kind") != delivery_kind:
        raise WorkflowError(
            "WORKFLOW_DELIVERY_KIND_INVALID",
            diagnostics=[
                located(
                    "WORKFLOW_DELIVERY_KIND_INVALID",
                    f"{MANIFEST_PATH}#/delivery_kind",
                    "the manifest and the request disagree on the delivery kind",
                )
            ],
        )
    declared = declared_inventory(manifest)
    supplied = {path: raw for path, raw in files.items() if path != MANIFEST_PATH}
    if set(declared) != set(supplied):
        raise WorkflowError(
            "WORKFLOW_FILE_INVENTORY_MISMATCH",
            diagnostics=[
                located(
                    "WORKFLOW_FILE_INVENTORY_MISMATCH",
                    MANIFEST_PATH,
                    "the manifest inventory does not match the supplied files",
                )
            ],
        )
    for path, entry in declared.items():
        raw = supplied[path]
        if entry["byte_digest"] != byte_digest(raw) or entry["byte_length"] != len(raw):
            raise WorkflowError(
                "WORKFLOW_FILE_DIGEST_MISMATCH",
                diagnostics=[
                    located(
                        "WORKFLOW_FILE_DIGEST_MISMATCH",
                        path,
                        "a declared digest does not describe the supplied bytes",
                    )
                ],
            )
    if manifest.get("bundle_digest") != bundle_digest(manifest):
        raise WorkflowError(
            "WORKFLOW_BUNDLE_DIGEST_MISMATCH",
            diagnostics=[
                located(
                    "WORKFLOW_BUNDLE_DIGEST_MISMATCH",
                    f"{MANIFEST_PATH}#/bundle_digest",
                    "the recorded digest does not describe this manifest",
                )
            ],
        )


def validate_manifest(document: Any) -> dict[str, Any]:
    """Validate a manifest read back from disk or supplied by a trusted caller."""
    if not isinstance(document, dict):
        raise WorkflowError(
            "WORKFLOW_MANIFEST_INVALID",
            diagnostics=[
                located("WORKFLOW_MANIFEST_INVALID", MANIFEST_PATH, "manifest must be an object")
            ],
        )
    unknown = sorted(set(document) - MANIFEST_FIELDS)
    if unknown:
        raise WorkflowError(
            "WORKFLOW_MANIFEST_INVALID",
            diagnostics=[
                located(
                    "WORKFLOW_MANIFEST_INVALID",
                    f"{MANIFEST_PATH}#/id={name}",
                    "unknown manifest field",
                )
                for name in unknown
            ],
        )
    missing = sorted(
        {"schema_version", "bundle_id", "revision", "files", "references"} - set(document)
    )
    if missing:
        raise WorkflowError(
            "WORKFLOW_MANIFEST_INCOMPLETE",
            diagnostics=[
                located("WORKFLOW_MANIFEST_INCOMPLETE", MANIFEST_PATH, f"missing field {name}")
                for name in missing
            ],
        )
    if document["schema_version"] != MANIFEST_SCHEMA_VERSION:
        raise WorkflowError(
            "WORKFLOW_MANIFEST_SCHEMA_UNSUPPORTED",
            diagnostics=[
                located(
                    "WORKFLOW_MANIFEST_SCHEMA_UNSUPPORTED",
                    f"{MANIFEST_PATH}#/schema_version",
                    "unsupported manifest schema version",
                )
            ],
        )
    if type(document["revision"]) is not int or not 1 <= document["revision"] <= 1_000_000:
        raise WorkflowError(
            "WORKFLOW_MANIFEST_INVALID",
            diagnostics=[
                located(
                    "WORKFLOW_MANIFEST_INVALID", f"{MANIFEST_PATH}#/revision", "invalid revision"
                )
            ],
        )
    BundlePaths.validate(document["bundle_id"])
    if document.get("delivery_kind") not in {"report", "patch", "pr"}:
        raise WorkflowError(
            "WORKFLOW_MANIFEST_INVALID",
            diagnostics=[
                located(
                    "WORKFLOW_MANIFEST_INVALID",
                    f"{MANIFEST_PATH}#/delivery_kind",
                    "declared delivery kind is invalid",
                )
            ],
        )
    if not isinstance(document["files"], list) or not document["files"]:
        raise WorkflowError(
            "WORKFLOW_MANIFEST_INCOMPLETE",
            diagnostics=[
                located(
                    "WORKFLOW_MANIFEST_INCOMPLETE", f"{MANIFEST_PATH}#/files", "files are required"
                )
            ],
        )
    references = document["references"]
    if not isinstance(references, dict) or set(references) != REFERENCE_FIELDS:
        raise WorkflowError(
            "WORKFLOW_MANIFEST_INVALID",
            diagnostics=[
                located(
                    "WORKFLOW_MANIFEST_INVALID",
                    f"{MANIFEST_PATH}#/references",
                    "references must declare exactly the known keys",
                )
            ],
        )
    if document.get("bundle_digest") is not None and document["bundle_digest"] != bundle_digest(
        document
    ):
        raise WorkflowError(
            "WORKFLOW_BUNDLE_DIGEST_MISMATCH",
            diagnostics=[
                located(
                    "WORKFLOW_BUNDLE_DIGEST_MISMATCH",
                    f"{MANIFEST_PATH}#/bundle_digest",
                    "the recorded digest does not describe this manifest",
                )
            ],
        )
    return dict(document)


def declared_inventory(manifest: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """Validate and index the manifest's declared files.

    ``manifest.json`` is refused inside the inventory: it describes the document
    whose bytes it would have to hash, which cannot be satisfied. Its own hash is
    reported separately as ``manifest_file_digest``.
    """
    recorded: dict[str, Mapping[str, Any]] = {}
    seen: dict[str, str] = {}
    for entry in manifest["files"]:
        if not isinstance(entry, Mapping) or set(entry) != ENTRY_FIELDS:
            raise WorkflowError(
                "WORKFLOW_MANIFEST_INVALID",
                diagnostics=[
                    located(
                        "WORKFLOW_MANIFEST_INVALID",
                        f"{MANIFEST_PATH}#/files",
                        "each file entry declares path, byte_digest and byte_length",
                    )
                ],
            )
        relative = BundlePaths.validate(entry["path"])
        BundlePaths.require_allowed(relative)
        if relative == MANIFEST_PATH:
            raise WorkflowError(
                "WORKFLOW_MANIFEST_SELF_REFERENCE",
                diagnostics=[
                    located(
                        "WORKFLOW_MANIFEST_SELF_REFERENCE",
                        MANIFEST_PATH,
                        "the manifest does not inventory its own bytes",
                    )
                ],
            )
        if relative in recorded:
            raise WorkflowError(
                "WORKFLOW_PATH_DUPLICATE",
                diagnostics=[
                    located("WORKFLOW_PATH_DUPLICATE", relative, "declared twice in the manifest")
                ],
            )
        BundlePaths.claim(seen, relative)
        if not isinstance(entry["byte_digest"], str) or len(entry["byte_digest"]) != 64:
            raise WorkflowError(
                "WORKFLOW_MANIFEST_INVALID",
                diagnostics=[
                    located(
                        "WORKFLOW_MANIFEST_INVALID",
                        f"{MANIFEST_PATH}#/files/id={relative}",
                        "byte_digest is not a sha256 digest",
                    )
                ],
            )
        if type(entry["byte_length"]) is not int or entry["byte_length"] < 0:
            raise WorkflowError(
                "WORKFLOW_MANIFEST_INVALID",
                diagnostics=[
                    located(
                        "WORKFLOW_MANIFEST_INVALID",
                        f"{MANIFEST_PATH}#/files/id={relative}",
                        "byte_length is not a length",
                    )
                ],
            )
        recorded[relative] = entry
    for required in REQUIRED_PATHS:
        if required != MANIFEST_PATH and required not in recorded:
            raise WorkflowError(
                "WORKFLOW_FILE_MISSING",
                diagnostics=[
                    located("WORKFLOW_FILE_MISSING", required, "a required file is not declared")
                ],
            )
    return recorded


def _observed_inventory(root: Path) -> set[str]:
    """Every file actually present below the revision root, relative and POSIX."""
    observed: set[str] = set()
    stack: list[tuple[Path, str]] = [(root, "")]
    while stack:
        directory, prefix = stack.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError:
            raise WorkflowError("WORKFLOW_DIRECTORY_UNREADABLE") from None
        for entry in entries:
            relative = f"{prefix}{entry.name}"
            if is_reparse_point(Path(entry.path)):
                # A link inside a verified revision is never expected: the bundle
                # is written by the controller from declared bytes only.
                raise WorkflowError(
                    "WORKFLOW_PATH_LINK_ESCAPE",
                    diagnostics=[
                        located(
                            "WORKFLOW_PATH_LINK_ESCAPE",
                            relative,
                            "a verified revision contains no links",
                        )
                    ],
                )
            if entry.is_dir(follow_symlinks=False):
                stack.append((Path(entry.path), f"{relative}/"))
            elif entry.is_file(follow_symlinks=False):
                observed.add(relative)
            else:
                raise WorkflowError("WORKFLOW_UNDECLARED_ENTRY")
    return observed


def read_directory(
    root: Path,
    *,
    managed_root: Path,
    bundle_id: str,
    revision: int,
    project_id: str,
    conversation_id: str,
) -> Bundle:
    """Read one materialised bundle directory and verify it against its manifest.

    This is the restart path: it reads real bytes from disk, re-hashes every
    declared file, refuses any undeclared entry, and rejects the revision when a
    digest, a length or the layout disagrees with the manifest published
    alongside it.

    ``managed_root`` is the top of this bundle's own managed subtree. Every
    component from there down to ``root`` is inspected for a reparse point, so a
    junction planted on an ancestor directory cannot redirect the read: checking
    only the leaf would miss it.
    """
    _reject_reparse_chain(managed_root, root)
    manifest_raw = _read_bytes(BundlePaths.resolve(root, MANIFEST_PATH), MANIFEST_PATH)
    manifest = validate_manifest(_parse_json(manifest_raw, MANIFEST_PATH))
    recorded = declared_inventory(manifest)

    observed = _observed_inventory(root)
    expected = set(recorded) | {MANIFEST_PATH}
    undeclared = sorted(observed - expected)
    if undeclared:
        raise WorkflowError(
            "WORKFLOW_UNDECLARED_FILE",
            diagnostics=[
                located(
                    "WORKFLOW_UNDECLARED_FILE",
                    name,
                    "the verified revision contains a file its manifest does not inventory",
                )
                for name in undeclared[:16]
            ],
        )

    files: list[BundleFile] = []
    total = 0
    for relative in sorted(recorded):
        entry = recorded[relative]
        data = _read_bytes(BundlePaths.resolve(root, relative), relative)
        total += len(data)
        if total > MAXIMUM_TOTAL_BYTES:
            raise WorkflowError("WORKFLOW_CONTENT_TOO_LARGE")
        digest = byte_digest(data)
        if digest != entry["byte_digest"] or len(data) != entry["byte_length"]:
            raise WorkflowError(
                "WORKFLOW_FILE_DIGEST_MISMATCH",
                diagnostics=[
                    located(
                        "WORKFLOW_FILE_DIGEST_MISMATCH",
                        relative,
                        "stored bytes do not match the published digest",
                    )
                ],
            )
        files.append(BundleFile(path=relative, raw=data, digest=digest))

    digest = bundle_digest(manifest)
    if manifest["bundle_id"] != bundle_id or manifest["revision"] != revision:
        raise WorkflowError(
            "WORKFLOW_BUNDLE_IDENTITY_MISMATCH",
            diagnostics=[
                located(
                    "WORKFLOW_BUNDLE_IDENTITY_MISMATCH",
                    MANIFEST_PATH,
                    "the manifest does not describe the requested revision",
                )
            ],
        )
    return Bundle(
        bundle_id=bundle_id,
        revision=revision,
        project_id=project_id,
        conversation_id=conversation_id,
        manifest=manifest,
        files=tuple(files),
        bundle_digest=digest,
        manifest_file_digest=byte_digest(manifest_raw),
    )


def _reject_reparse_chain(managed_root: Path, root: Path) -> None:
    """Refuse a reparse point on the root or anywhere on the way down to it."""
    if not root.is_relative_to(managed_root):
        raise WorkflowError(
            "WORKFLOW_PATH_LINK_ESCAPE",
            diagnostics=[
                located(
                    "WORKFLOW_PATH_LINK_ESCAPE",
                    MANIFEST_PATH,
                    "the revision is outside its managed directory",
                )
            ],
        )
    current = managed_root
    if is_reparse_point(current):
        raise WorkflowError("WORKFLOW_PATH_LINK_ESCAPE")
    for segment in root.relative_to(managed_root).parts:
        current = current / segment
        if is_reparse_point(current):
            raise WorkflowError(
                "WORKFLOW_PATH_LINK_ESCAPE",
                diagnostics=[
                    located(
                        "WORKFLOW_PATH_LINK_ESCAPE",
                        MANIFEST_PATH,
                        "the revision is reached through a link",
                    )
                ],
            )


def compile_bundle(
    bundle: Bundle,
    *,
    binding_index: Mapping[str, Mapping[str, Any]] | None = None,
    template_digests: Mapping[str, str] | None = None,
) -> CompiledWorkflow:
    """The pure compiler interface a trusted loader (#177) consumes.

    It takes an already verified bundle and returns the parameterised template
    plus its compiled digest. It reads nothing else and cannot see a Run input.

    ``binding_index`` is the already-resolved set of fixed gateway binding
    identities this revision was published against. It arrives as data rather
    than being looked up here, so this function stays pure and a readback
    re-derives exactly the identity that was published instead of re-resolving a
    reference against a catalog that may have moved on. ``template_digests``
    carries the instruction-template identities the same way.
    """
    documents = bundle.documents()
    workflow = documents.get(WORKFLOW_PATH)
    if workflow is None:
        raise WorkflowError(
            "WORKFLOW_FILE_MISSING",
            diagnostics=[located("WORKFLOW_FILE_MISSING", WORKFLOW_PATH, "not declared")],
        )
    if not isinstance(workflow, Mapping):
        raise WorkflowError(
            "WORKFLOW_SCHEMA_INVALID",
            diagnostics=[located("WORKFLOW_SCHEMA_INVALID", WORKFLOW_PATH, "expected a mapping")],
        )
    schema = workflow.get("schema_version", SCHEMA_VERSION)
    if schema != SCHEMA_VERSION:
        raise WorkflowError(
            "WORKFLOW_SCHEMA_UNSUPPORTED",
            diagnostics=[
                located(
                    "WORKFLOW_SCHEMA_UNSUPPORTED",
                    f"{WORKFLOW_PATH}#/schema_version",
                    "unsupported workflow schema version",
                )
            ],
        )
    roles: dict[str, Mapping[str, Any]] = {}
    for path, document in documents.items():
        if path.startswith(ROLES_PREFIX):
            if not isinstance(document, Mapping):
                raise WorkflowError(
                    "WORKFLOW_SCHEMA_INVALID",
                    diagnostics=[
                        located("WORKFLOW_SCHEMA_INVALID", path, "role file must be a mapping")
                    ],
                )
            roles[path] = document
    if template_digests is None:
        template_digests = {
            item.path: item.digest
            for item in bundle.files
            if item.path.startswith(TEMPLATES_PREFIX)
        }
    role_digest = content_digest({path: content_digest(roles[path]) for path in sorted(roles)})
    return compile_workflow(
        bundle.manifest,
        workflow,
        roles,
        bundle_digest=bundle.bundle_digest,
        role_digest=role_digest,
        template_digests=template_digests,
        binding_index=binding_index or {},
    )


def _read_bytes(path: Path, relative: str) -> bytes:
    try:
        data = path.read_bytes()
    except OSError:
        raise WorkflowError(
            "WORKFLOW_FILE_MISSING",
            diagnostics=[located("WORKFLOW_FILE_MISSING", relative, "the file is not readable")],
        ) from None
    if len(data) > MAXIMUM_FILE_BYTES:
        raise WorkflowError("WORKFLOW_CONTENT_TOO_LARGE")
    return data


def _parse_json(raw: bytes, relative: str) -> Any:
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        raise WorkflowError(
            "WORKFLOW_MANIFEST_INVALID",
            diagnostics=[located("WORKFLOW_MANIFEST_INVALID", relative, "manifest is not JSON")],
        ) from None
    if not isinstance(document, dict):
        raise WorkflowError(
            "WORKFLOW_MANIFEST_INVALID",
            diagnostics=[
                located("WORKFLOW_MANIFEST_INVALID", relative, "manifest must be an object")
            ],
        )
    return document


def prepare_files(declared: Sequence[Mapping[str, Any]]) -> dict[str, bytes]:
    """Validate caller-supplied paths and bytes before anything is written."""
    if not declared:
        raise WorkflowError(
            "WORKFLOW_FILE_MISSING",
            diagnostics=[
                located("WORKFLOW_FILE_MISSING", WORKFLOW_PATH, "bundle content is required")
            ],
        )
    if len(declared) > MAXIMUM_FILES:
        raise WorkflowError("WORKFLOW_CONTENT_TOO_LARGE")
    files: dict[str, bytes] = {}
    seen: dict[str, str] = {}
    for entry in declared:
        if not isinstance(entry, Mapping) or set(entry) != {"path", "content"}:
            raise WorkflowError(
                "WORKFLOW_SCHEMA_INVALID",
                diagnostics=[
                    located(
                        "WORKFLOW_SCHEMA_INVALID",
                        WORKFLOW_PATH,
                        "each file declares exactly path and content",
                    )
                ],
            )
        relative = BundlePaths.validate(entry["path"])
        BundlePaths.require_allowed(relative)
        if relative == MANIFEST_PATH:
            # The manifest is derived from the stored bytes; a caller-supplied one
            # could declare a digest that disagrees with them.
            raise WorkflowError(
                "WORKFLOW_MANIFEST_NOT_SUPPLIED",
                diagnostics=[
                    located(
                        "WORKFLOW_MANIFEST_NOT_SUPPLIED",
                        MANIFEST_PATH,
                        "the controller writes the manifest it verified",
                    )
                ],
            )
        BundlePaths.claim(seen, relative)
        content = entry["content"]
        if not isinstance(content, str):
            raise WorkflowError(
                "WORKFLOW_SCHEMA_INVALID",
                diagnostics=[located("WORKFLOW_SCHEMA_INVALID", relative, "content must be text")],
            )
        files[relative] = _encode_content(content, relative)
    for required in REQUIRED_PATHS:
        if required != MANIFEST_PATH and required not in files:
            raise WorkflowError(
                "WORKFLOW_FILE_MISSING",
                diagnostics=[
                    located("WORKFLOW_FILE_MISSING", required, "a required file is not supplied")
                ],
            )
    if sum(len(item) for item in files.values()) > MAXIMUM_TOTAL_BYTES:
        raise WorkflowError("WORKFLOW_CONTENT_TOO_LARGE")
    return files
