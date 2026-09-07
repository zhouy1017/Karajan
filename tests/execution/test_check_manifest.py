"""Deterministic Check attempts use the same Host without inventing a Profile."""

import json
import os
import sys
import time
from copy import deepcopy
from dataclasses import replace

import pytest
from karajan.contracts.probe import AttemptManifest
from karajan.execution import (
    Activation,
    CheckAttemptManifest,
    LaunchDenied,
    ProbeCrash,
    ProcessSpec,
    RunnerHost,
    StartConflict,
    parse_host_manifest,
    parse_host_manifest_json,
)
from pydantic import ValidationError
from test_runnerhost import manifest as model_manifest


def check_manifest():
    return CheckAttemptManifest(
        schema_version="karajan.check-attempt.v1",
        id="check-attempt-1",
        fence=1,
        role="check",
        authorization_ref="approved-plan-1",
        budget_ref="approved-budget-1",
        permissions=["candidate-check"],
        environment_id="python-check-1",
        environment_revision=2,
        environment_source_sha256="a" * 64,
        execution_sha256="b" * 64,
    )


def test_check_manifest_roundtrips_and_prepares_without_model_identity(tmp_path):
    attempt = check_manifest()
    assert parse_host_manifest(attempt.model_dump()) == attempt
    assert parse_host_manifest_json(attempt.model_dump_json()) == attempt
    assert set(attempt.model_dump()) == {
        "schema_version",
        "id",
        "fence",
        "role",
        "authorization_ref",
        "budget_ref",
        "permissions",
        "environment_id",
        "environment_revision",
        "environment_source_sha256",
        "execution_sha256",
    }
    host = RunnerHost(tmp_path / "host")
    spec = ProcessSpec((sys.executable, "-c", "pass"), tmp_path)
    first = host.prepare(attempt, "check-start", spec)
    reopened = RunnerHost(host.directory, existing_only=True)
    assert reopened.prepare(attempt, "check-start", spec) == first
    assert first.state == "prepared" and first.supervisor is None


def activation(attempt):
    return Activation(
        "check-activation",
        attempt.id,
        attempt.fence,
        attempt.authorization_ref,
        attempt.budget_ref,
        time.time() + 60,
    )


def wait_json(path):
    until = time.monotonic() + 8
    while not path.exists() and time.monotonic() < until:
        time.sleep(0.02)
    assert path.exists(), f"Check child did not report {path.name}"
    return json.loads(path.read_text())


@pytest.fixture
def check_runner(tmp_path):
    host = RunnerHost(tmp_path / "host")
    attempt = check_manifest()
    script = tmp_path / "check_child.py"
    script.write_text(
        # Launch the base interpreter: the Windows venv exe is a redirector.
        f"import sys\nsys.path[:0] = {sys.path!r}\n"
        + """import json, time
from dataclasses import asdict
from pathlib import Path
from karajan.execution import RunnerHost, LaunchDenied
host = RunnerHost(Path(sys.argv[1]), existing_only=True)
output = Path(sys.argv[2])
identity = host.wait_for_runner_registration('check-attempt-1')
kwargs = dict(fence=1, authorization_ref='approved-plan-1')
with host.current_runner_guard('check-attempt-1', **kwargs) as current:
    assert current == identity
    runner = asdict(current)
with host.current_fence_guard('check-attempt-1', **kwargs) as fence:
    output.write_text(json.dumps({'runner': runner, 'fence': fence}))
deadline = time.monotonic() + 15
while not output.with_suffix('.continue').exists() and time.monotonic() < deadline:
    time.sleep(0.02)
try:
    with host.current_runner_guard('check-attempt-1', **kwargs):
        result = 'allowed'
except LaunchDenied as error:
    result = str(error)
output.with_suffix('.after').write_text(json.dumps(result))
"""
    )
    output = tmp_path / "observation.json"
    spec = ProcessSpec(
        (sys._base_executable, str(script), str(host.directory), str(output)),
        tmp_path,
        20,
    )
    host.prepare(attempt, "check-start", spec)
    host.initialize_control_once(
        attempt.id,
        prepared_id="check-start",
        fence=1,
        authorization_ref=attempt.authorization_ref,
    )
    accepted = activation(attempt)
    try:
        host.start("check-start", accepted)
        yield host, attempt, accepted, spec, output
    finally:
        host.cancel(attempt.id, "check-cleanup")


def test_real_check_child_uses_environment_fence_and_replays_original_launch(check_runner):
    host, attempt, accepted, spec, output = check_runner
    observation = wait_json(output)
    assert observation["runner"]["pid"] != os.getpid()
    assert observation["fence"]["environment"] == {
        "id": "python-check-1",
        "revision": 2,
        "source_sha256": "a" * 64,
    }
    assert observation["fence"]["execution_sha256"] == "b" * 64
    assert observation["fence"]["activation_allowed"] is False
    assert "profile" not in observation["fence"]
    reopened = RunnerHost(host.directory, existing_only=True)
    original = reopened.inspect(attempt.id)
    assert reopened.prepare(attempt, "check-start", spec).prepared_id == original.prepared_id
    assert reopened.start("check-start", accepted).supervisor == original.supervisor
    assert any(p.pid == observation["runner"]["pid"] for p in original.processes)
    with pytest.raises(LaunchDenied, match="RUNNER_IDENTITY_NOT_CURRENT"):
        with reopened.current_runner_guard(
            attempt.id, fence=1, authorization_ref=attempt.authorization_ref
        ):
            pytest.fail("Controller cannot inherit the Check child's authority")
    output.with_suffix(".continue").touch()
    assert wait_json(output.with_suffix(".after")) == "allowed"


@pytest.mark.parametrize("role", ["commander", "worker", "reviewer"])
def test_original_model_json_shape_stays_exactly_compatible(role):
    original = model_manifest().model_copy(update={"role": role})
    document = original.model_dump_json()
    parsed = parse_host_manifest_json(document)
    assert type(parsed) is AttemptManifest
    assert parsed.model_dump_json() == document
    assert "schema_version" not in parsed.model_dump()
    assert parse_host_manifest(original.model_dump()) == original


@pytest.mark.parametrize(
    "change",
    [
        {"role": "worker"},
        {"role": "unknown"},
        {"schema_version": "karajan.check-attempt.v2"},
        {"environment_revision": True},
        {"fence": "1"},
        {"environment_source_sha256": "A" * 64},
        {"execution_sha256": "invalid"},
        {"profile_id": "invented-profile"},
        {"requested_binding": model_manifest().requested_binding.model_dump()},
    ],
)
def test_wrong_or_mixed_check_protocols_fail_both_parsers(change):
    document = check_manifest().model_dump() | change
    with pytest.raises(ValidationError):
        parse_host_manifest(document)
    with pytest.raises(ValidationError):
        parse_host_manifest_json(json.dumps(document))


def test_missing_discriminator_or_check_schema_cannot_fall_back_to_model():
    for field in ("role", "schema_version"):
        document = check_manifest().model_dump()
        del document[field]
        with pytest.raises(ValidationError):
            parse_host_manifest(document)


def test_changed_model_objects_cannot_bypass_persisted_protocol_validation(tmp_path):
    host = RunnerHost(tmp_path / "host")
    changed = check_manifest().model_copy(update={"environment_revision": "2"})
    before = host.database.read_bytes()
    with pytest.warns(UserWarning, match="serializer warnings"), pytest.raises(ValidationError):
        host.prepare(changed, "check-start", ProcessSpec((sys.executable, "-c", "pass"), tmp_path))
    assert host.database.read_bytes() == before
    with pytest.raises(ValidationError):
        parse_host_manifest(changed)


@pytest.fixture
def prepared_check(tmp_path):
    host = RunnerHost(tmp_path / "host")
    attempt = check_manifest()
    output = tmp_path / "execution-count.txt"
    spec = ProcessSpec(
        (sys.executable, "-c", f"open({str(output)!r}, 'a').write('executed')"),
        tmp_path,
    )
    host.prepare(attempt, "check-start", spec)
    host.initialize_control_once(
        attempt.id,
        prepared_id="check-start",
        fence=1,
        authorization_ref=attempt.authorization_ref,
    )
    return host, attempt, spec, output


@pytest.mark.parametrize("field", ["environment_revision", "execution_sha256"])
def test_environment_or_execution_change_cannot_reuse_start_or_cancel_binding(
    prepared_check, field
):
    host, attempt, spec, output = prepared_check
    changed = attempt.model_copy(update={field: 3 if field == "environment_revision" else "c" * 64})
    before = host.database.read_bytes()
    with pytest.raises(StartConflict, match="START_KEY_PAYLOAD_MISMATCH"):
        host.prepare(changed, "check-start", spec)
    with pytest.raises(LaunchDenied, match="CANCELLATION_BINDING_MISMATCH"):
        host.cancel(
            attempt.id,
            "cancel",
            expected_binding={
                "prepared_id": "check-start",
                "manifest": changed.model_dump(),
                "process_spec": spec.document(),
            },
        )
    assert host.database.read_bytes() == before and not output.exists()


@pytest.mark.parametrize("denial", ["paused", "fence", "expired", "budget"])
def test_check_launch_requires_current_original_activation(prepared_check, denial):
    host, attempt, _, output = prepared_check
    accepted = activation(attempt)
    if denial in {"paused", "fence"}:
        host.set_control(
            attempt.id,
            fence=2 if denial == "fence" else 1,
            authorization_ref=attempt.authorization_ref,
            dispatch_enabled=denial != "paused",
        )
    elif denial == "expired":
        accepted = replace(accepted, expires_at=time.time() - 1)
    else:
        accepted = replace(accepted, budget_ref="other-budget")
    with pytest.raises(LaunchDenied, match="ACTIVATION_NOT_CURRENT"):
        host.start("check-start", accepted)
    assert not output.exists() and host.inspect(attempt.id).supervisor is None


def test_lost_start_receipt_is_not_relaunched_and_exact_cancel_stays_historical(prepared_check):
    host, attempt, spec, output = prepared_check
    accepted = activation(attempt)
    binding = {
        "prepared_id": "check-start",
        "manifest": attempt.model_dump(),
        "process_spec": spec.document(),
    }
    with pytest.raises(ProbeCrash, match="after_accept"):
        host.start("check-start", accepted, crash_at="after_accept")
    reopened = RunnerHost(host.directory, existing_only=True)
    replay = reopened.start("check-start", accepted)
    assert replay.state == "unknown" and replay.supervisor is None
    assert not output.exists()
    reopened.set_control(attempt.id, fence=2, authorization_ref="withdrawn", dispatch_enabled=False)
    assert (
        reopened.cancel(
            attempt.id, "cancel", expected_binding=deepcopy(binding)
        ).snapshot.business_status
        == "cancelled"
    )
    assert not reopened.receive_result(attempt.id, 1, "late", {"passed": True}).accepted
    assert reopened.start("check-start", accepted).business_status == "cancelled"
    assert not output.exists()


def test_running_check_observes_control_withdrawal_and_rejects_late_result(check_runner):
    host, attempt, _, _, output = check_runner
    wait_json(output)
    host.set_control(
        attempt.id, fence=1, authorization_ref=attempt.authorization_ref, dispatch_enabled=False
    )
    initialization = host.initialize_control_once(
        attempt.id,
        prepared_id="check-start",
        fence=1,
        authorization_ref=attempt.authorization_ref,
    )
    assert initialization["dispatch_enabled"] is False
    output.with_suffix(".continue").touch()
    assert wait_json(output.with_suffix(".after")) == "CAPTURE_FENCE_NOT_CURRENT"
    host.cancel(attempt.id, "withdrawn-check")
    assert not host.receive_result(attempt.id, 1, "late", {"passed": True}).accepted
