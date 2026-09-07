"""Explicit v2 controller configuration; history and construction never run a provider."""

from dataclasses import replace

import pytest
from karajan.orchestration.check_services_factory import (
    BOOTSTRAP_NAME,
    CheckSettings,
    _read_bootstrap,
    open_check_services,
    write_check_bootstrap,
)
from karajan.orchestration.go_task_runtime import GoTaskCredentialSource
from karajan.orchestration.qualification_services import (
    GoQualificationSettings,
    open_go_qualification_store,
)
from karajan.projects import ProjectRegistry
from karajan.projects.go_reviewer_suite import FixedGoReviewerSuite
from karajan.projects.go_suite import FixedGoSuite
from karajan.projects.qualification import QualificationError
from karajan.runs import RunError
from test_check_services_factory import (
    case,
    check_deployment,
    go_deployment,
    prepared,
    private_fixture_directories,
    projected,
    ready,
    reservation,
    settings,
)
from test_go_context import artifacts
from test_readonly_reviewer_qualification_store import qualify, reviewer_case

__all__ = [
    "case",
    "check_deployment",
    "go_deployment",
    "prepared",
    "private_fixture_directories",
    "projected",
    "ready",
    "reservation",
    "settings",
    "artifacts",
    "reviewer_case",
]


@pytest.fixture
def qualification_settings(tmp_path):
    return GoQualificationSettings(
        tmp_path / "readonly-runtime",
        tmp_path / "readonly-tokenizer",
        tmp_path / "readonly-journal",
        tmp_path / "worker-qualification",
        tmp_path / "reviewer-qualification",
        tmp_path / "readonly-credentials",
        (GoTaskCredentialSource("project", "secret:go", "local-key", tmp_path / "private.key"),),
    )


def test_v1_document_is_unchanged_and_v2_is_explicit(settings, qualification_settings):
    legacy = settings.document()
    assert legacy["schema_version"] == "karajan.candidate-check-bootstrap.v1"
    assert "go_qualification_source" not in legacy
    current = replace(settings, go_qualification_source=qualification_settings)
    document = current.document()
    assert document["schema_version"] == "karajan.candidate-check-bootstrap.v2"
    assert CheckSettings.from_document(document) == current
    assert CheckSettings.from_document(legacy).document() == legacy
    write_check_bootstrap(current)
    assert _read_bootstrap(current.control_directory)[0] == current
    assert not qualification_settings.journal_path.exists()
    assert not qualification_settings.credential_private_directory.exists()


@pytest.mark.parametrize(
    "field", ["endpoint", "client_factory", "prompt", "credential", "fixture", "report"]
)
def test_current_source_rejects_model_and_transport_payload(qualification_settings, field):
    with pytest.raises(RunError, match="QUALIFICATION_BOOTSTRAP_INVALID"):
        GoQualificationSettings.from_document(qualification_settings.document() | {field: "no"})


def test_current_source_rejects_duplicate_auth_identity_and_noncanonical_path(
    qualification_settings,
):
    with pytest.raises(RunError, match="QUALIFICATION_BOOTSTRAP_INVALID"):
        GoQualificationSettings.from_document(
            replace(
                qualification_settings,
                credential_sources=qualification_settings.credential_sources * 2,
            ).document()
        )
    document = qualification_settings.document()
    document["runtime"] = str(qualification_settings.runtime.parent / ".." / "other")
    with pytest.raises(RunError, match="QUALIFICATION_BOOTSTRAP_INVALID"):
        GoQualificationSettings.from_document(document)


def test_v2_history_never_opens_source_assets(
    check_deployment, qualification_settings, monkeypatch
):
    settings, _, args = check_deployment
    original = settings.control_directory / BOOTSTRAP_NAME
    original.unlink()
    settings = replace(settings, go_qualification_source=qualification_settings)
    write_check_bootstrap(settings)

    def forbidden(*args, **kwargs):
        raise AssertionError("history must not construct current qualification")

    monkeypatch.setattr(
        "karajan.orchestration.check_services_factory.open_go_qualification_store", forbidden
    )
    service = open_check_services(settings, run_id=args[0], operation_id=args[1], principal="owner")
    assert service.get(*args, principal="owner") is None
    assert all(not path.exists() for path in qualification_settings.paths())


def test_history_qualification_factory_replays_without_current_source(
    reviewer_case, qualification_settings
):
    record = qualify(reviewer_case)
    projects = ProjectRegistry(
        reviewer_case["projects"].database,
        [reviewer_case["root"]],
        existing_only=True,
    )
    reopened = open_go_qualification_store(projects, qualification_settings)
    assert (
        reopened.get(reviewer_case["project_id"], record["id"], principal="owner")["record"]
        == record
    )
    assert all(not path.exists() for path in qualification_settings.paths())


def test_current_factory_composes_both_real_suites_without_granting_a_role(
    reviewer_case, artifacts, tmp_path
):
    """Real constructors/tokenizer/stores; fake inert ELF is never qualified or started."""
    runtime = tmp_path / "inert-runtime"
    runtime.write_bytes(b"not-an-executable-qualification")
    worker_root, reviewer_root = tmp_path / "worker-root", tmp_path / "reviewer-root"
    worker_root.mkdir(mode=0o700)
    reviewer_root.mkdir(mode=0o700)
    journal = reviewer_case["suite"].journal.path
    journal.chmod(0o600)
    settings = GoQualificationSettings(
        runtime,
        artifacts,
        journal,
        worker_root,
        reviewer_root,
        tmp_path / "credential-private",
        (
            GoTaskCredentialSource(
                reviewer_case["project_id"], "secret:go", "synthetic", reviewer_case["secret"]
            ),
        ),
    )
    projects = ProjectRegistry(
        reviewer_case["projects"].database,
        [reviewer_case["root"]],
        existing_only=True,
    )
    store = open_go_qualification_store(projects, settings, for_current=True)
    assert isinstance(store.go_suite, FixedGoSuite)
    assert isinstance(store.reviewer_suite, FixedGoReviewerSuite)
    assert store.go_suite.journal is store.reviewer_suite.journal
    assert store.projects is projects
    with pytest.raises(QualificationError):
        store.facts_for_profile(
            reviewer_case["project_id"],
            reviewer_case["reviewer"],
            principal="owner",
            scope="runtime_tools",
        )
    assert list(worker_root.iterdir()) == list(reviewer_root.iterdir()) == []


def test_actual_check_factory_injects_current_qualification_into_subject_validator(
    go_deployment,
    projected,
    artifacts,
    tmp_path,
):
    """Real Check composition/ledgers/tokenizer; no process or model is launched."""
    original, _, args = go_deployment
    original.runtime.write_bytes(b"inert-not-qualified-runtime")
    original.qualification_work_root.mkdir(mode=0o700)
    reviewer_root = tmp_path / "readonly-qualification"
    reviewer_root.mkdir(mode=0o700)
    original.journal_path.chmod(0o600)
    qualification = GoQualificationSettings(
        original.runtime,
        artifacts,
        original.journal_path,
        original.qualification_work_root,
        reviewer_root,
        tmp_path / "credential-private",
        (
            GoTaskCredentialSource(
                projected["project_id"], "secret:go", "synthetic", projected["secret"]
            ),
        ),
    )
    settings = CheckSettings(
        original.control_directory,
        original.state_directory,
        original.candidate_directory,
        original.host_directory,
        original.task_work_root,
        original.python_executable,
        original.allowed_roots,
        go_qualification_source=qualification,
    )
    write_check_bootstrap(settings)
    service = open_check_services(
        settings,
        run_id=args[0],
        operation_id=args[1],
        principal="owner",
        for_execution=True,
    )
    validator = service.subject_validator.__self__
    assert isinstance(validator.qualifications.go_suite, FixedGoSuite)
    assert isinstance(validator.qualifications.reviewer_suite, FixedGoReviewerSuite)
    assert validator.qualifications is service.admissions.routing.qualifications
    assert validator.qualifications.projects is service.admissions.routing.planner.projects
    assert list(original.qualification_work_root.iterdir()) == list(reviewer_root.iterdir()) == []
