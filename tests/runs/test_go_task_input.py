"""Real approved Workspace/CAS commands; qualification is an explicit synthetic suite."""

import json
from copy import deepcopy

import pytest
from karajan.candidates import CandidateStore
from karajan.orchestration.go_task_input import build_task_input
from karajan.orchestration.workspace import ApprovedTaskWorkspace
from karajan.routing.compiler import digest
from karajan.runs import RunError
from test_projected_go_routing import approved_task
from test_projected_qualification_store import case as case
from test_projected_qualification_store import projected as projected
from test_task_workspace import git


@pytest.fixture
def workspace_case(projected, tmp_path):
    repository = projected["repository"]
    for name, content in {
        "src/report.py": b"print('approved task')\n",
        "src/reference.txt": b"Reference contract\n",
        "tests/test_report.py": b"assert True\n",
        "docs/private.txt": b"Unprojected baseline file\n",
    }.items():
        path = repository / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    git(repository, "add", ".")
    git(
        repository,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-qm",
        "approved files",
    )
    projects = projected["projects"]
    configured = projects.get(projected["project_id"])
    projects.update(
        configured["id"],
        {
            "name": configured["name"],
            "base_ref": "HEAD",
            "target_branch": "main",
            "allowed_target_branches": ["main"],
        },
        expected_revision=configured["revision"],
        command_key="task-input-base",
        principal="owner",
    )
    admission, routing, run, _ = approved_task(projected, tmp_path)
    queued = admission.enqueue(run["id"], "implement", principal="owner", command_key="enqueue")
    operation = admission.advance(run["id"], queued["id"], principal="owner")
    assert operation["state"] == "reserved"
    candidates = CandidateStore(tmp_path / "candidates")
    workspace = ApprovedTaskWorkspace(admission, candidates).prepare(
        run["id"], operation["id"], principal="owner"
    )
    return workspace, candidates, repository, admission, routing


def compile_input(workspace, candidates):
    return build_task_input(
        workspace, candidates, native_source_sha256="b" * 64, runner_source_digest="c" * 64
    )


def test_input_comes_from_approved_text_and_cas_after_original_repository_disappears(
    workspace_case,
):
    workspace, candidates, repository, _, _ = workspace_case
    repository.rename(repository.with_name("repository-unavailable"))
    task = compile_input(workspace, CandidateStore(candidates.directory))
    assert task.workspace_digest == workspace["digest"]
    assert task.native_source_sha256 == "b" * 64
    assert task.runner_source_digest == "c" * 64
    assert task.timeout_seconds == 20
    assert {row.path: row.content for row in task.files} == {
        "src/report.py": b"print('approved task')\n",
        "src/reference.txt": b"Reference contract\n",
        "tests/test_report.py": b"assert True\n",
    }
    assert [row.path for row in task.files if row.writable] == ["src/report.py"]
    prompt = json.loads(task.prompt)
    assert prompt["requirement"] == workspace["source_binding"]["requirement"]
    assert prompt["task"]["id"] == "implement"
    assert prompt["task"]["acceptance"] == ["Report is repeatable"]
    assert prompt["read_paths"] == workspace["read_paths"]
    assert prompt["write_paths"] == ["src/report.py"]
    assert compile_input(workspace, candidates) == task
    assert not list(candidates.directory.parent.glob(".task-input-*"))


@pytest.mark.parametrize("fault", ["outer", "input"])
def test_workspace_both_digest_layers_are_required(workspace_case, fault):
    workspace, candidates, *_ = workspace_case
    changed = deepcopy(workspace)
    if fault == "outer":
        changed["write_paths"].append("src/reference.txt")
    else:
        changed["input_sha256"] = "0" * 64
        changed["digest"] = digest(
            {key: value for key, value in changed.items() if key != "digest"}
        )
    with pytest.raises(RunError, match="TASK_INPUT_WORKSPACE_DIGEST_MISMATCH"):
        compile_input(changed, candidates)


def seal(workspace):
    workspace.pop("digest", None)
    workspace.pop("input_sha256", None)
    workspace["input_sha256"] = digest(workspace)
    workspace["digest"] = digest(workspace)
    return workspace


@pytest.mark.parametrize(
    "fault",
    [
        "expand_read",
        "expand_write",
        "files",
        "baseline",
        "repository",
        "plan",
        "approval",
        "policy",
        "task_id",
        "duplicate_task",
    ],
)
def test_rehashed_workspace_cannot_expand_or_substitute_approved_bindings(workspace_case, fault):
    workspace, candidates, *_ = workspace_case
    changed = deepcopy(workspace)
    source = changed["source_binding"]
    if fault == "expand_read":
        row = next(
            item for item in changed["baseline"]["manifest"] if item["path"] == "docs/private.txt"
        )
        changed["read_paths"].append(row["path"])
        changed["files"].append({**row, "access": ["read"]})
    elif fault == "expand_write":
        changed["write_paths"].append("src/reference.txt")
        next(row for row in changed["files"] if row["path"] == "src/reference.txt")[
            "access"
        ].append("write")
    elif fault == "files":
        changed["files"].pop()
    elif fault == "baseline":
        changed["baseline"]["manifest"].pop()
    elif fault == "repository":
        source["repository"]["base_sha"] = "0" * 40
    elif fault == "plan":
        source["plan"]["plan"]["tasks"][0]["acceptance"] = ["Unapproved behavior"]
    elif fault == "approval":
        source["approval"]["run_id"] = "another-run"
    elif fault == "policy":
        source["execution_policy"]["max_context_tokens"] += 1
    elif fault == "task_id":
        changed["task_id"] = "review"
    else:
        source["plan"]["plan"]["tasks"].append(deepcopy(source["plan"]["plan"]["tasks"][0]))
    with pytest.raises(RunError):
        compile_input(seal(changed), candidates)


@pytest.mark.parametrize("mutation", ["change", "missing", "hardlink"])
def test_tampered_cas_cannot_supply_execution_bytes(workspace_case, mutation):
    workspace, candidates, *_ = workspace_case
    row = next(row for row in workspace["files"] if row["path"] == "src/report.py")
    artifact = candidates.directory / "artifacts" / row["artifact"]["sha256"]
    if mutation == "change":
        artifact.write_bytes(b"unapproved content")
    elif mutation == "missing":
        artifact.unlink()
    else:
        artifact.with_name("shared-artifact").hardlink_to(artifact)
    with pytest.raises(RunError, match="ARTIFACT_UNAVAILABLE"):
        compile_input(workspace, candidates)
    assert not list(candidates.directory.parent.glob(".task-input-*"))


def reseal_plan(workspace):
    """Adversarial internally consistent input, not a new real owner approval."""
    source = workspace["source_binding"]
    plan = source["plan"]
    plan["routing_digest"] = digest(plan["routing_binding"])
    plan["authorization_digest"] = digest(
        [source["configuration_digest"], plan["plan"]["authorization"], plan["routing_binding"]]
    )
    plan.pop("plan_digest")
    plan["plan_digest"] = digest(plan)
    for key in ("routing_digest", "authorization_digest", "plan_digest"):
        source["approval"][key] = plan[key]
    return seal(workspace)


@pytest.mark.parametrize("tools", [["read"], ["edit"], ["read", "edit", "bash"]])
def test_task_tool_subset_or_extra_is_not_promoted_to_fixed_native_permissions(
    workspace_case, tools
):
    workspace, candidates, *_ = workspace_case
    changed = deepcopy(workspace)
    plan = changed["source_binding"]["plan"]
    plan["plan"]["tasks"][0]["tools"] = tools
    plan["routing_binding"]["task_requirements"]["implement"]["tools"] = tools
    with pytest.raises(RunError, match="TASK_INPUT_TOOL_SCOPE_UNSUPPORTED"):
        compile_input(reseal_plan(changed), candidates)


def test_native_tools_must_still_be_covered_by_plan_authorization(workspace_case):
    workspace, candidates, *_ = workspace_case
    changed = deepcopy(workspace)
    changed["source_binding"]["plan"]["plan"]["authorization"]["tools"] = ["read"]
    with pytest.raises(RunError, match="TASK_INPUT_TOOL_SCOPE_UNSUPPORTED"):
        compile_input(reseal_plan(changed), candidates)


def test_oversized_approved_text_is_rejected_without_truncation(workspace_case):
    workspace, candidates, *_ = workspace_case
    changed = deepcopy(workspace)
    changed["source_binding"]["requirement"]["acceptance"] = [
        "Required behavior " + str(i) + "x" * 200 for i in range(40)
    ]
    with pytest.raises(RunError, match="TASK_INPUT_PROMPT_TOO_LARGE"):
        compile_input(seal(changed), candidates)


@pytest.mark.parametrize("field", ["native_source_sha256", "runner_source_digest"])
def test_invalid_source_envelope_digests_are_rejected(workspace_case, field):
    workspace, candidates, *_ = workspace_case
    sources = {"native_source_sha256": "b" * 64, "runner_source_digest": "c" * 64}
    sources[field] = "not-a-digest"
    with pytest.raises(RunError):
        build_task_input(workspace, candidates, **sources)


def test_incomplete_routing_task_requirements_are_not_a_valid_binding(workspace_case):
    workspace, candidates, *_ = workspace_case
    changed = deepcopy(workspace)
    changed["source_binding"]["plan"]["routing_binding"]["task_requirements"]["implement"] = {}
    with pytest.raises(RunError, match="TASK_INPUT_TASK_BINDING_MISMATCH"):
        compile_input(reseal_plan(changed), candidates)
