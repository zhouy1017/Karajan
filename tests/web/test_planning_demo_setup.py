"""No-live safety checks for the #153 demo entry point."""

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "examples" / "business-planning-demo" / "run_live.py"


def test_demo_requires_explicit_live_and_does_not_read_credential(tmp_path: Path) -> None:
    credential = tmp_path / "would-be-secret.key"
    credential.write_text("this must not be opened\n", encoding="ascii")
    state = tmp_path / "new-demo-state"
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--runtime",
            str(ROOT.parents[1] / ".cache" / "go-linux-runtime" / "package" / "bin" / "opencode"),
            "--tokenizer-directory",
            str(ROOT.parents[1] / ".cache" / "go-context-artifacts"),
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
