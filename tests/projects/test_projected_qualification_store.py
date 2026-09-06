"""Public Store persistence with an explicit synthetic producer, never provider evidence.

The producer substitute exercises official/fixture policy branches without model
calls. Git, ProjectRegistry, credentials and GoCallJournal are real local stores.
"""

import copy
import sqlite3
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pathlib import Path
from threading import Event

import pytest
from karajan.adapters.opencode.go_journal import GoCallJournal
from karajan.projects.credential_sources import CredentialSourceStore, LocalKeyFile
from karajan.projects.publication import digest
from karajan.projects.qualification import ProfileQualificationStore, QualificationError
from test_qualification_store import apply, case

__all__ = ["case"]
SUITE = {"id": "opencode-go-native-read-edit-linux", "revision": 2}
CONTEXT = {
    "source_sha256": "b" * 64,
    "approved_input_tokens": 12288,
    "reserved_output_tokens": 4096,
    "operating_context_tokens": 16384,
    "fixed_margin": 2048,
    "ratio_margin_basis_points": 2000,
}


class SyntheticSuite:
    """Explicit trusted-producer test substitute; does not qualify real models."""

    def __init__(self, journal):
        self.journal = journal
        self.calls = 0
        self.hook = None
        self.after = None
        self.source_value = {
            "schema_version": "karajan.fixed-go-suite-source.v2",
            "suite_ref": copy.deepcopy(SUITE),
            "observation_origin": "official_go",
            "qualification_scope": "projected_native_tools",
            "runtime_source": {"test_double": "synthetic-projected-producer"},
            "runtime_digest": "c" * 64,
            "probe_spec": {"context": CONTEXT.copy(), "test_double": True},
        }

    def source(self):
        return copy.deepcopy(self.source_value)

    def validate_profile(self, binding):
        assert binding["registration"]["profile"]["binding"]["native_settings"] == {
            "suite_ref": self.source_value["suite_ref"]
        }

    def observe(self, start, credential):
        if self.hook:
            self.hook(start)
        self.calls += 1
        assert credential.auth_ref == start["authentication_source"]["auth_ref"]
        for scenario in start["scenarios"]:
            self.journal.create_grant(scenario["grant_binding"], grant_id=scenario["grant_id"])
            self.journal.revoke_grant(scenario["grant_id"])
        result = {
            "schema_version": "karajan.fixed-go-suite-observation.v2",
            "suite_ref": copy.deepcopy(start["suite_ref"]),
            "qualification_id": start["qualification_id"],
            "source": copy.deepcopy(start["source"]),
            "observation_origin": start["source"]["observation_origin"],
            "qualification_scope": start["source"]["qualification_scope"],
            "status": "passed",
            "reason_codes": [],
            "scenarios": [
                {
                    **{key: item[key] for key in ("scenario", "attempt_id", "fence", "grant_id")},
                    "status": "passed",
                    "reason_codes": [],
                }
                for item in start["scenarios"]
            ],
            "validation": {
                "projected_native_tools": "passed",
                "candidate_capture": "passed",
                "context_accounting": "passed",
                "runtime_tools": "not_run",
                "budget": "unknown",
                "dispatch": False,
            },
            "test_double": True,
        }
        if self.after:
            self.after(start, result)
        return result


@pytest.fixture
def projected(case, tmp_path: Path):
    config = copy.deepcopy(case["configuration"])
    registration = config["resources"]["profiles"][0]
    profile = registration["profile"]
    profile["binding"].update(
        runtime_kind="opencode-go-isolated",
        runtime_version="1.18.29",
        model_id="glm-5.3-flash",
        auth_mode="api_key",
        native_settings={"suite_ref": SUITE.copy()},
    )
    profile.update(auth_ref="secret:go", required_permissions=["read", "edit"])
    config["resources"]["accounts"][0].update(provider_id="opencode-go", secret_ref="secret:go")
    apply(case, config)
    secret = tmp_path / "synthetic.key"
    secret.write_text("synthetic-material-not-a-provider-key", encoding="ascii")
    credentials = CredentialSourceStore(
        case["projects"],
        sources={(case["project_id"], "secret:go"): LocalKeyFile("synthetic", secret)},
        private_directory=tmp_path / "credential-private",
    )
    generation = credentials.register(
        case["project_id"], "secret:go", principal="owner", command_key="credential"
    )
    suite = SyntheticSuite(
        GoCallJournal(tmp_path / "journal.sqlite", clock=lambda: case["clock"][0])
    )
    store = ProfileQualificationStore(
        case["projects"], clock=lambda: case["clock"][0], credentials=credentials, go_suite=suite
    )
    return {
        **case,
        "registration": registration,
        "store": store,
        "suite": suite,
        "credentials": credentials,
        "generation": generation,
        "secret": secret,
    }


def qualify(projected, key="projected-qualification"):
    return projected["store"].qualify_runtime_tools(
        projected["project_id"],
        {"id": "fixture-profile", "revision": 1},
        principal="owner",
        command_key=key,
        suite_ref=projected["suite"].source()["suite_ref"],
        validity_seconds=60,
    )


def facts(projected, scope="runtime_tools"):
    return projected["store"].facts_for_profile(
        projected["project_id"], projected["registration"], principal="owner", scope=scope
    )


def test_v2_spec_and_sealed_grant_identities_are_durable_before_producer_effect(projected):
    starts = []

    def before_effect(start):
        persisted = projected["store"].get_command_start(
            projected["project_id"], "projected-qualification", principal="owner"
        )
        assert persisted["completed"] is False
        assert persisted["qualification_scope"] == "projected_native_tools"
        assert persisted["binding"]["execution_start"] == start
        assert start["source"]["probe_spec"]["context"] == CONTEXT
        for item in start["scenarios"]:
            binding = item["grant_binding"]
            assert binding["schema_version"] == "karajan.go-qualification-grant.v2"
            assert binding["scenario"] == item["scenario"]
            assert binding["context"] == CONTEXT
            assert binding["probe_spec_digest"] == digest(start["source"]["probe_spec"])
            assert "subject" not in binding  # No Task / reservation grants.
        with pytest.raises(QualificationError, match="QUALIFICATION_IN_PROGRESS_OR_UNKNOWN"):
            qualify(projected)
        starts.append(persisted)

    projected["suite"].hook = before_effect
    result = qualify(projected)
    assert result["status"] == "passed", result
    assert len(starts) == 1 and projected["suite"].calls == 1
    reopened = ProfileQualificationStore(projected["projects"])
    projected["store"] = reopened
    assert qualify(projected) == result  # Historical replay needs no current producer.
    assert projected["suite"].calls == 1


def test_only_v2_official_complete_observation_yields_bounded_worker_executor_facts(projected):
    result = qualify(projected)
    assert result["runtime_tools_status"] == "passed"
    qualified = facts(projected)
    assert qualified["executor_scope"] == {
        "schema_version": "karajan.go-projected-executor-scope.v1",
        "suite_ref": SUITE,
        "projection": "existing_regular_files",
        "new_files_supported": False,
        "tools": ["read", "edit"],
        "supported_roles": ["worker"],
        "task_classes": ["T1"],
        "context": CONTEXT,
        "max_requests": 6,
        "candidate_capture": True,
    }
    assert qualified["facts"]["roles"] == ["worker"]
    assert qualified["facts"]["tools"] == ["read", "edit"]
    assert qualified["facts"]["context_tokens"] == 16384
    assert qualified["facts"]["budget_enforcement"] == "unknown"
    assert {row["capability"] for row in qualified["capability_evidence"]} == {
        "bounded_code_edit",
        "controlled_tools",
        "candidate_capture",
    }
    assert qualified["context_evidence"] == {
        "provider_declared": {"context_tokens": 1000000, "max_output_tokens": 131072},
        "adapter_limits": {
            "operating_context_tokens": 16384,
            "reserved_output_tokens": 4096,
            "output_policy": "fixed_native_limit",
        },
        "observed": "bounded_small_input_accepted",
        "maximum_context_observed": False,
    }
    assert qualified["runtime_tools_status"] == "passed"
    assert qualified["dispatch_eligible"] is False
    with projected["store"].routing_facts_guard(
        projected["project_id"], [projected["registration"]], principal="owner"
    ) as view:
        assert view["profiles"][0]["qualification"] == qualified


@pytest.mark.parametrize("change", ["schema", "context", "scope", "suite"])
def test_unsupported_source_is_rejected_before_any_producer_effect(projected, change):
    source = projected["suite"].source_value
    if change == "schema":
        source["schema_version"] = "karajan.fixed-go-suite-source.v1"
    elif change == "context":
        source["probe_spec"]["context"]["reserved_output_tokens"] = 200000
    elif change == "scope":
        source["qualification_scope"] = "fixed_native_tools"
    else:
        source["suite_ref"]["revision"] = 3
    with pytest.raises(QualificationError, match="QUALIFICATION_SOURCE_UNSUPPORTED"):
        qualify(projected)
    assert projected["suite"].calls == 0
    with pytest.raises(QualificationError, match="QUALIFICATION_START_NOT_FOUND"):
        projected["store"].get_command_start(
            projected["project_id"], "projected-qualification", principal="owner"
        )


@pytest.mark.parametrize(
    "change", ["capture", "context", "scenario", "attempt", "source", "origin", "reason"]
)
def test_incomplete_or_misbound_suite_envelope_never_yields_runtime_facts(projected, change):
    def invalidate(start, result):
        if change in {"capture", "context"}:
            result["validation"][
                "candidate_capture" if change == "capture" else "context_accounting"
            ] = "not_run"
        elif change == "scenario":
            result["scenarios"].pop()
        elif change == "attempt":
            result["scenarios"][0]["attempt_id"] = "other-attempt"
        elif change == "source":
            result["source"]["runtime_digest"] = "f" * 64
        elif change == "origin":
            result["observation_origin"] = "http_fixture"
        else:
            result["reason_codes"] = ["COLLECTOR_INCOMPLETE"]

    projected["suite"].after = invalidate
    result = qualify(projected)
    assert result["status"] == "failed"
    assert result["runtime_tools_status"] == "not_run"
    assert "PROJECTED_EXECUTOR_EVIDENCE_INCOMPLETE" in result["reason_codes"]
    with pytest.raises(QualificationError, match="QUALIFICATION_NOT_PASSED"):
        facts(projected)


@pytest.mark.parametrize("latest", ["failed", "unknown", "revoked"])
def test_latest_v2_attempt_never_falls_back_to_an_earlier_pass(projected, latest):
    old = qualify(projected)
    if latest == "unknown":

        def interrupt(_start):
            raise KeyboardInterrupt("simulated-controller-loss")

        projected["suite"].hook = interrupt
        with pytest.raises(KeyboardInterrupt):
            qualify(projected, "latest")
        expected = "QUALIFICATION_IN_PROGRESS_OR_UNKNOWN"
    else:
        if latest == "failed":

            def fail(_start, result):
                result["status"] = "failed"

            projected["suite"].after = fail
        new = qualify(projected, "latest")
        if latest == "revoked":
            projected["store"].revoke(
                projected["project_id"], new["id"], principal="owner", reason="suspended"
            )
        expected = "QUALIFICATION_REVOKED" if latest == "revoked" else "QUALIFICATION_NOT_PASSED"
    calls = projected["suite"].calls
    with pytest.raises(QualificationError, match=expected):
        facts(projected)
    assert qualify(projected) == old
    if latest == "unknown":
        with pytest.raises(QualificationError, match=expected):
            qualify(projected, "latest")
    assert projected["suite"].calls == calls


def test_projected_fixture_observation_is_queryable_but_never_promoted(projected):
    projected["suite"].source_value.update(
        observation_origin="http_fixture", qualification_scope="projected_native_tools_fixture"
    )
    result = qualify(projected)
    assert result["status"] == "passed" and result["runtime_tools_status"] == "not_run"
    with pytest.raises(QualificationError, match="RUNTIME_TOOLS_NOT_QUALIFIED"):
        facts(projected)
    fixture = facts(projected, scope="projected_native_tools_fixture")
    assert fixture["facts"]["roles"] == []
    assert fixture["facts"]["tools"] == ["projected_fixture_read", "projected_fixture_edit"]
    assert fixture["facts"]["context_tokens"] is None
    assert "executor_scope" not in fixture


@pytest.mark.parametrize("change", ["generation", "revoked", "source"])
def test_current_credential_or_source_change_invalidates_passed_v2_facts(projected, change):
    result = qualify(projected)
    credentials = projected["credentials"]
    if change == "generation":
        projected["secret"].write_text("synthetic-replacement-only", encoding="ascii")
        credentials.register(
            projected["project_id"],
            "secret:go",
            principal="owner",
            command_key="rotate",
            expected_generation=projected["generation"]["generation"],
        )
    elif change == "revoked":
        credentials.revoke(
            projected["project_id"],
            "secret:go",
            projected["generation"]["generation"],
            principal="owner",
            command_key="revoke-credential",
        )
    else:
        projected["suite"].source_value["runtime_digest"] = "f" * 64
    with pytest.raises(QualificationError, match="SOURCE_MISMATCH|RUNTIME_MISMATCH"):
        facts(projected)
    assert qualify(projected) == result  # Receipt remains readable, never fresh qualification.
    assert projected["suite"].calls == 1


def test_wrong_owner_and_suite_request_have_zero_producer_effect(projected):
    with pytest.raises(ValueError, match="USER_DECISION_REQUIRED"):
        projected["store"].qualify_runtime_tools(
            projected["project_id"],
            {"id": "fixture-profile", "revision": 1},
            principal="other",
            command_key="not-owner",
            suite_ref=SUITE,
            validity_seconds=60,
        )
    with pytest.raises(QualificationError, match="QUALIFICATION_SUITE_UNSUPPORTED"):
        projected["store"].qualify_runtime_tools(
            projected["project_id"],
            {"id": "fixture-profile", "revision": 1},
            principal="owner",
            command_key="wrong-suite",
            suite_ref={**SUITE, "revision": 1},
            validity_seconds=60,
        )
    assert projected["suite"].calls == 0


def test_source_changed_during_observation_is_retained_as_failed_not_qualified(projected):
    def change(_start, _result):
        projected["suite"].source_value["runtime_digest"] = "a" * 64

    projected["suite"].after = change
    result = qualify(projected)
    assert result["status"] == "failed"
    assert result["runtime_tools_status"] == "not_run"
    assert "QUALIFICATION_SOURCE_CHANGED" in result["reason_codes"]
    assert projected["suite"].calls == 1
    with pytest.raises(QualificationError, match="QUALIFICATION_NOT_PASSED"):
        facts(projected)


def test_start_seal_detects_storage_corruption_before_replay_or_facts(projected):
    result = qualify(projected)
    # Deliberate controller-disk corruption, not a normal assertion through a
    # private store port. All detection is checked through public readers.
    with sqlite3.connect(projected["projects"].database) as db:
        db.execute(
            "UPDATE profile_qualification_start_seals SET digest=? WHERE id=?",
            ("0" * 64, result["id"]),
        )
    with pytest.raises(QualificationError, match="QUALIFICATION_START_CHANGED"):
        qualify(projected)
    with pytest.raises(QualificationError, match="QUALIFICATION_START_CHANGED"):
        facts(projected)
    assert projected["suite"].calls == 1


def test_runtime_facts_guard_serializes_credential_revoke_and_rechecks_after_release(projected):
    qualify(projected)
    entered, completed = Event(), Event()

    def revoke():
        entered.set()
        projected["credentials"].revoke(
            projected["project_id"],
            "secret:go",
            projected["generation"]["generation"],
            principal="owner",
            command_key="concurrent-revoke",
        )
        completed.set()

    with ThreadPoolExecutor(max_workers=1) as executor:
        with projected["store"].routing_facts_guard(
            projected["project_id"], [projected["registration"]], principal="owner"
        ) as view:
            assert view["profiles"][0]["qualification"]["runtime_tools_status"] == "passed"
            future = executor.submit(revoke)
            assert entered.wait(timeout=2)
            with pytest.raises(TimeoutError):
                future.result(timeout=0.2)
            assert not completed.is_set()
        future.result(timeout=5)
    with pytest.raises(QualificationError, match="AUTHENTICATION_SOURCE_MISMATCH"):
        facts(projected)
