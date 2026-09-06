"""Qualification is produced by the public local runner, not imported passed JSON."""

import copy
import json
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from karajan.projects import ProjectError, ProjectRegistry
from karajan.projects.qualification import ProfileQualificationStore, QualificationError


@pytest.fixture
def case(tmp_path: Path) -> dict:
    root = tmp_path / "fixture"
    repository = root / "repository"
    repository.mkdir(parents=True)
    (repository / "original.txt").write_text("baseline\n", encoding="utf-8")
    for args in (
        ["init", "--initial-branch=main", str(repository)],
        ["-C", str(repository), "add", "original.txt"],
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
        subprocess.run(["git", *args], check=True, capture_output=True)
    clock = [1000.0]
    projects = ProjectRegistry(tmp_path / "projects.sqlite", [root], clock=lambda: clock[0])
    project = projects.create(
        {
            "name": "Qualification fixture",
            "repository_path": str(repository),
            "base_ref": "main",
            "target_branch": "main",
            "allowed_target_branches": ["main"],
        },
        command_key="create",
        principal="owner",
    )
    configuration = json.loads(
        (Path(__file__).parents[2] / "examples/projects/offline-configuration.json").read_text()
    )
    preview = projects.preview_configuration(
        project["id"], configuration, command_key="preview", principal="owner"
    )
    projects.apply_configuration(
        project["id"],
        preview["preview_id"],
        expected_revision=1,
        command_key="apply",
        principal="owner",
    )
    return {
        "projects": projects,
        "project_id": project["id"],
        "root": root,
        "repository": repository,
        "clock": clock,
        "configuration": configuration,
        "registration": configuration["resources"]["profiles"][0],
    }


def qualify(case: dict, key: str = "qualify") -> dict:
    return ProfileQualificationStore(
        case["projects"], clock=lambda: case["clock"][0]
    ).qualify_local_fixture(
        case["project_id"],
        {"id": "fixture-profile", "revision": 1},
        principal="owner",
        command_key=key,
        fixture_root=case["root"],
        validity_seconds=60,
    )


def facts(case: dict, **kwargs: object) -> dict:
    return ProfileQualificationStore(
        case["projects"], clock=lambda: case["clock"][0]
    ).facts_for_profile(
        case["project_id"],
        case["registration"],
        principal="owner",
        scope="local_fixture",
        fixture_root=case["root"],
        **kwargs,
    )


def test_public_qualification_persists_real_process_observation_and_revocation(case: dict) -> None:
    observed = qualify(case)
    assert observed["status"] == "passed"
    assert observed["qualification_scope"] == "local_fixture"
    assert observed["runtime_tools_status"] == "not_run"
    assert observed["live_qualified"] is False
    assert observed["observation"]["checks"] == {
        "write": True,
        "check": True,
        "review": True,
        "source_unchanged": True,
    }
    assert len(observed["observation"]["processes"]) == 3
    assert all(row["exit_code"] == 0 for row in observed["observation"]["processes"])
    persisted = facts(case)
    assert persisted["facts"]["provenance"] == "fixture"
    assert persisted["facts"]["context_tokens"] is None
    assert persisted["facts"]["roles"] == ["worker", "reviewer"]
    assert qualify(case) == observed
    assert (case["repository"] / "original.txt").read_text() == "baseline\n"
    store = ProfileQualificationStore(case["projects"], clock=lambda: case["clock"][0])
    store.revoke(case["project_id"], observed["id"], principal="owner", reason="owner_suspended")
    with pytest.raises(QualificationError, match="QUALIFICATION_REVOKED"):
        facts(case)
    assert store.get(case["project_id"], observed["id"], principal="owner")["record"] == observed


def test_expiry_and_frozen_identity_mismatch_fail_closed(case: dict) -> None:
    qualify(case)
    wrong = copy.deepcopy(case)
    wrong["registration"]["profile"]["binding"]["auth_mode"] = "other"
    with pytest.raises(QualificationError, match="PROFILE_IDENTITY_MISMATCH"):
        facts(wrong)
    case["clock"][0] = 1060.0
    with pytest.raises(QualificationError, match="QUALIFICATION_EXPIRED"):
        facts(case)


def apply(case: dict, configuration: dict) -> None:
    projects = case["projects"]
    preview = projects.preview_configuration(
        case["project_id"], configuration, command_key="changed-preview", principal="owner"
    )
    projects.apply_configuration(
        case["project_id"],
        preview["preview_id"],
        expected_revision=projects.get(case["project_id"])["revision"],
        command_key="changed-apply",
        principal="owner",
    )


@pytest.mark.parametrize(
    "axis",
    [
        "enabled",
        "account_provider",
        "channel_approval",
        "family",
        "permissions",
        "model",
        "runtime",
        "auth_ref",
        "billing",
    ],
)
def test_current_identity_changes_do_not_reuse_old_observation(case: dict, axis: str) -> None:
    observed = qualify(case)
    configuration = copy.deepcopy(case["configuration"])
    registered = configuration["resources"]["profiles"][0]
    binding = registered["profile"]["binding"]
    if axis == "enabled":
        registered["enabled"] = False
    elif axis == "account_provider":
        configuration["resources"]["accounts"][0]["provider_id"] = "other-provider"
    elif axis == "channel_approval":
        configuration["resources"]["channels"][0]["approved_data_destination"] = False
    elif axis == "family":
        registered["model_family"] = "other-family"
    elif axis == "permissions":
        registered["profile"]["required_permissions"] = ["arbitrary-shell"]
    elif axis in ("model", "runtime"):
        binding["model_id" if axis == "model" else "runtime_version"] = "other"
    elif axis == "auth_ref":
        registered["profile"]["auth_ref"] = "secret:other-reference"
        configuration["resources"]["accounts"][0]["secret_ref"] = "secret:other-reference"
    else:
        binding["billing_path"] = "api_cash"
        configuration["resources"]["channels"][0]["billing_path"] = "api_cash"
    apply(case, configuration)
    with pytest.raises(QualificationError):
        facts(case)
    assert (
        ProfileQualificationStore(case["projects"]).get(
            case["project_id"], observed["id"], principal="owner"
        )["record"]
        == observed
    )


def test_missing_observation_and_runtime_tools_are_never_promoted(case: dict) -> None:
    with pytest.raises(QualificationError, match="PROFILE_FACTS_MISSING"):
        facts(case)
    # Existing registered capabilities say passed; that does not run qualification.
    assert case["registration"]["capability_evidence"][0]["status"] == "passed"
    qualify(case)
    observed = facts(case)
    assert {e["capability"] for e in observed["capability_evidence"]} == {
        "fixed_fixture_write",
        "fixed_fixture_check",
        "fixed_fixture_review",
    }
    assert observed["facts"]["context_tokens"] is None
    assert observed["facts"]["budget_enforcement"] == "unknown"
    store = ProfileQualificationStore(case["projects"])
    with pytest.raises(QualificationError, match="RUNTIME_TOOLS_NOT_QUALIFIED"):
        store.facts_for_profile(
            case["project_id"], case["registration"], principal="owner", scope="runtime_tools"
        )


def test_unknown_source_is_rejected_before_any_process_or_start(case: dict) -> None:
    configuration = copy.deepcopy(case["configuration"])
    configuration["resources"]["profiles"][0]["profile"]["binding"]["runtime_kind"] = "opencode"
    apply(case, configuration)
    with pytest.raises(QualificationError, match="QUALIFICATION_SOURCE_UNSUPPORTED"):
        qualify(case)
    assert list(case["root"].glob("qualification-*")) == []


def test_owner_and_idempotency_are_enforced(case: dict) -> None:
    store = ProfileQualificationStore(case["projects"], clock=lambda: case["clock"][0])
    with pytest.raises(ProjectError, match="USER_DECISION_REQUIRED"):
        store.qualify_local_fixture(
            case["project_id"],
            {"id": "fixture-profile", "revision": 1},
            principal="other",
            command_key="qualify",
            fixture_root=case["root"],
            validity_seconds=60,
        )
    observed = qualify(case)
    with pytest.raises(QualificationError, match="IDEMPOTENCY_CONFLICT"):
        store.qualify_local_fixture(
            case["project_id"],
            {"id": "fixture-profile", "revision": 1},
            principal="owner",
            command_key="qualify",
            fixture_root=case["root"],
            validity_seconds=61,
        )
    with pytest.raises(ProjectError, match="USER_DECISION_REQUIRED"):
        store.revoke(case["project_id"], observed["id"], principal="other", reason="deny")
    assert len(list(case["root"].glob("qualification-*"))) == 1


def test_later_failed_observation_blocks_previous_pass(
    case: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed = qualify(case)
    real_run = subprocess.run

    def fail_check(args: list, **kwargs: object) -> subprocess.CompletedProcess:
        if "check" in args:
            return subprocess.CompletedProcess(args, 1, b"", b"")
        return real_run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fail_check)
    failed = qualify(case, "failed-check")
    assert failed["status"] == "failed"
    assert failed["observation"]["checks"]["check"] is False
    with pytest.raises(QualificationError, match="QUALIFICATION_NOT_PASSED"):
        facts(case)
    assert (
        ProfileQualificationStore(case["projects"]).get(
            case["project_id"], observed["id"], principal="owner"
        )["record"]
        == observed
    )


def test_clock_rollback_and_execution_path_changes_reject(case: dict, tmp_path: Path) -> None:
    qualify(case)
    case["clock"][0] = 999.0
    with pytest.raises(QualificationError, match="QUALIFICATION_EXPIRED"):
        facts(case)
    case["clock"][0] = 1001.0
    other = tmp_path / "other-fixture"
    other.mkdir()
    with pytest.raises(QualificationError, match="QUALIFICATION_RUNTIME_MISMATCH"):
        ProfileQualificationStore(
            case["projects"], clock=lambda: case["clock"][0]
        ).facts_for_profile(
            case["project_id"],
            case["registration"],
            principal="owner",
            scope="local_fixture",
            fixture_root=other,
        )


def test_guard_returns_current_catalog_and_stable_profile_reasons(case: dict) -> None:
    qualify(case)
    store = ProfileQualificationStore(case["projects"], clock=lambda: case["clock"][0])
    with store.routing_facts_guard(
        case["project_id"],
        [case["registration"]],
        principal="owner",
        scope="local_fixture",
        fixture_root=case["root"],
    ) as view:
        assert view["catalog"]["project_id"] == case["project_id"]
        assert view["activation_allowed"] is False
        assert view["profiles"][0]["reason_codes"] == []
        assert view["profiles"][0]["qualification"]["facts"]["context_tokens"] is None
    with store.routing_facts_guard(
        case["project_id"], [case["registration"]], principal="owner"
    ) as view:
        assert view["profiles"] == [
            {
                "profile": {"id": "fixture-profile", "revision": 1},
                "qualification": None,
                "reason_codes": ["RUNTIME_TOOLS_NOT_QUALIFIED"],
            }
        ]


def test_guard_blocks_concurrent_revocation_until_admission_section_exits(case: dict) -> None:
    observed = qualify(case)
    store = ProfileQualificationStore(case["projects"], clock=lambda: case["clock"][0])
    started = threading.Event()

    def revoke() -> dict:
        started.set()
        return store.revoke(case["project_id"], observed["id"], principal="owner", reason="stop")

    with ThreadPoolExecutor(max_workers=1) as pool:
        with store.routing_facts_guard(
            case["project_id"],
            [case["registration"]],
            principal="owner",
            scope="local_fixture",
            fixture_root=case["root"],
        ) as view:
            future = pool.submit(revoke)
            assert started.wait(2)
            with pytest.raises(TimeoutError):
                future.result(timeout=0.1)
            assert view["profiles"][0]["reason_codes"] == []
        assert future.result(timeout=3)["reason"] == "stop"
    with store.routing_facts_guard(
        case["project_id"],
        [case["registration"]],
        principal="owner",
        scope="local_fixture",
        fixture_root=case["root"],
    ) as view:
        assert view["profiles"][0]["qualification"] is None
        assert view["profiles"][0]["reason_codes"] == ["QUALIFICATION_REVOKED"]


def test_same_command_in_progress_never_runs_second_process_set(
    case: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = threading.Event()
    resume = threading.Event()
    real_observe = ProfileQualificationStore._observe

    def pause_observation(
        self: ProfileQualificationStore, root: Path, observation_id: str, runtime: dict
    ) -> dict:
        started.set()
        assert resume.wait(3)
        return real_observe(self, root, observation_id, runtime)

    monkeypatch.setattr(ProfileQualificationStore, "_observe", pause_observation)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(qualify, case)
        try:
            assert started.wait(2)
            with pytest.raises(QualificationError, match="QUALIFICATION_IN_PROGRESS_OR_UNKNOWN"):
                qualify(case)
            with pytest.raises(QualificationError, match="QUALIFICATION_IN_PROGRESS_OR_UNKNOWN"):
                facts(case)
        finally:
            resume.set()
        assert future.result(timeout=3)["status"] == "passed"
    assert len(list(case["root"].glob("qualification-*"))) == 1
