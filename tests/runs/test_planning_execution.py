"""Planning execution uses real local SQLite and strictly read-only authorities."""

import hashlib
import json
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from threading import Event
from typing import Any

import pytest
from karajan.capacity import CapacityStore
from karajan.orchestration.planning_execution import PlanningExecution
from karajan.orchestration.planning_snapshot import PlanningRepositorySnapshotStore
from karajan.runs import RunError, RunPlanner
from karajan.runs.planning import digest
from test_planning import create_request, handoff_request, proposal
from test_routing_authorization import policy_request, request_v2, submit_request

pytest_plugins = ["test_planning"]


class FixtureAuthorities:
    """Explicitly test-only adapters around a persisted CapacityStore receipt."""

    def __init__(self, capacity: CapacityStore) -> None:
        self.capacity = capacity
        self.request: dict[str, Any] | None = None
        self.key: str | None = None
        self.activation_request: dict[str, Any] | None = None
        self.activation_key: str | None = None
        self.content: bytes | None = None
        self.output_source = "b" * 64

    def prepare(self, binding: dict[str, Any], content: bytes) -> None:
        self.capacity.register_profile(
            {
                "id": binding["profile"]["id"],
                "revision": binding["profile"]["revision"],
                "account_id": "shared-account",
                "pool_ids": ["short", "weekly", "allowance"],
            },
            command_key="planning-profile:" + binding["execution_id"],
        )
        self.request = capacity_request(
            binding["attempt_id"],
            profile_id=binding["profile"]["id"],
            run_id=binding["run_id"],
        )
        self.key = "planning-admit:" + binding["execution_id"]
        self.capacity.admit(self.request, command_key=self.key)
        receipt = self.capacity.command_receipt("admit", self.request, command_key=self.key)
        assert receipt is not None
        self.activation_request = {"admission_id": receipt["admission_id"]}
        self.activation_key = "planning-activate:" + binding["execution_id"]
        self.content = content

    def activate(self) -> None:
        assert self.activation_request is not None and self.activation_key is not None
        self.capacity.activate(
            self.activation_request["admission_id"], command_key=self.activation_key
        )

    def read_admission(self, binding: dict[str, Any]) -> object:
        assert (
            self.request is not None
            and self.key is not None
            and self.activation_request is not None
            and self.activation_key is not None
        )
        return {
            "schema_version": "karajan.planning-admission-evidence.v1",
            "binding_sha256": digest(binding),
            "authority_kind": "fixture",
            "source_sha256": digest(self.request),
            "budget_ref": binding["budget_ref"],
            "capacity_request": self.request,
            "capacity_command_key": self.key,
            "capacity_receipt": self.capacity.command_receipt(
                "admit", self.request, command_key=self.key
            ),
            "capacity_activation_request": self.activation_request,
            "capacity_activation_command_key": self.activation_key,
            "capacity_activation_receipt": self.capacity.command_receipt(
                "activate", self.activation_request, command_key=self.activation_key
            ),
            "state": "admitted",
        }

    def read_output(self, execution_id: str, binding: dict[str, Any]) -> object:
        assert self.content is not None
        return {
            "schema_version": "karajan.planning-output-evidence.v1",
            "execution_id": execution_id,
            "binding_sha256": digest(binding),
            "authority_kind": "fixture",
            "source_sha256": self.output_source,
            "completed": True,
            "artifact_sha256": hashlib.sha256(self.content).hexdigest(),
            "artifact_size": len(self.content),
            "content": self.content,
        }

    def read_source(self, binding: dict[str, Any]) -> object:
        return {
            "schema_version": "karajan.planning-output-source.v1",
            "binding_sha256": digest(binding),
            "authority_kind": "fixture",
            "source_sha256": self.output_source,
        }


def capacity_request(attempt_id: str, *, profile_id: str, run_id: str) -> dict[str, Any]:
    return {
        "attempt_id": attempt_id,
        "run_id": run_id,
        "profile_id": profile_id,
        "profile_revision": 1,
        "role": "commander",
        "purpose": "lead",
        "authorization_ref": "fixture-planning-scope",
        "rulebook_revision": "fixture-planning-rules",
        "duration_seconds": 30,
        "demand": {"short": "1", "weekly": "1", "allowance": "1"},
    }


def capacity_store(directory: Path) -> CapacityStore:
    directory.mkdir()
    store = CapacityStore(directory / "capacity.sqlite", clock=lambda: 1000.0)
    for pool_id, kind in (
        ("short", "service"),
        ("weekly", "service"),
        ("allowance", "platform_allowance"),
    ):
        store.register_pool(
            {
                "id": pool_id,
                "account_id": "shared-account",
                "kind": kind,
                "unit": "requests",
                "window_kind": "fixed",
            },
            command_key="pool:" + pool_id,
        )
        store.observe(
            {
                "pool_id": pool_id,
                "window_id": "fixture-window",
                "observed_at": 1000.0,
                "reset_at": 2000.0,
                "source": "fixture",
                "source_ref": "fixture-observer",
                "metric": "remaining",
                "amount": "10",
                "limit": "10",
                "covered_usage_ids": [],
            },
            command_key="observe:" + pool_id,
        )
    store.activate_policy(
        {
            "account_id": "shared-account",
            "max_active_attempts": 4,
            "max_attempt_duration_seconds": 60,
            "observation_max_age_seconds": 30,
            "require_official_observation": False,
            "safety_margin": {},
            "lead_reserve": {},
            "lead_reserved_slots": 0,
            "conservative_mode": None,
        },
        expected_revision=0,
        command_key="policy",
    )
    return store


def planning_case(
    tmp_path: Path, configured: dict
) -> tuple[PlanningExecution, dict, dict, FixtureAuthorities]:
    planner = RunPlanner(tmp_path / "runs.sqlite", configured.pop("registry"))
    run = planner.create(create_request(configured), command_key="run", principal="owner")
    intent = planner.planning_intent(run["id"], term=1, command_key="intent", principal="lead")
    authorities = FixtureAuthorities(capacity_store(tmp_path / "capacity"))
    service = PlanningExecution(
        tmp_path / "planning-execution.sqlite",
        planner,
        admissions=authorities,
        outputs=authorities,
        capacity=authorities.capacity,
        allow_fixture_authorities=True,
    )
    return service, run, intent, authorities


@pytest.fixture
def configured(project: tuple[Any, dict[str, Any], Path]) -> dict[str, Any]:
    registry, value, _ = project
    return {**deepcopy(value), "registry": registry}


def begin(
    service: PlanningExecution, run: dict, intent: dict, authorities: FixtureAuthorities
) -> dict:
    execution = service.begin(run["id"], intent["id"], principal="owner", command_key="begin")
    content = json.dumps(proposal(run, intent)["plan"], separators=(",", ":")).encode()
    authorities.prepare(execution["binding"], content)
    return execution


def snapshot_case(
    configured: dict, tmp_path: Path
) -> tuple[
    PlanningExecution, dict, dict, FixtureAuthorities, PlanningRepositorySnapshotStore, dict
]:
    """Create one real registered-base snapshot subject for receipt tests."""
    registry = configured["registry"]
    project = registry.get(configured["id"])
    root = Path(project["repository"]["root"])
    (root / "src").mkdir(exist_ok=True)
    (root / "tests").mkdir(exist_ok=True)
    (root / "src" / "snapshot-subject.txt").write_bytes(b"snapshot source\n")
    (root / "tests" / "snapshot-subject.txt").write_bytes(b"snapshot test\n")
    subprocess.run(
        ["git", "-C", str(root), "add", "src/snapshot-subject.txt", "tests/snapshot-subject.txt"],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=x",
            "-c",
            "user.email=x@y.z",
            "commit",
            "-m",
            "snapshot subject",
        ],
        check=True,
    )
    registry.update(
        project["id"],
        {
            "name": project["name"],
            "base_ref": project["repository"]["base_ref"],
            "target_branch": project["target_branch"],
            "allowed_target_branches": project["allowed_target_branches"],
        },
        expected_revision=project["revision"],
        command_key="snapshot-subject-base",
        principal="owner",
    )
    current = {**registry.get(project["id"]), "registry": registry}
    service, run, intent, authorities = planning_case(tmp_path, current)
    store = PlanningRepositorySnapshotStore(tmp_path / "snapshots.sqlite")
    service.snapshots = store
    execution = service.begin(run["id"], intent["id"], principal="owner", command_key="begin")
    return service, run, intent, authorities, store, execution


def test_id_only_output_consumption_reopens_exact_capacity_receipt_and_plan(
    configured: dict, tmp_path: Path
) -> None:
    service, run, intent, authorities = planning_case(tmp_path, configured)
    execution = begin(service, run, intent, authorities)
    authorities.activate()

    admitted = service.reconcile(execution["id"], principal="owner")
    assert admitted["state"] == "awaiting_output"
    before = authorities.capacity.snapshot()
    reopened = PlanningExecution(
        service.database,
        service.planner,
        admissions=authorities,
        outputs=authorities,
        capacity=authorities.capacity,
        allow_fixture_authorities=True,
    )
    assert reopened.reconcile(execution["id"], principal="owner") == admitted
    assert authorities.capacity.snapshot() == before

    submitted = reopened.submit(execution["id"], principal="owner", command_key="submit")
    assert submitted["state"] == "submitted"
    assert submitted["output"]["content_sha256"] == hashlib.sha256(authorities.content).hexdigest()
    assert submitted["submission"]["plan_revision"] == 1
    assert reopened.submit(execution["id"], principal="owner", command_key="submit") == submitted
    assert service.planner.get(run["id"], principal="owner")["plans"] == [submitted["submission"]]


@pytest.mark.parametrize("change", ["missing", "wrong_profile", "unknown", "changed_output"])
def test_missing_mismatched_or_changed_evidence_never_submits(
    configured: dict, tmp_path: Path, change: str
) -> None:
    service, run, intent, authorities = planning_case(tmp_path, configured)
    execution = begin(service, run, intent, authorities)
    if change == "missing":
        service.admissions = None
    elif change == "wrong_profile":
        original = authorities.read_admission

        def changed(binding: dict[str, Any]) -> object:
            result = original(binding)
            result["capacity_request"] = {**result["capacity_request"], "profile_id": "other"}
            result["source_sha256"] = digest(result["capacity_request"])
            return result

        service.admissions = type("BadAdmissions", (), {"read_admission": staticmethod(changed)})()
    elif change == "unknown":
        original = authorities.read_admission

        def unknown(binding: dict[str, Any]) -> object:
            result = original(binding)
            result["state"] = "unknown"
            return result

        service.admissions = type(
            "UnknownAdmissions", (), {"read_admission": staticmethod(unknown)}
        )()
    else:
        authorities.activate()
        service.reconcile(execution["id"], principal="owner")
        authorities.content = b'{"summary":"changed"}'
    result = service.submit(execution["id"], principal="owner", command_key="submit")
    assert result["submission"] is None
    assert service.planner.get(run["id"], principal="owner")["plans"] == []


def test_cancel_before_output_wins_and_owner_and_intent_are_not_replaceable(
    configured: dict, tmp_path: Path
) -> None:
    service, run, intent, authorities = planning_case(tmp_path, configured)
    execution = begin(service, run, intent, authorities)
    with pytest.raises(RunError, match="RUN_NOT_FOUND"):
        service.get(execution["id"], principal="lead")
    cancelled = service.cancel(execution["id"], principal="owner", command_key="cancel")
    assert cancelled["state"] == "cancelled"
    assert service.submit(execution["id"], principal="owner", command_key="submit") == cancelled
    assert service.planner.get(run["id"], principal="owner")["plans"] == []
    with pytest.raises(RunError, match="PLANNING_EXECUTION_PENDING"):
        service.begin(run["id"], intent["id"], principal="owner", command_key="new-begin")


def test_production_default_rejects_fixture_authorities(configured: dict, tmp_path: Path) -> None:
    service, run, intent, authorities = planning_case(tmp_path, configured)
    service.allow_fixture_authorities = False
    execution = begin(service, run, intent, authorities)
    blocked = service.reconcile(execution["id"], principal="owner")
    assert blocked["reason_codes"] == ["PLANNING_FIXTURE_AUTHORITY_FORBIDDEN"]
    assert blocked["submission"] is None


def test_repository_snapshot_is_id_only_and_has_no_capacity_effect(
    configured: dict, tmp_path: Path
) -> None:
    service, run, intent, authorities = planning_case(tmp_path, configured)
    service.snapshots = PlanningRepositorySnapshotStore(tmp_path / "snapshots.sqlite")
    execution = service.begin(run["id"], intent["id"], principal="owner", command_key="begin")
    before = authorities.capacity.snapshot()
    with pytest.raises(RunError, match="PLANNING_SNAPSHOT_PATH_EMPTY"):
        service.freeze_repository_snapshot(
            execution["id"], principal="owner", command_key="snapshot"
        )
    assert authorities.capacity.snapshot() == before
    with pytest.raises(RunError, match="RUN_NOT_FOUND"):
        service.freeze_repository_snapshot(execution["id"], principal="lead", command_key="other")


def test_snapshot_read_rejects_same_owner_execution_row_substitution_without_repair(
    configured: dict, tmp_path: Path
) -> None:
    """The requested execution primary key anchors all snapshot authority reads."""
    service, run, _, authorities, store, first = snapshot_case(configured, tmp_path)
    second_intent = service.planner.planning_intent(
        run["id"], term=1, command_key="second-intent", principal="lead"
    )
    second = service.begin(
        run["id"], second_intent["id"], principal="owner", command_key="second-begin"
    )
    first_manifest = service.freeze_repository_snapshot(
        first["id"], principal="owner", command_key="first-freeze"
    )
    second_manifest = service.freeze_repository_snapshot(
        second["id"], principal="owner", command_key="second-freeze"
    )
    assert first_manifest["execution_id"] != second_manifest["execution_id"]
    with sqlite3.connect(service.database) as db:
        second_data = db.execute(
            "SELECT data FROM executions WHERE id=?", (second["id"],)
        ).fetchone()[0]
        commands_before = db.execute(
            "SELECT principal,key,payload,result FROM commands ORDER BY principal,key"
        ).fetchall()
        db.execute("UPDATE executions SET data=? WHERE id=?", (second_data, first["id"]))
        db.commit()
    with sqlite3.connect(store.database) as db:
        snapshots_before = db.execute(
            "SELECT binding_sha256,data,source_sha256,manifest_sha256 "
            "FROM snapshots ORDER BY binding_sha256"
        ).fetchall()
        files_before = db.execute(
            "SELECT binding_sha256,path,sha256 FROM files ORDER BY binding_sha256,path"
        ).fetchall()
    before_capacity = authorities.capacity.snapshot()

    for operation in (
        lambda: service.get(first["id"], principal="owner"),
        lambda: service.read_repository_snapshot(first["id"], principal="owner"),
        lambda: service.freeze_repository_snapshot(
            first["id"], principal="owner", command_key="substituted-freeze"
        ),
    ):
        with pytest.raises(RunError, match="^PLANNING_EXECUTION_BINDING_STALE$"):
            operation()
    assert (
        service.read_repository_snapshot(second["id"], principal="owner")["execution_id"]
        == second["id"]
    )
    with pytest.raises(RunError, match="RUN_NOT_FOUND"):
        service.read_repository_snapshot(second["id"], principal="lead")

    with sqlite3.connect(service.database) as db:
        assert (
            db.execute("SELECT data FROM executions WHERE id=?", (first["id"],)).fetchone()[0]
            == second_data
        )
        assert db.execute(
            "SELECT principal,key,payload,result FROM commands ORDER BY principal,key"
        ).fetchall() == commands_before
    with sqlite3.connect(store.database) as db:
        assert db.execute(
            "SELECT binding_sha256,data,source_sha256,manifest_sha256 "
            "FROM snapshots ORDER BY binding_sha256"
        ).fetchall() == snapshots_before
        assert db.execute(
            "SELECT binding_sha256,path,sha256 FROM files ORDER BY binding_sha256,path"
        ).fetchall() == files_before
    assert authorities.capacity.snapshot() == before_capacity


def test_snapshot_rejects_same_owner_run_and_intent_substitution_before_first_write(
    configured: dict, tmp_path: Path
) -> None:
    """Indexed execution identity must win over rehashed embedded JSON authority."""
    service, first_run, _, authorities, store, first = snapshot_case(configured, tmp_path)
    registry = service.planner.projects
    project = registry.get(first_run["project_id"])
    root = Path(project["repository"]["root"])
    (root / "src" / "snapshot-subject.txt").write_bytes(b"second run bytes\n")
    subprocess.run(
        ["git", "-C", str(root), "add", "src/snapshot-subject.txt"], check=True
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=x",
            "-c",
            "user.email=x@y.z",
            "commit",
            "-m",
            "second snapshot subject",
        ],
        check=True,
    )
    registry.update(
        project["id"],
        {
            "name": project["name"],
            "base_ref": project["repository"]["base_ref"],
            "target_branch": project["target_branch"],
            "allowed_target_branches": project["allowed_target_branches"],
        },
        expected_revision=project["revision"],
        command_key="second-snapshot-base",
        principal="owner",
    )
    second_project = registry.get(project["id"])
    second_run = service.planner.create(
        create_request(second_project), command_key="second-run", principal="owner"
    )
    second_intent = service.planner.planning_intent(
        second_run["id"], term=1, command_key="second-intent", principal="lead"
    )
    second = service.begin(
        second_run["id"], second_intent["id"], principal="owner", command_key="second-begin"
    )
    with sqlite3.connect(service.database) as db:
        forged = json.loads(
            db.execute("SELECT data FROM executions WHERE id=?", (second["id"],)).fetchone()[0]
        )
        forged["id"] = first["id"]
        forged["binding"]["execution_id"] = first["id"]
        forged["binding"]["attempt_id"] = "planning:" + first["id"]
        forged["binding_sha256"] = digest(forged["binding"])
        db.execute("UPDATE executions SET data=? WHERE id=?", (json.dumps(forged), first["id"]))
        db.commit()
    with sqlite3.connect(store.database) as db:
        before_rows = (
            db.execute("SELECT count(*) FROM snapshots").fetchone()[0],
            db.execute("SELECT count(*) FROM files").fetchone()[0],
        )
    before_capacity = authorities.capacity.snapshot()

    for operation in (
        lambda: service.get(first["id"], principal="owner"),
        lambda: service.read_repository_snapshot(first["id"], principal="owner"),
        lambda: service.freeze_repository_snapshot(
            first["id"], principal="owner", command_key="forged-first-freeze"
        ),
    ):
        with pytest.raises(RunError, match="^PLANNING_EXECUTION_BINDING_STALE$"):
            operation()
    with sqlite3.connect(store.database) as db:
        assert before_rows == (
            db.execute("SELECT count(*) FROM snapshots").fetchone()[0],
            db.execute("SELECT count(*) FROM files").fetchone()[0],
        )
    assert authorities.capacity.snapshot() == before_capacity

    second_manifest = service.freeze_repository_snapshot(
        second["id"], principal="owner", command_key="second-freeze"
    )
    assert service.read_repository_snapshot(second["id"], principal="owner")["content"] == {
        "src/snapshot-subject.txt": b"second run bytes\n",
        "tests/snapshot-subject.txt": b"snapshot test\n",
    }
    assert second_manifest["run_id"] == second_run["id"]
    with pytest.raises(RunError, match="^RUN_NOT_FOUND$"):
        service.get(second["id"], principal="lead")


@pytest.mark.parametrize("field", ["base_sha", "file_metadata", "snapshot_id"])
def test_snapshot_command_replay_rejects_corrupted_manifest_receipt(
    configured: dict, tmp_path: Path, field: str
) -> None:
    service, _, _, authorities, store, execution = snapshot_case(configured, tmp_path)
    command_key = "freeze"
    manifest = service.freeze_repository_snapshot(
        execution["id"], principal="owner", command_key=command_key
    )
    corrupted = deepcopy(manifest)
    if field == "base_sha":
        corrupted["base_sha"] = "0" * len(corrupted["base_sha"])
    elif field == "file_metadata":
        corrupted["files"][0]["mode"] = "100755"
    else:
        corrupted["snapshot_sha256"] = "0" * 64
    with sqlite3.connect(service.database) as db:
        db.execute(
            "UPDATE commands SET result=? WHERE principal=? AND key=?",
            (json.dumps(corrupted, separators=(",", ":")), "owner", command_key),
        )
        db.commit()
    before_capacity = authorities.capacity.snapshot()
    verified = store.read(execution["binding"])

    with pytest.raises(RunError, match="^PLANNING_REPOSITORY_SNAPSHOT_CHANGED$"):
        service.freeze_repository_snapshot(
            execution["id"], principal="owner", command_key=command_key
        )

    with sqlite3.connect(service.database) as db:
        assert json.loads(
            db.execute(
                "SELECT result FROM commands WHERE principal=? AND key=?", ("owner", command_key)
            ).fetchone()[0]
        ) == corrupted
    assert store.read(execution["binding"]) == verified
    assert authorities.capacity.snapshot() == before_capacity


def test_snapshot_concurrent_receipt_branch_rejects_corrupted_manifest(
    configured: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _, _, _, store, execution = snapshot_case(configured, tmp_path)
    command_key = "freeze"
    original_freeze = store.freeze

    def publish_then_replace_receipt(*args: Any, **kwargs: Any) -> dict[str, Any]:
        manifest = original_freeze(*args, **kwargs)
        corrupted = deepcopy(manifest)
        corrupted["base_sha"] = "0" * len(corrupted["base_sha"])
        with sqlite3.connect(service.database) as db:
            db.execute(
                "UPDATE commands SET result=? WHERE principal=? AND key=?",
                (json.dumps(corrupted, separators=(",", ":")), "owner", command_key),
            )
            db.commit()
        return manifest

    monkeypatch.setattr(store, "freeze", publish_then_replace_receipt)
    with pytest.raises(RunError, match="^PLANNING_REPOSITORY_SNAPSHOT_CHANGED$"):
        service.freeze_repository_snapshot(
            execution["id"], principal="owner", command_key=command_key
        )
    assert store.read(execution["binding"])["execution_id"] == execution["id"]


def test_cancelled_execution_cannot_create_a_first_repository_snapshot(
    configured: dict, tmp_path: Path
) -> None:
    service, run, intent, authorities = planning_case(tmp_path, configured)
    database = tmp_path / "snapshots.sqlite"
    service.snapshots = PlanningRepositorySnapshotStore(database)
    execution = service.begin(run["id"], intent["id"], principal="owner", command_key="begin")
    before = authorities.capacity.snapshot()
    service.cancel(execution["id"], principal="owner", command_key="cancel")
    with pytest.raises(RunError, match="^PLANNING_EXECUTION_CANCELLED$"):
        service.freeze_repository_snapshot(
            execution["id"], principal="owner", command_key="snapshot"
        )
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 0
    assert authorities.capacity.snapshot() == before


@pytest.mark.parametrize("revocation", ["cancel", "handoff", "project"])
def test_snapshot_publication_holds_authority_after_final_check(
    configured: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, revocation: str
) -> None:
    """A revocation cannot commit in the final authority-to-manifest interval."""
    registry = configured["registry"]
    project = registry.get(configured["id"])
    root = Path(project["repository"]["root"])
    (root / "src").mkdir(exist_ok=True)
    (root / "tests").mkdir(exist_ok=True)
    (root / "src" / "race.txt").write_bytes(b"snapshot race\n")
    (root / "tests" / "race.txt").write_bytes(b"snapshot race\n")
    subprocess.run(
        ["git", "-C", str(root), "add", "src/race.txt", "tests/race.txt"], check=True
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=x",
            "-c",
            "user.email=x@y.z",
            "commit",
            "-m",
            "race",
        ],
        check=True,
    )
    registry.update(
        project["id"],
        {
            "name": project["name"],
            "base_ref": project["repository"]["base_ref"],
            "target_branch": project["target_branch"],
            "allowed_target_branches": project["allowed_target_branches"],
        },
        expected_revision=project["revision"],
        command_key="registered-race-base",
        principal="owner",
    )
    configured.update(registry.get(project["id"]))
    configured["registry"] = registry
    service, run, intent, _ = planning_case(tmp_path, configured)
    database = tmp_path / "snapshots.sqlite"
    store = PlanningRepositorySnapshotStore(database)
    service.snapshots = store
    execution = service.begin(run["id"], intent["id"], principal="owner", command_key="begin")
    reached = Event()
    release = Event()
    original_connect = store._connect
    connections = 0
    handoff = (
        service.planner.propose_handoff(
            run["id"], handoff_request(0), command_key="handoff", principal="owner"
        )
        if revocation == "handoff"
        else None
    )

    def pause_before_publication() -> sqlite3.Connection:
        nonlocal connections
        connections += 1
        # The first connection is the missing-snapshot read. The second is
        # the manifest writer, immediately after the candidate's final check.
        if connections == 2:
            reached.set()
            assert release.wait(timeout=5)
        return original_connect()

    monkeypatch.setattr(store, "_connect", pause_before_publication)
    with ThreadPoolExecutor(max_workers=2) as workers:
        frozen = workers.submit(
            service.freeze_repository_snapshot,
            execution["id"],
            principal="owner",
            command_key="freeze",
        )
        assert reached.wait(timeout=5)
        if revocation == "cancel":

            def revoke() -> dict[str, Any]:
                return service.cancel(execution["id"], principal="owner", command_key="cancel")

        elif revocation == "handoff":
            assert handoff is not None

            def revoke() -> dict[str, Any]:
                return service.planner.decide_handoff(
                    run["id"],
                    {
                        "handoff_id": handoff["id"],
                        "handoff_digest": handoff["digest"],
                        "term": 1,
                        "decision": "approve",
                    },
                    command_key="decide-handoff",
                    principal="owner",
                )
        else:

            def revoke() -> dict[str, Any]:
                current = registry.get(project["id"])
                return registry.update(
                    project["id"],
                    {
                        "name": current["name"],
                        "base_ref": current["repository"]["base_ref"],
                        "target_branch": current["target_branch"],
                        "allowed_target_branches": current["allowed_target_branches"],
                    },
                    expected_revision=current["revision"],
                    command_key="project-update-during-freeze",
                    principal="owner",
                )
        revocation_finished = Event()

        def revoke_and_record() -> dict[str, Any]:
            result = revoke()
            revocation_finished.set()
            return result

        revoked = workers.submit(revoke_and_record)
        # bcc releases authority before the paused manifest transaction, so
        # this real cancellation/handoff completes here. A held guard must
        # instead make it wait until publication has finished.
        assert not revocation_finished.wait(timeout=0.3)
        release.set()
        frozen.result(timeout=5)
        revoked.result(timeout=5)
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 1


def test_freeze_command_reservation_prevents_cross_execution_publication(
    configured: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The durable command identity is claimed before the first CAS publish."""
    registry = configured["registry"]
    project = registry.get(configured["id"])
    root = Path(project["repository"]["root"])
    (root / "src").mkdir(exist_ok=True)
    (root / "tests").mkdir(exist_ok=True)
    (root / "src" / "reservation.txt").write_bytes(b"source")
    (root / "tests" / "reservation.txt").write_bytes(b"test")
    subprocess.run(
        ["git", "-C", str(root), "add", "src/reservation.txt", "tests/reservation.txt"],
        check=True,
    )
    subprocess.run(
        [
            "git", "-C", str(root), "-c", "user.name=x", "-c", "user.email=x@y.z",
            "commit", "-m", "reservation",
        ],
        check=True,
    )
    registry.update(
        project["id"],
        {
            "name": project["name"],
            "base_ref": project["repository"]["base_ref"],
            "target_branch": project["target_branch"],
            "allowed_target_branches": project["allowed_target_branches"],
        },
        expected_revision=project["revision"],
        command_key="reservation-base",
        principal="owner",
    )
    configured.update(registry.get(project["id"]))
    configured["registry"] = registry
    service, run, intent, _ = planning_case(tmp_path, configured)
    second_intent = service.planner.planning_intent(
        run["id"], term=1, command_key="intent-two", principal="lead"
    )
    first = service.begin(run["id"], intent["id"], principal="owner", command_key="begin-one")
    second = service.begin(
        run["id"], second_intent["id"], principal="owner", command_key="begin-two"
    )
    store = PlanningRepositorySnapshotStore(tmp_path / "snapshots.sqlite")
    service.snapshots = store
    entered, release = Event(), Event()
    publish = store._publish

    def slow_publish(sha: str, content: bytes) -> None:
        entered.set()
        assert release.wait(timeout=5)
        publish(sha, content)

    monkeypatch.setattr(store, "_publish", slow_publish)
    with ThreadPoolExecutor(max_workers=2) as workers:
        first_freeze = workers.submit(
            service.freeze_repository_snapshot,
            first["id"],
            principal="owner",
            command_key="same-freeze-key",
        )
        assert entered.wait(timeout=5)
        conflict = workers.submit(
            service.freeze_repository_snapshot,
            second["id"],
            principal="owner",
            command_key="same-freeze-key",
        )
        with pytest.raises(RunError, match="^IDEMPOTENCY_CONFLICT$"):
            conflict.result(timeout=2)
        release.set()
        first_freeze.result(timeout=5)
    with sqlite3.connect(store.database) as db:
        assert db.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 1


@pytest.mark.parametrize("field", ["term", "configuration", "authorization"])
def test_changed_trusted_run_record_rejects_unfrozen_execution_without_snapshot(
    configured: dict, tmp_path: Path, field: str
) -> None:
    """The v1 binding is rebuilt from Run SQLite, never trusted from execution JSON."""
    service, run, intent, authorities = planning_case(tmp_path, configured)
    database = tmp_path / "snapshots.sqlite"
    service.snapshots = PlanningRepositorySnapshotStore(database)
    execution = service.begin(run["id"], intent["id"], principal="owner", command_key="begin")
    with service.planner._transaction() as db:
        changed = service.planner._get(db, run["id"])
        if field == "term":
            changed["commander"]["term"] = 2
        elif field == "configuration":
            changed["configuration_snapshot"]["digest"] = "f" * 64
        else:
            changed["authorization_ceiling"]["read_paths"] = ["different"]
        service.planner._save(db, changed)
    before = authorities.capacity.snapshot()
    with pytest.raises(RunError, match="^PLANNING_EXECUTION_BINDING_STALE$"):
        service.freeze_repository_snapshot(execution["id"], principal="owner", command_key="freeze")
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM files").fetchone()[0] == 0
    assert authorities.capacity.snapshot() == before


@pytest.mark.parametrize("tamper", ["binding", "binding_sha256"])
def test_persisted_snapshot_binding_tamper_is_stable_and_does_not_create(
    configured: dict, tmp_path: Path, tamper: str
) -> None:
    service, run, intent, _ = planning_case(tmp_path, configured)
    database = tmp_path / "snapshots.sqlite"
    service.snapshots = PlanningRepositorySnapshotStore(database)
    execution = service.begin(run["id"], intent["id"], principal="owner", command_key="begin")
    with service._transaction() as db:
        changed = service._load(db, execution["id"])
        if tamper == "binding":
            changed["binding"]["budget_ref"] = "forged"
            changed["binding_sha256"] = digest(changed["binding"])
        else:
            changed["binding_sha256"] = "f" * 64
        service._save(db, changed)
    for operation in (
        lambda: service.freeze_repository_snapshot(
            execution["id"], principal="owner", command_key="freeze"
        ),
        lambda: service.read_repository_snapshot(execution["id"], principal="owner"),
    ):
        with pytest.raises(RunError, match="^PLANNING_EXECUTION_BINDING_STALE$"):
            operation()
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 0


@pytest.mark.parametrize("receipt", ["lost", "saved"])
def test_committed_snapshot_survives_commander_handoff_and_source_change(
    configured: dict, tmp_path: Path, receipt: str
) -> None:
    registry = configured["registry"]
    project = registry.get(configured["id"])
    root = Path(project["repository"]["root"])
    (root / "src").mkdir(exist_ok=True)
    (root / "tests").mkdir(exist_ok=True)
    (root / "src" / "snapshot.txt").write_bytes(b"original snapshot\n")
    (root / "tests" / "snapshot.txt").write_bytes(b"original test snapshot\n")
    subprocess.run(
        ["git", "-C", str(root), "add", "src/snapshot.txt", "tests/snapshot.txt"], check=True
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=x",
            "-c",
            "user.email=x@y.z",
            "commit",
            "-m",
            "snapshot",
        ],
        check=True,
    )
    registry.update(
        project["id"],
        {
            "name": project["name"],
            "base_ref": project["repository"]["base_ref"],
            "target_branch": project["target_branch"],
            "allowed_target_branches": project["allowed_target_branches"],
        },
        expected_revision=project["revision"],
        command_key="registered-snapshot-base",
        principal="owner",
    )
    configured.update(registry.get(project["id"]))
    configured["registry"] = registry
    service, run, intent, authorities = planning_case(tmp_path, configured)
    database = tmp_path / "snapshots.sqlite"
    store = PlanningRepositorySnapshotStore(database)
    service.snapshots = store
    execution = service.begin(run["id"], intent["id"], principal="owner", command_key="begin")

    # In the lost-receipt case, the real producer committed before its public
    # command receipt was saved.  This crosses the public command reservation
    # and actual snapshot publication boundary, then loses only the reply.
    # The saved case exercises command-ledger replay after a legitimate Run
    # Commander transition.
    if receipt == "lost":
        published: list[dict[str, Any]] = []
        original_freeze = store.freeze

        def lose_reply(*args: Any, **kwargs: Any) -> dict[str, Any]:
            value = original_freeze(*args, **kwargs)
            published.append(value)
            raise RunError("simulated reply loss")

        store.freeze = lose_reply  # type: ignore[method-assign]
        with pytest.raises(RunError, match="^simulated reply loss$"):
            service.freeze_repository_snapshot(
                execution["id"], principal="owner", command_key="freeze"
            )
        assert len(published) == 1
        committed = published[0]
    else:
        committed = service.freeze_repository_snapshot(
            execution["id"], principal="owner", command_key="freeze"
        )
    handoff = service.planner.propose_handoff(
        run["id"], handoff_request(0), command_key="handoff", principal="owner"
    )
    service.planner.decide_handoff(
        run["id"],
        {
            "handoff_id": handoff["id"],
            "handoff_digest": handoff["digest"],
            "term": 1,
            "decision": "approve",
        },
        command_key="decide-handoff",
        principal="owner",
    )
    (root / "src" / "snapshot.txt").write_bytes(b"later source bytes\n")
    subprocess.run(["git", "-C", str(root), "add", "src/snapshot.txt"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=x",
            "-c",
            "user.email=x@y.z",
            "commit",
            "-m",
            "later",
        ],
        check=True,
    )
    current = registry.get(project["id"])
    registry.update(
        project["id"],
        {
            "name": current["name"],
            "base_ref": current["repository"]["base_ref"],
            "target_branch": current["target_branch"],
            "allowed_target_branches": current["allowed_target_branches"],
        },
        expected_revision=current["revision"],
        command_key="later-source",
        principal="owner",
    )
    reopened = PlanningExecution(
        service.database, service.planner, snapshots=PlanningRepositorySnapshotStore(database)
    )
    before = authorities.capacity.snapshot()
    assert (
        reopened.freeze_repository_snapshot(
            execution["id"], principal="owner", command_key="freeze"
        )
        == committed
    )
    assert reopened.read_repository_snapshot(execution["id"], principal="owner")["content"] == {
        "src/snapshot.txt": b"original snapshot\n",
        "tests/snapshot.txt": b"original test snapshot\n",
    }
    assert authorities.capacity.snapshot() == before
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM files").fetchone()[0] == 2


def test_handoff_rejects_first_snapshot_for_original_execution(
    configured: dict, tmp_path: Path
) -> None:
    """A historical identity can recover evidence, never create it after handoff."""
    service, run, intent, _ = planning_case(tmp_path, configured)
    database = tmp_path / "snapshots.sqlite"
    service.snapshots = PlanningRepositorySnapshotStore(database)
    execution = service.begin(run["id"], intent["id"], principal="owner", command_key="begin")
    handoff = service.planner.propose_handoff(
        run["id"], handoff_request(0), command_key="handoff", principal="owner"
    )
    service.planner.decide_handoff(
        run["id"],
        {
            "handoff_id": handoff["id"],
            "handoff_digest": handoff["digest"],
            "term": 1,
            "decision": "approve",
        },
        command_key="decide-handoff",
        principal="owner",
    )

    with pytest.raises(RunError, match="^PLANNING_EXECUTION_BINDING_STALE$"):
        service.freeze_repository_snapshot(execution["id"], principal="owner", command_key="freeze")
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM files").fetchone()[0] == 0


def test_production_label_cannot_promote_a_test_double(configured: dict, tmp_path: Path) -> None:
    service, run, intent, authorities = planning_case(tmp_path, configured)
    execution = begin(service, run, intent, authorities)
    original = authorities.read_admission

    def mislabeled(binding: dict[str, Any]) -> object:
        result = original(binding)
        result["authority_kind"] = "production"
        return result

    service.admissions = type(
        "MislabeledAdmissions", (), {"read_admission": staticmethod(mislabeled)}
    )()
    blocked = service.reconcile(execution["id"], principal="owner")
    assert blocked["reason_codes"] == ["PLANNING_PRODUCTION_AUTHORITY_UNAVAILABLE"]


def test_capacity_reader_and_frozen_output_source_reject_substitution(
    configured: dict, tmp_path: Path
) -> None:
    registry = configured["registry"]
    service, run, intent, authorities = planning_case(tmp_path, configured)
    execution = begin(service, run, intent, authorities)
    original = authorities.read_admission

    def unrelated_receipt(binding: dict[str, Any]) -> object:
        value = original(binding)
        value["capacity_command_key"] = "never-issued-command"
        value["capacity_receipt"] = {"unrelated": "receipt"}
        return value

    authorities.read_admission = unrelated_receipt  # type: ignore[method-assign]
    rejected = service.reconcile(execution["id"], principal="owner")
    assert rejected["submission"] is None
    assert rejected["reason_codes"] == ["PLANNING_CAPACITY_RECEIPT_MISMATCH"]

    (tmp_path / "source").mkdir()
    service, run, intent, authorities = planning_case(
        tmp_path / "source", {**configured, "registry": registry}
    )
    execution = begin(service, run, intent, authorities)
    authorities.activate()
    assert service.reconcile(execution["id"], principal="owner")["state"] == "awaiting_output"
    authorities.output_source = "c" * 64
    rejected = service.submit(execution["id"], principal="owner", command_key="submit")
    assert rejected["submission"] is None
    assert rejected["reason_codes"] == ["PLANNING_OUTPUT_SOURCE_CHANGED"]
    assert service.planner.get(run["id"], principal="owner")["plans"] == []


def test_missing_or_changed_activation_receipt_never_submits(
    configured: dict, tmp_path: Path
) -> None:
    service, run, intent, authorities = planning_case(tmp_path, configured)
    execution = begin(service, run, intent, authorities)
    original = authorities.read_admission

    def missing_activation(binding: dict[str, Any]) -> object:
        value = original(binding)
        value["capacity_activation_command_key"] = "unissued-activation"
        value["capacity_activation_receipt"] = None
        return value

    authorities.read_admission = missing_activation  # type: ignore[method-assign]
    result = service.submit(execution["id"], principal="owner", command_key="submit")
    assert result["submission"] is None
    assert result["reason_codes"] == ["PLANNING_CAPACITY_ACTIVATION_MISMATCH"]
    assert service.planner.get(run["id"], principal="owner")["plans"] == []


def test_exact_run_receipt_recovers_lost_reply_without_resubmission(
    configured: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, run, intent, authorities = planning_case(tmp_path, configured)
    execution = begin(service, run, intent, authorities)
    authorities.activate()
    original = service.planner._submit_planning_execution_plan

    def lose_reply(*args: Any, **kwargs: Any) -> dict[str, Any]:
        original(*args, **kwargs)
        raise RuntimeError("reply lost after RunPlanner commit")

    monkeypatch.setattr(service.planner, "_submit_planning_execution_plan", lose_reply)
    with pytest.raises(RuntimeError, match="reply lost"):
        service.submit(execution["id"], principal="owner", command_key="submit")
    recovered = PlanningExecution(service.database, service.planner).submit(
        execution["id"], principal="owner", command_key="submit"
    )
    assert recovered["state"] == "submitted"
    assert service.planner.get(run["id"], principal="owner")["plans"] == [recovered["submission"]]


def test_reopened_unstarted_claim_without_receipt_never_submits(
    configured: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash after the SQLite claim is uncertain, never a fresh submission."""
    service, run, intent, authorities = planning_case(tmp_path, configured)
    execution = begin(service, run, intent, authorities)
    authorities.activate()

    def crash_after_claim(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise SystemExit("claim persisted before submit")

    monkeypatch.setattr(service, "_recover_or_submit", crash_after_claim)
    with pytest.raises(SystemExit, match="claim persisted"):
        service.submit(execution["id"], principal="owner", command_key="submit")
    claimed = service.get(execution["id"], principal="owner")
    assert claimed["state"] == "submit_claimed"
    assert claimed["submission_started"] is False
    assert service.planner.get(run["id"], principal="owner")["plans"] == []

    # A new controller has no live authorities to re-authorize the persisted
    # claim. It may read its exact Run receipt, but cannot make a new Plan.
    reopened = PlanningExecution(service.database, service.planner)
    recovered = reopened.submit(execution["id"], principal="owner", command_key="submit")
    assert recovered["state"] == "submission_unknown"
    assert recovered["reason_codes"] == ["PLANNING_EXECUTION_SUBMISSION_UNKNOWN"]
    assert service.planner.get(run["id"], principal="owner")["plans"] == []


def test_concurrent_submitters_create_at_most_one_plan(configured: dict, tmp_path: Path) -> None:
    service, run, intent, authorities = planning_case(tmp_path, configured)
    execution = begin(service, run, intent, authorities)
    authorities.activate()
    reopened = PlanningExecution(
        service.database,
        service.planner,
        admissions=authorities,
        outputs=authorities,
        capacity=authorities.capacity,
        allow_fixture_authorities=True,
    )

    with ThreadPoolExecutor(max_workers=2) as workers:
        submissions = list(
            workers.map(
                lambda subject: subject.submit(
                    execution["id"], principal="owner", command_key="submit"
                ),
                (service, reopened),
            )
        )

    assert all(
        submission["state"] in {"submission_unknown", "submitted"} for submission in submissions
    ), [submission["state"] for submission in submissions]
    plans = service.planner.get(run["id"], principal="owner")["plans"]
    assert len(plans) == 1
    recovered = PlanningExecution(service.database, service.planner).submit(
        execution["id"], principal="owner", command_key="submit"
    )
    assert recovered["state"] == "submitted"
    assert recovered["submission"] == plans[0]


def test_delayed_capture_cannot_rollback_a_submitted_plan(configured: dict, tmp_path: Path) -> None:
    service, run, intent, authorities = planning_case(tmp_path, configured)
    execution = begin(service, run, intent, authorities)
    authorities.activate()
    assert service.reconcile(execution["id"], principal="owner")["state"] == "awaiting_output"
    captured = Event()
    release = Event()
    original_output = authorities.read_output

    def delayed_output(*args: Any) -> object:
        evidence = original_output(*args)
        captured.set()
        assert release.wait(5), "delayed capture did not resume"
        return evidence

    authorities.read_output = delayed_output  # type: ignore[method-assign]
    with ThreadPoolExecutor(max_workers=1) as workers:
        delayed = workers.submit(
            service.submit, execution["id"], principal="owner", command_key="submit-delayed"
        )
        assert captured.wait(5), "delayed capture did not read output"
        authorities.read_output = original_output  # type: ignore[method-assign]
        submitted = service.submit(execution["id"], principal="owner", command_key="submit-first")
        assert submitted["state"] == "submitted"
        release.set()
        late = delayed.result(timeout=5)

    assert late["state"] == "submitted"
    assert (
        service.get(execution["id"], principal="owner")["submission_request"][
            "expected_plan_revision"
        ]
        == 0
    )
    recovered = service.submit(execution["id"], principal="owner", command_key="submit-recovery")
    assert recovered["state"] == "submitted"
    assert recovered["submission"] == submitted["submission"]


def test_delayed_capture_cannot_take_an_active_claim(
    configured: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, run, intent, authorities = planning_case(tmp_path, configured)
    execution = begin(service, run, intent, authorities)
    authorities.activate()
    assert service.reconcile(execution["id"], principal="owner")["state"] == "awaiting_output"
    captured = Event()
    release_capture = Event()
    claim_entered = Event()
    release_claim = Event()
    original_output = authorities.read_output

    def delayed_output(*args: Any) -> object:
        evidence = original_output(*args)
        captured.set()
        assert release_capture.wait(5), "delayed capture did not resume"
        return evidence

    authorities.read_output = delayed_output  # type: ignore[method-assign]
    claimer = PlanningExecution(
        service.database,
        service.planner,
        admissions=authorities,
        outputs=authorities,
        capacity=authorities.capacity,
        allow_fixture_authorities=True,
    )
    original_recover = claimer._recover_or_submit

    def pause_active_claim(*args: Any, **kwargs: Any) -> dict[str, Any]:
        claim_entered.set()
        assert release_claim.wait(5), "active claim did not resume"
        return original_recover(*args, **kwargs)

    monkeypatch.setattr(claimer, "_recover_or_submit", pause_active_claim)
    with ThreadPoolExecutor(max_workers=2) as workers:
        delayed = workers.submit(
            service.submit, execution["id"], principal="owner", command_key="submit-delayed"
        )
        assert captured.wait(5), "delayed capture did not read output"
        authorities.read_output = original_output  # type: ignore[method-assign]
        active = workers.submit(
            claimer.submit, execution["id"], principal="owner", command_key="submit-active"
        )
        assert claim_entered.wait(5), "active submit did not claim"
        release_capture.set()
        late = delayed.result(timeout=5)
        assert late["state"] == "submission_unknown"
        assert service.get(execution["id"], principal="owner")["state"] == "submit_claimed"
        release_claim.set()
        submitted = active.result(timeout=5)

    assert submitted["state"] == "submitted"
    assert service.planner.get(run["id"], principal="owner")["plans"] == [submitted["submission"]]


def test_matching_run_receipt_repairs_a_stale_submission_state(
    configured: dict, tmp_path: Path
) -> None:
    service, run, intent, authorities = planning_case(tmp_path, configured)
    execution = begin(service, run, intent, authorities)
    authorities.activate()
    submitted = service.submit(execution["id"], principal="owner", command_key="submit")
    with service._transaction() as db:
        stale = service._load(db, execution["id"])
        stale["state"] = "submit_claimed"
        stale["reason_codes"] = []
        service._save(db, stale)

    recovered = PlanningExecution(service.database, service.planner).submit(
        execution["id"], principal="owner", command_key="submit-recovery"
    )
    assert recovered["state"] == "submitted"
    assert recovered["submission"] == submitted["submission"]


def test_receipt_only_recovery_cannot_stop_a_claiming_submitter(
    configured: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, run, intent, authorities = planning_case(tmp_path, configured)
    execution = begin(service, run, intent, authorities)
    authorities.activate()
    entered = Event()
    release = Event()
    original = service._recover_or_submit

    def pause_after_claim(*args: Any, **kwargs: Any) -> dict[str, Any]:
        entered.set()
        assert release.wait(5), "first submit did not resume"
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "_recover_or_submit", pause_after_claim)
    receipt_only = PlanningExecution(service.database, service.planner)
    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(
            service.submit, execution["id"], principal="owner", command_key="submit"
        )
        assert entered.wait(5), "first submit did not persist its claim"
        recovered = workers.submit(
            receipt_only.submit, execution["id"], principal="owner", command_key="submit"
        ).result(timeout=5)
        assert recovered["state"] == "submission_unknown"
        assert service.get(execution["id"], principal="owner")["state"] == "submit_claimed"
        release.set()
        submitted = first.result(timeout=5)

    assert submitted["state"] == "submitted"
    assert service.planner.get(run["id"], principal="owner")["plans"] == [submitted["submission"]]


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("changed", "PLANNING_OUTPUT_SOURCE_CHANGED"),
        ("unavailable", "PLANNING_OUTPUT_AUTHORITY_UNAVAILABLE"),
        ("forbidden", "PLANNING_FIXTURE_AUTHORITY_FORBIDDEN"),
    ],
)
def test_output_authority_change_at_run_submission_guard_prevents_plan(
    configured: dict,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
    reason: str,
) -> None:
    service, run, intent, authorities = planning_case(tmp_path, configured)
    execution = begin(service, run, intent, authorities)
    authorities.activate()
    original = service.planner._submit_planning_execution_plan

    def change_source(*args: Any, **kwargs: Any) -> dict[str, Any]:
        if change == "changed":
            authorities.output_source = "c" * 64
        elif change == "unavailable":
            service.outputs = None
        else:
            service.allow_fixture_authorities = False
        return original(*args, **kwargs)

    monkeypatch.setattr(service.planner, "_submit_planning_execution_plan", change_source)
    rejected = service.submit(execution["id"], principal="owner", command_key="submit")
    assert rejected["submission"] is None
    assert rejected["reason_codes"] == [reason]
    assert service.planner.get(run["id"], principal="owner")["plans"] == []


def test_cancellation_observed_before_run_submit_prevents_plan(
    configured: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, run, intent, authorities = planning_case(tmp_path, configured)
    execution = begin(service, run, intent, authorities)
    authorities.activate()
    original = service.planner._submit_planning_execution_plan

    def cancel_at_boundary(*args: Any, **kwargs: Any) -> dict[str, Any]:
        result = service.cancel(execution["id"], principal="owner", command_key="cancel")
        assert result["cancel_requested"]
        return original(*args, **kwargs)

    monkeypatch.setattr(service.planner, "_submit_planning_execution_plan", cancel_at_boundary)
    result = service.submit(execution["id"], principal="owner", command_key="submit")
    assert result["submission"] is None
    assert service.planner.get(run["id"], principal="owner")["plans"] == []


def test_cancellation_during_submission_guard_source_read_prevents_plan(
    configured: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, run, intent, authorities = planning_case(tmp_path, configured)
    execution = begin(service, run, intent, authorities)
    authorities.activate()
    original_submit = service.planner._submit_planning_execution_plan
    original_source = authorities.read_source

    def cancel_during_source_read(binding: dict[str, Any]) -> object:
        cancelled = service.cancel(execution["id"], principal="owner", command_key="cancel")
        assert cancelled["cancel_requested"]
        return original_source(binding)

    def submit_with_guard_cancellation(*args: Any, **kwargs: Any) -> dict[str, Any]:
        authorities.read_source = cancel_during_source_read  # type: ignore[method-assign]
        return original_submit(*args, **kwargs)

    monkeypatch.setattr(
        service.planner, "_submit_planning_execution_plan", submit_with_guard_cancellation
    )
    rejected = service.submit(execution["id"], principal="owner", command_key="submit")
    assert rejected["submission"] is None
    assert service.planner.get(run["id"], principal="owner")["plans"] == []


def test_source_drift_after_capture_blocks_reopened_claim(
    configured: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, run, intent, authorities = planning_case(tmp_path, configured)
    execution = begin(service, run, intent, authorities)
    authorities.activate()

    def stop_before_claim(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise SystemExit("output captured")

    monkeypatch.setattr(service, "_claim_submission", stop_before_claim)
    with pytest.raises(SystemExit, match="output captured"):
        service.submit(execution["id"], principal="owner", command_key="submit")
    assert service.get(execution["id"], principal="owner")["state"] == "output_captured"
    authorities.output_source = "c" * 64
    reopened = PlanningExecution(
        service.database,
        service.planner,
        admissions=authorities,
        outputs=authorities,
        capacity=authorities.capacity,
        allow_fixture_authorities=True,
    )
    result = reopened.submit(execution["id"], principal="owner", command_key="submit")
    assert result["submission"] is None
    assert result["reason_codes"] == ["PLANNING_OUTPUT_SOURCE_CHANGED"]
    assert reopened.planner.get(run["id"], principal="owner")["plans"] == []


def test_begin_replay_survives_original_intent_state_change(
    configured: dict, tmp_path: Path
) -> None:
    service, run, intent, authorities = planning_case(tmp_path, configured)
    execution = begin(service, run, intent, authorities)
    authorities.activate()
    service.submit(execution["id"], principal="owner", command_key="submit")
    assert (
        service.begin(run["id"], intent["id"], principal="owner", command_key="begin") == execution
    )


def test_v2_output_uses_the_versioned_parser_and_run_submission(
    project: tuple[Any, dict[str, Any], Path], tmp_path: Path
) -> None:
    registry, configured, _ = project
    policy = registry.register_execution_policy(
        configured["id"], policy_request(configured), command_key="policy", principal="owner"
    )
    planner = RunPlanner(tmp_path / "v2-runs.sqlite", registry)
    run = planner.create(request_v2(configured, policy), command_key="run", principal="owner")
    intent = planner.planning_intent(run["id"], term=1, command_key="intent", principal="lead")
    authorities = FixtureAuthorities(capacity_store(tmp_path / "v2-capacity"))
    service = PlanningExecution(
        tmp_path / "v2-planning-execution.sqlite",
        planner,
        admissions=authorities,
        outputs=authorities,
        capacity=authorities.capacity,
        allow_fixture_authorities=True,
    )
    execution = service.begin(run["id"], intent["id"], principal="owner", command_key="begin")
    content = json.dumps(submit_request(run, intent)["plan"], separators=(",", ":")).encode()
    authorities.prepare(execution["binding"], content)
    authorities.activate()
    submitted = service.submit(execution["id"], principal="owner", command_key="submit")
    assert submitted["state"] == "submitted"
    assert submitted["submission"]["routing_binding"]["execution_policy"]["id"] == policy["id"]
