"""No-live safety checks for the #153 demo entry point."""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "examples" / "business-planning-demo" / "run_live.py"


def _runtime(fallback: Path) -> Path:
    configured = os.environ.get("KARAJAN_OPENCODE_LINUX_BINARY") or os.environ.get(
        "KARAJAN_GO_RUNTIME"
    )
    return Path(configured) if configured else fallback


def _tokenizer() -> Path:
    configured = os.environ.get("KARAJAN_GO_TOKENIZER_DIRECTORY")
    tokenizer = Path(configured) if configured else ROOT / ".cache" / "go-context-artifacts"
    if not tokenizer.is_dir():
        if os.environ.get("KARAJAN_REQUIRE_GO_TOKENIZER") == "1":
            pytest.fail("Prepared Go tokenizer artifacts are required")
        pytest.skip("Prepared Go tokenizer artifacts are not available")
    return tokenizer


def _module():
    spec = importlib.util.spec_from_file_location("business_planning_demo", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_demo_requires_explicit_live_and_does_not_read_credential(tmp_path: Path) -> None:
    credential = tmp_path / "would-be-secret.key"
    credential.write_text("this must not be opened\n", encoding="ascii")
    state = tmp_path / "new-demo-state"
    runtime = _runtime(tmp_path / "runtime")
    if runtime == tmp_path / "runtime":
        runtime.write_bytes(b"dummy runtime")
    tokenizer = _tokenizer()
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--runtime",
            str(runtime),
            "--tokenizer-directory",
            str(tokenizer),
            "--credential-file",
            str(credential),
            "--directory",
            str(state),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    output = json.loads(result.stdout)
    assert output["status"] == "not_run"
    assert output["credential_file_checked"] is False
    assert output["provider_called"] is False
    assert not state.exists()


def test_live_setup_reaches_qualification_boundary_without_provider(tmp_path: Path) -> None:
    module = _module()
    runtime = _runtime(tmp_path / "runtime")
    if runtime == tmp_path / "runtime":
        runtime.write_bytes(b"dummy runtime")
    tokenizer = _tokenizer()
    credential = tmp_path / "credential.key"
    credential.write_text("dummy-credential-material-1234", encoding="ascii")
    state = tmp_path / "live-state"

    class StopBeforeProvider(Exception):
        pass

    qualification_project_id: str | None = None
    configuration_digest: str | None = None

    def stop_at_qualification(store, project_id, profile_ref, **kwargs):
        nonlocal qualification_project_id, configuration_digest
        qualification_project_id = project_id
        assert project_id
        assert profile_ref == {"id": "commander", "revision": 1}
        assert kwargs["principal"] == "owner"
        assert kwargs["validity_seconds"] == 3600
        assert store.commander_suite is not None
        assert store.projects.database == state / "planning-state" / "projects.sqlite"
        configured = store.projects.get_configuration(project_id)["configuration"]
        configuration_digest = store.projects.get(project_id)["configuration"]["digest"]
        evidence = configured["resources"]["profiles"][0]["capability_evidence"]
        assert {row["capability"] for row in evidence} == {
            "design_reasoning",
            "structured_plan_output",
        }
        assert all(row["status"] == "not_run" for row in evidence)
        assert all(row["profile_digest"] is None for row in evidence)
        policy = store.projects.get_execution_policy(
            project_id, "business-planning-demo", 1, principal="owner"
        )
        assert policy["schema_version"] == "karajan.execution-policy.v2"
        assert policy["max_context_tokens"] == 16384
        assert policy["context_policy"]["reserved_output_tokens"] == 4096
        assert policy["context_policy"]["measurement"]["fixed_margin"] == 2048
        assert policy["context_policy"]["measurement"]["ratio_margin_basis_points"] == 1000
        assert policy["constraints"]["tools"] == []
        raise StopBeforeProvider

    values = {
        "runtime": runtime,
        "tokenizer": tokenizer,
        "credential": credential,
        "directory": state,
    }
    try:
        module._live(values, qualifier=stop_at_qualification)
    except StopBeforeProvider:
        pass
    else:
        raise AssertionError("setup did not reach the qualification boundary")

    repository = state / "repository"
    assert (repository / "greeting.py").read_bytes() == (
        b'def greet(name: str) -> str:\n    return f"Hello, {name}!"\n'
    )
    assert (state / "planning-state" / "projects.sqlite").is_file()
    assert (state / "control" / "commander-qualification-source.v2.json").is_file()
    assert (state / "control" / "planning-admission-bootstrap.json").is_file()
    assert (state / "planning-state" / "projects.sqlite").is_file()
    assert (state / "planning-state" / "planning-admission.sqlite").is_file()
    projection = module._journal_scenario_projection(
        state / "commander-journal.sqlite",
        [{"scenario": "missing", "grant_id": "grant-not-present"}],
    )
    assert projection == [
        {
            "scenario": "missing",
            "grant_id": "grant-not-present",
            "state": "unknown",
            "request_count": None,
            "status": "not_run",
        }
    ]
    sys.path.insert(0, str(ROOT / "backend"))
    from karajan.capacity import CapacityStore

    capacity = CapacityStore(state / "planning-state" / "capacity.sqlite", existing_only=True)
    snapshot = capacity.snapshot()
    assert snapshot["pools"] == [
        {
            "id": "opencode-go-requests",
            "account_id": "opencode-go",
            "kind": "service",
            "unit": "requests",
            "window_kind": "unknown",
        }
    ]
    assert snapshot["profiles"] == [
        {
            "id": "commander",
            "revision": 1,
            "account_id": "opencode-go",
            "pool_ids": ["opencode-go-requests"],
        }
    ]
    assert snapshot["observations"] == []
    assert snapshot["policies"][0]["policy"]["max_active_attempts"] == 1
    assert snapshot["policies"][0]["policy"]["max_attempt_duration_seconds"] == 300

    from karajan.web.__main__ import _prepare_state
    from karajan.web.app import create_app

    web_state = _prepare_state(tmp_path / "web-state")
    app = create_app(
        web_state,
        origin="http://127.0.0.1:8765",
        bootstrap_token="test-bootstrap-token",
        planning_control_directory=state / "control",
    )
    assert app is not None
    assert qualification_project_id is not None
    assert configuration_digest is not None
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        login = client.post(
            "/v1/session/bootstrap",
            json={"token": "test-bootstrap-token"},
            headers={"Origin": "http://127.0.0.1:8765"},
        )
        assert login.status_code == 200
        readiness = client.get(
            f"/v1/projects/{qualification_project_id}/planning-preparation-readiness"
        )
        assert readiness.status_code == 200
        assert readiness.json() == {
            "schema_version": "karajan.planning-preparation-readiness.v1",
            "project_id": qualification_project_id,
            "configuration_revision": 1,
            "configuration_digest": configuration_digest,
            "configuration_status": "draft",
            "v2_preparation_allowed": True,
            "qualification_state": "unknown",
            "qualification_pending": True,
            "reason_codes": ["CAPABILITY_NOT_PASSED"],
            "activation_allowed": False,
        }
    assert (state / "commander-journal.sqlite").is_file()
    assert (state / "commander-work").is_dir()
    assert (state / "credential-private").is_dir()
