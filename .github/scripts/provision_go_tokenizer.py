"""Provision fixed public tokenizer data before offline tests; no credentials or weights.

The runtime module never invokes this script. Only this preparation step downloads,
and no CLI option can change an upstream URL, revision, expected size or digest.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import ipaddress
import json
import os
import select
import socket
import ssl
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from http.client import HTTPConnection, HTTPMessage, HTTPResponse, HTTPSConnection
from pathlib import Path
from typing import IO, Any, BinaryIO, NoReturn, cast
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import (
    HTTPError,
    HTTPHandler,
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)

REVISION = "690b705278a3a58e538fcb37c2ca8b5f9511213c"
ARTIFACTS = {
    "tokenizer.json": (
        20_217_442,
        "19e773648cb4e65de8660ea6365e10acca112d42a854923df93db4a6f333a82d",
    ),
    "tokenizer_config.json": (
        761,
        "98b1271574f41abf89427ae2dda030d94dc9478f0edc5a8bd240db213c6fd5fc",
    ),
    "chat_template.jinja": (
        10_950,
        "0c4099f3382d6c92700dfb99725025360966fd73032f0ecf32377c0d9e6309c5",
    ),
}
OpenURL = Callable[[str], AbstractContextManager[BinaryIO]]
MAX_DOWNLOAD_ATTEMPTS = 3
DOWNLOAD_BUDGET_SECONDS = 180
_RESOLVE_PROGRAM = """\
import json
import socket
import sys

try:
    addresses = socket.getaddrinfo(sys.argv[1], int(sys.argv[2]), 0, socket.SOCK_STREAM)
except socket.gaierror as error:
    print(json.dumps({"status": "gaierror", "code": error.errno}))
else:
    print(json.dumps({"status": "ok", "addresses": addresses}))
"""


class ProvisionError(ValueError):
    pass


class _RetryableDownloadError(Exception):
    """A network failure whose details must not cross the provisioning boundary."""

    def __init__(self, code: str) -> None:
        super().__init__()
        self.code = code


class _HTTPSRedirects(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> Request | None:
        destination = urlsplit(newurl)
        if destination.scheme != "https" or destination.username or destination.password:
            raise ProvisionError("TOKENIZER_INSECURE_REDIRECT")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and hasattr(req, "_karajan_deadline"):
            cast(Any, redirected)._karajan_deadline = cast(Any, req)._karajan_deadline
        return redirected


def _open(url: str, *, timeout: float = 30.0) -> AbstractContextManager[BinaryIO]:
    # Public HF files may redirect to its signed CDN. No proxy/auth/cookie handlers
    # or environment token lookup are used; the downloaded bytes still must match.
    opener = build_opener(
        ProxyHandler({}), _HTTPSRedirects(), _DeadlineHTTPHandler(), _DeadlineHTTPSHandler()
    )
    request = Request(
        url, headers={"User-Agent": "Karajan-tokenizer-provision/1", "Accept-Encoding": "identity"}
    )
    cast(Any, request)._karajan_deadline = time.monotonic() + timeout
    return cast(AbstractContextManager[BinaryIO], opener.open(request, timeout=timeout))


def _verified(path: Path, size: int, expected: str) -> bool:
    if not path.is_file():
        return False
    with path.open("rb") as stream:
        raw = stream.read(size + 1)
    return len(raw) == size and hashlib.sha256(raw).hexdigest() == expected


def _http_error_code(error: HTTPError) -> NoReturn:
    status = error.code
    if status == 429 or 500 <= status <= 599:
        raise _RetryableDownloadError(f"TOKENIZER_HTTP_STATUS_{status}") from None
    raise ProvisionError(f"TOKENIZER_HTTP_STATUS_{status}") from None


def _url_error_code(error: URLError) -> NoReturn:
    if isinstance(error.reason, (ConnectionError, TimeoutError)):
        raise _RetryableDownloadError("TOKENIZER_NETWORK_ERROR") from None
    raise ProvisionError("TOKENIZER_NETWORK_ERROR") from None


class _DeadlineSocketIO(io.RawIOBase):
    """Raw response stream whose every blocking read shares one absolute deadline."""

    def __init__(self, sock: socket.socket, stream: socket.SocketIO, deadline: float) -> None:
        super().__init__()
        self._sock = sock
        self._stream = stream
        self._deadline = deadline

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        try:
            self._sock.settimeout(remaining)
            count = self._stream.readinto(buffer)
        except TimeoutError as error:
            raise TimeoutError from error
        except OSError:
            raise
        if time.monotonic() >= self._deadline:
            raise TimeoutError
        return cast(int, count)

    def close(self) -> None:
        try:
            self._stream.close()
        finally:
            super().close()


class _DeadlineSocket:
    """Small socket facade preserving http.client's standard HTTPResponse parser."""

    def __init__(self, sock: socket.socket, deadline: float) -> None:
        self._sock = sock
        self._deadline = deadline

    def makefile(self, mode: str = "r", buffering: int | None = None) -> IO[bytes]:
        del buffering
        if "r" not in mode or "b" not in mode:
            raise ValueError("TOKENIZER_TRANSPORT_UNAVAILABLE")
        stream = cast(socket.SocketIO, cast(Any, self._sock).makefile(mode, 0))
        raw = _DeadlineSocketIO(self._sock, stream, self._deadline)
        return cast(IO[bytes], io.BufferedReader(raw))

    def sendall(self, data: bytes, flags: int = 0) -> None:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        self._sock.settimeout(remaining)
        self._sock.sendall(data, flags)
        if time.monotonic() >= self._deadline:
            raise TimeoutError

    def close(self) -> None:
        self._sock.close()

    def __getattr__(self, name: str) -> object:
        return getattr(self._sock, name)


def _resolve_addresses(host: str, port: int, deadline: float) -> list[tuple[Any, ...]]:
    """Resolve one host in a killable child, bounded by the artifact deadline."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        if address.version == 4:
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (host, port))]
        return [(socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (host, port, 0, 0))]

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    process = subprocess.Popen(
        [sys.executable, "-c", _RESOLVE_PROGRAM, host, str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        try:
            output, _ = process.communicate(timeout=remaining)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1.0)
            raise TimeoutError from None
        if time.monotonic() >= deadline:
            raise TimeoutError
        if process.returncode != 0:
            raise OSError
        decoded = json.loads(output)
    except TimeoutError:
        raise
    except (OSError, ValueError, TypeError):
        raise OSError("resolver failed") from None
    if not isinstance(decoded, dict):
        raise OSError("resolver failed")
    if decoded.get("status") == "gaierror":
        if decoded.get("code") == socket.EAI_AGAIN:
            raise ConnectionError
        raise OSError("resolver failed")
    if decoded.get("status") != "ok":
        raise OSError("resolver failed")
    decoded = decoded.get("addresses")
    if not isinstance(decoded, list) or not decoded:
        raise OSError("resolver failed")
    addresses: list[tuple[Any, ...]] = []
    for item in decoded:
        if not isinstance(item, list) or len(item) != 5:
            raise OSError("resolver failed")
        family, socktype, proto, canonname, sockaddr = item
        if (
            not isinstance(family, int)
            or not isinstance(socktype, int)
            or not isinstance(proto, int)
        ):
            raise OSError("resolver failed")
        if not isinstance(canonname, str) or not isinstance(sockaddr, list):
            raise OSError("resolver failed")
        addresses.append((family, socktype, proto, canonname, tuple(sockaddr)))
    return addresses


class _DeadlineConnectionMixin:
    sock: Any
    _deadline: float | None

    def __init__(self, *args: object, **kwargs: object) -> None:
        deadline = cast(float | None, kwargs.pop("deadline", None))
        timeout = cast(float | None, kwargs.get("timeout"))
        self._deadline = deadline
        if self._deadline is None and timeout is not None:
            self._deadline = time.monotonic() + timeout
        super().__init__(*args, **kwargs)
        self._create_connection = self._deadline_create_connection

    def _remaining(self) -> float:
        if self._deadline is None:
            return 30.0
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        return remaining

    def _deadline_create_connection(
        self,
        address: tuple[str, int],
        timeout: float | None = None,
        source_address: tuple[str, int] | None = None,
    ) -> socket.socket:
        if self._deadline is None:
            return socket.create_connection(address, timeout, source_address)
        host, port = address
        self._remaining()
        try:
            deadline = self._deadline
            assert deadline is not None
            addresses = _resolve_addresses(host, port, deadline)
        except OSError:
            self._remaining()
            raise
        last_error: OSError | None = None
        for family, socktype, proto, _, sockaddr in addresses:
            sock: socket.socket | None = None
            try:
                sock = socket.socket(family, socktype, proto)
                sock.settimeout(self._remaining())
                if source_address:
                    sock.bind(source_address)
                sock.connect(sockaddr)
                return sock
            except OSError as error:
                last_error = error
                if sock is not None:
                    sock.close()
                self._remaining()
        if last_error is not None:
            raise last_error
        raise OSError("no address available")

    def _connect_tcp(self) -> None:
        HTTPConnection.connect(cast(HTTPConnection, self))

    def _wrap_connected_socket(self) -> None:
        if self.sock is not None and self._deadline is not None:
            self.sock = _DeadlineSocket(self.sock, self._deadline)


class _DeadlineHTTPConnection(_DeadlineConnectionMixin, HTTPConnection):
    def connect(self) -> None:
        self._connect_tcp()
        self._wrap_connected_socket()


class _DeadlineHTTPSConnection(_DeadlineConnectionMixin, HTTPSConnection):
    _context: ssl.SSLContext
    _tunnel_host: str | None

    def connect(self) -> None:
        self._connect_tcp()
        if self._tunnel_host:
            server_hostname = self._tunnel_host
        else:
            server_hostname = self.host
        self.sock = self._context.wrap_socket(
            self.sock,
            server_hostname=server_hostname,
            do_handshake_on_connect=False,
        )
        self.sock.setblocking(False)
        while True:
            remaining = self._remaining()
            ready = False
            try:
                self.sock.do_handshake()
                break
            except ssl.SSLWantReadError:
                readable, _, _ = select.select([self.sock], [], [], remaining)
                ready = bool(readable)
            except ssl.SSLWantWriteError:
                _, writable, _ = select.select([], [self.sock], [], remaining)
                ready = bool(writable)
            else:
                break
            if not ready:
                raise TimeoutError
        self.sock.settimeout(self._remaining())
        self._wrap_connected_socket()


class _DeadlineHTTPHandler(HTTPHandler):
    def do_open(self, http_class: Any, req: Request, **http_conn_args: Any) -> HTTPResponse:
        deadline = getattr(req, "_karajan_deadline", None)
        if deadline is not None:
            http_conn_args["deadline"] = deadline
        return super().do_open(http_class, req, **http_conn_args)

    def http_open(self, req: Request) -> HTTPResponse:
        return self.do_open(_DeadlineHTTPConnection, req)


class _DeadlineHTTPSHandler(HTTPSHandler):
    def do_open(self, http_class: Any, req: Request, **http_conn_args: Any) -> HTTPResponse:
        deadline = getattr(req, "_karajan_deadline", None)
        if deadline is not None:
            http_conn_args["deadline"] = deadline
        return super().do_open(http_class, req, **http_conn_args)

    def https_open(self, req: Request) -> HTTPResponse:
        context = cast(Any, self)._context
        return self.do_open(
            _DeadlineHTTPSConnection,
            req,
            context=context,
        )


def provision(directory: Path, *, open_url: OpenURL | None = None) -> dict[str, object]:
    """Verify/reuse or atomically publish each artifact; injection is only for offline tests."""
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    receipts = []
    for name, (size, expected) in ARTIFACTS.items():
        target = directory / name
        status = "verified"
        if not _verified(target, size, expected):
            deadline = time.monotonic() + DOWNLOAD_BUDGET_SECONDS
            for attempt in range(MAX_DOWNLOAD_ATTEMPTS):
                if time.monotonic() >= deadline:
                    raise ProvisionError("TOKENIZER_DOWNLOAD_TIMEOUT")
                temporary: Path | None = None
                try:
                    with tempfile.NamedTemporaryFile(
                        mode="wb", dir=directory, prefix=f".{name}.", suffix=".tmp", delete=False
                    ) as stream:
                        temporary = Path(stream.name)
                        digest = hashlib.sha256()
                        count = 0
                        url = f"https://huggingface.co/zai-org/GLM-5.3-Flash/resolve/{REVISION}/{name}"
                        try:
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                raise ProvisionError("TOKENIZER_DOWNLOAD_TIMEOUT")
                            if open_url is None:
                                response_context = _open(url, timeout=min(30.0, remaining))
                            else:
                                response_context = open_url(url)
                            with response_context as response:
                                while True:
                                    if deadline - time.monotonic() <= 0:
                                        raise ProvisionError("TOKENIZER_DOWNLOAD_TIMEOUT")
                                    try:
                                        amount = min(65_536, size + 1 - count)
                                        chunk = response.read(amount)
                                    except (ConnectionError, TimeoutError) as error:
                                        raise _RetryableDownloadError(
                                            "TOKENIZER_NETWORK_ERROR"
                                        ) from error
                                    if time.monotonic() >= deadline:
                                        raise ProvisionError("TOKENIZER_DOWNLOAD_TIMEOUT")
                                    if not chunk:
                                        break
                                    count += len(chunk)
                                    if count > size:
                                        raise ProvisionError("TOKENIZER_LENGTH_MISMATCH")
                                    digest.update(chunk)
                                    try:
                                        stream.write(chunk)
                                    except OSError:
                                        raise ProvisionError("TOKENIZER_FILESYSTEM_ERROR") from None
                        except HTTPError as error:
                            _http_error_code(error)
                        except URLError as error:
                            _url_error_code(error)
                        except (ConnectionError, TimeoutError) as error:
                            raise _RetryableDownloadError("TOKENIZER_NETWORK_ERROR") from error
                        if count != size:
                            raise ProvisionError("TOKENIZER_LENGTH_MISMATCH")
                        if digest.hexdigest() != expected:
                            raise ProvisionError("TOKENIZER_DIGEST_MISMATCH")
                        if time.monotonic() >= deadline:
                            raise ProvisionError("TOKENIZER_DOWNLOAD_TIMEOUT")
                        stream.flush()
                        os.fsync(stream.fileno())
                    if time.monotonic() >= deadline:
                        raise ProvisionError("TOKENIZER_DOWNLOAD_TIMEOUT")
                    os.replace(temporary, target)
                    temporary = None
                    status = "downloaded"
                    break
                except _RetryableDownloadError as error:
                    if time.monotonic() >= deadline:
                        raise ProvisionError("TOKENIZER_DOWNLOAD_TIMEOUT") from None
                    if attempt + 1 == MAX_DOWNLOAD_ATTEMPTS:
                        raise ProvisionError(error.code) from None
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
        receipts.append({"name": name, "status": status, "bytes": size, "sha256": expected})
    return {"schema_version": "karajan.go-tokenizer-provision.v1", "artifacts": receipts}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = provision(args.directory)
    except ProvisionError as error:
        print(json.dumps({"status": "failed", "reason": str(error)}))
        return 1
    except Exception:
        # Do not emit a signed redirect URL, local path or response body on errors.
        print(json.dumps({"status": "failed", "reason": "TOKENIZER_PROVISION_FAILED"}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
