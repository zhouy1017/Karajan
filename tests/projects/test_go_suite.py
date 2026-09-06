"""Public fixed-suite contracts and actual journal/native fixture integration."""

import json
import os
import sys
import time
from pathlib import Path

import httpx
import pytest
from karajan.adapters.opencode.go_journal import GoCallJournal
from karajan.isolation.go_probe import source_digest
from karajan.projects.credential_sources import ResolvedCredential


def profile_binding():
    return {
        "registration": {
            "id": "go-profile",
            "revision": 1,
            "profile": {
                "id": "go-profile",
                "revision": 1,
                "auth_ref": "go-secret",
                "required_permissions": ["read", "edit"],
                "admission_granularity": "model_call",
                "usage_coverage": "unknown",
                "binding": {
                    "model_id": "glm-5.3-flash",
                    "channel_id": "go-channel",
                    "account_id": "go-account",
                    "runtime_kind": "opencode-go-isolated",
                    "runtime_version": "1.18.29",
                    "auth_mode": "api_key",
                    "billing_path": "subscription_only",
                    "native_settings": {
                        "suite_ref": {"id": "opencode-go-native-read-edit-linux", "revision": 1}
                    },
                },
            },
        },
        "account": {"id": "go-account", "provider_id": "opencode-go", "secret_ref": "go-secret"},
        "channel": {
            "id": "go-channel",
            "account_id": "go-account",
            "billing_path": "subscription_only",
            "approved_data_destination": True,
        },
        "repository": {"root": "unused-by-fixed-suite"},
    }


def test_source_binds_controller_identity_and_fixture_origin(tmp_path, monkeypatch):
    from karajan.projects.go_suite import FixedGoSuite

    monkeypatch.setattr("karajan.projects.go_suite.go_runtime_source", lambda _: {"fixed": True})
    journal = GoCallJournal(tmp_path / "journal.sqlite")
    suite = FixedGoSuite(
        Path("controller-runtime"),
        tmp_path,
        journal,
        client_factory=lambda: httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200))
        ),
    )
    source = suite.source()
    assert source["suite_ref"] == {"id": "opencode-go-native-read-edit-linux", "revision": 1}
    assert source["observation_origin"] == "http_fixture"
    assert source["qualification_scope"] == "fixed_native_tools_fixture"
    assert source["work_root"]["path"] == str(tmp_path.resolve())
    assert source["journal"]["path"] == str(journal.path.resolve())
    assert len(source["producer_sha256"]) == 64
    suite.validate_profile(profile_binding())
    changed = profile_binding()
    changed["registration"]["profile"]["binding"]["native_settings"] = {}
    with pytest.raises(ValueError, match="FIXED_GO_PROFILE_UNSUPPORTED"):
        suite.validate_profile(changed)


def test_fixed_official_source_rejects_other_provider_registration(tmp_path, monkeypatch):
    from karajan.projects.go_suite import FixedGoSuite

    monkeypatch.setattr("karajan.projects.go_suite.go_runtime_source", lambda _: {"fixed": True})
    suite = FixedGoSuite(Path("controller-runtime"), tmp_path, GoCallJournal(tmp_path / "calls"))
    binding = profile_binding()
    binding["account"]["provider_id"] = "deepseek"
    with pytest.raises(ValueError, match="FIXED_GO_PROFILE_UNSUPPORTED"):
        suite.validate_profile(binding)


@pytest.fixture
def synthetic_suite(tmp_path, monkeypatch):
    """Only failure-injection tests use a synthetic runtime descriptor."""
    from karajan.projects.go_suite import FixedGoSuite

    monkeypatch.setattr("karajan.projects.go_suite.go_runtime_source", lambda _: {"fixed": True})
    journal = GoCallJournal(tmp_path / "calls.sqlite")
    suite = FixedGoSuite(Path("test-runtime"), tmp_path, journal, client_factory=lambda: None)
    return suite, journal, execution_start(suite)


@pytest.mark.parametrize("change", ["credential", "fence", "expiry", "source", "grant", "profile"])
def test_identity_or_bound_changes_are_rejected_before_effect(synthetic_suite, change):
    from dataclasses import replace

    from karajan.adapters.opencode.go_journal import GoJournalError

    suite, journal, start = synthetic_suite
    secret = credential()
    if change == "credential":
        secret = replace(secret, generation="rotated")
    elif change == "fence":
        start["scenarios"][0]["fence"] = True
    elif change == "expiry":
        start["expires_at"] += 1
        for item in start["scenarios"]:
            item["grant_binding"]["expires_at"] = start["expires_at"]
    elif change == "source":
        start["source"]["observation_origin"] = "official_go"
    elif change == "grant":
        start["scenarios"][1]["grant_id"] = "grant-edit"
    else:
        start["profile_digest"] = "a" * 64
    with pytest.raises(ValueError, match="FIXED_GO_START_BINDING_MISMATCH"):
        suite.observe(start, secret)
    with pytest.raises(GoJournalError, match="GRANT_NOT_FOUND"):
        journal.snapshot("grant-edit")


@pytest.mark.skipif(sys.platform != "linux", reason="Short native controller paths use Linux")
@pytest.mark.parametrize("lost_index", [1, 2])
def test_lost_create_return_revokes_preallocated_ids_without_any_observation(
    synthetic_suite, monkeypatch, lost_index
):
    suite, journal, start = synthetic_suite
    original, count = journal.create_grant, 0

    def create(binding, *, grant_id):
        nonlocal count
        result = original(binding, grant_id=grant_id)
        count += 1
        if count == lost_index:
            raise ConnectionError("lost synthetic response")
        return result

    monkeypatch.setattr(journal, "create_grant", create)
    result = suite.observe(start, credential())
    assert result["status"] == "failed"
    assert [item["status"] for item in result["scenarios"]] == ["not_run", "not_run"]
    assert journal.snapshot("grant-edit")["state"] == "revoked"
    assert journal.snapshot("grant-edit")["request_count"] == 0
    assert [item["state"] for item in result["grant_cleanup"]] == [
        "revoked",
        "revoked" if lost_index == 2 else "not_created",
    ]


@pytest.mark.skipif(sys.platform != "linux", reason="Short native controller paths use Linux")
@pytest.mark.parametrize("conflict_index", [0, 1])
def test_conflicting_grant_cleanup_preserves_foreign_binding(synthetic_suite, conflict_index):
    suite, journal, start = synthetic_suite
    scenario = start["scenarios"][conflict_index]
    foreign = {**scenario["grant_binding"], "auth_generation": "another-generation"}
    journal.create_grant(foreign, grant_id=scenario["grant_id"])
    before = journal.snapshot(scenario["grant_id"])

    result = suite.observe(start, credential())

    assert result["status"] == "failed"
    assert journal.snapshot(scenario["grant_id"]) == before
    assert result["grant_cleanup"][conflict_index]["state"] == "not_owned"
    assert "GRANT_CLEANUP_BINDING_MISMATCH" in result["reason_codes"]
    if conflict_index == 1:
        assert journal.snapshot(start["scenarios"][0]["grant_id"])["state"] == "revoked"


@pytest.mark.skipif(sys.platform != "linux", reason="Short native controller paths use Linux")
def test_observer_failure_marks_started_scenario_failed_and_next_not_run(
    synthetic_suite, monkeypatch
):
    suite, journal, start = synthetic_suite

    def broken(*args, **kwargs):
        raise RuntimeError(credential().reveal())

    monkeypatch.setattr("karajan.projects.go_suite.observe_go_tools", broken)
    result = suite.observe(start, credential())
    assert [item["status"] for item in result["scenarios"]] == ["failed", "not_run"]
    assert all(
        journal.snapshot(item["grant_id"])["state"] == "revoked" for item in start["scenarios"]
    )
    assert credential().reveal() not in json.dumps(result)


@pytest.mark.skipif(sys.platform != "linux", reason="Short native controller paths use Linux")
def test_source_disappearing_during_effect_preserves_failed_cleanup_result(
    synthetic_suite, monkeypatch
):
    suite, journal, start = synthetic_suite

    def unavailable(*args):
        raise FileNotFoundError("synthetic source removed")

    def broken(*args, **kwargs):
        monkeypatch.setattr("karajan.projects.go_suite.go_runtime_source", unavailable)
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr("karajan.projects.go_suite.observe_go_tools", broken)
    result = suite.observe(start, credential())
    assert result["status"] == "failed"
    assert "SUITE_SOURCE_UNAVAILABLE" in result["reason_codes"]
    assert all(
        journal.snapshot(item["grant_id"])["state"] == "revoked" for item in start["scenarios"]
    )


def execution_start(suite):
    source, binding, started = suite.source(), profile_binding(), time.time()
    start = {
        "qualification_id": "qualification-one",
        "project_id": "project-one",
        "suite_ref": source["suite_ref"],
        "profile_binding": binding,
        "profile_digest": source_digest(binding["registration"]["profile"]),
        "auth_generation": "generation-one",
        "credential_source_id": "source-one",
        "authentication_source": {
            "schema_version": "karajan.credential-generation.v1",
            "project_id": "project-one",
            "auth_ref": "go-secret",
            "generation": "generation-one",
            "source": {"kind": "controller_local_key_file", "id": "source-one"},
            "registered_at": started - 1,
            "previous_generation": None,
        },
        "source": source,
        "started_at": started,
        "expires_at": started + 420,
        "scenarios": [],
    }
    for scenario in ("edit", "denied_read"):
        start["scenarios"].append(
            {
                "scenario": scenario,
                "attempt_id": f"attempt-{scenario}",
                "fence": 1,
                "grant_id": f"grant-{scenario}",
                "grant_binding": {
                    "qualification_id": start["qualification_id"],
                    "attempt_id": f"attempt-{scenario}",
                    "fence": 1,
                    "profile_digest": start["profile_digest"],
                    "runtime_digest": source["runtime_digest"],
                    "channel": "go-channel",
                    "model": "glm-5.3-flash",
                    "auth_generation": "generation-one",
                    "expires_at": start["expires_at"],
                    "max_requests": 6,
                },
            }
        )
    return start


def credential():
    return ResolvedCredential(
        "project-one",
        "go-secret",
        "generation-one",
        "source-one",
        "synthetic-suite-provider-credential",
    )


def fixture_response(index, denied):
    if index == 1 or (index == 2 and not denied):
        arguments = {"filePath": "/workspace/blocked.txt" if denied else "/workspace/fixture.py"}
        if index == 2:
            arguments.update(
                oldString="return min(low, max(value, high))",
                newString="return min(high, max(low, value))",
            )
        delta, finish = (
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": f"call_{index}",
                        "type": "function",
                        "function": {
                            "name": "read" if index == 1 else "edit",
                            "arguments": json.dumps(arguments),
                        },
                    }
                ]
            },
            "tool_calls",
        )
    else:
        delta, finish = {"content": "Done."}, "stop"
    events = [
        {
            "id": "chatcmpl-fixed",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "glm-5.3-flash",
            "choices": [{"index": 0, "delta": change, "finish_reason": reason}],
        }
        for change, reason in (({"role": "assistant"}, None), (delta, None), ({}, finish))
    ]
    data = "".join("data: " + json.dumps(event) + "\n\n" for event in events)
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=(data + "data: [DONE]\n\n").encode(),
    )


@pytest.mark.skipif(sys.platform != "linux", reason="Linux namespaces required")
@pytest.mark.parametrize("report_change", [None, "call_id", "origin", "tools"])
def test_fixed_suite_cross_checks_two_real_native_scenarios_and_durable_journal(
    tmp_path, monkeypatch, report_change
):
    from karajan.isolation.go_probe import observe_go_tools
    from karajan.projects.go_suite import FixedGoSuite

    artifact = Path(
        os.environ.get(
            "KARAJAN_OPENCODE_LINUX_BINARY",
            str(
                Path(__file__).resolve().parents[2]
                / "runtimes/opencode/node_modules/opencode-linux-x64/bin/opencode"
            ),
        )
    )
    if not artifact.is_file():
        if os.environ.get("KARAJAN_REQUIRE_OPENCODE_ISOLATION") == "1":
            pytest.fail("Prepared fixed Linux OpenCode artifact is required")
        pytest.skip("Prepared fixed Linux OpenCode artifact is unavailable")
    journal, calls = GoCallJournal(tmp_path / "journal.sqlite"), [[], []]

    def factory():
        def receive(request):
            # Both grants must be durable before the first native request.
            assert journal.snapshot("grant-edit")["binding"]["max_requests"] == 6
            assert journal.snapshot("grant-denied_read")["binding"]["max_requests"] == 6
            denied = journal.snapshot("grant-edit")["state"] == "revoked"
            scenario_calls = calls[1 if denied else 0]
            scenario_calls.append(request)
            return fixture_response(len(scenario_calls), denied)

        return httpx.Client(transport=httpx.MockTransport(receive), trust_env=False)

    suite = FixedGoSuite(artifact, tmp_path, journal, client_factory=factory)
    start = execution_start(suite)
    if report_change:

        def altered(*args, **kwargs):
            report = observe_go_tools(*args, **kwargs)
            assert report["status"] == "passed", report
            if report_change == "call_id":
                report["requests"][0]["journal_call_id"] = "other-real-call"
            elif report_change == "origin":
                report["observation_origin"] = "official_go"
            else:
                report["tools"] = []
            return report

        monkeypatch.setattr("karajan.projects.go_suite.observe_go_tools", altered)
    result = suite.observe(start, credential())
    if report_change:
        assert result["status"] == "failed", result
        assert [item["status"] for item in result["scenarios"]] == ["failed", "not_run"]
        assert [len(items) for items in calls] == [3, 0]
        assert journal.snapshot("grant-denied_read")["state"] == "revoked"
        return
    assert result["status"] == "passed", json.dumps(result, indent=2)
    assert result["qualification_scope"] == "fixed_native_tools_fixture"
    assert result["runtime_tools_status"] == "not_run"
    assert result["dispatch_eligible"] is False
    assert result["provider_remote_stop"] == "unknown"
    assert [len(items) for items in calls] == [3, 2]
    assert [item["status"] for item in result["scenarios"]] == ["passed", "passed"]
    reopened = GoCallJournal(tmp_path / "journal.sqlite")
    for scenario in ("edit", "denied_read"):
        assert reopened.snapshot(f"grant-{scenario}")["state"] == "revoked"
    assert credential().reveal() not in json.dumps(result)
    with pytest.raises(ValueError, match="NEW_CONTROLLER_DIRECTORY_REQUIRED"):
        suite.observe(start, credential())
    assert [len(items) for items in calls] == [3, 2]
