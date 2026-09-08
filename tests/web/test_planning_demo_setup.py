"""No-live safety checks for the #153 demo entry point."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "examples" / "business-planning-demo" / "run_live.py"


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
    runtime = tmp_path / "runtime"
    runtime.write_bytes(b"dummy runtime")
    tokenizer = tmp_path / "tokenizer"
    tokenizer.mkdir()
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
    runtime = tmp_path / "runtime"
    runtime.write_bytes(b"dummy runtime")
    tokenizer = tmp_path / "tokenizer"
    tokenizer.mkdir()
    credential = tmp_path / "credential.key"
    credential.write_text("dummy-credential-material-1234", encoding="ascii")
    state = tmp_path / "live-state"

    class StopBeforeProvider(Exception):
        pass

    def stop_at_qualification(store, project_id, profile_ref, **kwargs):
        assert project_id
        assert profile_ref == {"id": "commander", "revision": 1}
        assert kwargs["principal"] == "owner"
        assert kwargs["validity_seconds"] == 3600
        assert store.commander_suite is not None
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
    assert (state / "projects.sqlite").is_file()
    assert (state / "control" / "commander-qualification-source.v2.json").is_file()
    assert (state / "planning-control" / "planning-admission-bootstrap.json").is_file()
    assert (state / "planning-state" / "projects.sqlite").is_file()
    assert (state / "planning-state" / "planning-admission.sqlite").is_file()
    assert (state / "commander-journal.sqlite").is_file()
    assert (state / "commander-work").is_dir()
    assert (state / "credential-private").is_dir()
