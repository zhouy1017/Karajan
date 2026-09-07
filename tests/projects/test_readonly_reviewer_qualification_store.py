"""Real public stores with an explicit producer double; no native or official observation."""

from copy import deepcopy

import pytest
from karajan.adapters.opencode.go_journal import GoJournalError
from karajan.candidates.review_output import PARSER_REVISION
from karajan.projects.publication import digest
from karajan.projects.qualification import (
    READONLY_GO_SUITE,
    ProfileQualificationStore,
    QualificationError,
)
from test_projected_qualification_store import SyntheticSuite, case, projected

__all__ = ["case", "projected"]


class ReviewerProducerDouble(SyntheticSuite):
    """Exercise official-source policy only; this is not a deployable qualification producer."""

    def __init__(self, journal):
        super().__init__(journal)
        self.source_value.update(
            schema_version="karajan.fixed-go-reviewer-suite-source.v1",
            suite_ref=deepcopy(READONLY_GO_SUITE),
            qualification_scope="readonly_reviewer_tools",
        )
        self.source_value["probe_spec"].update(
            scenarios=["clean_review", "defect_review", "denied_read"],
            parser_revision=PARSER_REVISION,
        )
        self.mutate = None
        self.after_scenario = None
        self.effects = []

    def validate_profile(self, binding):
        super().validate_profile(binding)
        assert binding["registration"]["profile"]["required_permissions"] == ["read"]

    def observe(self, start, credential, *, current_guard):
        if self.hook:
            self.hook(start)
        self.calls += 1
        assert credential.auth_ref == start["authentication_source"]["auth_ref"]
        for scenario in start["scenarios"]:
            with current_guard():
                self.journal.create_grant(scenario["grant_binding"], grant_id=scenario["grant_id"])
                self.journal.revoke_grant(scenario["grant_id"])
                self.effects.append((start["qualification_id"], scenario["scenario"]))
            if self.after_scenario:
                self.after_scenario(start, scenario)
        result = {
            "suite_ref": deepcopy(start["suite_ref"]),
            "qualification_id": start["qualification_id"],
            "source": deepcopy(start["source"]),
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
            "test_double": True,
        }
        result["schema_version"] = "karajan.fixed-go-reviewer-suite-observation.v1"
        result["validation"] = {
            "readonly_reviewer_tools": "passed",
            "context_accounting": "passed",
            "structured_findings": "passed",
            "readonly_projection": "passed",
            "runtime_tools": "not_run",
            "budget": "unknown",
            "dispatch": False,
        }
        result["grant_cleanup"] = [
            {"grant_id": item["grant_id"], "state": "revoked"} for item in start["scenarios"]
        ]
        if self.mutate:
            self.mutate(start, result)
        return result


@pytest.fixture
def reviewer_case(projected):
    projects = projected["projects"]
    configuration = projects.get_configuration(projected["project_id"])["configuration"]
    reviewer = deepcopy(configuration["resources"]["profiles"][0])
    reviewer["id"] = reviewer["profile"]["id"] = "readonly-reviewer"
    reviewer["profile"]["required_permissions"] = ["read"]
    reviewer["profile"]["binding"]["native_settings"] = {"suite_ref": deepcopy(READONLY_GO_SUITE)}
    for evidence in reviewer["capability_evidence"]:
        evidence["profile_digest"] = digest(reviewer["profile"])
    configuration["resources"]["profiles"].append(reviewer)
    configuration["approved_profile_refs"].append({"id": reviewer["id"], "revision": 1})
    preview = projects.preview_configuration(
        projected["project_id"], configuration, command_key="readonly-preview", principal="owner"
    )
    projects.apply_configuration(
        projected["project_id"],
        preview["preview_id"],
        expected_revision=projects.get(projected["project_id"])["revision"],
        command_key="readonly-apply",
        principal="owner",
    )
    suite = ReviewerProducerDouble(projected["suite"].journal)
    store = ProfileQualificationStore(
        projects,
        clock=lambda: projected["clock"][0],
        credentials=projected["credentials"],
        go_suite=projected["suite"],
        reviewer_suite=suite,
    )
    return {**projected, "store": store, "reviewer": reviewer, "reviewer_suite": suite}


def qualify(case, key="readonly-start"):
    return case["store"].qualify_runtime_tools(
        case["project_id"],
        {"id": "readonly-reviewer", "revision": 1},
        principal="owner",
        command_key=key,
        suite_ref=READONLY_GO_SUITE,
        validity_seconds=60,
    )


def facts(case, scope="runtime_tools"):
    return case["store"].facts_for_profile(
        case["project_id"],
        case["reviewer"],
        principal="owner",
        scope=scope,
    )


def test_all_three_identities_and_seal_commit_before_any_grant(reviewer_case):
    case = reviewer_case
    before = []

    def hook(start):
        persisted = case["store"].get_command_start(
            case["project_id"], "readonly-start", principal="owner"
        )
        assert persisted["binding"]["execution_start"] == start
        assert not persisted["completed"]
        assert start["expires_at"] == start["started_at"] + 600
        assert [row["scenario"] for row in start["scenarios"]] == [
            "clean_review",
            "defect_review",
            "denied_read",
        ]
        assert len({row["grant_id"] for row in start["scenarios"]}) == 3
        assert len({row["attempt_id"] for row in start["scenarios"]}) == 3
        for row in start["scenarios"]:
            assert (
                row["grant_binding"]["schema_version"]
                == "karajan.go-reviewer-qualification-grant.v1"
            )
            with pytest.raises(GoJournalError, match="GRANT_NOT_FOUND"):
                case["suite"].journal.snapshot(row["grant_id"])
        with pytest.raises(QualificationError, match="QUALIFICATION_IN_PROGRESS_OR_UNKNOWN"):
            qualify(case)
        before.append(start)

    case["reviewer_suite"].hook = hook
    record = qualify(case)
    assert record["status"] == "passed", record
    assert len(before) == 1
    assert record["dispatch_eligible"] is False
    assert qualify(case) == record
    assert case["reviewer_suite"].calls == 1


def test_worker_and_reviewer_sources_coexist_without_promoting_worker(reviewer_case):
    case = reviewer_case
    worker = case["store"].qualify_runtime_tools(
        case["project_id"],
        {"id": "fixture-profile", "revision": 1},
        principal="owner",
        command_key="worker-start",
        suite_ref=case["suite"].source()["suite_ref"],
        validity_seconds=60,
    )
    assert worker["status"] == "passed"
    record = qualify(case)
    current = facts(case)
    assert current["observation"] == record
    assert current["facts"]["roles"] == ["reviewer"]
    assert current["facts"]["tools"] == ["read"]
    assert current["executor_scope"]["task_classes"] == ["T1"]
    assert current["executor_scope"]["output_parser_revision"] == PARSER_REVISION
    assert current["executor_scope"]["candidate_capture"] is False
    assert current["dispatch_eligible"] is False
    assert {row["capability"] for row in current["capability_evidence"]} == {
        "code_review",
        "structured_findings",
    }
    worker_facts = case["store"].facts_for_profile(
        case["project_id"],
        case["registration"],
        principal="owner",
        scope="runtime_tools",
    )
    assert worker_facts["facts"]["roles"] == ["worker"]
    with pytest.raises(QualificationError, match="RUNTIME_TOOLS_NOT_QUALIFIED"):
        case["store"].facts_for_profile(
            case["project_id"],
            case["registration"],
            principal="owner",
            scope="readonly_reviewer_tools",
        )


def test_fixture_never_exports_production_reviewer_role_or_capabilities(reviewer_case):
    case = reviewer_case
    case["reviewer_suite"].source_value.update(
        observation_origin="http_fixture",
        qualification_scope="readonly_reviewer_tools_fixture",
    )
    record = qualify(case)
    assert record["status"] == "passed"
    assert record["runtime_tools_status"] == "not_run"
    assert record["live_qualified"] is False
    with pytest.raises(QualificationError, match="RUNTIME_TOOLS_NOT_QUALIFIED"):
        facts(case)
    observed = facts(case, "readonly_reviewer_tools_fixture")
    assert observed["facts"]["roles"] == observed["facts"]["tools"] == []
    assert observed["capability_evidence"] == []
    assert "executor_scope" not in observed


@pytest.mark.parametrize("fault", ["partial", "identity", "cleanup", "validation", "parser"])
def test_complete_seal_rejects_mismatched_or_partial_producer_envelopes(reviewer_case, fault):
    def mutate(start, result):
        if fault == "partial":
            result["scenarios"].pop()
        elif fault == "identity":
            result["scenarios"][1]["grant_id"] = result["scenarios"][0]["grant_id"]
        elif fault == "cleanup":
            result["grant_cleanup"][2]["state"] = "unknown"
        elif fault == "validation":
            result["validation"]["structured_findings"] = "not_run"
        else:
            result["source"]["probe_spec"]["parser_revision"] = "changed-parser"

    reviewer_case["reviewer_suite"].mutate = mutate
    assert qualify(reviewer_case)["status"] == "failed"
    with pytest.raises(QualificationError, match="QUALIFICATION_NOT_PASSED"):
        facts(reviewer_case)


@pytest.mark.parametrize("latest", ["failed", "unknown"])
def test_latest_failed_or_lost_result_cannot_reuse_earlier_pass(reviewer_case, latest):
    case = reviewer_case
    assert qualify(case)["status"] == "passed"

    def stop(start):
        if latest == "unknown":
            raise KeyboardInterrupt("synthetic process death before result")
        raise ValueError("synthetic producer failure")

    case["reviewer_suite"].hook = stop
    if latest == "unknown":
        with pytest.raises(KeyboardInterrupt):
            qualify(case, "second-start")
    else:
        assert qualify(case, "second-start")["status"] == "failed"
    with pytest.raises(QualificationError):
        facts(case)
    assert case["reviewer_suite"].calls == 1


def test_history_and_revoke_do_not_load_current_credential_or_suite(reviewer_case):
    case = reviewer_case
    record = qualify(case)
    case["secret"].unlink()
    case["store"] = ProfileQualificationStore(case["projects"])
    assert qualify(case) == record
    assert (
        case["store"].get(case["project_id"], record["id"], principal="owner")["record"] == record
    )
    revoked = case["store"].revoke(
        case["project_id"],
        record["id"],
        principal="owner",
        reason="owner_suspended",
    )
    assert revoked


def test_current_parser_source_and_generation_changes_invalidate_facts(reviewer_case):
    case = reviewer_case
    qualify(case)
    case["reviewer_suite"].source_value["probe_spec"]["parser_revision"] = "changed-parser"
    with pytest.raises(QualificationError):
        facts(case)
    case["reviewer_suite"].source_value["probe_spec"]["parser_revision"] = PARSER_REVISION
    case["secret"].write_text("changed-synthetic-secret", encoding="ascii")
    with pytest.raises(QualificationError, match="AUTHENTICATION_SOURCE_MISMATCH"):
        facts(case)


@pytest.mark.parametrize("change", ["revoke", "key", "deadline", "new_start"])
def test_mid_suite_authority_change_stops_later_effects(reviewer_case, change):
    case = reviewer_case
    suite = case["reviewer_suite"]

    def after_first(start, scenario):
        suite.after_scenario = None
        if change == "revoke":
            historical = ProfileQualificationStore(case["projects"], clock=lambda: case["clock"][0])
            revoked = historical.revoke(
                case["project_id"], start["qualification_id"], principal="owner", reason="cancelled"
            )
            assert revoked["id"] == start["qualification_id"]
        elif change == "key":
            case["secret"].write_text("changed-synthetic-secret", encoding="ascii")
        elif change == "deadline":
            case["clock"][0] = start["expires_at"]
        else:

            def lost_reply(new_start):
                raise KeyboardInterrupt("synthetic new start without result")

            suite.hook = lost_reply
            with pytest.raises(KeyboardInterrupt):
                qualify(case, "superseding-start")

    suite.after_scenario = after_first
    result = qualify(case)
    assert result["status"] == "failed"
    assert suite.effects == [(result["id"], "clean_review")]
    assert qualify(case) == result
    assert suite.effects == [(result["id"], "clean_review")]
    with pytest.raises(QualificationError):
        facts(case)


def test_authority_source_read_crossing_deadline_prevents_first_effect(reviewer_case):
    case = reviewer_case
    suite = case["reviewer_suite"]
    original = suite.source

    def arm(start):
        def delayed_source():
            value = original()
            case["clock"][0] = start["expires_at"]
            return value

        suite.source = delayed_source

    suite.hook = arm
    result = qualify(case)
    assert result["status"] == "failed"
    assert suite.effects == []


def test_revocation_after_last_scenario_cannot_seal_a_pass(reviewer_case):
    case = reviewer_case

    def revoke(start, result):
        case["store"].revoke(
            case["project_id"], start["qualification_id"], principal="owner", reason="cancelled"
        )

    case["reviewer_suite"].mutate = revoke
    result = qualify(case)
    assert result["status"] == "failed"
    assert "QUALIFICATION_SOURCE_CHANGED" in result["reason_codes"]
