"""Real approved stores/CAS/Journal; explicit synthetic native-result producer.

These compiler tests do not qualify stop or model behavior. The fixed child
integration separately hands over an actual execute_go_task result.
"""

import json
import sys
from contextlib import contextmanager
from dataclasses import asdict, replace

import pytest
from karajan.adapters.opencode.go_journal import GoCallJournal
from karajan.candidates import CandidateStore
from karajan.execution import ProcessIdentity, ProcessSpec, RunnerHost
from karajan.isolation._opencode_capture import ProjectionEntry, StoppedProjection
from karajan.isolation.go_task import GoTaskResult
from karajan.isolation.opencode_runtime import RUNTIME_SHA256
from karajan.orchestration.go_execution_intent import (
    GoExecutionIntents,
    GoExecutionSource,
    GoLaunchSpec,
)
from karajan.orchestration.go_task_binding import task_grant_binding, task_host_manifest
from karajan.orchestration.go_task_input import build_task_input
from karajan.routing.compiler import digest
from karajan.runs import RunError
from test_go_task_input import (
    case as case,
)
from test_go_task_input import (
    projected as projected,
)
from test_go_task_input import (
    workspace_case as workspace_case,
)


class SyntheticHostAuthority(RunnerHost):
    """Only current child/stop authority is a double; its ledger is real."""

    runner = ProcessIdentity(12345, "synthetic-collector")
    profile = {"id": "fixture-profile", "revision": 1}

    @contextmanager
    def current_fence_guard(self, attempt_id, *, fence, authorization_ref):
        yield {"profile": self.profile}

    @contextmanager
    def current_runner_guard(self, attempt_id, *, fence, authorization_ref):
        yield self.runner


@pytest.fixture
def captured_case(workspace_case, tmp_path):
    (
        workspace,
        candidates,
        _,
        admissions,
        _,
    ) = workspace_case
    native_source = {"qualified_mechanism_descriptor": {"artifact_sha256": RUNTIME_SHA256}}
    service = GoExecutionIntents(
        admissions,
        source=GoExecutionSource("a" * 64, digest(native_source)),
        host=SyntheticHostAuthority(tmp_path / "host"),
        candidates=candidates,
        launch_compiler=lambda op: GoLaunchSpec(
            ProcessSpec((sys._base_executable, "-c", "pass"), tmp_path), "c" * 64
        ),
    )
    args = workspace["run_id"], workspace["operation_id"]
    operation = service.prepare_intent(*args, principal="owner", command_key="prepare")
    intent = operation["execution"]["intent"]
    admissions.routing.capacity.activate(
        intent["admission_id"], command_key=intent["activation_key"]
    )
    operation = service.activation_recorded(*args, principal="owner")
    task = build_task_input(
        workspace,
        candidates,
        native_source_sha256=digest(native_source),
        runner_source_digest="a" * 64,
    )
    binding = task_grant_binding(operation)
    journal = GoCallJournal(tmp_path / "journal.sqlite", clock=lambda: 1000.0)
    grant = journal.create_grant(binding, grant_id=intent["grant_id"])
    context = {
        "schema_version": "karajan.go-context-measurement.v1",
        "model": "glm-5.3-flash",
        "measurement_method": "reference_tokenizer_estimate",
        "measurement_confidence": "local_estimate",
        "request_digest": "d" * 64,
        **intent["execution_context"]["context"],
        "local_input_tokens": 100,
        "margin_tokens": 2322,
        "accounted_input_tokens": 2422,
        "requested_output_tokens": 4096,
        "declared_context_tokens": 1000000,
        "declared_max_output_tokens": 131072,
        "template_reasoning_effort": "low",
        "template_clear_thinking": False,
    }
    journal.begin_call(
        intent["grant_id"],
        "call-1",
        capability=grant["capability"],
        binding=binding,
        request_context=context,
    )
    journal.complete_call(
        intent["grant_id"],
        "call-1",
        capability=grant["capability"],
        binding=binding,
        outcome={
            "state": "response_received",
            "upstream_status": 200,
            "response_bytes": 100,
            "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
            "protocol_passed": True,
            "reason_codes": [],
        },
    )
    journal.revoke_grant(intent["grant_id"])
    stop = {"local_stop": "confirmed", "remote_stop": "unknown"}
    capture = StoppedProjection(
        RUNTIME_SHA256,
        tuple(ProjectionEntry(**row.projection()) for row in task.files),
        tuple(
            (row.path, b"print('collected')\n" if row.writable else row.content)
            for row in task.files
        ),
        json.dumps(stop),
    )
    report = {
        "schema_version": "karajan.go-native-task-observation.v1",
        "status": "completed",
        "scope": "native_task_execution",
        "subject": binding["subject"],
        "attempt_id": intent["attempt_id"],
        "fence": intent["fence"],
        "grant_id": intent["grant_id"],
        "grant_binding": binding,
        "input": task.descriptor(),
        "input_digest": digest(task.descriptor()),
        "native_source": native_source,
        "native_source_sha256": digest(native_source),
        "runner_source_digest": "a" * 64,
        "observation_origin": "http_fixture",
        "candidate_validation": "not_run",
        "dispatch_eligible": False,
        "provider_remote_stop": "unknown",
        "real_credential_passed_to_runtime": False,
        "native_cleanup": stop,
        "relay_cleanup": {"status": "closed"},
        "journal": journal.snapshot(intent["grant_id"]),
        "requests": [
            {
                "protocol_passed": True,
                "reason_codes": [],
                "journal_call_id": "call-1",
                "request_context": context,
            }
        ],
        "reason_codes": [],
    }
    return service, args, operation, candidates, journal, GoTaskResult(capture, json.dumps(report))


@pytest.fixture
def collection_case(captured_case, tmp_path):
    service, args, _, candidates, journal, result = captured_case
    op = service.freeze_launch(*args, principal="owner")
    service.host.prepare(
        task_host_manifest(op),
        op["execution"]["intent"]["start_key"],
        ProcessSpec((sys._base_executable, "-c", "pass"), tmp_path),
    )
    service.record_host_prepared(*args, principal="owner")
    service.mark_start_unknown(*args, principal="owner")
    service.effect_start_claim(*args, principal="owner", runner=service.host.runner)
    return service, args, candidates, journal, result


def test_trusted_collection_commits_full_tree_and_waits_for_checks_review(
    collection_case, tmp_path
):
    from karajan.orchestration.go_task_collector import ApprovedGoCollector

    service, args, candidates, journal, result = collection_case
    collector = ApprovedGoCollector(service, candidates, journal, source_check=lambda: None)
    receipt = collector.collect(*args, principal="owner", runner=service.host.runner, result=result)
    op = service.read(*args, principal="owner")
    assert op["execution"]["collection"]["candidate"] == receipt
    candidate = candidates.get(receipt["id"])
    candidates.materialize(receipt["id"], tmp_path / "restored")
    assert (tmp_path / "restored/src/report.py").read_bytes() == b"print('collected')\n"
    assert (tmp_path / "restored/docs/private.txt").read_bytes() == b"Unprojected baseline file\n"
    current = {
        key: candidate[key]
        for key in ("repository_identity", "base_sha", "input_sha256", "policy_sha256")
    }
    gate = candidates.gate(receipt["id"], current=current)
    assert gate["local_gate_passed"] is False
    assert gate["reasons"] == ["CHECK_EVIDENCE_MISSING:tests", "REVIEW_EVIDENCE_MISSING"]


def test_capture_compiler_uses_original_validation_authors_and_full_baseline(captured_case):
    from karajan.orchestration.go_task_collector import compile_go_capture

    _, _, operation, candidates, journal, result = captured_case
    receipt = compile_go_capture(operation, result, candidates, journal).as_dict()
    request = receipt["freeze_request"]
    assert request["allowed_paths"] == ["src/report.py"]
    assert request["policy"]["checks"] == [
        {
            "id": "tests",
            "revision": 1,
            "argv": ["python", "-m", "pytest"],
            "environment_sha256": "e" * 64,
        }
    ]
    assert request["policy"]["review"]["approved_reviewers"] == []
    assert request["authors"][0]["context_id"] == operation["planned_context_id"]
    assert request["baseline_id"] == operation["workspace"]["baseline"]["id"]
    assert receipt["projection"] == [asdict(row) for row in result.capture.projection]
    assert receipt["report"]["provider_remote_stop"] == "unknown"


class LostCandidateReply(RuntimeError):
    pass


class CommitThenLoseReply(CandidateStore):
    def freeze_projection(self, projection, contents, request):
        super().freeze_projection(projection, contents, request)
        raise LostCandidateReply()


def test_lost_candidate_reply_recovers_exact_history_after_cancel_without_live_source(
    collection_case, tmp_path
):
    from karajan.orchestration.go_task_collector import ApprovedGoCollector

    service, args, candidates, journal, result = collection_case
    failing = CommitThenLoseReply(candidates.directory, existing_only=True)
    collector = ApprovedGoCollector(service, failing, journal, source_check=lambda: None)
    with pytest.raises(LostCandidateReply):
        collector.collect(*args, principal="owner", runner=service.host.runner, result=result)
    op = service.read(*args, principal="owner")
    assert op["execution"]["collection"]["candidate"] is None
    assert op["execution"]["collection"]["capture"]["captured_files"]
    service.cancel_intent(*args, principal="owner")
    source_changed = GoExecutionIntents(
        service.admissions,
        source=GoExecutionSource("f" * 64, "e" * 64),
        host=service.host,
        candidates=candidates,
    )

    def forbidden():
        pytest.fail("Historical recovery asked for current source/effect authority")

    recovery = ApprovedGoCollector(source_changed, candidates, journal, source_check=forbidden)
    before = (candidates.directory / "candidates.sqlite").read_bytes()
    candidates.objects.rename(candidates.directory / "old-artifacts")
    candidates.git_directory.rename(candidates.directory / "old-git")
    found = recovery.recover(*args, principal="owner")
    assert found is not None
    assert recovery.recover(*args, principal="owner") == found
    assert (candidates.directory / "candidates.sqlite").read_bytes() == before
    final = service.read(*args, principal="owner")
    assert final["cancel_requested"] is True
    assert final["execution"]["cancel_requested"] is True
    assert final["execution"]["collection"]["candidate"] == found
    assert journal.snapshot(final["execution"]["intent"]["grant_id"])["request_count"] == 1


@pytest.mark.parametrize("failure", ["source_first", "source_final", "approval", "host_profile"])
def test_current_collection_gate_failure_preserves_capture_but_never_freezes(
    collection_case, failure
):
    from karajan.orchestration.go_task_collector import ApprovedGoCollector

    service, args, candidates, journal, result = collection_case
    checks = 0

    def source_check():
        nonlocal checks
        checks += 1
        if (failure == "source_first" and checks == 1) or (
            failure == "source_final" and checks == 2
        ):
            raise RunError("TASK_EXECUTION_SOURCE_CHANGED")

    if failure == "approval":
        # A real estimate becomes stale; this is separate from native stop.
        service.admissions.routing.estimates.clock = lambda: 2000.0
    elif failure == "host_profile":
        service.host.profile = {"id": "different-profile", "revision": 1}
    collector = ApprovedGoCollector(service, candidates, journal, source_check=source_check)
    with pytest.raises(RunError):
        collector.collect(*args, principal="owner", runner=service.host.runner, result=result)
    op = service.read(*args, principal="owner")
    assert op["execution"]["collection"]["candidate"] is None
    assert collector.recover(*args, principal="owner") is None
    with pytest.raises(RunError, match="TASK_CAPTURE_RECONCILIATION_REQUIRED"):
        collector.collect(*args, principal="owner", runner=service.host.runner, result=result)


@pytest.mark.parametrize(
    "fault",
    [
        "dict",
        "missing",
        "failed",
        "stop",
        "journal",
        "source",
        "input",
        "grant",
        "projection",
        "duplicate",
        "readonly",
    ],
)
def test_capture_compiler_rejects_substituted_or_incomplete_results(captured_case, fault):
    from karajan.orchestration.go_task_collector import compile_go_capture

    _, _, operation, candidates, journal, result = captured_case
    report, capture = result.report, result.capture
    if fault == "dict":
        result = report
    elif fault == "missing":
        result = replace(result, capture=None)
    elif fault == "failed":
        report["status"] = "failed"
    elif fault == "stop":
        report["native_cleanup"]["local_stop"] = "unknown"
        capture = replace(capture, _stop_json=json.dumps(report["native_cleanup"]))
    elif fault == "journal":
        report["journal"]["calls"].clear()
    elif fault == "source":
        report["native_source"]["qualified_mechanism_descriptor"]["artifact_sha256"] = "f" * 64
    elif fault == "input":
        report["input"]["prompt_sha256"] = "f" * 64
        report["input_digest"] = digest(report["input"])
    elif fault == "grant":
        report["grant_binding"]["attempt_id"] = "other"
    elif fault == "projection":
        capture = replace(capture, projection=capture.projection[:-1])
    elif fault == "duplicate":
        capture = replace(capture, files=capture.files + (capture.files[0],))
    else:
        capture = replace(capture, files=tuple((path, b"overwritten") for path, _ in capture.files))
    if fault not in {"dict", "missing"}:
        result = GoTaskResult(capture, json.dumps(report))
    with pytest.raises(RunError):
        compile_go_capture(operation, result, candidates, journal)
