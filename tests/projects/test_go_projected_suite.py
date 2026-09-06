"""Explicit projected-suite contracts; synthetic credentials and local upstream only."""

import copy
import hashlib
import json
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest
from karajan.adapters.opencode.go_journal import GoCallJournal
from karajan.isolation.go_probe import source_digest
from karajan.projects.go_suite import FixedGoSuite
from test_go_context import accounting, artifacts
from test_go_suite import credential, execution_start, profile_binding

__all__ = ["accounting", "artifacts"]

V2_REF = {"id": "opencode-go-native-read-edit-linux", "revision": 2}


def test_projected_revision_requires_explicit_accounting_before_any_effect(tmp_path):
    with pytest.raises(ValueError, match="^PROJECTED_GO_ACCOUNTING_REQUIRED$"):
        FixedGoSuite(
            Path("unused-runtime"),
            tmp_path,
            GoCallJournal(tmp_path / "calls.sqlite"),
            suite_ref=V2_REF,
        )


@pytest.mark.parametrize("revision", [True, 0, 3, "2"])
def test_unrecognized_revision_never_falls_back_to_legacy(tmp_path, revision):
    with pytest.raises(ValueError, match="^FIXED_GO_SUITE_UNSUPPORTED$"):
        FixedGoSuite(
            Path("unused"),
            tmp_path,
            GoCallJournal(tmp_path / "calls"),
            suite_ref={**V2_REF, "revision": revision},
        )


def test_accounting_does_not_silently_upgrade_an_unversioned_suite(tmp_path, accounting):
    with pytest.raises(ValueError, match="^FIXED_GO_ACCOUNTING_UNSUPPORTED$"):
        FixedGoSuite(
            Path("unused"), tmp_path, GoCallJournal(tmp_path / "calls"), accounting=accounting
        )


def test_profile_must_explicitly_bind_the_selected_revision(tmp_path, accounting):
    suite = FixedGoSuite(
        Path("unused"),
        tmp_path,
        GoCallJournal(tmp_path / "calls"),
        suite_ref=V2_REF,
        accounting=accounting,
    )
    bound = profile_binding()
    with pytest.raises(ValueError, match="^FIXED_GO_PROFILE_UNSUPPORTED$"):
        suite.validate_profile(bound)
    bound["registration"]["profile"]["binding"]["native_settings"]["suite_ref"] = dict(V2_REF)
    suite.validate_profile(bound)
    bound["registration"]["profile"]["binding"]["native_settings"]["permission"] = "bash"
    with pytest.raises(ValueError, match="^FIXED_GO_PROFILE_UNSUPPORTED$"):
        suite.validate_profile(bound)


def projected_start(suite):
    start = execution_start(suite)
    profile = start["profile_binding"]["registration"]["profile"]
    profile["binding"]["native_settings"]["suite_ref"] = dict(V2_REF)
    start["profile_digest"] = source_digest(profile)
    for item in start["scenarios"]:
        item["grant_binding"].update(
            schema_version="karajan.go-qualification-grant.v2",
            profile_digest=start["profile_digest"],
            probe_spec_digest=source_digest(start["source"]["probe_spec"]),
            scenario=item["scenario"],
            context=copy.deepcopy(start["source"]["probe_spec"]["context"]),
        )
    return start


def projected_factory(journal, sent):
    from test_go_projected_probe import response

    def receive(request):
        denied = journal.snapshot("grant-edit")["state"] == "revoked"
        scenario = "denied_read" if denied else "edit"
        calls = sent[scenario]
        calls.append(json.loads(request.content))
        current = journal.snapshot("grant-" + scenario)
        assert current["calls"][-1]["state"] == "send_unknown"
        assert current["request_count"] == len(calls)
        encoded = json.dumps(
            calls[-1], ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode()
        assert (
            current["calls"][-1]["request_context"]["request_digest"]
            == hashlib.sha256(encoded).hexdigest()
        )
        assert journal.snapshot("grant-denied_read")["binding"]["scenario"] == "denied_read"
        return response(len(calls), scenario)

    return lambda: httpx.Client(transport=httpx.MockTransport(receive), trust_env=False)


@pytest.mark.skipif(sys.platform != "linux", reason="Actual fixed Linux runtime source required")
@pytest.mark.parametrize(
    "change", ["spec", "limits", "scenario", "legacy", "profile", "credential"]
)
def test_exact_persisted_projected_start_precedes_grants_and_namespace(accounting, change):
    from dataclasses import replace

    from karajan.adapters.opencode.go_journal import GoJournalError
    from test_opencode_go_composition import runtime_artifact

    with tempfile.TemporaryDirectory(prefix="kgqs-") as name:
        root = Path(name)
        journal = GoCallJournal(root / "calls.sqlite")
        suite = FixedGoSuite(
            runtime_artifact(), root, journal, suite_ref=V2_REF, accounting=accounting
        )
        start = projected_start(suite)
        secret = credential()
        if change == "spec":
            start["source"]["probe_spec"]["prompt_sha256"]["edit"] = "0" * 64
        elif change == "limits":
            start["scenarios"][0]["grant_binding"]["context"]["approved_input_tokens"] -= 1
        elif change == "scenario":
            start["scenarios"][0]["grant_binding"]["scenario"] = "denied_read"
        elif change == "legacy":
            for key in ("schema_version", "probe_spec_digest", "scenario", "context"):
                del start["scenarios"][0]["grant_binding"][key]
        elif change == "profile":
            start["profile_binding"]["registration"]["profile"]["binding"]["native_settings"][
                "suite_ref"
            ]["revision"] = 1
        else:
            secret = replace(secret, generation="new-generation")
        with pytest.raises(
            ValueError, match="^FIXED_GO_(START_BINDING_MISMATCH|PROFILE_UNSUPPORTED)$"
        ):
            suite.observe(start, secret)
        with pytest.raises(GoJournalError, match="^GRANT_NOT_FOUND$"):
            journal.snapshot("grant-edit")
        assert sorted(p.name for p in root.iterdir()) == ["calls.sqlite"]


@pytest.mark.skipif(
    sys.platform != "linux", reason="Actual Linux native tools and Collector required"
)
@pytest.mark.parametrize("change", [None, "retention", "measurement", "manifest", "fixture_cases"])
def test_projected_suite_correlates_real_native_wire_and_complete_candidate(
    accounting, monkeypatch, tmp_path, change
):
    import karajan.isolation.go_projected_probe as producer
    from test_opencode_go_composition import runtime_artifact

    with tempfile.TemporaryDirectory(prefix="kgqs-") as name:
        root = Path(name)
        journal = GoCallJournal(root / "calls.sqlite")
        sent = {"edit": [], "denied_read": []}
        clock_observations = []

        def observed_clock():
            now = time.time()
            clock_observations.append(now)
            (tmp_path / "clock.json").write_text(
                json.dumps(
                    {
                        "started_at": start["started_at"],
                        "observations": clock_observations,
                    }
                )
            )
            return now

        suite = FixedGoSuite(
            runtime_artifact(),
            root,
            journal,
            suite_ref=V2_REF,
            accounting=accounting,
            client_factory=projected_factory(journal, sent),
            clock=observed_clock,
        )
        start = projected_start(suite)
        source = suite.source()
        assert source["schema_version"] == "karajan.fixed-go-suite-source.v2"
        assert source["qualification_scope"] == "projected_native_tools_fixture"
        assert source["probe_spec"] == source["runtime_source"]["probe_spec"]
        (tmp_path / "start.json").write_text(json.dumps(start, indent=2))
        if change:
            original = producer.observe_go_projected_tools

            def altered(*args, **kwargs):
                report = original(*args, **kwargs)
                assert report["status"] == "passed", report["reason_codes"]
                if change == "retention":
                    report["retention"]["calls"][-1]["request_digest"] = "e" * 64
                elif change == "measurement":
                    report["requests"][0]["request_context"]["source_sha256"] = "e" * 64
                elif change == "fixture_cases":
                    report["fixture_cases"] = [False] * 4
                else:
                    report["capture"]["candidate_manifest"].pop(0)
                return report

            monkeypatch.setattr(producer, "observe_go_projected_tools", altered)
        result = suite.observe(start, credential())
        (tmp_path / "suite-report.json").write_text(json.dumps(result, indent=2))
        (tmp_path / "clock.json").write_text(
            json.dumps(
                {
                    "started_at": start["started_at"],
                    "observations": clock_observations,
                }
            )
        )
        if change:
            assert result["status"] == "failed", result
            assert [r["status"] for r in result["scenarios"]] == ["failed", "not_run"]
            assert result["validation"]["projected_native_tools"] == "failed"
            assert result["validation"]["candidate_capture"] == "failed"
            assert result["validation"]["context_accounting"] == "failed"
            assert [len(v) for v in sent.values()] == [4, 0]
            expected = {
                "manifest": "PROJECTED_CAPTURE_EVIDENCE_INVALID",
                "fixture_cases": "PROJECTED_TOOL_EVIDENCE_INCOMPLETE",
            }.get(change, "PROJECTED_CONTEXT_OR_RETENTION_INVALID")
            assert expected in result["scenarios"][0]["reason_codes"]
        else:
            assert result["status"] == "passed", result
            assert [len(v) for v in sent.values()] == [4, 2]
            assert all(
                result["validation"][key] == "passed"
                for key in ("projected_native_tools", "candidate_capture", "context_accounting")
            )
            assert result["validation"]["runtime_tools"] == "not_run"
            assert result["validation"]["dispatch"] is False
            assert result["billing_limit_qualification"] == "not_run"
            for scenario in result["scenarios"]:
                capture = scenario["observation"]["capture"]
                assert len(capture["candidate_manifest"]) == 4
                assert capture["validation_gate"]["local_gate_passed"] is False
            with pytest.raises(ValueError, match="^NEW_CONTROLLER_DIRECTORY_REQUIRED$"):
                suite.observe(start, credential())
            assert [len(v) for v in sent.values()] == [4, 2]
        assert credential().reveal() not in json.dumps(result)
        assert all(
            journal.snapshot(item["grant_id"])["state"] == "revoked" for item in start["scenarios"]
        )
