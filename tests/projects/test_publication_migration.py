"""Migration uses actual legacy project/configuration records without inventing publication."""

import json
import sqlite3
from pathlib import Path

import pytest
from karajan.projects import ProjectError, ProjectRegistry
from test_publication import configured
from test_registry import repository as repository


def legacy_copy(source: Path, target: Path, *, conflict: bool = False) -> None:
    with sqlite3.connect(source) as original, sqlite3.connect(target) as legacy:
        for name in ("projects", "commands", "previews"):
            ddl = original.execute(
                "SELECT sql FROM sqlite_master WHERE name=?", (name,)
            ).fetchone()[0]
            legacy.execute(ddl)
            for row in original.execute("SELECT * FROM " + name):
                legacy.execute(
                    "INSERT INTO " + name + " VALUES (" + ",".join("?" for _ in row) + ")", row
                )
        if conflict:
            project_id, raw = legacy.execute("SELECT id,snapshot FROM projects").fetchone()
            snapshot = json.loads(raw)
            previous = legacy.execute(
                "SELECT configuration,result FROM previews WHERE id=?",
                (snapshot["configuration"]["preview_id"],),
            ).fetchone()
            configuration = json.loads(previous[0])
            configuration["rulebook"]["collaboration"]["max_parallel_writers_per_project"] = 3
            preview = json.loads(previous[1])
            preview["preview_id"] = "legacy-second"
            legacy.execute(
                "INSERT INTO previews VALUES (?,?,?,?)",
                ("legacy-second", project_id, json.dumps(configuration), json.dumps(preview)),
            )
            snapshot["configuration"]["preview_id"] = "legacy-second"
            snapshot["revision"] += 1
            legacy.execute(
                "INSERT INTO commands VALUES (?,?,?,?)",
                ("owner", "legacy-second-apply", "old-digest", json.dumps(snapshot)),
            )
            legacy.execute(
                "UPDATE projects SET snapshot=? WHERE id=?", (json.dumps(snapshot), project_id)
            )
        for identity, raw in legacy.execute("SELECT id,result FROM previews").fetchall():
            preview = json.loads(raw)
            for key in (
                "principal",
                "expires_at",
                "compiler_identity",
                "catalog_binding",
                "can_publish",
                "can_save_draft",
            ):
                preview.pop(key, None)
            legacy.execute(
                "UPDATE previews SET result=? WHERE id=?", (json.dumps(preview), identity)
            )


def test_legacy_upgrade_preserves_owner_catalog_and_frozen_binding_without_publication(
    tmp_path: Path, repository: Path
) -> None:
    _, project, config = configured(tmp_path, repository)
    database = tmp_path / "legacy.sqlite"
    legacy_copy(tmp_path / "projects.sqlite", database)
    upgraded = ProjectRegistry(database, [repository.parent])
    assert upgraded.list_rulebook_publications(project["id"]) == []
    assert upgraded.get_rulebook(project["id"], config["rulebook"]["id"], 1)["revision"] == 1
    assert (
        upgraded.get_effective_resources(project["id"])["resources"]["profiles"][0]["enabled"]
        is True
    )
    with pytest.raises(ProjectError, match="PREVIEW_REVIEW_REQUIRED"):
        upgraded.apply_configuration(
            project["id"],
            project["configuration"]["preview_id"],
            expected_revision=2,
            command_key="old-preview",
            principal="owner",
        )
    document = config["rulebook"]
    document["revision"] = 2
    preview = upgraded.preview_rulebook(
        project["id"], document, expected_revision=2, command_key="new-preview", principal="owner"
    )
    assert preview["can_publish"] is True


def test_conflicting_legacy_revision_is_reported_and_never_automatically_selected(
    tmp_path: Path, repository: Path
) -> None:
    _, project, config = configured(tmp_path, repository)
    database = tmp_path / "legacy-conflict.sqlite"
    legacy_copy(tmp_path / "projects.sqlite", database, conflict=True)
    upgraded = ProjectRegistry(database, [repository.parent])
    versions = upgraded.list_rulebook_versions(project["id"])
    assert versions[0]["status"] == "legacy_conflict"
    assert len(versions[0]["digest_candidates"]) == 2
    with pytest.raises(ProjectError, match="LEGACY_RULEBOOK_IDENTITY_CONFLICT"):
        upgraded.get_rulebook(project["id"], config["rulebook"]["id"], 1)
    preview = upgraded.preview_rulebook(
        project["id"],
        config["rulebook"],
        expected_revision=3,
        command_key="preview",
        principal="owner",
    )
    assert preview["can_publish"] is False
    assert preview["can_save_draft"] is False
    with pytest.raises(ProjectError, match="LEGACY_RULEBOOK_IDENTITY_CONFLICT"):
        upgraded.publish_rulebook(
            project["id"],
            preview["preview_id"],
            expected_revision=3,
            command_key="publish",
            principal="owner",
        )
    assert upgraded.list_rulebook_publications(project["id"]) == []


def test_legacy_latest_invalid_draft_keeps_last_applied_valid_revocation(
    tmp_path: Path, repository: Path
) -> None:
    service, project, config = configured(tmp_path, repository)
    config["resources"]["profiles"][0]["enabled"] = False
    preview = service.preview_configuration(
        project["id"], config, command_key="revoke-preview", principal="owner"
    )
    service.apply_configuration(
        project["id"],
        preview["preview_id"],
        expected_revision=2,
        command_key="revoke",
        principal="owner",
    )
    config["resources"]["channels"][0]["account_id"] = "broken"
    preview = service.preview_configuration(
        project["id"], config, command_key="draft-preview", principal="owner"
    )
    service.apply_configuration(
        project["id"],
        preview["preview_id"],
        expected_revision=3,
        command_key="draft",
        principal="owner",
    )
    database = tmp_path / "legacy-draft.sqlite"
    legacy_copy(tmp_path / "projects.sqlite", database)
    upgraded = ProjectRegistry(database, [repository.parent])
    assert (
        upgraded.get_effective_resources(project["id"])["resources"]["profiles"][0]["enabled"]
        is False
    )
