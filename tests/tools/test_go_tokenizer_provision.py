"""Local public provisioning boundary; only the official cached bytes are served."""

import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from karajan.adapters.opencode.go_context import GoRequestAccounting

REPO = Path(__file__).resolve().parents[2]
ARTIFACT_DIRECTORY = Path(
    os.environ.get("KARAJAN_GO_TOKENIZER_DIRECTORY", str(REPO / ".cache/go-context-artifacts"))
).resolve()


@pytest.fixture(scope="module", autouse=True)
def require_local_artifacts():
    if not ARTIFACT_DIRECTORY.is_dir():
        if os.environ.get("KARAJAN_REQUIRE_GO_TOKENIZER") == "1":
            pytest.fail("Prepared fixed tokenizer artifacts are required")
        pytest.skip("Prepared fixed tokenizer artifacts are unavailable")


SPEC = importlib.util.spec_from_file_location(
    "go_tokenizer_provision", REPO / ".github/scripts/provision_go_tokenizer.py"
)
assert SPEC is not None and SPEC.loader is not None
SCRIPT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SCRIPT)


def test_verified_existing_artifacts_do_not_connect() -> None:
    def forbidden(url: str) -> io.BytesIO:
        raise AssertionError("No connection allowed for complete verified artifacts")

    result = SCRIPT.provision(ARTIFACT_DIRECTORY, open_url=forbidden)
    assert [item["status"] for item in result["artifacts"]] == ["verified"] * 3


def test_verified_downloads_are_atomic_and_match_frozen_accounting_source(tmp_path: Path) -> None:
    calls = []
    source = GoRequestAccounting(ARTIFACT_DIRECTORY).source()

    def local_file(url: str):
        name = url.rsplit("/", 1)[-1]
        assert url == source["artifacts"][name]["url"]
        calls.append(name)
        return (ARTIFACT_DIRECTORY / name).open("rb")

    result = SCRIPT.provision(tmp_path / "new", open_url=local_file)
    assert calls == list(source["artifacts"])
    assert all(item["status"] == "downloaded" for item in result["artifacts"])
    for name, expected in source["artifacts"].items():
        data = (tmp_path / "new" / name).read_bytes()
        assert len(data) == expected["bytes"]
        assert hashlib.sha256(data).hexdigest() == expected["sha256"]
    assert not list((tmp_path / "new").glob("*.tmp"))


@pytest.mark.parametrize("kind", ["short", "extra", "hash", "lost_stream"])
def test_failed_download_preserves_old_file_and_removes_temporary(
    tmp_path: Path, kind: str
) -> None:
    existing = tmp_path / "tokenizer.json"
    existing.write_bytes(b"old-invalid-cache")
    reference = (ARTIFACT_DIRECTORY / "tokenizer.json").read_bytes()

    class LostStream(io.BytesIO):
        def read(self, amount=-1):
            if self.tell():
                raise OSError("synthetic response failure")
            return super().read(amount)

    responses = {
        "short": lambda: io.BytesIO(reference[:-1]),
        "extra": lambda: io.BytesIO(reference + b"x"),
        "hash": lambda: io.BytesIO(b"x" + reference[1:]),
        "lost_stream": lambda: LostStream(reference),
    }
    with pytest.raises((SCRIPT.ProvisionError, OSError)):
        SCRIPT.provision(tmp_path, open_url=lambda _: responses[kind]())
    assert existing.read_bytes() == b"old-invalid-cache"
    assert list(tmp_path.iterdir()) == [existing]


def test_ci_required_artifact_is_failure_not_skip(tmp_path: Path) -> None:
    env = dict(os.environ)
    env.update(
        {
            "KARAJAN_GO_TOKENIZER_DIRECTORY": str(tmp_path / "not-provisioned"),
            "KARAJAN_REQUIRE_GO_TOKENIZER": "1",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
        }
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/adapters/opencode/test_go_context.py",
            "-q",
            "-k",
            "counts_actual_official_template",
        ],
        cwd=REPO,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "Required pinned official tokenizer artifacts were not provisioned" in result.stdout
    assert "skipped" not in result.stdout


def test_public_cli_reuses_verified_files_without_network() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(REPO / ".github/scripts/provision_go_tokenizer.py"),
            "--directory",
            str(ARTIFACT_DIRECTORY),
        ],
        cwd=REPO,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout
    assert all(item["status"] == "verified" for item in json.loads(result.stdout)["artifacts"])
