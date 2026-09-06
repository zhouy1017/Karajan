"""Controller qualification commands preserve their source and completed history."""

import copy
import json
import os
import sys
import tempfile
from pathlib import Path

import httpx
import pytest
import test_qualification_store as qualification_fixtures
from karajan.projects.qualification import ProfileQualificationStore, QualificationError
from test_qualification_store import apply, qualify

SUITE = {"id": "opencode-go-native-read-edit-linux", "revision": 1}
case = qualification_fixtures.case


def test_runtime_qualification_requires_a_controller_configured_source(case: dict) -> None:
    store = ProfileQualificationStore(case["projects"])
    with pytest.raises(QualificationError, match="RUNTIME_QUALIFICATION_SOURCE_UNCONFIGURED"):
        store.qualify_runtime_tools(
            case["project_id"],
            {"id": "fixture-profile", "revision": 1},
            principal="owner",
            command_key="runtime-qualify",
            suite_ref={"id": "opencode-go-native-read-edit-linux", "revision": 1},
            validity_seconds=60,
        )


def test_qualification_start_is_readable_with_explicit_source_scope(case: dict) -> None:
    result = qualify(case)
    store = ProfileQualificationStore(case["projects"])
    start = store.get_start(case["project_id"], result["id"], principal="owner")
    assert start["qualification_scope"] == "local_fixture"
    assert start["suite_ref"] == {"id": "fixed-local-fixture-qualification", "revision": 1}
    assert start["completed"] is True
    assert start["binding"]["profile_binding"] == result["binding"]["profile_binding"]
    with pytest.raises(ValueError, match="USER_DECISION_REQUIRED"):
        store.get_start(case["project_id"], result["id"], principal="other")


def test_command_key_recovers_start_without_knowing_response_identity(case: dict) -> None:
    result = qualify(case, "lost-client-reply")
    store = ProfileQualificationStore(case["projects"])
    recovered = store.get_command_start(case["project_id"], "lost-client-reply", principal="owner")
    assert recovered["id"] == result["id"]
    assert recovered["completed"] is True


@pytest.fixture
def go_case(case: dict, tmp_path: Path):
    if sys.platform != "linux":
        pytest.skip("Fixed native Go qualification requires Linux namespaces")
    from karajan.adapters.opencode.go_journal import GoCallJournal
    from karajan.projects.credential_sources import CredentialSourceStore, LocalKeyFile
    from karajan.projects.go_suite import FixedGoSuite

    artifact = Path(
        os.environ.get(
            "KARAJAN_OPENCODE_LINUX_BINARY",
            str(
                Path(__file__).parents[2]
                / "runtimes/opencode/node_modules/opencode-linux-x64/bin/opencode"
            ),
        )
    )
    if not artifact.is_file():
        if os.environ.get("KARAJAN_REQUIRE_OPENCODE_ISOLATION") == "1":
            pytest.fail("Required fixed Linux OpenCode artifact is absent")
        pytest.skip("Fixed Linux OpenCode artifact is unavailable")
    config = copy.deepcopy(case["configuration"])
    registered = config["resources"]["profiles"][0]
    profile = registered["profile"]
    profile["binding"].update(
        runtime_kind="opencode-go-isolated",
        runtime_version="1.18.29",
        model_id="glm-5.3-flash",
        auth_mode="api_key",
        native_settings={"suite_ref": SUITE},
    )
    profile["auth_ref"] = "secret:go"
    profile["required_permissions"] = ["read", "edit"]
    config["resources"]["accounts"][0].update(provider_id="opencode-go", secret_ref="secret:go")
    apply(case, config)
    key_file = tmp_path / "synthetic.key"
    key_file.write_text("synthetic-go-credential-for-fixture-only", encoding="ascii")
    credentials = CredentialSourceStore(
        case["projects"],
        sources={(case["project_id"], "secret:go"): LocalKeyFile("go", key_file)},
        private_directory=tmp_path / "credential-private",
    )
    credentials.register(
        case["project_id"], "secret:go", principal="owner", command_key="credential"
    )
    requests = []
    sessions = {}
    hook = [None]

    def receive(request):
        from test_opencode_go_composition import native_response

        body = json.loads(request.content)
        session = request.headers["x-opencode-session"]
        sessions[session] = sessions.get(session, 0) + 1
        requests.append(request.url.path)
        if hook[0] is not None:
            hook[0](request)
        return native_response(
            sessions[session],
            denied="KARAJAN_READ_DENIED" in json.dumps(body),
            old_string="return min(low, max(value, high))",
        )

    journal = GoCallJournal(tmp_path / "calls.sqlite")
    # UDS paths have a fixed kernel length limit, independent of pytest's long
    # per-test directory names. The controller selects a short private root.
    with tempfile.TemporaryDirectory(prefix="kgq-") as work_root:
        suite = FixedGoSuite(
            artifact,
            Path(work_root),
            journal,
            client_factory=lambda: httpx.Client(transport=httpx.MockTransport(receive)),
        )
        store = ProfileQualificationStore(case["projects"], credentials=credentials, go_suite=suite)
        yield {
            **case,
            "registration": registered,
            "store": store,
            "suite": suite,
            "credentials": credentials,
            "key_file": key_file,
            "requests": requests,
            "hook": hook,
        }


def run_go(case: dict, key: str = "go-qualify") -> dict:
    return case["store"].qualify_runtime_tools(
        case["project_id"],
        {"id": "fixture-profile", "revision": 1},
        principal="owner",
        command_key=key,
        suite_ref=SUITE,
        validity_seconds=3600,
    )


def test_fixed_native_suite_is_persisted_and_replayed_without_promoting_fixture(go_case):
    result = run_go(go_case)
    assert result["status"] == "passed", result["reason_codes"]
    assert result["qualification_scope"] == "fixed_native_tools_fixture"
    assert result["runtime_tools_status"] == "not_run"
    assert result["dispatch_eligible"] is False
    assert len(go_case["requests"]) == 5
    reopened = ProfileQualificationStore(
        go_case["projects"], credentials=go_case["credentials"], go_suite=go_case["suite"]
    )
    go_case["store"] = reopened
    assert run_go(go_case) == result
    assert len(go_case["requests"]) == 5
    assert reopened.get(go_case["project_id"], result["id"], principal="owner")["record"] == result
    with reopened.routing_facts_guard(
        go_case["project_id"], [go_case["registration"]], principal="owner"
    ) as guarded:
        assert guarded["profiles"][0]["qualification"] is None
        assert guarded["profiles"][0]["reason_codes"] == ["RUNTIME_TOOLS_NOT_QUALIFIED"]


def test_start_is_readable_before_http_and_inflight_replay_cannot_send_again(go_case):
    starts = []

    def before_response(_request):
        if len(go_case["requests"]) != 1:
            return
        store = go_case["store"]
        start = store.get_command_start(go_case["project_id"], "go-qualify", principal="owner")
        assert start["completed"] is False
        attempts = start["binding"]["execution_start"]["scenarios"]
        assert len(attempts) == 2
        states = [go_case["suite"].journal.snapshot(attempt["grant_id"]) for attempt in attempts]
        assert states[0]["request_count"] == 1
        assert states[0]["calls"][0]["state"] == "send_unknown"
        assert states[1]["request_count"] == 0
        with pytest.raises(QualificationError, match="QUALIFICATION_IN_PROGRESS_OR_UNKNOWN"):
            run_go(go_case)
        starts.append(start)

    go_case["hook"][0] = before_response
    record = run_go(go_case)
    assert record["status"] == "passed", record["reason_codes"]
    assert len(starts) == 1
    assert starts[0]["id"] == record["id"]
    assert len(go_case["requests"]) == 5
    fixed = go_case["store"].facts_for_profile(
        go_case["project_id"],
        go_case["registration"],
        principal="owner",
        scope="fixed_native_tools_fixture",
    )
    assert fixed["facts"]["tools"] == ["fixed_go_fixture_read", "fixed_go_fixture_edit"]
    assert fixed["facts"]["roles"] == []
    assert fixed["facts"]["context_tokens"] is None
    assert fixed["facts"]["budget_enforcement"] == "unknown"
    assert fixed["dispatch_eligible"] is False
