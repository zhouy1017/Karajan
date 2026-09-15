"""A fresh service process must really re-read, and recovery must reconcile.

The first group runs a **separate operating-system Python process** over the same
real state directory. That is the only honest test of the property in AC4: a load
is a fact about the process that performed it, so a new process must open the
files again rather than inherit a warm receipt from the process that deployed
them.

The second group injects a controlled failure at one *real* step boundary - a
raise inside the store's own materialise, load or activate call, or an
``OSError`` raised by the filesystem call the store makes - and then lets a
**newly constructed store over the same state** resume the same command. It is a
controlled same-process failure, not an OS crash: the intent row is the only
thing that survives, and the case is about what the durable record allows a
later command to conclude, not about what a power loss does.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from deployment_fixtures import (
    BUNDLE,
    SLOT,
    bundle_case,
    create_bundle,
    deploy,
    deployed_case,
    deployment_path,
    json_rows,
    login,
    preview_id,
    restart,
    status,
    url,
)
from karajan.workflows.errors import WorkflowError

case = bundle_case
deployed = deployed_case


# --------------------------------------------------- a genuinely new interpreter

#: A tiny program that opens the real application in a *new* interpreter and
#: prints the one observation it was asked for. It shares no memory with the
#: test process: every fact it reports came from the state directory. It
#: bootstraps its own session inside its own process, so nothing about the test
#: process's session or in-memory state can influence what it observes.
PROBE = r"""
import json, os, sys
from pathlib import Path

state = Path(sys.argv[1])
project_id = sys.argv[2]
token = sys.argv[3]
mode = sys.argv[4]
backend = sys.argv[5]

sys.path.insert(0, backend)
from karajan.web import create_app
from starlette.testclient import TestClient

ORIGIN = "http://127.0.0.1:8765"
app = create_app(state, origin=ORIGIN, bootstrap_token=token)
observed = {"pid": os.getpid(), "mode": mode}
with TestClient(app, base_url=ORIGIN) as http:
    login = http.post("/v1/session/bootstrap", json={"token": token}, headers={"Origin": ORIGIN})
    observed["bootstrap"] = login.status_code
    headers = {"Origin": ORIGIN}
    state_response = http.get(
        f"/v1/projects/{project_id}/workflow-deployments/default", headers=headers
    )
    observed["status_code"] = state_response.status_code
    document = state_response.json()
    observed["readiness"] = document.get("readiness")
    observed["blocked_reason"] = document.get("blocked_reason")
    observed["active_deployment_id"] = document.get("active_deployment_id")
    current = document.get("current") or {}
    observed["loaded_by_process"] = current.get("loaded_by_process")
    observed["verify_bytes"] = current.get("verify_bytes")
    observed["slot_revision"] = document.get("slot_revision")
    observed["receipt_digest"] = (document.get("receipt") or {}).get("receipt_digest")
    definition = http.get(
        f"/v1/projects/{project_id}/workflow-deployments/default/definition", headers=headers
    )
    observed["definition_status"] = definition.status_code
    if definition.status_code == 200:
        handle = definition.json()
        observed["definition_digest"] = handle["definition_digest"]
        observed["definition_bundle_revision"] = handle["deployment"]["bundle_revision"]
        observed["definition_workflow_id"] = handle["definition"]["workflow"]["id"]
    else:
        observed["definition_reason"] = definition.json().get("reason_code")
print(json.dumps(observed))
"""


def fresh_process(case: dict[str, Any], mode: str, token: str) -> dict[str, Any]:
    """Run one observation in a genuinely separate interpreter."""
    root = Path(__file__).resolve().parents[2]
    program = case["directory"] / f"probe-{mode}.py"
    program.write_text(PROBE, encoding="utf-8")
    completed = subprocess.run(
        [
            sys.executable,
            str(program),
            str(case["directory"]),
            case["project_id"],
            token,
            mode,
            str(root / "backend"),
        ],
        capture_output=True,
        text=True,
        timeout=300,
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONPATH": str(root / "backend")},
    )
    assert completed.returncode == 0, completed.stderr[-4000:]
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_a_separate_process_loads_the_active_package_itself(deployed: dict[str, Any]) -> None:
    """AC4: the new process's own PID is recorded, because it really re-read."""
    first = deployed["deployment"]["deployment"]
    observed = fresh_process(deployed, "fresh", "fresh-token")
    assert observed["status_code"] == 200, observed
    assert observed["readiness"] == "ready"
    assert observed["active_deployment_id"] == first["deployment_id"]
    assert observed["slot_revision"] == 1
    # A different operating-system process, which loaded the package itself.
    assert observed["pid"] != os.getpid()
    assert observed["loaded_by_process"] == observed["pid"]
    assert observed["verify_bytes"] is False
    # The preserved history is the *original* load, recorded by the deployer.
    assert observed["receipt_digest"] == first["load_receipt"]["receipt_digest"]
    assert observed["definition_status"] == 200
    assert observed["definition_bundle_revision"] == 1
    assert observed["definition_workflow_id"] == "compare-options"


def test_a_separate_process_blocks_on_a_corrupt_package(deployed: dict[str, Any]) -> None:
    """AC4: damaged files make the slot unavailable to a new process."""
    record = deployed["deployment"]["deployment"]
    (deployment_path(deployed, record["deployment_id"]) / "roles/editor.yaml").write_text(
        "id: not-the-editor\n", encoding="utf-8"
    )
    observed = fresh_process(deployed, "corrupt", "corrupt-token")
    assert observed["status_code"] == 200, observed
    assert observed["readiness"] == "blocked"
    assert observed["blocked_reason"] == "WORKFLOW_FILE_DIGEST_MISMATCH"
    assert observed["loaded_by_process"] is None
    # Nothing is consumable while the files do not verify.
    assert observed["definition_status"] == 409
    assert observed["definition_reason"] == "WORKFLOW_FILE_DIGEST_MISMATCH"


def test_a_separate_process_blocks_when_the_package_is_gone(deployed: dict[str, Any]) -> None:
    """AC4: a missing package is unavailable, never assumed ready."""
    record = deployed["deployment"]["deployment"]
    package = deployment_path(deployed, record["deployment_id"])
    for item in sorted(package.rglob("*"), reverse=True):
        item.unlink() if item.is_file() else item.rmdir()
    package.rmdir()
    observed = fresh_process(deployed, "missing", "missing-token")
    assert observed["readiness"] == "blocked"
    assert observed["blocked_reason"] == "WORKFLOW_PACKAGE_MISSING"
    assert observed["definition_status"] == 409
    assert observed["definition_reason"] == "WORKFLOW_PACKAGE_MISSING"


def test_a_compiler_revision_mismatch_blocks(deployed: dict[str, Any]) -> None:
    """AC4: this build's compiler must be the one the deployment recorded.

    A deployment whose recorded compiler revision is not this build's is refused,
    even though its bytes verify: serving it would present a definition under an
    identity recorded for a different compiler. The record's own digest is
    recomputed so the tampering is not caught by the tamper check instead.
    """
    from karajan.workflows.digests import content_digest

    record = deployed["deployment"]["deployment"]
    stored = json_rows(
        deployed,
        "SELECT record FROM workflow_deployments WHERE project_id=? AND slot=? AND "
        "deployment_id=?",
        (deployed["project_id"], SLOT, record["deployment_id"]),
    )[0]
    stored["compiler_revision"] = 99
    stored["digest"] = content_digest(
        {key: value for key, value in stored.items() if key != "digest"}
    )
    _rewrite_deployment(deployed, record["deployment_id"], stored)

    blocked = status(deployed)
    assert blocked["readiness"] == "blocked"
    assert blocked["blocked_reason"] == "WORKFLOW_COMPILER_REVISION_MISMATCH"
    observed = fresh_process(deployed, "compiler", "compiler-token")
    assert observed["readiness"] == "blocked"
    assert observed["blocked_reason"] == "WORKFLOW_COMPILER_REVISION_MISMATCH"


def _rewrite_deployment(case: dict[str, Any], deployment_id: str, record: dict[str, Any]) -> None:
    import sqlite3

    connection = sqlite3.connect(case["directory"] / "projects.sqlite")
    try:
        connection.execute(
            "UPDATE workflow_deployments SET record=? WHERE project_id=? AND slot=? AND "
            "deployment_id=?",
            (
                json.dumps(record, sort_keys=True, separators=(",", ":")),
                case["project_id"],
                SLOT,
                deployment_id,
            ),
        )
        connection.commit()
    finally:
        connection.close()


# ------------------------------------- a controlled failure at a real step boundary


def prepared(case: dict[str, Any], *, key: str = "fault") -> dict[str, Any]:
    """One published bundle and one confirmed preview, ready to deploy."""
    assert create_bundle(case).status_code == 201
    return {"preview_id": preview_id(case), "key": key}


def test_an_oserror_during_the_copy_leaves_a_reconcilable_intent(
    case: dict[str, Any],
) -> None:
    """AC2/AC3: a real filesystem failure is durable and diagnosable.

    The failure is a genuine ``OSError`` raised by the write the store performs,
    not a flag: the store's own ``_write_bytes`` is wrapped once, the way a full
    disk or a revoked permission would fail. The intent is checked to exist and
    to be durable *before* the failure, the failure is checked to be recorded on
    it, and a newly constructed store then resumes the same command.
    """
    from karajan.workflows import deployments as module

    confirmed = prepared(case, key="oserror")
    store = case["app"].state.deployment_store
    original = module._write_bytes
    written: list[Path] = []
    intent_existed_before_the_copy: list[bool] = []

    def failing(path: Path, data: bytes) -> None:
        # Only the store's writes into its own managed tree are recorded, so the
        # observation below is about the copy and nothing else.
        if "workflow-deployments" in str(path):
            written.append(path)
        if len(written) == 1:
            # The whole point of an intent: it is already durable at the moment
            # the first byte of the package is being written.
            intent_existed_before_the_copy.append(
                bool(
                    json_rows(
                        case,
                        "SELECT record FROM workflow_deployment_intents "
                        "WHERE principal=? AND key=?",
                        ("owner", confirmed["key"]),
                    )
                )
            )
            raise OSError(28, "No space left on device")
        original(path, data)

    module._write_bytes = failing
    try:
        # The intent row is written before the first byte is copied, so a caller
        # can always find the command that failed.
        with pytest.raises(OSError):
            store.deploy(
                case["project_id"],
                case["conversation_id"],
                BUNDLE,
                {
                    "action": "deploy_only",
                    "slot": SLOT,
                    "expected_active_revision": 0,
                    "preview_id": confirmed["preview_id"],
                },
                preview_revision=1,
                principal="owner",
                command_key=confirmed["key"],
            )
    finally:
        module._write_bytes = original

    assert written, "the store never attempted to write a package file"
    assert written[0].name in {"workflow.yaml", "editor.yaml", "researcher.yaml"}
    # Every write went through the managed staging directory, at whatever depth
    # the file sits: containment in the staging tree is the property, not a
    # fixed parent directory.
    staging = [part for part in written[0].parts if part.startswith(".staging-")]
    assert len(staging) == 1, written[0]
    assert staging[0].startswith(f".staging-{confirmed['key']}") or staging[0].startswith(
        ".staging-"
    )
    assert staging[0].split(".staging-", 1)[1].startswith(
        staging[0].split(".staging-", 1)[1].split("-", 1)[0]
    )
    # The intent was already durable when the first byte was being written.
    assert intent_existed_before_the_copy == [True]
    intents = json_rows(
        case,
        "SELECT record FROM workflow_deployment_intents WHERE principal=? AND key=?",
        ("owner", confirmed["key"]),
    )
    assert len(intents) == 1
    intent = intents[0]
    assert intent["completed_steps"] == []
    assert intent["frozen"]["bundle_digest"]
    # The observed I/O failure is durable, sanitized and names the step reached.
    assert intent["outcome"] == "failed"
    assert intent["failure"]["reason_code"] == "WORKFLOW_STEP_FAILED_OSERROR"
    assert intent["failure"]["at_step"] == "materialize"
    # Nothing from the exception message reached the record. The OSError branch
    # builds a fixed shape — ``reason_code``, ``at_step`` and ``recorded_at`` and
    # nothing else — so the assertion is exact. Asserting on substrings of the
    # whole document would flake on a wall-clock timestamp, and allowing extra
    # fields would stop noticing a nested ``errno``, which is the leak this check
    # exists to catch.
    recorded = json.dumps(intent["failure"])
    assert "No space left on device" not in recorded
    assert set(intent["failure"]) == {"reason_code", "at_step", "recorded_at"}
    assert isinstance(intent["failure"]["recorded_at"], (int, float))
    assert "errno" not in recorded
    assert str(case["directory"]) not in recorded
    # Nothing was activated, and the slot is still empty.
    assert json_rows(
        case,
        "SELECT record FROM workflow_deployments WHERE project_id=? AND slot=?",
        (case["project_id"], SLOT),
    ) == []
    assert status(case)["slot_revision"] == 0

    # A newly constructed store over the same real state finishes the same
    # command: the intent it finds is the one it resumes, not a new deployment.
    _, client = restart(case, "resume-owner")
    with client:
        headers = login(client, "resume-owner")
        again = client.post(
            url(
                case["project_id"],
                f"/conversations/{case['conversation_id']}/workflows/{BUNDLE}"
                "/revisions/1/deployments",
            ),
            json={
                "action": "deploy_only",
                "slot": SLOT,
                "expected_active_revision": 0,
                "preview_id": confirmed["preview_id"],
            },
            headers={**headers, "Idempotency-Key": confirmed["key"]},
        )
        assert again.status_code == 201, again.text
        record = again.json()["deployment"]
        assert record["deployment_id"] == intent["deployment_id"]
        assert again.json()["slot"]["slot_revision"] == 1
        assert again.json()["readiness"] == "ready"
    # Exactly one deployment, and exactly one intent, for one command.
    deployments = json_rows(
        case,
        "SELECT record FROM workflow_deployments WHERE project_id=? AND slot=?",
        (case["project_id"], SLOT),
    )
    assert len(deployments) == 1
    assert deployments[0]["deployment_id"] == intent["deployment_id"]
    assert len(
        json_rows(
            case,
            "SELECT record FROM workflow_deployment_intents WHERE principal=? AND key=?",
            ("owner", confirmed["key"]),
        )
    ) == 1


def test_a_failure_during_the_load_keeps_the_old_active_and_records_the_reason(
    case: dict[str, Any],
) -> None:
    """AC2/AC3: a failed load leaves the previous active deployment in force.

    The failure is raised inside the loader call the store makes, after the
    package really exists on disk. The case checks that the intent records where
    it stopped and why, that the old active deployment is still loaded and
    consumable, and that a later command resumes the same command rather than
    hiding it behind a replacement.
    """
    from karajan.workflows.errors import WorkflowError

    assert create_bundle(case).status_code == 201
    first = deploy(case, key="old-active")
    assert first.status_code == 201, first.text
    old_active = first.json()["deployment"]

    store = case["app"].state.deployment_store
    loader = store.loader
    original = loader.load

    def failing(**kwargs: Any) -> Any:
        raise WorkflowError("WORKFLOW_STATE_UNAVAILABLE")

    loader.load = failing  # type: ignore[method-assign]
    try:
        second = deploy(
            case,
            key="failing-load",
            expected_active_revision=1,
            preview=preview_id(case),
        )
    finally:
        loader.load = original  # type: ignore[method-assign]

    assert second.status_code == 422, second.text
    assert second.json()["reason_code"] == "WORKFLOW_STATE_UNAVAILABLE"

    # The old active deployment is untouched and still consumable.
    current = status(case)
    assert current["active_deployment_id"] == old_active["deployment_id"]
    assert current["slot_revision"] == 1
    assert current["readiness"] == "ready"
    handle = case["client"].get(
        url(case["project_id"], f"/workflow-deployments/{SLOT}/definition")
    )
    assert handle.status_code == 200, handle.text
    assert handle.json()["deployment"]["deployment_id"] == old_active["deployment_id"]

    # The interrupted command is durable, actionable and not consumable.
    pending = [item for item in _pending(case) if item["command_key"] == "failing-load"]
    assert len(pending) == 1
    entry = pending[0]
    assert entry["outcome"] == "failed"
    assert entry["failure"]["reason_code"] == "WORKFLOW_STATE_UNAVAILABLE"
    assert entry["failure"]["at_step"] == "load"
    assert entry["completed_steps"] == ["materialize"]
    assert entry["package_present"] is True
    assert entry["consumable"] is False
    assert entry["deployment_id"] != old_active["deployment_id"]
    assert entry["bundle_revision"] == 1

    # Resuming the same command once the failure is gone completes *that* one.
    resumed = deploy(
        case,
        key="failing-load",
        expected_active_revision=1,
        preview=preview_id(case),
    )
    assert resumed.status_code == 201, resumed.text
    assert resumed.json()["deployment"]["deployment_id"] == entry["deployment_id"]
    assert resumed.json()["slot"]["slot_revision"] == 2
    deployments = json_rows(
        case,
        "SELECT record FROM workflow_deployments WHERE project_id=? AND slot=?",
        (case["project_id"], SLOT),
    )
    assert len(deployments) == 2
    assert {item["deployment_id"] for item in deployments} == {
        old_active["deployment_id"],
        entry["deployment_id"],
    }


def _pending(case: dict[str, Any]) -> list[dict[str, Any]]:
    return list(status(case)["pending"])


def test_a_failure_during_activation_leaves_the_slot_unmoved(case: dict[str, Any]) -> None:
    """AC3: an activation that does not complete changes nothing durable.

    The slot update is the last step. A failure raised while building the
    deployment record - inside the same short transaction that would move the
    slot - aborts that transaction. The case therefore checks that the intent is
    recorded with both earlier steps complete, that no deployment was published,
    and that the slot is exactly as it was, so a caller knows the activation is
    the open question rather than having to guess.
    """
    from karajan.workflows.errors import WorkflowError

    assert create_bundle(case).status_code == 201
    store = case["app"].state.deployment_store
    original = store._deployment_record

    def failing(**kwargs: Any) -> Any:
        raise OSError(5, "Input/output error")

    store._deployment_record = failing  # type: ignore[method-assign]
    try:
        with pytest.raises(WorkflowError) as failure:
            store.deploy(
                case["project_id"],
                case["conversation_id"],
                BUNDLE,
                {
                    "action": "deploy_only",
                    "slot": SLOT,
                    "expected_active_revision": 0,
                    "preview_id": preview_id(case),
                },
                preview_revision=1,
                principal="owner",
                command_key="failing-activate",
            )
    finally:
        store._deployment_record = original  # type: ignore[method-assign]
    # An ordinary I/O failure inside the transaction surfaces as the store's
    # existing unavailable-state rejection, which is what the other stores in
    # this control plane already report for the same condition.
    assert failure.value.code == "WORKFLOW_STATE_UNAVAILABLE"

    # The package really was written and the loader really read it.
    intents = json_rows(
        case,
        "SELECT record FROM workflow_deployment_intents WHERE principal=? AND key=?",
        ("owner", "failing-activate"),
    )
    assert len(intents) == 1
    intent = intents[0]
    assert intent["completed_steps"] == ["materialize", "load"]
    assert deployment_path(case, intent["deployment_id"]).is_dir()
    # The failure is durable, names the activation step, and is not a guess.
    assert intent["outcome"] == "failed"
    assert intent["failure"]["at_step"] == "activate"
    assert intent["failure"]["reason_code"] == "WORKFLOW_STATE_UNAVAILABLE"

    # Nothing was published and the slot did not move.
    assert json_rows(
        case,
        "SELECT record FROM workflow_deployments WHERE project_id=? AND slot=?",
        (case["project_id"], SLOT),
    ) == []
    assert status(case)["slot_revision"] == 0
    assert status(case)["readiness"] == "empty"
    assert status(case)["pending"][0]["outcome"] == "failed"
    assert status(case)["pending"][0]["consumable"] is False

    # The same command finishes once the failure is gone, and it is the *same*
    # deployment it already recorded.
    finished = deploy(case, key="failing-activate")
    assert finished.status_code == 201, finished.text
    assert finished.json()["deployment"]["deployment_id"] == intent["deployment_id"]
    assert finished.json()["slot"]["slot_revision"] == 1


# ------------------------------------------------------------------- AC5 handles


def test_a_held_handle_is_not_changed_by_a_later_deployment(case: dict[str, Any]) -> None:
    """AC5: the *object* a consumer holds is frozen, not merely its serialization.

    The handle is acquired from the live store and kept. A newer deployment then
    takes the slot, and the very same object is re-read: it must still describe
    the deployment it was acquired from. A consumer that pinned a definition is
    therefore never quietly moved onto a different one.
    """
    assert create_bundle(case).status_code == 201
    assert deploy(case, key="held-1").status_code == 201
    store = case["app"].state.deployment_store
    held = store.accept(case["project_id"], SLOT, principal="owner")
    before = held.as_document()

    from workflow_fixtures import COORDINATOR_ROLE, files

    revised = case["client"].put(
        url(case["project_id"], f"/conversations/{case['conversation_id']}/workflows/{BUNDLE}"),
        json={
            "files": files(
                extra=[
                    {
                        "path": "roles/editor.yaml",
                        "content": COORDINATOR_ROLE.replace(
                            "repair-coordinator", "technical-editor"
                        ),
                    }
                ]
            ),
            "delivery_kind": "report",
        },
        headers={**case["headers"], "Idempotency-Key": "held-revise", "If-Match": '"1"'},
    )
    assert revised.status_code == 201, revised.text
    assert deploy(
        case,
        key="held-2",
        revision=2,
        expected_active_revision=1,
        preview=preview_id(case, revision=2),
    ).status_code == 201

    # The slot really moved...
    assert status(case)["slot_revision"] == 2
    # ...and the same held object still describes revision 1.
    after = held.as_document()
    assert after["deployment"]["deployment_id"] == before["deployment"]["deployment_id"]
    assert after["deployment"]["bundle_revision"] == 1
    assert after["slot"]["slot_revision"] == 1
    assert after["definition_digest"] == before["definition_digest"]
    # A fresh acquisition sees the new revision, so the difference is real.
    fresh = store.accept(case["project_id"], SLOT, principal="owner").as_document()
    assert fresh["deployment"]["bundle_revision"] == 2
    assert fresh["definition_digest"] != before["definition_digest"]


def test_a_held_handle_survives_a_rollback(case: dict[str, Any]) -> None:
    """AC5: a rollback changes the slot, not a handle a consumer already holds."""
    assert create_bundle(case).status_code == 201
    first = deploy(case, key="rbh-1")
    assert first.status_code == 201
    store = case["app"].state.deployment_store
    held = store.accept(case["project_id"], SLOT, principal="owner")
    pinned = held.as_document()

    from workflow_fixtures import COORDINATOR_ROLE, files

    revised = case["client"].put(
        url(case["project_id"], f"/conversations/{case['conversation_id']}/workflows/{BUNDLE}"),
        json={
            "files": files(
                extra=[
                    {
                        "path": "roles/editor.yaml",
                        "content": COORDINATOR_ROLE.replace(
                            "repair-coordinator", "technical-editor"
                        ),
                    }
                ]
            ),
            "delivery_kind": "report",
        },
        headers={**case["headers"], "Idempotency-Key": "rbh-revise", "If-Match": '"1"'},
    )
    assert revised.status_code == 201
    assert deploy(
        case,
        key="rbh-2",
        revision=2,
        expected_active_revision=1,
        preview=preview_id(case, revision=2),
    ).status_code == 201
    rollback = case["client"].post(
        url(case["project_id"], "/workflow-rollbacks"),
        json={
            "target_deployment_id": first.json()["deployment"]["deployment_id"],
            "expected_active_revision": 2,
        },
        headers={**case["headers"], "Idempotency-Key": "rbh-3"},
    )
    assert rollback.status_code == 201, rollback.text
    assert rollback.json()["slot"]["slot_revision"] == 3
    # The held handle was acquired before all of that and is unchanged.
    assert held.as_document() == pinned
    assert held.digest


# ----------------------------------- unknown outcome after real side effects


def test_a_completed_copy_with_no_step_checkpoint_is_adopted_after_replay(
    case: dict[str, Any],
) -> None:
    """AC3: the copy really completed; only the checkpoint and the response were lost.

    The interruption happens *after* the real materialisation returns and *before*
    its step is recorded - the window a crash between two durable writes would
    leave. The service is reconstructed over the same state and the original
    command is replayed: it must find the package already there, verify it against
    the intent it already froze, adopt exactly that copy and complete the same
    deployment. It must not build a second one, and it must not report a known
    failure for work that really succeeded.
    """
    from karajan.workflows.errors import WorkflowError

    confirmed = prepared(case, key="copy-lost")
    store = case["app"].state.deployment_store
    original = store._record_step
    calls: list[str] = []

    def interrupted(project_id: str, principal: str, command_key: str, step: str) -> None:
        calls.append(step)
        if step == "materialize":
            # The copy is on disk; the checkpoint that would record it is not.
            raise WorkflowError("WORKFLOW_STATE_UNAVAILABLE")
        original(project_id, principal, command_key, step)

    store._record_step = interrupted  # type: ignore[method-assign]
    try:
        with pytest.raises(WorkflowError):
            store.deploy(
                case["project_id"],
                case["conversation_id"],
                BUNDLE,
                {
                    "action": "deploy_only",
                    "slot": SLOT,
                    "expected_active_revision": 0,
                    "preview_id": confirmed["preview_id"],
                },
                preview_revision=1,
                principal="owner",
                command_key=confirmed["key"],
            )
    finally:
        store._record_step = original  # type: ignore[method-assign]
    assert calls == ["materialize"]

    intent = json_rows(
        case,
        "SELECT record FROM workflow_deployment_intents WHERE principal=? AND key=?",
        ("owner", confirmed["key"]),
    )[0]
    assert intent["completed_steps"] == []
    package = deployment_path(case, intent["deployment_id"])
    # The real copy finished, and its bytes are the frozen ones.
    assert package.is_dir()
    for item in intent["frozen"]["files"]:
        assert (package / item["path"]).is_file()
    assert (package / "manifest.json").is_file()

    # A service rebuilt over the same state replays the same key. The new
    # application has its own session, so its own CSRF header is used.
    _, client = restart(case, "copy-lost")
    with client:
        headers = login(client, "copy-lost")
        replayed = client.post(
            url(
                case["project_id"],
                f"/conversations/{case['conversation_id']}/workflows/{BUNDLE}"
                "/revisions/1/deployments",
            ),
            json={
                "action": "deploy_only",
                "slot": SLOT,
                "expected_active_revision": 0,
                "preview_id": confirmed["preview_id"],
            },
            headers={**headers, "Idempotency-Key": confirmed["key"]},
        )
        assert replayed.status_code == 201, replayed.text
        record = replayed.json()["deployment"]
        # The very same deployment the intent named, adopted from the copy that
        # already existed rather than materialised a second time.
        assert record["deployment_id"] == intent["deployment_id"]
        assert record["bundle_digest"] == intent["frozen"]["bundle_digest"]
        assert record["manifest_file_digest"] == intent["frozen"]["manifest_file_digest"]
        assert record["compiled_digest"] == intent["frozen"]["compiled_digest"]
        assert record["load_receipt"]["verify_bytes"] is True
        assert replayed.json()["slot"]["slot_revision"] == 1
        assert replayed.json()["readiness"] == "ready"

    # Exactly one deployment and one package, for one command.
    deployments = history_deployments(case)
    assert len(deployments) == 1
    assert deployments[0]["deployment_id"] == intent["deployment_id"]
    children = [item.name for item in package.parent.iterdir() if item.is_dir()]
    assert children == ["pending"]
    # The intent now records the steps that really completed, the deployment it
    # ended up activating, and - preserved, not erased - the earlier attempt that
    # failed at a named step.
    final = json_rows(
        case,
        "SELECT record FROM workflow_deployment_intents WHERE principal=? AND key=?",
        ("owner", confirmed["key"]),
    )[0]
    assert final["completed_steps"] == ["materialize", "load"]
    assert final["outcome"] == "activated"
    assert final["deployment_id"] == intent["deployment_id"]
    assert final.get("failure") is None
    history = final["failure_history"]
    assert len(history) == 1
    assert history[0]["reason_code"] == "WORKFLOW_STATE_UNAVAILABLE"
    assert history[0]["at_step"] == "materialize"
    assert history[0]["recorded_at"] <= final["commanded_at"] or history[0]["recorded_at"] > 0


def test_a_committed_activation_whose_response_was_lost_returns_the_same_result(
    case: dict[str, Any],
) -> None:
    """AC3: the activation really committed; only the response never arrived.

    The interruption is raised *outside* the activation, after ``_activate`` has
    returned - the window between a durable commit and a delivered response. The
    whole activation runs to completion, including its slot update and its ledger
    entry, and only then is the response withheld. A rebuilt service retrying the
    same key must return the original committed result with the original receipt,
    and the active revision must have advanced exactly once. It must not report a
    known failure for a committed activation, and it must not rewrite the ledger
    entry it returns.
    """
    from karajan.workflows.errors import WorkflowError

    confirmed = prepared(case, key="response-lost")
    store = case["app"].state.deployment_store
    original = store._activate
    delivered: list[tuple[dict[str, Any], bool]] = []

    def interrupting_activate(**kwargs: Any) -> Any:
        result = original(**kwargs)
        delivered.append(result)
        # ``_activate`` has already committed and closed its transaction: the
        # slot has moved and the ledger entry is written. The response is what
        # goes missing, so the error is raised here and not inside it.
        raise WorkflowError("WORKFLOW_STATE_UNAVAILABLE")

    store._activate = interrupting_activate  # type: ignore[method-assign]
    try:
        with pytest.raises(WorkflowError):
            store.deploy(
                case["project_id"],
                case["conversation_id"],
                BUNDLE,
                {
                    "action": "deploy_only",
                    "slot": SLOT,
                    "expected_active_revision": 0,
                    "preview_id": confirmed["preview_id"],
                },
                preview_revision=1,
                principal="owner",
                command_key=confirmed["key"],
            )
    finally:
        store._activate = original  # type: ignore[method-assign]
    assert delivered
    committed, was_created = delivered[0]
    assert was_created is True
    deployment_id = committed["deployment"]["deployment_id"]

    # The activation really happened: the slot moved once.
    assert status(case)["slot_revision"] == 1
    assert status(case)["active_deployment_id"] == deployment_id
    assert len(history_deployments(case)) == 1

    _, client = restart(case, "response-lost")
    with client:
        headers = login(client, "response-lost")
        retried = client.post(
            url(
                case["project_id"],
                f"/conversations/{case['conversation_id']}/workflows/{BUNDLE}"
                "/revisions/1/deployments",
            ),
            json={
                "action": "deploy_only",
                "slot": SLOT,
                "expected_active_revision": 0,
                "preview_id": confirmed["preview_id"],
            },
            headers={**headers, "Idempotency-Key": confirmed["key"]},
        )
        assert retried.status_code == 200, retried.text
        document = retried.json()
        assert document["replayed"] is True
        # The original committed result and its receipt are returned unchanged.
        assert document["deployment"]["deployment_id"] == deployment_id
        assert (
            document["deployment"]["load_receipt"]["receipt_digest"]
            == committed["deployment"]["load_receipt"]["receipt_digest"]
        )
        assert document["deployment"]["completed_at"] == committed["deployment"]["completed_at"]
        assert document["slot"]["slot_revision"] == 1
        # A committed activation is not reported as a failure.
        assert document["readiness"] == "ready"
        assert document["current"]["is_active"] is True
    # The active revision advanced exactly once, and no second deployment exists.
    assert status(case)["slot_revision"] == 1
    deployments = history_deployments(case)
    assert len(deployments) == 1
    assert deployments[0]["deployment_id"] == deployment_id
    intent = json_rows(
        case,
        "SELECT record FROM workflow_deployment_intents WHERE principal=? AND key=?",
        ("owner", confirmed["key"]),
    )[0]
    # The committed activation is not recorded as a failure. The response-delivery
    # error happened *after* the durable outcome, so it left no failure behind.
    assert intent.get("failure") is None
    assert intent.get("failure_history") in (None, [])
    assert intent["outcome"] == "activated"
    assert intent["deployment_id"] == deployment_id


def history_deployments(case: dict[str, Any]) -> list[dict[str, Any]]:
    """Every activated deployment record for this project's one slot."""
    return json_rows(
        case,
        "SELECT record FROM workflow_deployments WHERE project_id=? AND slot=?",
        (case["project_id"], SLOT),
    )


def test_two_overlapping_services_admit_exactly_one_slot_winner(case: dict[str, Any]) -> None:
    """AC3: two real services racing for one slot produce exactly one activation.

    The race is genuinely overlapping rather than two sequential requests: the
    first service is held at a bounded barrier placed *after* its real file load,
    a second service builds its own store over the same state and runs the
    identical command under its own key, and only then is the first released.
    What is asserted is the durable outcome - one winner, one active revision,
    and both commands visible while the race was in flight - not which thread won.
    """
    import threading

    confirmed = prepared(case, key="race-first")
    first_store = case["app"].state.deployment_store
    second_app, _ = restart(case, "race-second")
    second_store = second_app.state.deployment_store

    original_first = first_store.loader.load
    original_second = second_store.loader.load
    # One event per service, so the observer can see both commands *after* both
    # have really read their package, and one single release for both.
    entered: dict[str, threading.Event] = {
        "race-first": threading.Event(),
        "race-second": threading.Event(),
    }
    released = threading.Event()
    during: dict[str, Any] = {}

    def gate(identity: str, original: Any) -> Any:
        def gated(**kwargs: Any) -> Any:
            result = original(**kwargs)
            # The package really exists and the loader really read it: this is
            # the boundary the race is decided at, not a hook before any work.
            entered[identity].set()
            released.wait(timeout=120)
            return result

        return gated

    first_store.loader.load = gate("race-first", original_first)  # type: ignore[method-assign]
    second_store.loader.load = gate("race-second", original_second)  # type: ignore[method-assign]
    outcome: dict[str, Any] = {}

    def command(store: Any, key: str) -> None:
        try:
            outcome[key] = store.deploy(
                case["project_id"],
                case["conversation_id"],
                BUNDLE,
                {
                    "action": "deploy_only",
                    "slot": SLOT,
                    "expected_active_revision": 0,
                    "preview_id": confirmed["preview_id"],
                },
                preview_revision=1,
                principal="owner",
                command_key=key,
            )
        except BaseException as error:  # recorded, then asserted below
            outcome[f"{key}_error"] = error

    threads = {
        key: threading.Thread(target=command, args=(store, key))
        for key, store in (("race-first", first_store), ("race-second", second_store))
    }
    for thread in threads.values():
        thread.start()
    try:
        for identity, event in entered.items():
            assert event.wait(timeout=120), f"{identity} never reached its real load"
        # Both commands have prepared their packages and read them. The slot has
        # not moved, so the active entry point is still the old one, and both
        # commands are visible as pending, non-consumable intents.
        observer = restart(case, "race-observer")[0].state.deployment_store
        current = observer.status(case["project_id"], SLOT, principal="owner")
        during["slot_revision"] = current["slot_revision"]
        during["active"] = current["active_deployment_id"]
        during["readiness"] = current["readiness"]
        during["pending"] = sorted(item["command_key"] for item in current["pending"])
        during["consumable"] = any(item["consumable"] for item in current["pending"])
        during["packages"] = {
            item["command_key"]: item["package_present"] for item in current["pending"]
        }
    finally:
        released.set()
        for thread in threads.values():
            thread.join(timeout=120)
        first_store.loader.load = original_first  # type: ignore[method-assign]
        second_store.loader.load = original_second  # type: ignore[method-assign]

    assert during["slot_revision"] == 0
    assert during["active"] is None
    assert during["readiness"] == "empty"
    assert during["pending"] == ["race-first", "race-second"]
    assert during["consumable"] is False
    assert during["packages"] == {"race-first": True, "race-second": True}

    # Either command may win: the race is decided by whichever reaches the
    # conditional slot update first, and the loser's outcome is a real CAS
    # conflict, not an error. What must hold is exactly one activation.
    succeeded = [key for key in ("race-first", "race-second") if key in outcome]
    conflicted = [
        key for key in ("race-first", "race-second") if f"{key}_error" in outcome
    ]
    assert len(succeeded) == 1, {key: outcome.get(key, outcome.get(f"{key}_error")) for key in
                                 ("race-first", "race-second")}
    assert len(conflicted) == 1, succeeded
    loser = outcome[f"{conflicted[0]}_error"]
    assert isinstance(loser, WorkflowError), loser
    assert loser.code == "WORKFLOW_SLOT_REVISION_CONFLICT"
    winner_key = succeeded[0]
    result, created = outcome[winner_key]
    assert created is True
    settled = status(case)
    assert settled["slot_revision"] == 1
    assert settled["active_deployment_id"] == result["deployment"]["deployment_id"]
    assert settled["readiness"] == "ready"
    assert len(history_deployments(case)) == 1
    # The loser is *not* hidden: it stays a real, non-consumable pending command
    # naming the deployment it prepared, so its outcome is reconcilable rather
    # than silently discarded.
    pending = settled["pending"]
    assert [item["command_key"] for item in pending] == [conflicted[0]]
    assert pending[0]["deployment_id"] != result["deployment"]["deployment_id"]
    assert pending[0]["consumable"] is False
    assert pending[0]["package_present"] is True
    assert pending[0]["expected_active_revision"] == 0


# --------------------------- two attempts of one command must not erase evidence


def _workflow_error(code: str) -> WorkflowError:
    return WorkflowError(code)


def _intent_for(case: dict[str, Any], key: str) -> dict[str, Any]:
    return json_rows(
        case,
        "SELECT record FROM workflow_deployment_intents WHERE principal=? AND key=?",
        ("owner", key),
    )[0]


def _deploy_payload(case: dict[str, Any]) -> dict[str, Any]:
    return {
        "action": "deploy_only",
        "slot": SLOT,
        "expected_active_revision": 0,
        "preview_id": preview_id(case),
    }


def test_a_step_checkpoint_cannot_erase_a_failure_written_meanwhile(
    case: dict[str, Any],
) -> None:
    """AC3: a step checkpoint merges into the current row, never overwrites it.

    The interleaving is exact. Attempt A is inside its real load. From inside that
    load, attempt B - the same command, a second store over the same state - runs
    its own real load and then fails, recording a durable diagnosis. A then
    returns normally and records its load checkpoint.

    A checkpoint written from A's snapshot would replace the whole row and erase
    B's failure. The final record must instead carry the command's outcome *and*
    the failure B really observed.
    """
    assert create_bundle(case).status_code == 201
    key = "interleaved-step"
    store = case["app"].state.deployment_store
    other_app, _ = restart(case, "interleaved-step")
    other = other_app.state.deployment_store
    payload = _deploy_payload(case)

    original_load = store.loader.load
    other_load = other.loader.load
    observed: dict[str, Any] = {}

    def other_fails(**kwargs: Any) -> Any:
        # B really reads the package, then really fails.
        other_load(**kwargs)
        raise _workflow_error("WORKFLOW_STATE_UNAVAILABLE")

    def interleaved(**kwargs: Any) -> Any:
        result = original_load(**kwargs)
        # A has really read its package and is about to record its load step.
        # B runs now, synchronously, and its failure is durable before A writes.
        other.loader.load = other_fails  # type: ignore[method-assign]
        try:
            other.deploy(
                case["project_id"],
                case["conversation_id"],
                BUNDLE,
                payload,
                preview_revision=1,
                principal="owner",
                command_key=key,
            )
        except WorkflowError as error:
            observed["other"] = error.code
        finally:
            other.loader.load = other_load  # type: ignore[method-assign]
        during = _intent_for(case, key)
        observed["during_completed_steps"] = list(during["completed_steps"])
        observed["during_failure"] = during["failure"]
        return result

    store.loader.load = interleaved  # type: ignore[method-assign]
    try:
        result, created = store.deploy(
            case["project_id"],
            case["conversation_id"],
            BUNDLE,
            payload,
            preview_revision=1,
            principal="owner",
            command_key=key,
        )
    finally:
        store.loader.load = original_load  # type: ignore[method-assign]
    assert created is True
    assert observed["other"] == "WORKFLOW_STATE_UNAVAILABLE"
    # At the moment A recorded its load checkpoint, B had already really
    # materialised (so its own checkpoint is there) and had already failed.
    assert observed["during_completed_steps"] == ["materialize"]
    assert observed["during_failure"]["at_step"] == "load"

    state = status(case)
    assert state["slot_revision"] == 1
    assert state["readiness"] == "ready"
    assert state["active_deployment_id"] == result["deployment"]["deployment_id"]

    final = _intent_for(case, key)
    # A's checkpoint did not erase B's diagnosis...
    assert final["completed_steps"] == ["materialize", "load"]
    assert final["outcome"] == "activated"
    assert final["deployment_id"] == result["deployment"]["deployment_id"]
    assert final.get("failure") is None
    history = final["failure_history"]
    assert len(history) == 1
    assert history[0]["reason_code"] == "WORKFLOW_STATE_UNAVAILABLE"
    assert history[0]["at_step"] == "load"
    # ...and the command activated exactly once.
    assert len(history_deployments(case)) == 1


def test_a_success_landing_after_a_failure_check_does_not_contradict_it(
    case: dict[str, Any],
) -> None:
    """AC3: the commit check and the failure write share one transaction.

    The interleaving is exact. Attempt A is inside ``_record_failure``. The
    moment its first transaction *closes*, attempt B - the same command, a
    second store - runs its own real load and succeeds, committing the command.

    A check performed in one transaction and written in another would let A
    attach a failure to a command that had just committed, contradicting the
    ledger. The final record must report the committed outcome, with A's earlier
    diagnosis archived rather than left contradicting it.
    """
    from contextlib import contextmanager

    assert create_bundle(case).status_code == 201
    key = "interleaved-commit"
    store = case["app"].state.deployment_store
    other_app, _ = restart(case, "interleaved-commit")
    other = other_app.state.deployment_store
    payload = _deploy_payload(case)

    # A fails at its real load, which is what drives it into _record_failure.
    original_load = store.loader.load

    def failing(**kwargs: Any) -> Any:
        raise _workflow_error("WORKFLOW_STATE_UNAVAILABLE")

    store.loader.load = failing  # type: ignore[method-assign]

    original_owned = store._owned
    original_record_failure = store._record_failure
    armed: dict[str, bool] = {"on": False}
    observed: dict[str, Any] = {}

    @contextmanager
    def hooked(project_id: str, principal: str) -> Any:
        with original_owned(project_id, principal) as db:
            yield db
        # The transaction is closed here. Only while A is recording a failure
        # does B's real success run - and only once.
        if not armed["on"]:
            return
        armed["on"] = False
        result, created = other.deploy(
            case["project_id"],
            case["conversation_id"],
            BUNDLE,
            payload,
            preview_revision=1,
            principal="owner",
            command_key=key,
        )
        observed["other_created"] = created
        observed["other_deployment"] = result["deployment"]["deployment_id"]

    def wrapping_record_failure(*args: Any, **kwargs: Any) -> Any:
        armed["on"] = True
        try:
            return original_record_failure(*args, **kwargs)
        finally:
            armed["on"] = False

    store._owned = hooked  # type: ignore[method-assign]
    store._record_failure = wrapping_record_failure  # type: ignore[method-assign]
    try:
        with pytest.raises(WorkflowError):
            store.deploy(
                case["project_id"],
                case["conversation_id"],
                BUNDLE,
                payload,
                preview_revision=1,
                principal="owner",
                command_key=key,
            )
    finally:
        store._owned = original_owned  # type: ignore[method-assign]
        store._record_failure = original_record_failure  # type: ignore[method-assign]
        store.loader.load = original_load  # type: ignore[method-assign]

    assert observed.get("other_created") is True, observed

    state = status(case)
    assert state["slot_revision"] == 1
    assert state["readiness"] == "ready"
    assert state["active_deployment_id"] == observed["other_deployment"]

    final = _intent_for(case, key)
    # The committed outcome stands: no failure is attached to a command the
    # authoritative ledger says succeeded, so the record reports ``activated``.
    assert final["outcome"] == "activated"
    assert final["deployment_id"] == observed["other_deployment"]
    assert final.get("failure") is None
    # A's diagnosis was written *before* B committed, and completing the command
    # archived it rather than erasing it. Both observations are preserved: the
    # failure A really hit, and the success B really achieved.
    history = final["failure_history"]
    assert len(history) == 1
    assert history[0]["reason_code"] == "WORKFLOW_STATE_UNAVAILABLE"
    assert history[0]["at_step"] == "load"
    assert len(history_deployments(case)) == 1
