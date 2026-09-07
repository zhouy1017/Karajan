"""Offline tests for the bounded, redacted tokenizer download retry boundary."""

from __future__ import annotations

import hashlib
import http.server
import importlib.util
import io
import json
import ssl
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from email.message import Message
from pathlib import Path
from typing import IO, BinaryIO, cast
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


@contextmanager
def local_server(
    writer: Callable[[IO[bytes]], None], content_length: int | None, *, chunked: bool = False
) -> Iterator[str]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            if chunked:
                self.send_header("Transfer-Encoding", "chunked")
            else:
                assert content_length is not None
                self.send_header("Content-Length", str(content_length))
            self.end_headers()
            writer(cast(IO[bytes], self.wfile))

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/fixture"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


@contextmanager
def raw_server(writer: Callable[[IO[bytes]], None]) -> Iterator[str]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            writer(cast(IO[bytes], self.wfile))

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/fixture"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


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
        raise URLError(ConnectionResetError("private transport detail"))

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
    clock = iter([0.0, 0.0, 1.0, 181.0])
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


def test_default_connect_receives_remaining_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, small_artifact: bytes
) -> None:
    clock = [0.0]
    monkeypatch.setattr(SCRIPT.time, "monotonic", lambda: clock[0])
    timeouts: list[float] = []

    class Opener:
        def open(self, request: object, timeout: float) -> BinaryIO:
            del request
            timeouts.append(timeout)
            if len(timeouts) == 1:
                clock[0] = 179.0
                raise TimeoutError("private transport detail")
            assert timeout <= 1.0
            return io.BytesIO(small_artifact)

    monkeypatch.setattr(SCRIPT, "build_opener", lambda *args: Opener())

    SCRIPT.provision(tmp_path)

    assert timeouts == [30.0, 1.0]
    assert (tmp_path / "fixture.bin").read_bytes() == small_artifact


def test_loopback_slow_byte_stream_stops_at_small_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, small_artifact: bytes
) -> None:
    stop = threading.Event()

    def writer(stream: IO[bytes]) -> None:
        for index, byte in enumerate(small_artifact):
            try:
                stream.write(bytes((byte,)))
                stream.flush()
            except (BrokenPipeError, ConnectionResetError):
                return
            if index + 1 < len(small_artifact) and stop.wait(0.07):
                return

    monkeypatch.setattr(SCRIPT, "DOWNLOAD_BUDGET_SECONDS", 0.30)
    open_default = SCRIPT._open
    with local_server(writer, len(small_artifact)) as url:
        monkeypatch.setattr(
            SCRIPT,
            "_open",
            lambda _, *, timeout=30.0: open_default(url, timeout=timeout),
        )
        started = time.perf_counter()
        with pytest.raises(SCRIPT.ProvisionError, match="^TOKENIZER_DOWNLOAD_TIMEOUT$"):
            SCRIPT.provision(tmp_path)
        elapsed = time.perf_counter() - started
        stop.set()

    assert elapsed < 0.90
    assert not (tmp_path / "fixture.bin").exists()


def test_loopback_delayed_byte_cannot_extend_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload = b"x" * 65_537
    monkeypatch.setattr(
        SCRIPT,
        "ARTIFACTS",
        {"fixture.bin": (len(payload), hashlib.sha256(payload).hexdigest())},
    )
    sent = threading.Event()
    release = threading.Event()

    def writer(stream: IO[bytes]) -> None:
        try:
            time.sleep(0.50)
            stream.write(payload[:-1])
            stream.flush()
            sent.set()
            release.wait(2)
            stream.write(payload[-1:])
            stream.flush()
        except (BrokenPipeError, ConnectionResetError):
            return

    monkeypatch.setattr(SCRIPT, "DOWNLOAD_BUDGET_SECONDS", 0.80)
    open_default = SCRIPT._open
    with local_server(writer, len(payload)) as url:
        monkeypatch.setattr(
            SCRIPT,
            "_open",
            lambda _, *, timeout=30.0: open_default(url, timeout=timeout),
        )
        failure: list[BaseException] = []

        def run() -> None:
            try:
                SCRIPT.provision(tmp_path)
            except BaseException as error:
                failure.append(error)

        worker = threading.Thread(target=run)
        started = time.perf_counter()
        worker.start()
        assert sent.wait(2)
        assert release.wait(0.60) is False
        release.set()
        worker.join(timeout=2)
        elapsed = time.perf_counter() - started

    assert not worker.is_alive()
    assert len(failure) == 1
    assert isinstance(failure[0], SCRIPT.ProvisionError)
    assert str(failure[0]) == "TOKENIZER_DOWNLOAD_TIMEOUT"
    assert elapsed < 1.40
    assert not (tmp_path / "fixture.bin").exists()


def test_loopback_chunk_size_framing_cannot_extend_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, small_artifact: bytes
) -> None:
    stop = threading.Event()

    def writer(stream: IO[bytes]) -> None:
        try:
            for byte in b"00000000000010\r\n":
                if stop.wait(0.07):
                    return
                stream.write(bytes((byte,)))
                stream.flush()
            stream.write(small_artifact + b"\r\n0\r\n\r\n")
            stream.flush()
        except (BrokenPipeError, ConnectionResetError):
            return

    monkeypatch.setattr(SCRIPT, "DOWNLOAD_BUDGET_SECONDS", 0.30)
    open_default = SCRIPT._open
    with local_server(writer, None, chunked=True) as url:
        monkeypatch.setattr(
            SCRIPT,
            "_open",
            lambda _, *, timeout=30.0: open_default(url, timeout=timeout),
        )
        started = time.perf_counter()
        with pytest.raises(SCRIPT.ProvisionError, match="^TOKENIZER_DOWNLOAD_TIMEOUT$"):
            SCRIPT.provision(tmp_path)
        elapsed = time.perf_counter() - started
        stop.set()

    assert elapsed < 0.90
    assert not (tmp_path / "fixture.bin").exists()


def test_loopback_slow_response_headers_share_the_download_deadline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, small_artifact: bytes
) -> None:
    stop = threading.Event()

    def writer(stream: IO[bytes]) -> None:
        try:
            stream.write(
                b"HTTP/1.0 200 OK\r\nContent-Length: "
                + str(len(small_artifact)).encode()
                + b"\r\nX-Slow: "
            )
            stream.flush()
            for byte in b"abcdefghijklmnop":
                if stop.wait(0.07):
                    return
                stream.write(bytes((byte,)))
                stream.flush()
            stream.write(b"\r\n\r\n" + small_artifact)
            stream.flush()
        except (BrokenPipeError, ConnectionResetError):
            return

    monkeypatch.setattr(SCRIPT, "DOWNLOAD_BUDGET_SECONDS", 0.30)
    open_default = SCRIPT._open
    with raw_server(writer) as url:
        monkeypatch.setattr(
            SCRIPT,
            "_open",
            lambda _, *, timeout=30.0: open_default(url, timeout=timeout),
        )
        started = time.perf_counter()
        with pytest.raises(SCRIPT.ProvisionError, match="^TOKENIZER_DOWNLOAD_TIMEOUT$"):
            SCRIPT.provision(tmp_path)
        elapsed = time.perf_counter() - started
        stop.set()

    assert elapsed < 0.90
    assert not (tmp_path / "fixture.bin").exists()


def test_complete_content_length_does_not_wait_for_delayed_connection_close(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, small_artifact: bytes
) -> None:
    stop = threading.Event()

    def writer(stream: IO[bytes]) -> None:
        try:
            stream.write(b"HTTP/1.0 200 OK\r\nContent-Length: " + str(len(small_artifact)).encode())
            stream.write(b"\r\n\r\n" + small_artifact)
            stream.flush()
            stop.wait(0.80)
        except (BrokenPipeError, ConnectionResetError):
            return

    monkeypatch.setattr(SCRIPT, "DOWNLOAD_BUDGET_SECONDS", 0.30)
    open_default = SCRIPT._open
    with raw_server(writer) as url:
        monkeypatch.setattr(
            SCRIPT,
            "_open",
            lambda _, *, timeout=30.0: open_default(url, timeout=timeout),
        )
        started = time.perf_counter()
        result = SCRIPT.provision(tmp_path)
        elapsed = time.perf_counter() - started
        stop.set()

    assert result["artifacts"][0]["status"] == "downloaded"
    assert elapsed < 0.60
    assert (tmp_path / "fixture.bin").read_bytes() == small_artifact


def test_chunked_response_preserves_standard_read_semantics_across_tcp_splits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload = b"abcd"

    def writer(stream: IO[bytes]) -> None:
        try:
            stream.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n4\r\nab")
            stream.flush()
            time.sleep(0.10)
            stream.write(b"cd\r\n0\r\n\r\n")
            stream.flush()
        except (BrokenPipeError, ConnectionResetError):
            return

    monkeypatch.setattr(
        SCRIPT,
        "ARTIFACTS",
        {"fixture.bin": (len(payload), hashlib.sha256(payload).hexdigest())},
    )
    monkeypatch.setattr(SCRIPT, "DOWNLOAD_BUDGET_SECONDS", 1.0)
    open_default = SCRIPT._open
    with raw_server(writer) as url:
        monkeypatch.setattr(
            SCRIPT,
            "_open",
            lambda _, *, timeout=30.0: open_default(url, timeout=timeout),
        )
        result = SCRIPT.provision(tmp_path)

    assert result["artifacts"][0]["status"] == "downloaded"
    assert (tmp_path / "fixture.bin").read_bytes() == payload


def test_final_read_crossing_deadline_cannot_publish(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, small_artifact: bytes
) -> None:
    clock = [0.0]
    monkeypatch.setattr(SCRIPT.time, "monotonic", lambda: clock[0])

    class LateEOF(io.BytesIO):
        def read(self, amount: int | None = -1) -> bytes:
            if self.tell():
                clock[0] = 181.0
                return b""
            clock[0] = 179.0
            return super().read(amount)

    with pytest.raises(SCRIPT.ProvisionError, match="^TOKENIZER_DOWNLOAD_TIMEOUT$"):
        SCRIPT.provision(tmp_path, open_url=lambda _: response(LateEOF(small_artifact)))

    assert not (tmp_path / "fixture.bin").exists()


def test_certificate_failure_is_deterministic_and_not_retried(
    tmp_path: Path, small_artifact: bytes
) -> None:
    calls = 0

    def connect(url: str) -> object:
        nonlocal calls
        calls += 1
        raise URLError(ssl.SSLCertVerificationError(1, "CERTIFICATE_VERIFY_FAILED"))

    with pytest.raises(SCRIPT.ProvisionError, match="^TOKENIZER_NETWORK_ERROR$"):
        SCRIPT.provision(tmp_path, open_url=connect)

    assert calls == 1


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
    def fail_open(_: str, *, timeout: float = 30.0) -> object:
        raise URLError("secret URL")

    monkeypatch.setattr(SCRIPT, "_open", fail_open)
    monkeypatch.setattr(sys, "argv", ["provision_go_tokenizer.py", "--directory", str(tmp_path)])

    assert SCRIPT.main() == 1
    output = capsys.readouterr().out
    assert json.loads(output) == {"status": "failed", "reason": "TOKENIZER_NETWORK_ERROR"}
    assert "secret URL" not in output
    assert "huggingface" not in output.lower()
