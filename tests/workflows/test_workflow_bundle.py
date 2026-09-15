"""Bundle persistence: real bytes, immutable revisions, tamper detection.

These cases write and re-read real files under a real SQLite database. No case
uses an in-memory mock, because the properties under test — atomic publication,
restart readback, tamper refusal — are properties of the filesystem and the
ledger, not of a call graph.
"""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from karajan.projects import ProjectRegistry
from karajan.workflows import WorkflowError
from karajan.workflows import bundle as bundles

REPOSITORY_HELPERS = Path(__file__).resolve().parents[2] / "tests" / "web"


def make_repository(root: Path, name: str = "fixture") -> Path:
    repository = root / "repositories" / name
    repository.mkdir(parents=True)
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
    return repository


def make_bundle_root(tmp_path: Path, *, name: str = "bundle") -> Path:
    return tmp_path / "bundles" / name


WORKFLOW_TEXT = """schema_version: workflow.v1
id: compare-options
revision: 1
delivery_kind: report
input_contract: text@1
inputs:
  - requirement.option_a
  - requirement.option_b
roles:
  researcher: role:source-researcher@2
steps:
  - id: option-a
    execution_kind: artifact_aggregate@1
    role: researcher
    output_contract: aggregated-report@1
    inputs:
      sources: requirement.option_a
  - id: comparison
    execution_kind: artifact_aggregate@1
    role: researcher
    output_contract: aggregated-report@1
    depends_on:
      - option-a
    inputs:
      sources:
        - option-a.output
completion:
  required_steps:
    - option-a
    - comparison
  artifact: comparison.output
"""

ROLE_TEXT = """id: source-researcher
revision: 2
display_name: Source researcher
responsibilities:
  - read approved material
stop_conditions:
  - material is outside the approved scope
required_capabilities:
  - read_only_research
tool_constraints:
  paths:
    - docs/**
  network: denied
"""


def declared_files(workflow_text: str = WORKFLOW_TEXT) -> list[dict[str, str]]:
    return [
        {"path": "workflow.yaml", "content": workflow_text},
        {"path": "roles/researcher.yaml", "content": ROLE_TEXT},
    ]


def write_bundle_files(
    root: Path,
    files: dict[str, bytes],
    manifest: dict[str, Any],
) -> Path:
    """Materialise a bundle directory exactly as the store would."""
    root.mkdir(parents=True, exist_ok=True)
    for relative, data in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (root / "manifest.json").write_bytes(bundles.render_manifest(manifest))
    return root


def verified_fixture(tmp_path: Path) -> tuple[Path, Path, bundles.Bundle]:
    """A real materialised bundle plus its manifest, for reader-level cases."""
    files = bundles.prepare_files(declared_files())
    manifest = bundles.manifest_document(
        bundle_id="compare-options",
        revision=1,
        files=files,
        references={
            "workflow_id": "compare-options",
            "workflow_revision": 1,
            "input_contract_ref": "text@1",
            "roles": {"researcher": "role:source-researcher@2"},
            "execution_kinds": ["artifact_aggregate@1"],
            "contracts": ["aggregated-report@1", "text@1"],
        },
        delivery_kind="report",
    )
    managed = make_bundle_root(tmp_path)
    root = write_bundle_files(managed / "revision-1", files, manifest)
    read = bundles.read_directory(
        root,
        managed_root=managed,
        bundle_id="compare-options",
        revision=1,
        project_id="project",
        conversation_id="conversation",
    )
    return managed, root, read


def test_real_files_round_trip_with_separate_bundle_and_manifest_identities(
    tmp_path: Path,
) -> None:
    """AC1: real bytes, per-file digests, one bundle digest, no self-reference."""
    _, root, read = verified_fixture(tmp_path)
    assert {item.path for item in read.files} == {"workflow.yaml", "roles/researcher.yaml"}
    for item in read.files:
        assert item.digest == bundles.byte_digest(item.raw)
        assert item.raw == (root / item.path).read_bytes()
    # The manifest does not inventory itself; its own hash is reported beside it.
    assert "manifest.json" not in {entry["path"] for entry in read.manifest["files"]}
    assert read.manifest_file_digest == bundles.byte_digest((root / "manifest.json").read_bytes())
    assert read.bundle_digest == bundles.bundle_digest(read.manifest)
    # The published digest does not depend on the field that stores it.
    without = {k: v for k, v in read.manifest.items() if k != "bundle_digest"}
    assert bundles.bundle_digest({**without, "bundle_digest": "anything"}) == read.bundle_digest
    assert read.manifest["bundle_digest"] == read.bundle_digest

    compiled = bundles.compile_bundle(read)
    assert compiled.executable is True
    assert compiled.reviewable is True
    assert compiled.role_definitions[0].digest


def test_a_second_read_of_the_same_directory_is_identical(tmp_path: Path) -> None:
    """The reader is a function of the bytes: no stored preview is consulted."""
    _, root, first = verified_fixture(tmp_path)
    second = bundles.read_directory(
        root,
        managed_root=root.parent,
        bundle_id="compare-options",
        revision=1,
        project_id="project",
        conversation_id="conversation",
    )
    assert first.bundle_digest == second.bundle_digest
    assert first.manifest_file_digest == second.manifest_file_digest
    assert bundles.compile_bundle(first).compiled_digest == (
        bundles.compile_bundle(second).compiled_digest
    )


def test_edited_file_is_refused_on_read(tmp_path: Path) -> None:
    """AC1: bytes that changed after publication no longer match the manifest."""
    managed, root, _ = verified_fixture(tmp_path)
    (root / "workflow.yaml").write_text(WORKFLOW_TEXT.replace("compare-options", "rewritten"))
    with pytest.raises(WorkflowError) as raised:
        bundles.read_directory(
            root,
            managed_root=managed,
            bundle_id="compare-options",
            revision=1,
            project_id="project",
            conversation_id="conversation",
        )
    assert raised.value.code == "WORKFLOW_FILE_DIGEST_MISMATCH"


def test_tampered_manifest_digest_is_refused(tmp_path: Path) -> None:
    """A manifest whose own recorded digest is wrong is not a verified manifest."""
    managed, root, _ = verified_fixture(tmp_path)
    document = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    document["bundle_digest"] = "0" * 64
    (root / "manifest.json").write_bytes(bundles.render_manifest(document))
    with pytest.raises(WorkflowError) as raised:
        bundles.read_directory(
            root,
            managed_root=managed,
            bundle_id="compare-options",
            revision=1,
            project_id="project",
            conversation_id="conversation",
        )
    assert raised.value.code == "WORKFLOW_BUNDLE_DIGEST_MISMATCH"


def test_an_undeclared_extra_file_is_refused(tmp_path: Path) -> None:
    """AC1: the declared inventory is the closed set of files present."""
    managed, root, _ = verified_fixture(tmp_path)
    (root / "notes.txt").write_text("planted\n", encoding="utf-8")
    with pytest.raises(WorkflowError) as raised:
        bundles.read_directory(
            root,
            managed_root=managed,
            bundle_id="compare-options",
            revision=1,
            project_id="project",
            conversation_id="conversation",
        )
    assert raised.value.code == "WORKFLOW_UNDECLARED_FILE"
    assert raised.value.diagnostics[0].location.startswith("workflow.yaml")


def test_a_manifest_that_lists_itself_is_refused(tmp_path: Path) -> None:
    """The manifest cannot inventory its own bytes: that digest is unsatisfiable."""
    managed, root, read = verified_fixture(tmp_path)
    document = dict(read.manifest)
    document["files"] = [
        *document["files"],
        {
            "path": "manifest.json",
            "byte_digest": "0" * 64,
            "byte_length": 1,
        },
    ]
    document["bundle_digest"] = bundles.bundle_digest(document)
    (root / "manifest.json").write_bytes(bundles.render_manifest(document))
    with pytest.raises(WorkflowError) as raised:
        bundles.read_directory(
            root,
            managed_root=managed,
            bundle_id="compare-options",
            revision=1,
            project_id="project",
            conversation_id="conversation",
        )
    assert raised.value.code == "WORKFLOW_MANIFEST_SELF_REFERENCE"


@pytest.mark.parametrize(
    ("path", "code"),
    [
        ("../escape.yaml", "WORKFLOW_PATH_INVALID"),
        ("/etc/passwd", "WORKFLOW_PATH_NOT_RELATIVE"),
        ("C:/windows/system32/config", "WORKFLOW_PATH_NOT_RELATIVE"),
        ("\\\\server\\share\\a.yaml", "WORKFLOW_PATH_NOT_RELATIVE"),
        ("roles/../../escape.yaml", "WORKFLOW_PATH_INVALID"),
        ("workflow.yaml.", "WORKFLOW_PATH_AMBIGUOUS"),
        ("workflow.yaml ", "WORKFLOW_PATH_AMBIGUOUS"),
        ("workflow.yaml:stream", "WORKFLOW_PATH_AMBIGUOUS"),
        ("con.yaml", "WORKFLOW_PATH_AMBIGUOUS"),
        ("roles/nul.yaml", "WORKFLOW_PATH_AMBIGUOUS"),
        ("Roles/researcher.yaml", "WORKFLOW_PATH_NOT_ALLOWED"),
        ("workflow.yaml/sub.yaml", "WORKFLOW_PATH_NOT_ALLOWED"),
        ("roles/researcher.exe", "WORKFLOW_PATH_NOT_ALLOWED"),
        ("notes.txt", "WORKFLOW_PATH_NOT_ALLOWED"),
    ],
)
def test_declared_paths_are_validated_before_any_write(path: str, code: str) -> None:
    """AC1: traversal, absolute, drive, UNC and ambiguous names are all refused."""
    with pytest.raises(WorkflowError) as raised:
        bundles.prepare_files(
            [{"path": path, "content": "x"}, {"path": "workflow.yaml", "content": "y"}]
        )
    assert raised.value.code == code
    # The submitted text is never used as a diagnostic location.
    for item in raised.value.diagnostics:
        assert item.location.startswith(("workflow.yaml", "roles/", "manifest.json"))


def test_case_and_separator_duplicates_are_refused() -> None:
    """AC1: two spellings that name one file on this host are not both stored."""
    with pytest.raises(WorkflowError) as raised:
        bundles.prepare_files(
            [
                {"path": "roles/a.yaml", "content": "x"},
                {"path": "roles/A.yaml", "content": "y"},
                {"path": "workflow.yaml", "content": "z"},
            ]
        )
    assert raised.value.code == "WORKFLOW_PATH_DUPLICATE"
    assert raised.value.diagnostics[0].location == "roles/A.yaml"

    with pytest.raises(WorkflowError) as raised:
        bundles.prepare_files(
            [{"path": "workflow.yaml", "content": "x"}, {"path": "workflow.yaml", "content": "y"}]
        )
    assert raised.value.code == "WORKFLOW_PATH_DUPLICATE"

    # A backslash separator is normalized before comparison, not treated as a
    # different file name.
    with pytest.raises(WorkflowError) as raised:
        bundles.prepare_files(
            [
                {"path": "roles/a.yaml", "content": "x"},
                {"path": "roles\\a.yaml", "content": "y"},
                {"path": "workflow.yaml", "content": "z"},
            ]
        )
    assert raised.value.code == "WORKFLOW_PATH_DUPLICATE"


def test_a_missing_required_file_is_refused() -> None:
    with pytest.raises(WorkflowError) as raised:
        bundles.prepare_files([{"path": "roles/a.yaml", "content": "x"}])
    assert raised.value.code == "WORKFLOW_FILE_MISSING"
    assert raised.value.diagnostics[0].location == "workflow.yaml"


def test_a_caller_cannot_supply_the_manifest() -> None:
    with pytest.raises(WorkflowError) as raised:
        bundles.prepare_files(
            [{"path": "manifest.json", "content": "{}"}, {"path": "workflow.yaml", "content": "x"}]
        )
    assert raised.value.code == "WORKFLOW_MANIFEST_NOT_SUPPLIED"


def test_normalized_duplicate_paths_are_refused_in_a_manifest(tmp_path: Path) -> None:
    """A manifest declaring one file twice is not a valid inventory."""
    managed, root, read = verified_fixture(tmp_path)
    document = dict(read.manifest)
    document["files"] = [*document["files"], dict(document["files"][0])]
    document["bundle_digest"] = bundles.bundle_digest(document)
    (root / "manifest.json").write_bytes(bundles.render_manifest(document))
    with pytest.raises(WorkflowError) as raised:
        bundles.read_directory(
            root,
            managed_root=managed,
            bundle_id="compare-options",
            revision=1,
            project_id="project",
            conversation_id="conversation",
        )
    assert raised.value.code == "WORKFLOW_PATH_DUPLICATE"


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS junctions are Windows-only")
def test_a_junction_inside_the_bundle_is_refused(tmp_path: Path) -> None:
    """AC1: a junction planted in a verified revision cannot redirect a read."""
    managed, root, _ = verified_fixture(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "planted.yaml").write_text("id: planted\nrevision: 1\n", encoding="utf-8")
    junction = root / "roles" / "linked"
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
        check=True,
        capture_output=True,
    )
    try:
        with pytest.raises(WorkflowError) as raised:
            bundles.read_directory(
                root,
                managed_root=managed,
                bundle_id="compare-options",
                revision=1,
                project_id="project",
                conversation_id="conversation",
            )
        assert raised.value.code == "WORKFLOW_PATH_LINK_ESCAPE"
    finally:
        # The junction resolves to a real directory; removing the link itself is
        # safe and leaves the target intact.
        os.rmdir(junction)


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS junctions are Windows-only")
def test_a_junction_on_an_ancestor_of_the_revision_is_refused(tmp_path: Path) -> None:
    """A redirect above the revision directory is caught, not only at the leaf."""
    files = bundles.prepare_files(declared_files())
    manifest = bundles.manifest_document(
        bundle_id="compare-options",
        revision=1,
        files=files,
        references={
            "workflow_id": "compare-options",
            "workflow_revision": 1,
            "input_contract_ref": "text@1",
            "roles": {"researcher": "role:source-researcher@2"},
            "execution_kinds": ["artifact_aggregate@1"],
            "contracts": ["aggregated-report@1", "text@1"],
        },
        delivery_kind="report",
    )
    elsewhere = tmp_path / "elsewhere"
    write_bundle_files(elsewhere, files, manifest)
    managed = tmp_path / "managed"
    managed.mkdir()
    link = managed / "compare-options"
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(elsewhere)],
        check=True,
        capture_output=True,
    )
    try:
        with pytest.raises(WorkflowError) as raised:
            bundles.read_directory(
                link / "revision-1",
                managed_root=link,
                bundle_id="compare-options",
                revision=1,
                project_id="project",
                conversation_id="conversation",
            )
        assert raised.value.code == "WORKFLOW_PATH_LINK_ESCAPE"
    finally:
        os.rmdir(link)


def test_bundle_identity_must_be_one_addressable_segment() -> None:
    """A bundle identity is a path segment, never a nested path."""
    for value in ("a/b", "a\\b", "..", ".hidden", "", "x" * 200):
        with pytest.raises(WorkflowError) as raised:
            _identity(value)
        assert raised.value.code in {
            "WORKFLOW_BUNDLE_ID_INVALID",
            "WORKFLOW_PATH_INVALID",
        }
    # A single ordinary segment is accepted unchanged.
    assert _identity("compare-options") == "compare-options"


def _identity(value: str) -> str:
    from karajan.workflows.store import WorkflowStore

    return WorkflowStore._identity(value)


def test_a_project_registry_database_is_not_touched_by_the_reader(tmp_path: Path) -> None:
    """The reader is read-only: it opens no ledger and writes nothing."""
    _, root, _ = verified_fixture(tmp_path)
    before = sorted(
        str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*") if path.is_file()
    )
    bundles.read_directory(
        root,
        managed_root=root.parent,
        bundle_id="compare-options",
        revision=1,
        project_id="project",
        conversation_id="conversation",
    )
    after = sorted(
        str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*") if path.is_file()
    )
    assert before == after


def test_schema_helpers_are_absent_from_the_reader(tmp_path: Path) -> None:
    """A SQLite file in the tree is refused as an undeclared entry, not opened."""
    managed, root, _ = verified_fixture(tmp_path)
    sqlite3.connect(root / "planted.sqlite").close()
    with pytest.raises(WorkflowError) as raised:
        bundles.read_directory(
            root,
            managed_root=managed,
            bundle_id="compare-options",
            revision=1,
            project_id="project",
            conversation_id="conversation",
        )
    assert raised.value.code == "WORKFLOW_UNDECLARED_FILE"


def test_a_preexisting_junction_never_receives_any_bytes(tmp_path: Path) -> None:
    """AC1: the chain is validated before the first write, not after it.

    A junction planted on the managed root, the project directory or the bundle
    directory would otherwise redirect the very first write outside the managed
    tree. The outside directory's inventory is asserted unchanged, so this is a
    statement about bytes rather than about which exception was raised.
    """
    from karajan.workflows.store import WorkflowStore

    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep.txt"
    sentinel.write_text("untouched\n", encoding="utf-8")
    before = sorted(str(path.relative_to(outside)) for path in outside.rglob("*"))

    repository = make_repository(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    registry = ProjectRegistry(state / "projects.sqlite", [repository.parent])
    project = registry.create(
        {
            "name": "Boundary",
            "repository_path": str(repository),
            "base_ref": "main",
            "target_branch": "main",
            "allowed_target_branches": ["main"],
        },
        command_key="project",
        principal="owner",
    )
    managed = state / "workflow-bundles"
    managed.mkdir()

    class ConversationsShim:
        def __init__(self, project_id: str) -> None:
            self.project_id = project_id

        def conversation_project(self, conversation_id: str) -> str:
            return self.project_id

    store = WorkflowStore(registry, ConversationsShim(project["id"]), managed)
    payload = {
        "files": declared_files(),
        "delivery_kind": "report",
    }

    # 1) The project directory is a junction pointing outside the managed tree.
    project_link = managed / project["id"]
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(project_link), str(outside)],
        check=True,
        capture_output=True,
    )
    try:
        with pytest.raises(WorkflowError) as raised:
            store.create_bundle(
                project["id"],
                "conversation",
                "bundle",
                payload,
                principal="owner",
                command_key="junction-project",
            )
        assert raised.value.code == "WORKFLOW_PATH_LINK_ESCAPE"
        assert sorted(str(path.relative_to(outside)) for path in outside.rglob("*")) == before
    finally:
        os.rmdir(project_link)

    # 2) The bundle directory itself is a junction pointing outside.
    bundle_link = managed / project["id"] / "bundle"
    bundle_link.parent.mkdir(parents=True)
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(bundle_link), str(outside)],
        check=True,
        capture_output=True,
    )
    try:
        with pytest.raises(WorkflowError) as raised:
            store.create_bundle(
                project["id"],
                "conversation",
                "bundle",
                payload,
                principal="owner",
                command_key="junction-bundle",
            )
        assert raised.value.code == "WORKFLOW_PATH_LINK_ESCAPE"
        assert sorted(str(path.relative_to(outside)) for path in outside.rglob("*")) == before
    finally:
        os.rmdir(bundle_link)


def test_an_uncommitted_materialization_is_adopted_or_refused(tmp_path: Path) -> None:
    """AC1: an interrupted attempt must not block its revision permanently.

    A crash between materialising the tree and committing the revision row
    leaves a complete directory no row references. Replaying the same command
    adopts it when its bytes are identical, and refuses a mismatch instead of
    silently reusing or replacing different bytes.
    """
    from karajan.workflows.store import WorkflowStore

    repository = make_repository(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    registry = ProjectRegistry(state / "projects.sqlite", [repository.parent])
    project = registry.create(
        {
            "name": "Recovery",
            "repository_path": str(repository),
            "base_ref": "main",
            "target_branch": "main",
            "allowed_target_branches": ["main"],
        },
        command_key="project",
        principal="owner",
    )

    class ConversationsShim:
        def __init__(self, project_id: str) -> None:
            self.project_id = project_id

        def conversation_project(self, conversation_id: str) -> str:
            return self.project_id

    managed = state / "workflow-bundles"
    store = WorkflowStore(registry, ConversationsShim(project["id"]), managed)
    payload = {
        "files": [
            {"path": "workflow.yaml", "content": WORKFLOW_TEXT},
            {"path": "roles/researcher.yaml", "content": ROLE_TEXT},
        ],
        "delivery_kind": "report",
    }

    # Materialise the revision through the real path, then remove only the
    # committed rows: this is exactly the state a crash before commit leaves.
    created, _ = store.create_bundle(
        project["id"],
        "conversation",
        "bundle",
        payload,
        principal="owner",
        command_key="first",
    )
    with sqlite3.connect(state / "projects.sqlite") as db:
        db.execute("DELETE FROM workflow_bundles")
        db.execute("DELETE FROM workflow_bundle_files")
        db.execute("DELETE FROM workflow_bundle_current")
        db.execute("DELETE FROM commands")
    reopened = WorkflowStore(registry, ConversationsShim(project["id"]), managed)
    adopted, was_created = reopened.create_bundle(
        project["id"],
        "conversation",
        "bundle",
        payload,
        principal="owner",
        command_key="retry",
    )
    assert was_created is True
    # The adopted revision is the same template; its record digest differs only
    # because the recording principal and time are part of that record.
    assert adopted["compiled_digest"] == created["compiled_digest"]
    assert adopted["bundle_digest"] == created["bundle_digest"]
    assert adopted["files"] == created["files"]

    # A directory holding different bytes is refused, and left for inspection.
    root = managed / project["id"] / "bundle" / "revision-1"
    (root / "workflow.yaml").write_text(WORKFLOW_TEXT.replace("compare-options", "other"))
    with sqlite3.connect(state / "projects.sqlite") as db:
        db.execute("DELETE FROM workflow_bundles")
        db.execute("DELETE FROM workflow_bundle_files")
        db.execute("DELETE FROM workflow_bundle_current")
        db.execute("DELETE FROM commands")
    third = WorkflowStore(registry, ConversationsShim(project["id"]), managed)
    with pytest.raises(WorkflowError) as raised:
        third.create_bundle(
            project["id"],
            "conversation",
            "bundle",
            payload,
            principal="owner",
            command_key="third",
        )
    assert raised.value.code in {
        # The orphan's own manifest no longer matches its bytes, so the reader
        # refuses it first; when the manifest does agree, the adoption check
        # reports the mismatch instead.
        "WORKFLOW_FILE_DIGEST_MISMATCH",
        "WORKFLOW_UNCOMMITTED_MATERIALIZATION_MISMATCH",
    }
    assert (root / "workflow.yaml").exists()
