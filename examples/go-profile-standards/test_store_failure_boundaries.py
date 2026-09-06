"""Independent Store boundary checks: real SQLite, explicit unavailable-credential double.

No FixedGoSuite implementation or model is exercised here. The shared case
helper only creates a synthetic repository and approved project configuration.
"""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
import test_qualification_store as setup_fixture
from karajan.projects.qualification import ProfileQualificationStore, QualificationError

case = setup_fixture.case
SUITE = {"id": "opencode-go-native-read-edit-linux", "revision": 1}


class UnavailableCredentialSource:
    def __init__(self, projects):
        self.projects = projects
        self.resolutions = 0
        self.generation = "generation-before"
        self.entered = None
        self.release = None

    def current_locked(self, db, project_id, auth_ref, *, principal):
        return {
            "project_id": project_id,
            "auth_ref": auth_ref,
            "generation": self.generation,
            "source": {"id": "synthetic-source"},
        }

    def resolve_exact(self, *args, **kwargs):
        self.resolutions += 1
        if self.entered is not None:
            self.entered.set()
            assert self.release.wait(5)
        raise ValueError("synthetic-private-text-that-must-not-be-persisted")


class UnusedFixtureProducer:
    def source(self):
        return {
            "suite_ref": SUITE.copy(),
            "observation_origin": "http_fixture",
            "runtime_digest": "c" * 64,
        }

    def validate_profile(self, binding):
        pass

    def observe(self, *args, **kwargs):
        pytest.fail("Credential resolution failed; producer must not run")


def controller(case):
    credentials = UnavailableCredentialSource(case["projects"])
    store = ProfileQualificationStore(
        case["projects"], credentials=credentials, go_suite=UnusedFixtureProducer()
    )
    return store, credentials


def command(store, case, *, validity=60):
    return store.qualify_runtime_tools(
        case["project_id"],
        {"id": "fixture-profile", "revision": 1},
        principal="owner",
        command_key="independent-standards",
        suite_ref=SUITE,
        validity_seconds=validity,
    )


def test_failed_resolution_retains_readable_intent_and_sanitized_result(case):
    store, credentials = controller(case)
    result = command(store, case)
    assert result["status"] == "failed"
    assert result["reason_codes"] == ["QUALIFICATION_EXECUTION_INCOMPLETE"]
    assert "synthetic-private-text" not in str(result)
    start = store.get_command_start(case["project_id"], "independent-standards", principal="owner")
    assert start["completed"] is True
    assert start["id"] == result["id"]
    assert len(start["binding"]["execution_start"]["scenarios"]) == 2
    assert credentials.resolutions == 1


def test_completed_failure_replays_after_reopen_without_any_source(case):
    store, credentials = controller(case)
    result = command(store, case)
    reopened = ProfileQualificationStore(case["projects"])
    assert command(reopened, case) == result
    with pytest.raises(QualificationError, match="IDEMPOTENCY_CONFLICT"):
        command(reopened, case, validity=61)
    assert credentials.resolutions == 1
    with pytest.raises(ValueError, match="USER_DECISION_REQUIRED"):
        reopened.get_start(case["project_id"], result["id"], principal="outsider")


def test_inflight_replay_reads_start_without_second_resolution(case):
    store, credentials = controller(case)
    credentials.entered, credentials.release = threading.Event(), threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(command, store, case)
        try:
            assert credentials.entered.wait(5)
            start = store.get_command_start(
                case["project_id"], "independent-standards", principal="owner"
            )
            assert start["completed"] is False
            with pytest.raises(QualificationError, match="QUALIFICATION_IN_PROGRESS_OR_UNKNOWN"):
                command(store, case)
            assert credentials.resolutions == 1
        finally:
            credentials.release.set()
        assert pending.result()["status"] == "failed"


def test_generation_change_during_resolution_adds_source_failure(case):
    store, credentials = controller(case)
    credentials.entered, credentials.release = threading.Event(), threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(command, store, case)
        try:
            assert credentials.entered.wait(5)
            credentials.generation = "generation-after"
        finally:
            credentials.release.set()
        result = pending.result()
    assert result["status"] == "failed"
    assert "QUALIFICATION_SOURCE_CHANGED" in result["reason_codes"]
    assert result["binding"]["execution_start"]["auth_generation"] == "generation-before"
