"""Rulebook publication goes through ProjectRegistry and a real project database."""

import copy
from pathlib import Path

import pytest
from karajan.projects import ProjectRegistry
from test_registry import configuration, request
from test_registry import repository as repository


def test_explicit_publication_is_durable_and_retries_return_the_same_receipt(
    tmp_path: Path, repository: Path
) -> None:
    service = ProjectRegistry(tmp_path / "projects.sqlite", [repository.parent])
    project = service.create(request(repository), command_key="create", principal="owner")
    config = configuration()
    preview = service.preview_configuration(
        project["id"], config, command_key="config-preview", principal="owner"
    )
    project = service.apply_configuration(
        project["id"],
        preview["preview_id"],
        expected_revision=1,
        command_key="config-apply",
        principal="owner",
    )
    document = config["rulebook"]
    document["revision"] = 2
    document["rules"][2]["priority"] = 101
    preview = service.preview_rulebook(
        project["id"],
        document,
        expected_revision=project["revision"],
        command_key="preview",
        principal="owner",
    )
    assert preview["can_save_draft"] is True
    assert preview["can_publish"] is True
    result = service.publish_rulebook(
        project["id"],
        preview["preview_id"],
        expected_revision=project["revision"],
        command_key="publish",
        principal="owner",
    )
    assert result["state"] == "waiting_qualification"
    assert result["activation_allowed"] is False
    assert result["rulebook"]["revision"] == 2
    reopened = ProjectRegistry(tmp_path / "projects.sqlite", [repository.parent])
    assert (
        reopened.publish_rulebook(
            project["id"],
            preview["preview_id"],
            expected_revision=project["revision"],
            command_key="publish",
            principal="owner",
        )
        == result
    )
    assert len(reopened.list_rulebook_publications(project["id"])) == 1
    assert reopened.get_configuration(project["id"])["configuration"]["rulebook"] == document
    assert (
        reopened.get_rulebook(project["id"], document["id"], 2)["rulebook_sha256"]
        == result["rulebook"]["rulebook_sha256"]
    )


def configured(
    tmp_path: Path, repository: Path, **options: object
) -> tuple[ProjectRegistry, dict, dict]:
    service = ProjectRegistry(tmp_path / "projects.sqlite", [repository.parent], **options)
    project = service.create(request(repository), command_key="create", principal="owner")
    config = configuration()
    preview = service.preview_configuration(
        project["id"], config, command_key="config-preview", principal="owner"
    )
    project = service.apply_configuration(
        project["id"],
        preview["preview_id"],
        expected_revision=1,
        command_key="config-apply",
        principal="owner",
    )
    return service, project, config


@pytest.mark.parametrize("entry", ["configuration", "rulebook"])
def test_same_revision_different_execution_is_rejected_at_both_write_entries(
    tmp_path: Path, repository: Path, entry: str
) -> None:
    from karajan.projects import ProjectError

    service, project, config = configured(tmp_path, repository)
    config["rulebook"]["rules"][2]["priority"] = 101
    if entry == "configuration":
        preview = service.preview_configuration(
            project["id"], config, command_key="changed-preview", principal="owner"
        )
        operation = service.apply_configuration
    else:
        preview = service.preview_rulebook(
            project["id"],
            config["rulebook"],
            expected_revision=2,
            command_key="changed-preview",
            principal="owner",
        )
        operation = service.publish_rulebook
    assert preview["can_save_draft"] is False
    assert preview["can_publish"] is False
    assert any(issue["code"] == "RULEBOOK_REVISION_CONFLICT" for issue in preview["issues"])
    with pytest.raises(ProjectError, match="RULEBOOK_REVISION_CONFLICT"):
        operation(
            project["id"],
            preview["preview_id"],
            expected_revision=2,
            command_key="changed-apply",
            principal="owner",
        )
    assert service.get(project["id"]) == project
    assert service.list_rulebook_publications(project["id"]) == []


@pytest.mark.parametrize("case", ["structure", "ambiguous", "empty-group"])
def test_draft_storage_and_publication_have_separate_permissions(
    tmp_path: Path, repository: Path, case: str
) -> None:
    from karajan.projects import ProjectError

    service, project, config = configured(tmp_path, repository)
    document = config["rulebook"]
    document["revision"] = 2
    if case == "structure":
        document["rules"][0]["script"] = "arbitrary code"
    elif case == "ambiguous":
        duplicate = copy.deepcopy(document["rules"][2])
        duplicate["id"] = "duplicate"
        document["rules"].append(duplicate)
    else:
        document["profile_groups"]["fast_qualified"] = []
    preview = service.preview_rulebook(
        project["id"], document, expected_revision=2, command_key="preview", principal="owner"
    )
    assert preview["can_save_draft"] is True
    assert preview["can_publish"] is (case == "empty-group")
    if case != "empty-group":
        with pytest.raises(ProjectError, match="RULEBOOK_NOT_PUBLISHABLE"):
            service.publish_rulebook(
                project["id"],
                preview["preview_id"],
                expected_revision=2,
                command_key="publish",
                principal="owner",
            )
        saved = service.apply_configuration(
            project["id"],
            preview["preview_id"],
            expected_revision=2,
            command_key="save-draft",
            principal="owner",
        )
        assert saved["configuration"]["status"] == "draft"
        assert service.list_rulebook_publications(project["id"]) == []
    else:
        published = service.publish_rulebook(
            project["id"],
            preview["preview_id"],
            expected_revision=2,
            command_key="publish",
            principal="owner",
        )
        assert published["state"] == "waiting_qualification"


def test_custom_matrix_is_not_evaluated_by_the_legacy_matcher(
    tmp_path: Path, repository: Path
) -> None:
    service, project, config = configured(tmp_path, repository)
    document = config["rulebook"]
    document["revision"] = 2
    document["rules"][2]["priority"] = 101
    preview = service.preview_rulebook(
        project["id"], document, expected_revision=2, command_key="preview", principal="owner"
    )
    assert not preview["issues"]
    service.publish_rulebook(
        project["id"],
        preview["preview_id"],
        expected_revision=2,
        command_key="publish",
        principal="owner",
    )
    result = service.evaluate_task(
        project["id"],
        {
            "role": "worker",
            "readiness": "ready",
            "complexity": "T1",
            "risk": "standard",
            "approved_profile_refs": config["approved_profile_refs"],
        },
    )
    assert result["reason_codes"] == ["ROUTING_SNAPSHOT_REQUIRED"]
