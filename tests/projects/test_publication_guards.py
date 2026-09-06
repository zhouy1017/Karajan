"""Owner, revision and server-preview bindings are checked at the atomic write."""

import copy
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from karajan.projects import ProjectError
from test_publication import configured
from test_registry import repository as repository


def test_owner_and_expired_preview_cannot_publish_but_completed_command_replays(
    tmp_path: Path, repository: Path
) -> None:
    now = [1000.0]
    service, project, config = configured(
        tmp_path, repository, clock=lambda: now[0], preview_ttl_seconds=2
    )
    document = config["rulebook"]
    document["revision"] = 2
    preview = service.preview_rulebook(
        project["id"], document, expected_revision=2, command_key="preview", principal="owner"
    )
    with pytest.raises(ProjectError, match="USER_DECISION_REQUIRED"):
        service.publish_rulebook(
            project["id"],
            preview["preview_id"],
            expected_revision=2,
            command_key="other",
            principal="other",
        )
    now[0] = 1002.0
    with pytest.raises(ProjectError, match="PREVIEW_EXPIRED"):
        service.publish_rulebook(
            project["id"],
            preview["preview_id"],
            expected_revision=2,
            command_key="expired",
            principal="owner",
        )
    preview = service.preview_rulebook(
        project["id"], document, expected_revision=2, command_key="fresh", principal="owner"
    )
    published = service.publish_rulebook(
        project["id"],
        preview["preview_id"],
        expected_revision=2,
        command_key="publish",
        principal="owner",
    )
    now[0] = 2000.0
    assert (
        service.publish_rulebook(
            project["id"],
            preview["preview_id"],
            expected_revision=2,
            command_key="publish",
            principal="owner",
        )
        == published
    )


def test_concurrent_publications_have_one_winner_and_replay_one_receipt(
    tmp_path: Path, repository: Path
) -> None:
    service, project, config = configured(tmp_path, repository)
    document = config["rulebook"]
    document["revision"] = 2
    preview = service.preview_rulebook(
        project["id"], document, expected_revision=2, command_key="preview", principal="owner"
    )

    def publish(key: str) -> dict | str:
        try:
            return service.publish_rulebook(
                project["id"],
                preview["preview_id"],
                expected_revision=2,
                command_key=key,
                principal="owner",
            )
        except ProjectError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(publish, ["publish-a", "publish-b"]))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert results.count("REVISION_CONFLICT") == 1
    assert len(service.list_rulebook_publications(project["id"])) == 1


@pytest.mark.parametrize(
    "field,reason",
    [
        ("compiler_identity", "PREVIEW_COMPILER_CHANGED"),
        ("catalog_binding", "PREVIEW_CATALOG_CHANGED"),
        ("principal", "PREVIEW_OWNER_MISMATCH"),
    ],
)
def test_persisted_preview_from_a_different_contract_or_catalog_is_not_authority(
    tmp_path: Path, repository: Path, field: str, reason: str
) -> None:
    service, project, config = configured(tmp_path, repository)
    config["rulebook"]["revision"] = 2
    preview = service.preview_rulebook(
        project["id"],
        config["rulebook"],
        expected_revision=2,
        command_key="preview",
        principal="owner",
    )
    # A fixture of an older persisted preview, not a patch to the compiler or application logic.
    changed = copy.deepcopy(preview)
    changed[field] = (
        {"revision": 0, "digest": None} if field == "catalog_binding" else "previous-contract"
    )
    with sqlite3.connect(service.database) as db:
        db.execute(
            "UPDATE previews SET result=? WHERE id=?", (json.dumps(changed), preview["preview_id"])
        )
    with pytest.raises(ProjectError, match=reason):
        service.publish_rulebook(
            project["id"],
            preview["preview_id"],
            expected_revision=2,
            command_key="publish",
            principal="owner",
        )
    assert service.get(project["id"]) == project


def test_presentation_edit_keeps_immutable_execution_bytes_and_new_publication_record(
    tmp_path: Path, repository: Path
) -> None:
    service, project, config = configured(tmp_path, repository)
    before = service.get_rulebook(project["id"], config["rulebook"]["id"], 1)
    config["rulebook"]["description"] = "Updated explanation only"
    preview = service.preview_rulebook(
        project["id"],
        config["rulebook"],
        expected_revision=2,
        command_key="preview",
        principal="owner",
    )
    result = service.publish_rulebook(
        project["id"],
        preview["preview_id"],
        expected_revision=2,
        command_key="publish",
        principal="owner",
    )
    assert result["rulebook"]["rulebook_sha256"] == before["rulebook_sha256"]
    assert service.get_rulebook(project["id"], config["rulebook"]["id"], 1) == before
    assert (
        service.get_configuration(project["id"])["configuration"]["rulebook"]["description"]
        == "Updated explanation only"
    )


def test_effective_catalog_preserves_revocation_and_ignores_structurally_invalid_draft(
    tmp_path: Path, repository: Path
) -> None:
    service, project, config = configured(tmp_path, repository)
    original = service.get_effective_resources(project["id"])
    revoked = copy.deepcopy(config)
    revoked["resources"]["profiles"][0]["enabled"] = False
    revoked["approved_profile_refs"] = []
    preview = service.preview_configuration(
        project["id"], revoked, command_key="revoke-preview", principal="owner"
    )
    service.apply_configuration(
        project["id"],
        preview["preview_id"],
        expected_revision=2,
        command_key="revoke",
        principal="owner",
    )
    current = service.get_effective_resources(project["id"])
    assert current["revision"] == original["revision"] + 1
    assert current["resources"]["profiles"][0]["enabled"] is False
    assert current["approved_profile_refs"] == []
    broken = copy.deepcopy(config)
    broken["resources"]["channels"][0]["account_id"] = "unregistered"
    preview = service.preview_configuration(
        project["id"], broken, command_key="draft-preview", principal="owner"
    )
    service.apply_configuration(
        project["id"],
        preview["preview_id"],
        expected_revision=3,
        command_key="draft",
        principal="owner",
    )
    assert service.get_effective_resources(project["id"]) == current


@pytest.mark.parametrize("entry", ["configuration", "rulebook"])
def test_confirmation_rejects_changed_persisted_content_even_when_execution_hash_is_unchanged(
    tmp_path: Path, repository: Path, entry: str
) -> None:
    service, project, config = configured(tmp_path, repository)
    config["rulebook"]["revision"] = 2
    if entry == "rulebook":
        preview = service.preview_rulebook(
            project["id"],
            config["rulebook"],
            expected_revision=2,
            command_key="preview",
            principal="owner",
        )
        operation = service.publish_rulebook
    else:
        preview = service.preview_configuration(
            project["id"], config, command_key="preview", principal="owner"
        )
        operation = service.apply_configuration
    with sqlite3.connect(service.database) as db:
        raw = db.execute(
            "SELECT configuration FROM previews WHERE id=?", (preview["preview_id"],)
        ).fetchone()[0]
        changed = json.loads(raw)
        changed["rulebook"]["description"] = "Not the explanation confirmed by the owner"
        db.execute(
            "UPDATE previews SET configuration=? WHERE id=?",
            (json.dumps(changed), preview["preview_id"]),
        )
    with pytest.raises(ProjectError, match="PREVIEW_CONTENT_CHANGED"):
        operation(
            project["id"],
            preview["preview_id"],
            expected_revision=2,
            command_key="publish",
            principal="owner",
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("amount", "NaN"),
        ("amount", "-1"),
        ("amount", "1.0000001"),
        ("amount", None),
        ("currency", "usd"),
        ("duration", 0),
        ("pool_limit", "Infinity"),
        ("pool_limit", "-1"),
        ("conservative_bound", -1),
        ("approval", "missing-profile"),
    ],
)
def test_invalid_resource_values_cannot_restore_revoked_permissions(
    tmp_path: Path, repository: Path, field: str, value: object
) -> None:
    service, project, config = configured(tmp_path, repository)
    revoked = copy.deepcopy(config)
    revoked["resources"]["profiles"][0]["enabled"] = False
    revoked["approved_profile_refs"] = []
    preview = service.preview_configuration(
        project["id"], revoked, command_key="revoke-preview", principal="owner"
    )
    service.apply_configuration(
        project["id"],
        preview["preview_id"],
        expected_revision=2,
        command_key="revoke",
        principal="owner",
    )
    current = service.get_effective_resources(project["id"])
    broken = copy.deepcopy(config)
    resources = broken["resources"]
    if field == "amount":
        resources["budgets"][0]["currency_limits"]["USD"] = value
    elif field == "currency":
        resources["budgets"][0]["currency_limits"] = {value: "1"}
    elif field == "duration":
        resources["budgets"][0]["max_duration_seconds"] = value
    elif field == "pool_limit":
        resources["quota_pools"][0]["limit"] = value
    elif field == "conservative_bound":
        resources["capacity_policies"][0]["conservative_mode"]["cooldown_seconds"] = value
    else:
        broken["approved_profile_refs"][0]["id"] = value
    preview = service.preview_configuration(
        project["id"], broken, command_key="draft-preview", principal="owner"
    )
    saved = service.apply_configuration(
        project["id"],
        preview["preview_id"],
        expected_revision=3,
        command_key="draft",
        principal="owner",
    )
    assert saved["configuration"]["status"] == "draft"
    assert service.get_effective_resources(project["id"]) == current


@pytest.mark.parametrize("entry", ["configuration", "rulebook"])
@pytest.mark.parametrize("field", ["id", "description"])
def test_invalid_unicode_is_not_storable_and_preview_remains_json_exportable(
    tmp_path: Path, repository: Path, entry: str, field: str
) -> None:
    from starlette.responses import JSONResponse

    service, project, config = configured(tmp_path, repository)
    config["rulebook"]["revision"] = 2
    config["rulebook"][field] = "bad\ud800text"
    if entry == "configuration":
        preview = service.preview_configuration(
            project["id"], config, command_key="preview", principal="owner"
        )
    else:
        preview = service.preview_rulebook(
            project["id"],
            config["rulebook"],
            expected_revision=2,
            command_key="preview",
            principal="owner",
        )
    assert preview["can_save_draft"] is False
    assert preview["can_publish"] is False
    JSONResponse(preview)
    with pytest.raises(ProjectError, match="CONFIGURATION_NOT_STORABLE"):
        service.apply_configuration(
            project["id"],
            preview["preview_id"],
            expected_revision=2,
            command_key="apply",
            principal="owner",
        )


def test_configuration_preview_does_not_grant_explicit_publication_authority(
    tmp_path: Path, repository: Path
) -> None:
    service, project, config = configured(tmp_path, repository)
    config["rulebook"]["revision"] = 2
    preview = service.preview_configuration(
        project["id"], config, command_key="preview", principal="owner"
    )
    assert preview["can_publish"] is True
    with pytest.raises(ProjectError, match="RULEBOOK_NOT_PUBLISHABLE"):
        service.publish_rulebook(
            project["id"],
            preview["preview_id"],
            expected_revision=2,
            command_key="publish",
            principal="owner",
        )
