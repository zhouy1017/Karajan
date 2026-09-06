"""Independent public-store checks with synthetic repositories and real fixed processes."""

import copy
import hashlib
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pathlib import Path
from threading import Event

import pytest
from karajan.projects import ProjectError, ProjectRegistry
from karajan.projects.qualification import ProfileQualificationStore, QualificationError

WORKTREE = next(
    parent for parent in Path(__file__).resolve().parents if (parent / "pyproject.toml").is_file()
)


@pytest.fixture
def case(tmp_path):
    root = tmp_path / "fixture-root"
    repository = root / "repository"
    repository.mkdir(parents=True)
    (repository / "README.md").write_text("Independent synthetic qualification input.\n")
    for args in (
        ["init", "--initial-branch=main", str(repository)],
        ["-C", str(repository), "add", "README.md"],
        [
            "-C",
            str(repository),
            "-c",
            "user.name=Spec",
            "-c",
            "user.email=spec@example.invalid",
            "commit",
            "-qm",
            "synthetic baseline",
        ],
    ):
        subprocess.run(["git", *args], check=True, capture_output=True)
    clock = [6000.0]
    projects = ProjectRegistry(tmp_path / "projects.sqlite", [root], clock=lambda: clock[0])
    project = projects.create(
        {
            "name": "Independent synthetic Spec",
            "repository_path": str(repository),
            "base_ref": "main",
            "target_branch": "main",
            "allowed_target_branches": ["main"],
        },
        command_key="create",
        principal="owner",
    )
    config = json.loads(
        (WORKTREE / "examples/projects/offline-configuration.json").read_text(encoding="utf-8")
    )
    row = {
        "root": root,
        "repository": repository,
        "clock": clock,
        "projects": projects,
        "project": project["id"],
        "config": config,
    }
    apply(row, config, "initial")
    row["store"] = ProfileQualificationStore(projects, clock=lambda: clock[0])
    return row


def apply(case, config, key):
    registry = case["projects"]
    preview = registry.preview_configuration(
        case["project"], config, command_key=key + "-preview", principal="owner"
    )
    registry.apply_configuration(
        case["project"],
        preview["preview_id"],
        expected_revision=registry.get(case["project"])["revision"],
        command_key=key + "-apply",
        principal="owner",
    )


def registration(case):
    return case["config"]["resources"]["profiles"][0]


def qualify(case, key="observed"):
    return case["store"].qualify_local_fixture(
        case["project"],
        {"id": "fixture-profile", "revision": 1},
        principal="owner",
        command_key=key,
        fixture_root=case["root"],
        validity_seconds=90,
    )


def facts(case, frozen=None, scope="local_fixture"):
    return case["store"].facts_for_profile(
        case["project"],
        frozen or registration(case),
        principal="owner",
        scope=scope,
        fixture_root=case["root"],
    )


def test_actual_three_process_results_and_persistent_scope_are_not_inferred_from_declarations(
    case, monkeypatch
):
    actual = subprocess.run
    captured = []

    def recording(args, **kwargs):
        result = actual(args, **kwargs)
        if "_fixture_process.py" in str(args[2] if len(args) > 2 else ""):
            captured.append({"args": args, "cwd": kwargs["cwd"], "result": result})
        return result

    monkeypatch.setattr(subprocess, "run", recording)
    record = qualify(case)
    assert [call["args"][3] for call in captured] == ["write", "check", "review"]
    assert all(call["args"][1] == "-I" for call in captured)
    workspace = Path(record["observation"]["workspace"])
    assert all(call["cwd"] == workspace for call in captured)
    assert workspace.is_relative_to(case["root"]) and not workspace.is_relative_to(
        case["repository"]
    )
    candidate = workspace / "fixture.py"
    assert candidate.read_bytes() == b"print('fixture candidate')\n"
    assert (
        record["observation"]["candidate_sha256"]
        == hashlib.sha256(candidate.read_bytes()).hexdigest()
    )
    for entry, call in zip(record["observation"]["processes"], captured, strict=True):
        assert entry["exit_code"] == call["result"].returncode == 0
        assert entry["stdout_sha256"] == hashlib.sha256(call["result"].stdout).hexdigest()
        assert entry["stderr_sha256"] == hashlib.sha256(call["result"].stderr).hexdigest()
    for operation in ("check", "review"):
        assert json.loads((workspace / (operation + ".json")).read_bytes()) == {
            "operation": operation,
            "verdict": "passed",
            "synthetic": True,
            "files": ["fixture.py"],
            "author_reasoning_included": False,
        }
    projection = facts(case)
    assert projection["facts"]["roles"] == ["worker", "reviewer"]
    assert projection["facts"]["context_tokens"] is None
    assert projection["facts"]["budget_enforcement"] == "unknown"
    assert projection["facts"]["provenance"] == "fixture"
    assert projection["dispatch_eligible"] is False
    assert {item["capability"] for item in projection["capability_evidence"]} == {
        "fixed_fixture_write",
        "fixed_fixture_check",
        "fixed_fixture_review",
    }
    assert case["store"].get(case["project"], record["id"], principal="owner")["record"] == record
    assert qualify(case) == record
    assert len(captured) == 3
    assert (
        case["repository"] / "README.md"
    ).read_text() == "Independent synthetic qualification input.\n"


@pytest.mark.parametrize("axis", ["model", "native", "account", "declaration"])
def test_old_observation_cannot_qualify_changed_identity_even_with_updated_frozen_registration(
    case, axis
):
    first = qualify(case)
    changed = copy.deepcopy(case["config"])
    registered = changed["resources"]["profiles"][0]
    if axis == "model":
        registered["profile"]["binding"]["model_id"] = "different-model"
    elif axis == "native":
        registered["profile"]["binding"]["native_settings"] = {"temperature": 0}
    elif axis == "account":
        changed["resources"]["accounts"][0]["provider_id"] = "different-provider"
    else:
        registered["model_family"] = "changed-owner-declaration"
    apply(case, changed, "changed")
    with pytest.raises(QualificationError, match="^PROFILE_IDENTITY_MISMATCH$"):
        facts(case, registered)
    assert case["store"].get(case["project"], first["id"], principal="owner")["record"] == first


def test_revocation_survives_new_service_and_original_observation_remains_immutable(case):
    first = qualify(case)
    store = case["store"]
    revoked = store.revoke(
        case["project"], first["id"], principal="owner", reason="independent-spec-revoke"
    )
    reopened = ProfileQualificationStore(
        ProjectRegistry(case["projects"].database, [case["root"]]), clock=lambda: case["clock"][0]
    )
    assert reopened.get(case["project"], first["id"], principal="owner") == {
        "record": first,
        "revocation": revoked,
    }
    with pytest.raises(QualificationError, match="^QUALIFICATION_REVOKED$"):
        reopened.facts_for_profile(
            case["project"],
            registration(case),
            principal="owner",
            scope="local_fixture",
            fixture_root=case["root"],
        )
    with pytest.raises(ProjectError, match="^USER_DECISION_REQUIRED$"):
        reopened.revoke(case["project"], first["id"], principal="intruder", reason="try")


def test_real_database_guard_holds_concurrent_revocation_until_exit(case):
    first = qualify(case)
    started = Event()
    other = ProfileQualificationStore(
        ProjectRegistry(case["projects"].database, [case["root"]]), clock=lambda: case["clock"][0]
    )

    def revoke():
        started.set()
        return other.revoke(
            case["project"], first["id"], principal="owner", reason="concurrent-stop"
        )

    with ThreadPoolExecutor(max_workers=1) as executor:
        with case["store"].routing_facts_guard(
            case["project"],
            [registration(case)],
            principal="owner",
            scope="local_fixture",
            fixture_root=case["root"],
        ) as view:
            future = executor.submit(revoke)
            assert started.wait(timeout=2)
            with pytest.raises(TimeoutError):
                future.result(timeout=0.15)
            assert view["profiles"][0]["qualification"]["observation"]["id"] == first["id"]
        assert future.result(timeout=3)["reason"] == "concurrent-stop"
    with pytest.raises(QualificationError, match="^QUALIFICATION_REVOKED$"):
        facts(case)


def test_unknown_runtime_and_uploaded_style_declarations_never_produce_live_qualification(case):
    first = qualify(case)
    with pytest.raises(QualificationError, match="^RUNTIME_TOOLS_NOT_QUALIFIED$"):
        facts(case, scope="runtime_tools")
    changed = copy.deepcopy(case["config"])
    registered = changed["resources"]["profiles"][0]
    registered["profile"]["binding"]["runtime_kind"] = "opencode"
    for evidence in registered["capability_evidence"]:
        evidence.update(
            status="passed",
            provenance="imported_observation",
            evidence_ref="untrusted-upload-looking-ref",
        )
    apply(case, changed, "unknown")
    with pytest.raises(QualificationError, match="^QUALIFICATION_SOURCE_UNSUPPORTED$"):
        qualify(case, "unknown-runtime")
    assert len(list(case["root"].glob("qualification-*"))) == 1
    with case["store"].routing_facts_guard(
        case["project"], [registered], principal="owner"
    ) as view:
        assert view["profiles"][0]["qualification"] is None
        assert view["profiles"][0]["reason_codes"] == ["RUNTIME_TOOLS_NOT_QUALIFIED"]
    assert case["store"].get(case["project"], first["id"], principal="owner")["record"] == first


def test_later_persisted_start_without_result_blocks_fallback_to_prior_pass(case, monkeypatch):
    first = qualify(case)
    actual = subprocess.run

    def simulated_crash(args, **kwargs):
        if "_fixture_process.py" in str(args[2] if len(args) > 2 else ""):
            raise RuntimeError("synthetic controller crash after start was persisted")
        return actual(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", simulated_crash)
    with pytest.raises(RuntimeError, match="synthetic controller crash"):
        qualify(case, "unknown-start")
    with pytest.raises(QualificationError, match="^QUALIFICATION_IN_PROGRESS_OR_UNKNOWN$"):
        facts(case)
    with pytest.raises(QualificationError, match="^QUALIFICATION_IN_PROGRESS_OR_UNKNOWN$"):
        qualify(case, "unknown-start")
    assert case["store"].get(case["project"], first["id"], principal="owner")["record"] == first


@pytest.mark.parametrize("at", [5999.0, 6090.0])
def test_clock_rollback_and_exact_expiry_do_not_return_facts(case, at):
    qualify(case)
    case["clock"][0] = at
    with pytest.raises(QualificationError, match="^QUALIFICATION_EXPIRED$"):
        facts(case)
