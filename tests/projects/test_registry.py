"""Trusted project registration through the public service and real local Git."""

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from karajan.projects import ProjectError, ProjectRegistry


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    path = tmp_path / "repositories" / "example"
    path.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(path)], check=True, capture_output=True
    )
    (path / "example.txt").write_text("original content\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "example.txt"], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            "fixture",
        ],
        check=True,
        capture_output=True,
    )
    return path


def request(repository: Path) -> dict:
    return {
        "name": "Example project",
        "repository_path": str(repository),
        "base_ref": "main",
        "target_branch": "main",
        "allowed_target_branches": ["main"],
    }


def test_registered_project_has_a_fixed_base_and_survives_service_restart(
    tmp_path: Path, repository: Path
) -> None:
    service = ProjectRegistry(tmp_path / "projects.sqlite", [repository.parent])

    created = service.create(request(repository), command_key="create-1", principal="owner")

    assert created["revision"] == 1
    assert created["name"] == "Example project"
    assert created["repository"]["root"] == str(repository.resolve())
    assert created["repository"]["base_ref"] == "main"
    assert len(created["repository"]["base_sha"]) == 40
    assert created["target_branch"] == "main"
    assert created["configuration"]["status"] == "unconfigured"
    assert created["configuration"]["dispatch_eligible"] is False
    reopened = ProjectRegistry(tmp_path / "projects.sqlite", [repository.parent])
    assert reopened.get(created["id"]) == created
    assert reopened.list() == [created]
    assert (repository / "example.txt").read_text(encoding="utf-8") == "original content\n"


@pytest.mark.parametrize(
    "variant, reason",
    [
        ("outside", "REPOSITORY_OUTSIDE_ROOTS"),
        ("unknown-base", "BASE_UNRESOLVED"),
        ("branch", "TARGET_BRANCH_NOT_ALLOWED"),
        ("not-git", "REPOSITORY_INVALID"),
    ],
)
def test_invalid_project_identity_never_creates_a_project(
    tmp_path: Path, repository: Path, variant: str, reason: str
) -> None:
    service = ProjectRegistry(tmp_path / "projects.sqlite", [repository.parent])
    document = request(repository)
    if variant == "outside":
        document["repository_path"] = str(tmp_path)
    elif variant == "unknown-base":
        document["base_ref"] = "missing-branch"
    elif variant == "branch":
        document["target_branch"] = "unapproved"
    else:
        other = repository.parent / "not-a-repository"
        other.mkdir()
        document["repository_path"] = str(other)

    with pytest.raises(ProjectError) as caught:
        service.create(document, command_key="invalid-create", principal="owner")

    assert caught.value.code == reason
    assert service.list() == []


def test_create_command_is_durable_and_concurrent_replay_returns_one_saved_result(
    tmp_path: Path, repository: Path
) -> None:
    service = ProjectRegistry(tmp_path / "projects.sqlite", [repository.parent])
    with ThreadPoolExecutor(max_workers=4) as workers:
        results = list(
            workers.map(
                lambda _: service.create(
                    request(repository), command_key="same", principal="owner"
                ),
                range(4),
            )
        )
    assert results == [results[0]] * 4
    assert len(service.list()) == 1
    reopened = ProjectRegistry(tmp_path / "projects.sqlite", [repository.parent])
    assert reopened.create(request(repository), command_key="same", principal="owner") == results[0]
    changed = {**request(repository), "name": "Changed"}
    with pytest.raises(ProjectError, match="IDEMPOTENCY_CONFLICT"):
        reopened.create(changed, command_key="same", principal="owner")
    assert len(service.list()) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"api_key": "FAKE-SECRET-value"},
        {"name": True},
        {"allowed_target_branches": []},
        {"base_ref": None},
    ],
)
def test_project_envelope_rejects_unknown_or_missing_fields_without_exposing_input(
    tmp_path: Path, repository: Path, change: dict
) -> None:
    service = ProjectRegistry(tmp_path / "projects.sqlite", [repository.parent])

    with pytest.raises(ProjectError) as caught:
        service.create({**request(repository), **change}, command_key="bad", principal="owner")

    assert caught.value.code == "PROJECT_INPUT_INVALID"
    assert "FAKE-SECRET" not in str(caught.value)
    assert service.list() == []


def test_update_requires_current_revision_but_identical_command_retry_returns_original_result(
    tmp_path: Path, repository: Path
) -> None:
    service = ProjectRegistry(tmp_path / "projects.sqlite", [repository.parent])
    created = service.create(request(repository), command_key="create", principal="owner")
    change = {key: value for key, value in request(repository).items() if key != "repository_path"}
    change["name"] = "Renamed"

    updated = service.update(
        created["id"], change, expected_revision=1, command_key="update", principal="owner"
    )

    assert updated["revision"] == 2
    assert updated["name"] == "Renamed"
    assert (
        service.update(
            created["id"], change, expected_revision=1, command_key="update", principal="owner"
        )
        == updated
    )
    with pytest.raises(ProjectError) as caught:
        service.update(
            created["id"], change, expected_revision=1, command_key="new-update", principal="owner"
        )
    assert caught.value.code == "REVISION_CONFLICT"
    assert caught.value.current_revision == 2
    assert service.get(created["id"]) == updated


def test_incomplete_configuration_can_be_previewed_as_a_draft_without_changing_project(
    tmp_path: Path, repository: Path
) -> None:
    service = ProjectRegistry(tmp_path / "projects.sqlite", [repository.parent])
    created = service.create(request(repository), command_key="create", principal="owner")

    preview = service.preview_configuration(
        created["id"],
        {"schema_version": "karajan.project-config.v1"},
        command_key="preview",
        principal="owner",
    )

    assert preview["status"] == "draft"
    assert preview["project_revision"] == 1
    assert preview["dispatch_eligible"] is False
    assert {issue["code"] for issue in preview["issues"]} >= {
        "RULEBOOK_REQUIRED",
        "RESOURCES_REQUIRED",
    }
    assert len(preview["configuration_digest"]) == 64
    assert service.get(created["id"]) == created
    assert "secret" not in json.dumps(preview)


def test_apply_binds_saved_preview_and_revision_and_replays_the_exact_original_result(
    tmp_path: Path, repository: Path
) -> None:
    service = ProjectRegistry(tmp_path / "projects.sqlite", [repository.parent])
    created = service.create(request(repository), command_key="create", principal="owner")
    configuration = {"schema_version": "karajan.project-config.v1"}
    preview = service.preview_configuration(
        created["id"], configuration, command_key="preview", principal="owner"
    )
    configuration["rulebook"] = {"unreviewed": True}

    applied = service.apply_configuration(
        created["id"],
        preview["preview_id"],
        expected_revision=1,
        command_key="apply",
        principal="owner",
    )

    assert applied["revision"] == 2
    assert applied["configuration"]["revision"] == 1
    assert applied["configuration"]["status"] == "draft"
    assert applied["configuration"]["digest"] == preview["configuration_digest"]
    reopened = ProjectRegistry(tmp_path / "projects.sqlite", [repository.parent])
    assert (
        reopened.apply_configuration(
            created["id"],
            preview["preview_id"],
            expected_revision=1,
            command_key="apply",
            principal="owner",
        )
        == applied
    )
    with pytest.raises(ProjectError, match="PREVIEW_STALE"):
        reopened.apply_configuration(
            created["id"],
            preview["preview_id"],
            expected_revision=2,
            command_key="apply-again",
            principal="owner",
        )
    other = service.create(request(repository), command_key="create-other", principal="owner")
    with pytest.raises(ProjectError, match="PREVIEW_NOT_FOUND"):
        service.apply_configuration(
            other["id"],
            preview["preview_id"],
            expected_revision=1,
            command_key="cross-project",
            principal="owner",
        )


def configuration() -> dict:
    return json.loads(
        (
            Path(__file__).resolve().parents[2] / "examples/projects/offline-configuration.json"
        ).read_text(encoding="utf-8")
    )


@pytest.mark.parametrize("field", ["planning_budget_ref", "run_budget_ref"])
def test_required_platform_budget_is_not_replaced_by_service_quota(
    tmp_path: Path, repository: Path, field: str
) -> None:
    service = ProjectRegistry(tmp_path / "projects.sqlite", [repository.parent])
    created = service.create(request(repository), command_key="create", principal="owner")
    document = configuration()
    document["rulebook"]["resource_policy"][field] = None

    preview = service.preview_configuration(
        created["id"], document, command_key="preview", principal="owner"
    )

    assert preview["status"] == "draft"
    assert {"code": "BUDGET_REQUIRED", "path": "rulebook.resource_policy." + field} in preview[
        "issues"
    ]
    assert preview["dispatch_eligible"] is False


@pytest.mark.parametrize(
    "change",
    [
        {"currency_limits": {"USD": None}},
        {"currency_limits": {"usd": "2"}},
        {"currency_limits": {"USD": "NaN"}},
        {"max_total_attempts": None},
        {"max_duration_seconds": 0},
    ],
)
def test_platform_budgets_require_native_currencies_and_finite_explicit_limits(
    tmp_path: Path, repository: Path, change: dict
) -> None:
    service = ProjectRegistry(tmp_path / "projects.sqlite", [repository.parent])
    created = service.create(request(repository), command_key="create", principal="owner")
    document = configuration()
    document["resources"]["budgets"][0].update(change)

    preview = service.preview_configuration(
        created["id"], document, command_key="preview", principal="owner"
    )

    assert preview["status"] == "draft"
    assert {"code": "BUDGET_INVALID", "path": "resources.budgets.0"} in preview["issues"]


@pytest.mark.parametrize(
    "field",
    [
        "enabled",
        "max_local_active_attempts",
        "max_attempt_duration_seconds",
        "observation_max_age_seconds",
        "cooldown_seconds",
    ],
)
def test_unknown_subscription_observation_requires_every_finite_conservative_setting(
    tmp_path: Path, repository: Path, field: str
) -> None:
    service = ProjectRegistry(tmp_path / "projects.sqlite", [repository.parent])
    created = service.create(request(repository), command_key="create", principal="owner")
    document = configuration()
    document["resources"]["capacity_policies"][0]["conservative_mode"].pop(field)

    preview = service.preview_configuration(
        created["id"], document, command_key="preview", principal="owner"
    )

    assert preview["status"] == "draft"
    assert {
        "code": "UNKNOWN_QUOTA_UNBOUNDED",
        "path": "resources.capacity_policies.0.conservative_mode",
    } in preview["issues"]


def test_preview_command_is_idempotent_and_rejects_changed_content(
    tmp_path: Path, repository: Path
) -> None:
    service = ProjectRegistry(tmp_path / "projects.sqlite", [repository.parent])
    created = service.create(request(repository), command_key="create", principal="owner")
    document = configuration()
    first = service.preview_configuration(
        created["id"], document, command_key="preview", principal="owner"
    )
    assert (
        service.preview_configuration(
            created["id"], document, command_key="preview", principal="owner"
        )
        == first
    )
    document["resources"]["budgets"] = []
    with pytest.raises(ProjectError, match="IDEMPOTENCY_CONFLICT"):
        service.preview_configuration(
            created["id"], document, command_key="preview", principal="owner"
        )


def test_raw_credentials_in_configuration_are_reported_without_persisting_or_exporting_them(
    tmp_path: Path, repository: Path
) -> None:
    database = tmp_path / "projects.sqlite"
    service = ProjectRegistry(database, [repository.parent])
    created = service.create(request(repository), command_key="create", principal="owner")
    document = configuration()
    document["resources"]["accounts"][0]["api_key"] = "FAKE-CREDENTIAL-MUST-NOT-BE-STORED"

    preview = service.preview_configuration(
        created["id"], document, command_key="preview", principal="owner"
    )

    assert preview["status"] == "draft"
    assert {"code": "CREDENTIAL_VALUE_FORBIDDEN", "path": "configuration"} in preview["issues"]
    assert "FAKE-CREDENTIAL" not in json.dumps(preview)
    assert b"FAKE-CREDENTIAL" not in database.read_bytes()


@pytest.mark.parametrize(
    "change",
    [
        {"execute_now": True},
        {"schema_version": "unknown-version"},
        {"resources": {"profiles": "not-an-array"}},
    ],
)
def test_unknown_configuration_shapes_produce_a_bounded_draft_issue(
    tmp_path: Path, repository: Path, change: dict
) -> None:
    service = ProjectRegistry(tmp_path / "projects.sqlite", [repository.parent])
    created = service.create(request(repository), command_key="create", principal="owner")

    preview = service.preview_configuration(
        created["id"], {**configuration(), **change}, command_key="preview", principal="owner"
    )

    assert preview["status"] == "draft"
    assert {"code": "CONFIGURATION_SCHEMA_INVALID", "path": "configuration"} in preview["issues"]


@pytest.mark.parametrize("variant", ["fallback", "t0-worker", "critical-downgrade"])
def test_fixed_rulebook_hard_conditions_cannot_be_overridden_in_configuration(
    tmp_path: Path, repository: Path, variant: str
) -> None:
    service = ProjectRegistry(tmp_path / "projects.sqlite", [repository.parent])
    created = service.create(request(repository), command_key="create", principal="owner")
    document = configuration()
    if variant == "fallback":
        document["rulebook"]["global_constraints"]["no_silent_model_or_billing_fallback"] = False
    elif variant == "t0-worker":
        document["rulebook"]["rules"][2]["when"]["effective_class"] = "T0"
    else:
        document["rulebook"]["rules"][4]["capabilities_all"] = ["bounded_code_edit"]

    preview = service.preview_configuration(
        created["id"], document, command_key="preview", principal="owner"
    )

    assert preview["status"] == "draft"
    assert {"code": "RULEBOOK_HARD_CONSTRAINT_INVALID", "path": "rulebook"} in preview["issues"]


@pytest.mark.parametrize(
    "variant",
    ["channel-account", "pool-account", "profile-channel", "duplicate-pool", "missing-policy"],
)
def test_resource_identity_and_shared_account_relationships_must_resolve(
    tmp_path: Path, repository: Path, variant: str
) -> None:
    service = ProjectRegistry(tmp_path / "projects.sqlite", [repository.parent])
    created = service.create(request(repository), command_key="create", principal="owner")
    document = configuration()
    resources = document["resources"]
    if variant == "channel-account":
        resources["channels"][0]["account_id"] = "other"
    elif variant == "pool-account":
        resources["quota_pools"][0]["account_id"] = "other"
    elif variant == "profile-channel":
        resources["profiles"][0]["profile"]["binding"]["channel_id"] = "other"
    elif variant == "duplicate-pool":
        resources["quota_pools"].append(resources["quota_pools"][0])
    else:
        resources["capacity_policies"] = []

    preview = service.preview_configuration(
        created["id"], document, command_key="preview", principal="owner"
    )

    assert preview["status"] == "draft"
    assert any(issue["code"] == "RESOURCE_REFERENCE_INVALID" for issue in preview["issues"])


@pytest.mark.parametrize(
    "variant", ["not-run", "digest", "runtime", "missing-reference", "unapproved", "weaker-class"]
)
def test_candidate_requires_current_evidence_approved_membership_and_task_class(
    tmp_path: Path, repository: Path, variant: str
) -> None:
    service = ProjectRegistry(tmp_path / "projects.sqlite", [repository.parent])
    created = service.create(request(repository), command_key="create", principal="owner")
    document = configuration()
    profile = document["resources"]["profiles"][0]
    evidence = profile["capability_evidence"][0]
    if variant == "not-run":
        evidence["status"] = "not_run"
    elif variant == "digest":
        evidence["profile_digest"] = "d" * 64
    elif variant == "runtime":
        evidence["runtime_version"] = "different"
    elif variant == "missing-reference":
        evidence["evidence_ref"] = None
    elif variant == "weaker-class":
        profile["max_class"] = "T1"
    else:
        document["approved_profile_refs"] = []

    preview = service.preview_configuration(
        created["id"], document, command_key="preview", principal="owner"
    )

    assert preview["status"] == "draft"
    assert any(
        issue["code"]
        in {"CAPABILITY_NOT_PASSED", "PROFILE_NOT_APPROVED", "PROFILE_CLASS_INSUFFICIENT"}
        for issue in preview["issues"]
    )


@pytest.mark.parametrize("variant", ["destination", "isolation", "authentication"])
def test_profile_permission_requirements_need_explicit_correlated_registration(
    tmp_path: Path, repository: Path, variant: str
) -> None:
    service = ProjectRegistry(tmp_path / "projects.sqlite", [repository.parent])
    created = service.create(request(repository), command_key="create", principal="owner")
    document = configuration()
    if variant == "destination":
        document["resources"]["channels"][0]["approved_data_destination"] = False
    elif variant == "isolation":
        document["resources"]["profiles"][0]["required_isolation"] = "attempt_isolated"
    else:
        document["resources"]["accounts"][0]["secret_ref"] = None

    preview = service.preview_configuration(
        created["id"], document, command_key="preview", principal="owner"
    )

    assert preview["status"] == "draft"
    assert any(issue["code"] == "PROFILE_PERMISSION_UNVERIFIED" for issue in preview["issues"])


def test_repository_containing_control_storage_cannot_be_registered(repository: Path) -> None:
    control_directory = repository / "control-state"
    control_directory.mkdir()
    service = ProjectRegistry(control_directory / "projects.sqlite", [repository.parent])

    with pytest.raises(ProjectError, match="REPOSITORY_CONTAINS_CONTROL_STATE"):
        service.create(request(repository), command_key="create", principal="owner")

    assert service.list() == []


@pytest.mark.parametrize(
    "readiness,risk,effective,reason",
    [("T0", "standard", "T1", "TASK_NOT_READY"), ("ready", "critical", "T3", None)],
)
def test_task_preview_uses_trusted_risk_floor_and_never_dispatches_t0(
    tmp_path: Path, repository: Path, readiness: str, risk: str, effective: str, reason: str | None
) -> None:
    service = ProjectRegistry(tmp_path / "projects.sqlite", [repository.parent])
    created = service.create(request(repository), command_key="create", principal="owner")
    document = configuration()
    preview = service.preview_configuration(
        created["id"], document, command_key="preview", principal="owner"
    )
    assert preview["status"] == "offline_valid"
    assert preview["issues"] == []
    applied = service.apply_configuration(
        created["id"],
        preview["preview_id"],
        expected_revision=1,
        command_key="apply",
        principal="owner",
    )
    task = {
        "role": "worker",
        "readiness": readiness,
        "complexity": "T1",
        "risk": risk,
        "approved_profile_refs": document["approved_profile_refs"],
    }

    outcome = service.evaluate_task(created["id"], task)

    assert outcome["effective_class"] == effective
    assert outcome["dispatch_eligible"] is False
    assert service.evaluate_task(created["id"], task) == outcome
    assert service.get(created["id"]) == applied
    if reason:
        assert outcome["reason_codes"] == [reason]
        assert outcome["qualified_candidates"] == []
    else:
        assert outcome["rule_id"] == "critical-worker"
        assert outcome["qualified_candidates"] == document["approved_profile_refs"]


@pytest.mark.parametrize(
    "variant",
    ["unapproved", "extra-capability", "extra-stale-capability", "author-attempt", "author-family"],
)
def test_task_specific_hard_requirements_exclude_candidates_without_a_fallback(
    tmp_path: Path, repository: Path, variant: str
) -> None:
    service = ProjectRegistry(tmp_path / "projects.sqlite", [repository.parent])
    created = service.create(request(repository), command_key="create", principal="owner")
    document = configuration()
    if variant == "extra-stale-capability":
        document["resources"]["profiles"][0]["capability_evidence"].append(
            {
                "capability": "unproven-required-capability",
                "status": "passed",
                "profile_digest": "d" * 64,
                "runtime_version": "1",
                "evidence_ref": "fixture:stale",
                "provenance": "fixture",
            }
        )
    preview = service.preview_configuration(
        created["id"], document, command_key="preview", principal="owner"
    )
    service.apply_configuration(
        created["id"],
        preview["preview_id"],
        expected_revision=1,
        command_key="apply",
        principal="owner",
    )
    task = {
        "role": "reviewer",
        "readiness": "ready",
        "complexity": "T1",
        "risk": "critical",
        "approved_profile_refs": document["approved_profile_refs"],
    }
    if variant == "unapproved":
        task["approved_profile_refs"] = []
    elif variant in {"extra-capability", "extra-stale-capability"}:
        task["required_capabilities"] = ["unproven-required-capability"]
    elif variant == "author-attempt":
        task["author_profile_refs"] = document["approved_profile_refs"]
    else:
        task["author_model_families"] = ["fixture-family"]

    outcome = service.evaluate_task(created["id"], task)

    assert outcome["qualified_candidates"] == []
    assert outcome["reason_codes"] == ["NO_APPROVED_CANDIDATE"]
    assert outcome["effective_class"] == "T3"


def test_a_malformed_budget_reference_is_a_draft_issue_not_an_exception(
    tmp_path: Path, repository: Path
) -> None:
    service = ProjectRegistry(tmp_path / "projects.sqlite", [repository.parent])
    created = service.create(request(repository), command_key="create", principal="owner")
    document = configuration()
    document["rulebook"]["resource_policy"]["run_budget_ref"] = ["run"]

    preview = service.preview_configuration(
        created["id"], document, command_key="preview", principal="owner"
    )

    assert preview["status"] == "draft"
    assert preview["issues"][0]["code"] == "RULEBOOK_HARD_CONSTRAINT_INVALID"


@pytest.mark.parametrize("key,principal", [("", "owner"), ("x", ""), (True, "owner")])
def test_mutation_identity_is_explicit_and_strict(
    tmp_path: Path, repository: Path, key: object, principal: str
) -> None:
    service = ProjectRegistry(tmp_path / "projects.sqlite", [repository.parent])

    with pytest.raises(ProjectError, match="COMMAND_IDENTITY_INVALID"):
        service.create(request(repository), command_key=key, principal=principal)

    assert service.list() == []


def test_preview_records_policy_revision_as_offline_evidence(
    tmp_path: Path, repository: Path
) -> None:
    service = ProjectRegistry(tmp_path / "projects.sqlite", [repository.parent])
    created = service.create(request(repository), command_key="create", principal="owner")

    preview = service.preview_configuration(
        created["id"], configuration(), command_key="preview", principal="owner"
    )

    assert preview["qualification_scope"] == "offline_configuration"
    assert preview["live_qualified"] is False
    assert preview["validation"]["validator_revision"] == "karajan.m1-fixed.v1"
    assert preview["validation"]["rulebook_id"] == "personal-code-delivery"
    assert preview["validation"]["rulebook_revision"] == 1


def test_non_json_configuration_fails_with_a_stable_error(tmp_path: Path, repository: Path) -> None:
    service = ProjectRegistry(tmp_path / "projects.sqlite", [repository.parent])
    created = service.create(request(repository), command_key="create", principal="owner")
    document = configuration()
    document["rulebook"]["revision"] = float("nan")

    with pytest.raises(ProjectError, match="INPUT_NOT_JSON"):
        service.preview_configuration(
            created["id"], document, command_key="preview", principal="owner"
        )

    assert service.get(created["id"]) == created


@pytest.mark.parametrize(
    "document", [{}, {"schema_version": "karajan.project-config.v1", "api_key": "FAKE-NOT-STORED"}]
)
def test_discarded_configuration_cannot_be_applied_as_if_it_were_saved(
    tmp_path: Path, repository: Path, document: dict
) -> None:
    service = ProjectRegistry(tmp_path / "projects.sqlite", [repository.parent])
    created = service.create(request(repository), command_key="create", principal="owner")
    preview = service.preview_configuration(
        created["id"], document, command_key="preview", principal="owner"
    )

    assert preview["can_apply"] is False
    with pytest.raises(ProjectError, match="CONFIGURATION_NOT_STORABLE"):
        service.apply_configuration(
            created["id"],
            preview["preview_id"],
            expected_revision=1,
            command_key="apply",
            principal="owner",
        )
    assert service.get(created["id"]) == created


def test_current_configuration_export_returns_only_the_applied_storable_content(
    tmp_path: Path, repository: Path
) -> None:
    service = ProjectRegistry(tmp_path / "projects.sqlite", [repository.parent])
    created = service.create(request(repository), command_key="create", principal="owner")
    assert service.get_configuration(created["id"])["configuration"] is None
    document = {"schema_version": "karajan.project-config.v1"}
    preview = service.preview_configuration(
        created["id"], document, command_key="preview", principal="owner"
    )
    assert preview["can_apply"] is True
    service.apply_configuration(
        created["id"],
        preview["preview_id"],
        expected_revision=1,
        command_key="apply",
        principal="owner",
    )
    rejected = service.preview_configuration(
        created["id"],
        {**document, "password": "FAKE-PRIVATE-PASSWORD"},
        command_key="secret-preview",
        principal="owner",
    )
    assert rejected["can_apply"] is False

    exported = service.get_configuration(created["id"])

    assert exported == {
        "project_id": created["id"],
        "project_revision": 2,
        "configuration_revision": 1,
        "configuration": document,
    }
    assert "FAKE-PRIVATE" not in json.dumps(exported)


def test_standard_review_may_reuse_a_profile_but_requires_a_fresh_non_author_attempt(
    tmp_path: Path, repository: Path
) -> None:
    service = ProjectRegistry(tmp_path / "projects.sqlite", [repository.parent])
    created = service.create(request(repository), command_key="create", principal="owner")
    document = configuration()
    preview = service.preview_configuration(
        created["id"], document, command_key="preview", principal="owner"
    )
    service.apply_configuration(
        created["id"],
        preview["preview_id"],
        expected_revision=1,
        command_key="apply",
        principal="owner",
    )

    outcome = service.evaluate_task(
        created["id"],
        {
            "role": "reviewer",
            "readiness": "ready",
            "complexity": "T1",
            "risk": "standard",
            "approved_profile_refs": document["approved_profile_refs"],
            "author_profile_refs": document["approved_profile_refs"],
        },
    )

    assert outcome["qualified_candidates"] == document["approved_profile_refs"]
    assert outcome["required_independence"] == ["fresh_context", "non_author_attempt"]
    assert outcome["dispatch_eligible"] is False


@pytest.mark.parametrize(
    "settings",
    [
        {"env": {"OPENAI_API_KEY": "FAKE-REVIEW-CANARY"}},
        {"OPENAI_API_KEY": "FAKE-REVIEW-CANARY"},
        {"headers": {"X-Api-Key": "FAKE-REVIEW-CANARY"}},
    ],
)
def test_native_authentication_payload_cannot_enter_persistent_configuration(
    tmp_path: Path, repository: Path, settings: dict
) -> None:
    database = tmp_path / "projects.sqlite"
    service = ProjectRegistry(database, [repository.parent])
    created = service.create(request(repository), command_key="create", principal="owner")
    document = configuration()
    document["resources"]["profiles"][0]["profile"]["binding"]["native_settings"] = settings

    preview = service.preview_configuration(
        created["id"], document, command_key="preview", principal="owner"
    )

    assert preview["can_apply"] is False
    assert preview["issues"] == [{"code": "CREDENTIAL_VALUE_FORBIDDEN", "path": "configuration"}]
    with pytest.raises(ProjectError, match="CONFIGURATION_NOT_STORABLE"):
        service.apply_configuration(
            created["id"],
            preview["preview_id"],
            expected_revision=1,
            command_key="apply",
            principal="owner",
        )
    assert service.get_configuration(created["id"])["configuration"] is None
    assert "FAKE-REVIEW-CANARY" not in json.dumps(preview)
    assert b"FAKE-REVIEW-CANARY" not in database.read_bytes()


@pytest.mark.parametrize("operation", ["get", "get_configuration", "update", "preview", "apply"])
def test_public_object_identifiers_reject_unpaired_unicode_before_storage(
    tmp_path: Path, repository: Path, operation: str
) -> None:
    service = ProjectRegistry(tmp_path / "projects.sqlite", [repository.parent])
    created = service.create(request(repository), command_key="create", principal="owner")
    invalid = "\ud800"

    with pytest.raises(ProjectError, match="IDENTIFIER_INVALID"):
        if operation in {"get", "get_configuration"}:
            getattr(service, operation)(invalid)
        elif operation == "update":
            service.update(
                invalid,
                {
                    key: value
                    for key, value in request(repository).items()
                    if key != "repository_path"
                },
                expected_revision=1,
                command_key="update",
                principal="owner",
            )
        elif operation == "preview":
            service.preview_configuration(
                invalid, configuration(), command_key="preview", principal="owner"
            )
        else:
            service.apply_configuration(
                created["id"], invalid, expected_revision=1, command_key="apply", principal="owner"
            )

    assert service.get(created["id"]) == created
