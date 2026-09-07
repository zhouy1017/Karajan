"""Offline tests for the bounded, redacted tokenizer download retry boundary."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from email.message import Message
from pathlib import Path
from typing import BinaryIO
from urllib.error import HTTPError, URLError

import pytest

REPO = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "go_tokenizer_provision", REPO / ".github/scripts/provision_go_tokenizer.py"
)
assert SPEC is not None and SPEC.loader is not None
SCRIPT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SCRIPT)


@contextmanager
def response(data: bytes | BinaryIO) -> Iterator[BinaryIO]:
    yield io.BytesIO(data) if isinstance(data, bytes) else data


@pytest.fixture
def small_artifact(monkeypatch: pytest.MonkeyPatch) -> bytes:
    data = b"fixed offline tokenizer fixture"
    monkeypatch.setattr(
        SCRIPT,
        "ARTIFACTS",
        {"fixture.bin": (len(data), hashlib.sha256(data).hexdigest())},
    )
    return data


def test_transient_network_failure_retries_then_publishes_atomically(
    tmp_path: Path, small_artifact: bytes
) -> None:
    calls: list[str] = []

    def connect(url: str) -> object:
        calls.append(url)
        if len(calls) < 3:
            raise TimeoutError("private transport detail")
        return response(small_artifact)

    result = SCRIPT.provision(tmp_path, open_url=connect)

    assert len(calls) == 3
    assert result["artifacts"] == [
        {
            "name": "fixture.bin",
            "status": "downloaded",
            "bytes": len(small_artifact),
            "sha256": hashlib.sha256(small_artifact).hexdigest(),
        }
    ]
    assert (tmp_path / "fixture.bin").read_bytes() == small_artifact
    assert not list(tmp_path.glob("*.tmp"))


def test_transient_read_failure_is_retried(tmp_path: Path, small_artifact: bytes) -> None:
    calls = 0

    class FlakyResponse(io.BytesIO):
        def read(self, amount: int | None = -1) -> bytes:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ConnectionResetError("private transport detail")
            return super().read(amount)

    result = SCRIPT.provision(
        tmp_path,
        open_url=lambda _: response(FlakyResponse(small_artifact)),
    )

    assert result["artifacts"][0]["status"] == "downloaded"
    assert (tmp_path / "fixture.bin").read_bytes() == small_artifact


def test_persistent_network_failure_is_bounded_and_cleans_each_temp(
    tmp_path: Path, small_artifact: bytes
) -> None:
    calls = 0

    def connect(_: str) -> object:
        nonlocal calls
        calls += 1
        raise URLError("signed response detail")

    with pytest.raises(SCRIPT.ProvisionError, match="^TOKENIZER_NETWORK_ERROR$"):
        SCRIPT.provision(tmp_path, open_url=connect)

    assert calls == SCRIPT.MAX_DOWNLOAD_ATTEMPTS
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("status", [400, 404, 401, 403])
def test_non_retryable_http_status_is_redacted_and_single_attempt(
    tmp_path: Path, small_artifact: bytes, status: int
) -> None:
    calls = 0

    def connect(url: str) -> object:
        nonlocal calls
        calls += 1
        raise HTTPError(url, status, "secret response body", Message(), None)

    with pytest.raises(SCRIPT.ProvisionError, match=rf"^TOKENIZER_HTTP_STATUS_{status}$"):
        SCRIPT.provision(tmp_path, open_url=connect)

    assert calls == 1
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("status", [429, 500, 503])
def test_retryable_http_status_uses_three_attempts(
    tmp_path: Path, small_artifact: bytes, status: int
) -> None:
    calls = 0

    def connect(url: str) -> object:
        nonlocal calls
        calls += 1
        raise HTTPError(url, status, "secret response body", Message(), None)

    with pytest.raises(SCRIPT.ProvisionError, match=rf"^TOKENIZER_HTTP_STATUS_{status}$"):
        SCRIPT.provision(tmp_path, open_url=connect)

    assert calls == SCRIPT.MAX_DOWNLOAD_ATTEMPTS


def test_retry_attempts_share_one_download_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, small_artifact: bytes
) -> None:
    clock = iter([0.0, 1.0, 181.0, 182.0])
    monkeypatch.setattr(SCRIPT.time, "monotonic", lambda: next(clock))
    calls = 0

    def connect(_: str) -> object:
        nonlocal calls
        calls += 1
        raise TimeoutError("private transport detail")

    with pytest.raises(SCRIPT.ProvisionError, match="^TOKENIZER_DOWNLOAD_TIMEOUT$"):
        SCRIPT.provision(tmp_path, open_url=connect)

    assert calls == 1
    assert list(tmp_path.iterdir()) == []


def test_bad_digest_is_deterministic_and_never_retried(
    tmp_path: Path, small_artifact: bytes
) -> None:
    calls = 0

    def connect(_: str) -> object:
        nonlocal calls
        calls += 1
        return response(b"x" * len(small_artifact))

    with pytest.raises(SCRIPT.ProvisionError, match="^TOKENIZER_DIGEST_MISMATCH$"):
        SCRIPT.provision(tmp_path, open_url=connect)

    assert calls == 1
    assert list(tmp_path.iterdir()) == []


def test_cli_network_error_does_not_emit_exception_detail(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    small_artifact: bytes,
) -> None:
    monkeypatch.setattr(SCRIPT, "_open", lambda _: (_ for _ in ()).throw(URLError("secret URL")))
    monkeypatch.setattr(sys, "argv", ["provision_go_tokenizer.py", "--directory", str(tmp_path)])

    assert SCRIPT.main() == 1
    output = capsys.readouterr().out
    assert json.loads(output) == {"status": "failed", "reason": "TOKENIZER_NETWORK_ERROR"}
    assert "secret URL" not in output
    assert "huggingface" not in output.lower()
