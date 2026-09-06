"""Independent controller-store Spec: actual native tools, synthetic HTTP and credentials."""

import copy
import json
import time
from pathlib import Path

import pytest
import test_runtime_qualification_store as fixtures
from karajan.projects.go_suite import FixedGoSuite
from karajan.projects.qualification import ProfileQualificationStore, QualificationError

case = fixtures.case
go_case = fixtures.go_case
SUITE = {"id": "opencode-go-native-read-edit-linux", "revision": 1}
OUTPUT = Path(__file__).resolve().parent


def execute(case, command="independent-first", *, validity=3600, store=None):
    return (store or case["store"]).qualify_runtime_tools(
        case["project_id"],
        {"id": "fixture-profile", "revision": 1},
        principal="owner",
        command_key=command,
        suite_ref=SUITE,
        validity_seconds=validity,
    )


def read_fixed(case, *, store=None):
    return (store or case["store"]).facts_for_profile(
        case["project_id"],
        case["registration"],
        principal="owner",
        scope="fixed_native_tools_fixture",
    )


def reopen(case, **options):
    return ProfileQualificationStore(
        case["projects"],
        credentials=case["credentials"],
        go_suite=options.pop("go_suite", case["suite"]),
        **options,
    )


def preserve(name, value):
    encoded = json.dumps(value, sort_keys=True, indent=2)
    assert "synthetic-go-credential-for-fixture-only" not in encoded
    (OUTPUT / (name + ".json")).write_text(encoded + "\n", encoding="utf-8")


def test_fixed_facts_replay_scope_source_expiry_and_revocation(go_case):
    record = execute(go_case)
    assert record["status"] == "passed", record["reason_codes"]
    request_count = len(go_case["requests"])
    assert request_count == 5
    fixed = read_fixed(go_case)
    assert fixed["facts"]["provenance"] == "fixture"
    assert fixed["facts"]["roles"] == []
    assert fixed["facts"]["tools"] == ["fixed_go_fixture_read", "fixed_go_fixture_edit"]
    assert fixed["facts"]["context_tokens"] is None
    assert fixed["facts"]["budget_enforcement"] == "unknown"
    assert {item["capability"] for item in fixed["capability_evidence"]} == {
        "fixed_go_fixture_read",
        "fixed_go_fixture_edit",
        "fixed_go_fixture_denied_read",
    }
    assert fixed["dispatch_eligible"] is False
    assert fixed["runtime_tools_status"] == "not_run"
    reopened = reopen(go_case)
    assert execute(go_case, store=reopened) == record
    with pytest.raises(QualificationError, match="IDEMPOTENCY_CONFLICT"):
        execute(go_case, validity=3599, store=reopened)
    with pytest.raises(QualificationError, match="QUALIFICATION_SUITE_UNSUPPORTED"):
        reopened.qualify_runtime_tools(
            go_case["project_id"],
            {"id": "fixture-profile", "revision": 1},
            principal="owner",
            command_key="wrong-suite",
            suite_ref={**SUITE, "revision": 2},
            validity_seconds=3600,
        )
    with pytest.raises(QualificationError, match="QUALIFICATION_START_NOT_FOUND"):
        reopened.get_command_start(go_case["project_id"], "wrong-suite", principal="owner")
    for invalid_time in (record["observed_at"] - 1, record["valid_until"]):
        with pytest.raises(QualificationError, match="QUALIFICATION_EXPIRED"):
            read_fixed(go_case, store=reopen(go_case, clock=lambda value=invalid_time: value))
    changed_suite = FixedGoSuite(
        go_case["suite"].runtime,
        go_case["suite"].work_root / "new-controller-source",
        go_case["suite"].journal,
        client_factory=go_case["suite"].client_factory,
    )
    changed = reopen(go_case, go_suite=changed_suite)
    with pytest.raises(QualificationError, match="QUALIFICATION_RUNTIME_MISMATCH"):
        read_fixed(go_case, store=changed)
    assert execute(go_case, store=changed) == record
    # Construct a genuine official controller for read-only selection. It never
    # observes or relabels the historical HTTP-fixture record as official Go.
    official = reopen(
        go_case,
        go_suite=FixedGoSuite(
            go_case["suite"].runtime, go_case["suite"].work_root, go_case["suite"].journal
        ),
    )
    with pytest.raises(QualificationError, match="PROFILE_FACTS_MISSING"):
        official.facts_for_profile(
            go_case["project_id"],
            go_case["registration"],
            principal="owner",
            scope="fixed_native_tools",
        )
    with reopened.routing_facts_guard(
        go_case["project_id"], [go_case["registration"]], principal="owner"
    ) as view:
        assert view["profiles"][0]["qualification"] is None
        assert view["profiles"][0]["reason_codes"] == ["RUNTIME_TOOLS_NOT_QUALIFIED"]
        assert view["activation_allowed"] is False
    revoked = reopened.revoke(
        go_case["project_id"], record["id"], principal="owner", reason="independent-revoke"
    )
    final = reopen(go_case)
    with pytest.raises(QualificationError, match="QUALIFICATION_REVOKED"):
        read_fixed(go_case, store=final)
    assert final.get(go_case["project_id"], record["id"], principal="owner") == {
        "record": record,
        "revocation": revoked,
    }
    assert execute(go_case, store=final) == record
    assert len(go_case["requests"]) == request_count
    preserve(
        "fixed-history-and-narrow-facts", {"record": record, "facts": fixed, "revocation": revoked}
    )


def test_lost_completion_is_latest_unknown_and_reopen_never_resends(go_case):
    first = execute(go_case)
    assert first["status"] == "passed", first["reason_codes"]
    clock_reads = 0

    def clock():
        nonlocal clock_reads
        clock_reads += 1
        if clock_reads == 2:
            raise RuntimeError("synthetic controller failure before result persistence")
        return time.time()

    interrupted = reopen(go_case, clock=clock)
    with pytest.raises(RuntimeError, match="synthetic controller failure"):
        execute(go_case, "independent-lost-completion", store=interrupted)
    assert len(go_case["requests"]) == 10
    reopened = reopen(go_case)
    start = reopened.get_command_start(
        go_case["project_id"], "independent-lost-completion", principal="owner"
    )
    assert start["id"] != first["id"]
    assert start["completed"] is False
    grants = [
        go_case["suite"].journal.snapshot(scenario["grant_id"])
        for scenario in start["binding"]["execution_start"]["scenarios"]
    ]
    assert [grant["request_count"] for grant in grants] == [3, 2]
    assert all(grant["state"] == "revoked" for grant in grants)
    with pytest.raises(QualificationError, match="QUALIFICATION_IN_PROGRESS_OR_UNKNOWN"):
        execute(go_case, "independent-lost-completion", store=reopened)
    with pytest.raises(QualificationError, match="QUALIFICATION_IN_PROGRESS_OR_UNKNOWN"):
        read_fixed(go_case, store=reopened)
    assert execute(go_case, store=reopened) == first
    assert len(go_case["requests"]) == 10
    preserve("latest-unknown", {"start": start, "grants": grants, "older_record": first})


@pytest.mark.parametrize("change", ["profile", "credential_revoke", "credential_rotation"])
def test_source_change_during_actual_tools_preserves_failed_history(go_case, change):
    changes = []

    def while_executing(_request):
        if changes:
            return
        changes.append(change)
        credentials = go_case["credentials"]
        project = go_case["project_id"]
        if change == "profile":
            exported = go_case["projects"].get_configuration(project)
            configuration = copy.deepcopy(exported["configuration"])
            configuration["resources"]["profiles"][0]["enabled"] = False
            preview = go_case["projects"].preview_configuration(
                project, configuration, principal="owner", command_key="independent-preview"
            )
            go_case["projects"].apply_configuration(
                project,
                preview["preview_id"],
                expected_revision=exported["project_revision"],
                principal="owner",
                command_key="independent-apply",
            )
        else:
            original = credentials.current(project, "secret:go", principal="owner")
            if change == "credential_revoke":
                credentials.revoke(
                    project,
                    "secret:go",
                    original["generation"],
                    principal="owner",
                    command_key="independent-revoke-generation",
                )
            else:
                go_case["key_file"].write_text(
                    "synthetic-rotated-provider-credential-only", encoding="ascii"
                )
                credentials.register(
                    project,
                    "secret:go",
                    principal="owner",
                    command_key="independent-rotate",
                    expected_generation=original["generation"],
                )

    go_case["hook"][0] = while_executing
    record = execute(go_case)
    assert changes == [change]
    assert record["status"] == "failed"
    assert "QUALIFICATION_SOURCE_CHANGED" in record["reason_codes"]
    assert record["dispatch_eligible"] is False
    request_count = len(go_case["requests"])
    assert 1 <= request_count <= 5
    reopened = reopen(go_case)
    assert execute(go_case, store=reopened) == record
    with pytest.raises(QualificationError):
        read_fixed(go_case, store=reopened)
    assert len(go_case["requests"]) == request_count
    for scenario in record["binding"]["execution_start"]["scenarios"]:
        grant = go_case["suite"].journal.snapshot(scenario["grant_id"])
        assert grant["state"] == "revoked"
    assert reopened.get(go_case["project_id"], record["id"], principal="owner")["record"] == record
    preserve("changed-" + change, record)
