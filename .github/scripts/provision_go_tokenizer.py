"""Provision fixed public tokenizer data before offline tests; no credentials or weights.

The runtime module never invokes this script. Only this preparation step downloads,
and no CLI option can change an upstream URL, revision, expected size or digest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from http.client import HTTPMessage
from pathlib import Path
from typing import IO, BinaryIO, cast
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

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


class ProvisionError(ValueError):
    pass


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
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url: str) -> AbstractContextManager[BinaryIO]:
    # Public HF files may redirect to its signed CDN. No proxy/auth/cookie handlers
    # or environment token lookup are used; the downloaded bytes still must match.
    opener = build_opener(ProxyHandler({}), _HTTPSRedirects())
    request = Request(
        url, headers={"User-Agent": "Karajan-tokenizer-provision/1", "Accept-Encoding": "identity"}
    )
    return cast(AbstractContextManager[BinaryIO], opener.open(request, timeout=30))


def _verified(path: Path, size: int, expected: str) -> bool:
    if not path.is_file():
        return False
    with path.open("rb") as stream:
        raw = stream.read(size + 1)
    return len(raw) == size and hashlib.sha256(raw).hexdigest() == expected


def provision(directory: Path, *, open_url: OpenURL | None = None) -> dict[str, object]:
    """Verify/reuse or atomically publish each artifact; injection is only for offline tests."""
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    connect = open_url if open_url is not None else _open
    receipts = []
    for name, (size, expected) in ARTIFACTS.items():
        target = directory / name
        status = "verified"
        if not _verified(target, size, expected):
            temporary: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="wb", dir=directory, prefix=f".{name}.", suffix=".tmp", delete=False
                ) as stream:
                    temporary = Path(stream.name)
                    digest = hashlib.sha256()
                    count = 0
                    started = time.monotonic()
                    url = f"https://huggingface.co/zai-org/GLM-5.3-Flash/resolve/{REVISION}/{name}"
                    with connect(url) as response:
                        while True:
                            if time.monotonic() - started > 180:
                                raise ProvisionError("TOKENIZER_DOWNLOAD_TIMEOUT")
                            chunk = response.read(min(65_536, size + 1 - count))
                            if not chunk:
                                break
                            count += len(chunk)
                            if count > size:
                                raise ProvisionError("TOKENIZER_LENGTH_MISMATCH")
                            digest.update(chunk)
                            stream.write(chunk)
                    if count != size:
                        raise ProvisionError("TOKENIZER_LENGTH_MISMATCH")
                    if digest.hexdigest() != expected:
                        raise ProvisionError("TOKENIZER_DIGEST_MISMATCH")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
                temporary = None
                status = "downloaded"
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
