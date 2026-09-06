"""Real approved Runs, reservations and Git baselines; qualification is synthetic."""

import subprocess
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path

import pytest
from karajan.candidates import CandidateStore
from karajan.orchestration.admission import ApprovedTaskAdmission
from karajan.orchestration.workspace import ApprovedTaskWorkspace
from karajan.runs import RunError
from test_planning import project as source_project
from test_routing_authorization import approve_request
from test_task_admission import prepared

__all__ = ["prepared", "source_project"]


def git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def project(source_project, request):
    registry, configured, repository = source_project
    files = {
        "src/report.py": b"print('approved repository task')\n",
        "src/support.py": b"CONSTANT = 7\n",
        "tests/test_report.py": b"assert True\n",
        "docs/unprojected.md": b"Keep this complete-baseline file.\n",
    }
    for name, content in files.items():
        target = repository / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    git(repository, "add", ".")
    if getattr(request, "param", None) == "case_collision":
        blob = git(repository, "hash-object", "src/report.py")
        git(repository, "update-index", "--add", "--cacheinfo", f"100644,{blob},src/REPORT.py")
    git(
        repository,
        "-c",
        "user.name=Workspace Fixture",
        "-c",
        "user.email=workspace@example.invalid",
        "commit",
        "-qm",
        "task workspace inputs",
    )
    configured = registry.update(
        configured["id"],
        {
            "name": configured["name"],
            "base_ref": "HEAD",
            "target_branch": "main",
            "allowed_target_branches": ["main"],
        },
        expected_revision=configured["revision"],
        command_key="workspace-base",
        principal="owner",
    )
    return registry, configured, repository


def reserved(prepared):
    admissions, routing, run = prepared
    queued = admissions.enqueue(run["id"], "implement", principal="owner", command_key="enqueue")
    operation = admissions.advance(run["id"], queued["id"], principal="owner")
    assert operation["state"] == "reserved"
    return admissions, routing, run, operation


def test_prepare_uses_approved_repository_paths_and_preserves_full_baseline(prepared, tmp_path):
    admissions, routing, run, operation = reserved(prepared)
    candidates = CandidateStore(tmp_path / "candidates")
    service = ApprovedTaskWorkspace(admissions, candidates)
    manifest = service.prepare(run["id"], operation["id"], principal="owner")

    assert manifest["read_paths"] == ["src/report.py", "src/support.py", "tests/test_report.py"]
    assert manifest["write_paths"] == ["src/report.py"]
    assert manifest["planned_attempt_id"] == operation["planned_attempt_id"]
    assert manifest["planned_context_id"] == operation["planned_context_id"]
    assert manifest["source_binding"]["approval"] == operation["assessment"]["sources"]["approval"]
    assert manifest["source_binding"]["requirement"] == run["requirement"]
    assert manifest["source_binding"]["selected_profile"] == {
        "id": "fixture-profile",
        "revision": 1,
    }
    assert manifest["dispatch_enabled"] is False
    assert manifest["activation_allowed"] is False
    assert manifest["new_files_supported"] is False
    assert len(manifest["input_sha256"]) == 64
    assert service.get(run["id"], operation["id"], principal="owner") == manifest
    assert admissions.get(run["id"], operation["id"], principal="owner")["workspace"] == manifest
    assert routing.capacity.snapshot()["reservations"][0]["state"] == "reserved"
    destination = tmp_path / "collector"
    candidates.materialize_baseline(manifest["baseline"]["id"], destination)
    assert (
        destination / "docs/unprojected.md"
    ).read_bytes() == b"Keep this complete-baseline file.\n"
    assert (destination / "original.txt").read_text() == "untouched\n"


def revise_plan(prepared, *, read_paths=None, task_paths=None):
    _, routing, run = prepared
    current = routing.planner.get(run["id"], principal="owner")
    plan = deepcopy(current["plans"][-1]["plan"])
    if read_paths is not None:
        plan["authorization"]["read_paths"] = read_paths
        plan["tasks"][1]["paths"] = [read_paths[0]]
        plan["tasks"][1]["revision"] += 1
    if task_paths is not None:
        plan["tasks"][0]["paths"] = task_paths
        plan["tasks"][0]["revision"] += 1
    revised = routing.planner.submit_plan(
        run["id"],
        {
            "schema_version": "karajan.submit-plan.v2",
            "term": current["commander"]["term"],
            "intent_id": current["planning_intents"][0]["id"],
            "expected_plan_revision": current["latest_plan_revision"],
            "plan": plan,
        },
        command_key="revise",
        principal="lead",
    )
    routing.planner.approve_plan(
        run["id"], approve_request(revised), command_key="approve-revised", principal="owner"
    )
    prior = routing.estimates.get(run["project_id"], "prediction", 1, principal="owner")["record"]
    prediction = {
        key: prior[key]
        for key in (
            "id",
            "revision",
            "source_kind",
            "validity_seconds",
            "measurement_semantics",
            "demand",
            "completion_seconds",
            "basis",
        )
    }
    prediction["revision"] = 2
    routing.estimates.register(
        run["id"],
        "implement",
        {"id": "fixture-profile", "revision": 1},
        prediction,
        principal="owner",
        command_key="revised-prediction",
    )


@pytest.mark.parametrize(
    ("read_paths", "task_paths", "code"),
    [
        (["tests"], None, "TASK_WORKSPACE_WRITE_NOT_READABLE"),
        (None, ["src/new.py"], "TASK_WORKSPACE_NEW_FILES_NOT_SUPPORTED"),
        (None, [], "TASK_WORKSPACE_SCOPE_EMPTY"),
        (["src", "src/report.py", "tests"], None, "TASK_WORKSPACE_PATH_CONFLICT"),
    ],
)
def test_prepare_rejects_unusable_approved_path_scopes(
    prepared, tmp_path, read_paths, task_paths, code
):
    revise_plan(prepared, read_paths=read_paths, task_paths=task_paths)
    admissions, routing, run, operation = reserved(prepared)
    service = ApprovedTaskWorkspace(admissions, CandidateStore(tmp_path / "candidates"))
    with pytest.raises(RunError, match=code):
        service.prepare(run["id"], operation["id"], principal="owner")
    with pytest.raises(RunError, match="TASK_WORKSPACE_NOT_PREPARED"):
        service.get(run["id"], operation["id"], principal="owner")
    assert routing.capacity.snapshot()["reservations"][0]["state"] == "reserved"


@pytest.mark.parametrize("project", ["case_collision"], indirect=True)
def test_case_colliding_git_files_are_rejected_on_every_platform(prepared, tmp_path):
    admissions, _, run, operation = reserved(prepared)
    service = ApprovedTaskWorkspace(admissions, CandidateStore(tmp_path / "candidates"))
    with pytest.raises(RunError, match="TASK_WORKSPACE_PATH_CONFLICT"):
        service.prepare(run["id"], operation["id"], principal="owner")


@pytest.mark.parametrize("state", ["queued", "cancelled", "expired", "active"])
def test_prepare_requires_a_current_uncancelled_reservation(prepared, tmp_path, state):
    admissions, routing, run = prepared
    operation = admissions.enqueue(run["id"], "implement", principal="owner", command_key="enqueue")
    if state != "queued":
        operation = admissions.advance(run["id"], operation["id"], principal="owner")
    if state == "cancelled":
        admissions.cancel(run["id"], operation["id"], principal="owner")
    elif state == "expired":
        routing.capacity.clock = lambda: 1500.0
    elif state == "active":
        routing.capacity.activate(
            operation["capacity_receipt"]["admission_id"], command_key="external"
        )
    service = ApprovedTaskWorkspace(admissions, CandidateStore(tmp_path / "candidates"))
    with pytest.raises(RunError, match="TASK_WORKSPACE_RESERVATION_REQUIRED"):
        service.prepare(run["id"], operation["id"], principal="owner")
    with pytest.raises(RunError, match="TASK_WORKSPACE_NOT_PREPARED"):
        service.get(run["id"], operation["id"], principal="owner")


def test_replacing_active_approval_invalidates_the_old_operation(prepared, tmp_path):
    admissions, _, run, operation = reserved(prepared)
    revise_plan(prepared)
    service = ApprovedTaskWorkspace(admissions, CandidateStore(tmp_path / "candidates"))
    with pytest.raises(RunError, match="APPROVAL_BINDING_MISMATCH"):
        service.prepare(run["id"], operation["id"], principal="owner")


def test_reopen_replays_original_manifest_without_reading_changed_repository(prepared, tmp_path):
    admissions, routing, run, operation = reserved(prepared)
    service = ApprovedTaskWorkspace(admissions, CandidateStore(tmp_path / "candidates"))
    original = service.prepare(run["id"], operation["id"], principal="owner")
    (tmp_path / "repository").rename(tmp_path / "repository-unavailable")
    reopened = ApprovedTaskWorkspace(
        ApprovedTaskAdmission(admissions.database, routing), CandidateStore(tmp_path / "candidates")
    )
    replay = reopened.prepare(run["id"], operation["id"], principal="owner")
    assert replay == original
    replay["write_paths"].append("docs/unprojected.md")
    replay["source_binding"]["approval"].clear()
    assert reopened.get(run["id"], operation["id"], principal="owner") == original
    admissions.cancel(run["id"], operation["id"], principal="owner")
    assert reopened.get(run["id"], operation["id"], principal="owner") == original
    with pytest.raises(RunError, match="TASK_WORKSPACE_RESERVATION_REQUIRED"):
        reopened.prepare(run["id"], operation["id"], principal="owner")


def test_workspace_read_and_prepare_require_the_run_owner(prepared, tmp_path):
    admissions, _, run, operation = reserved(prepared)
    service = ApprovedTaskWorkspace(admissions, CandidateStore(tmp_path / "candidates"))
    service.prepare(run["id"], operation["id"], principal="owner")
    for method in (service.get, service.prepare):
        with pytest.raises(RunError, match="RUN_NOT_FOUND"):
            method(run["id"], operation["id"], principal="another-owner")


def test_capture_uses_the_run_frozen_commit_after_repository_head_moves(prepared, tmp_path):
    admissions, _, run, operation = reserved(prepared)
    repository = tmp_path / "repository"
    (repository / "src/report.py").write_text("unapproved working-tree bytes\n")
    git(repository, "add", ".")
    git(
        repository,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-qm",
        "advance mutable HEAD",
    )
    candidates = CandidateStore(tmp_path / "candidates")
    manifest = ApprovedTaskWorkspace(admissions, candidates).prepare(
        run["id"], operation["id"], principal="owner"
    )
    assert (
        manifest["baseline"]["base_sha"] == run["configuration_snapshot"]["repository"]["base_sha"]
    )
    assert manifest["baseline"]["base_sha"] != git(repository, "rev-parse", "HEAD")
    restored = tmp_path / "restored"
    candidates.materialize_baseline(manifest["baseline"]["id"], restored)
    assert (restored / "src/report.py").read_bytes() == b"print('approved repository task')\n"


def test_concurrent_prepare_returns_one_immutable_manifest(prepared, tmp_path):
    admissions, _, run, operation = reserved(prepared)
    candidates = CandidateStore(tmp_path / "candidates")
    service = ApprovedTaskWorkspace(admissions, candidates)
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(
            workers.map(
                lambda _: service.prepare(run["id"], operation["id"], principal="owner"), range(2)
            )
        )
    assert results[0] == results[1]
    assert service.get(run["id"], operation["id"], principal="owner") == results[0]


def test_control_database_alias_is_rejected_without_waiting_for_nested_locks(prepared, tmp_path):
    _, routing, _ = prepared
    candidates = CandidateStore(tmp_path / "shared")
    admissions = ApprovedTaskAdmission(tmp_path / "shared/candidates.sqlite", routing)
    with pytest.raises(RunError, match="TASK_WORKSPACE_DATABASE_MUST_BE_SEPARATE"):
        ApprovedTaskWorkspace(admissions, candidates)


def test_unavailable_frozen_repository_rejects_capture_with_no_manifest(prepared, tmp_path):
    admissions, _, run, operation = reserved(prepared)
    (tmp_path / "repository").rename(tmp_path / "repository-unavailable")
    service = ApprovedTaskWorkspace(admissions, CandidateStore(tmp_path / "candidates"))
    with pytest.raises(RunError, match="BASELINE_INVALID"):
        service.prepare(run["id"], operation["id"], principal="owner")
    with pytest.raises(RunError, match="TASK_WORKSPACE_NOT_PREPARED"):
        service.get(run["id"], operation["id"], principal="owner")
