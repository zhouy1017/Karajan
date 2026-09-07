"""Real pathname Unix HTTP transport; the upstream is explicitly synthetic."""

import contextlib
import sys
from pathlib import Path

import httpx
import pytest
from karajan.adapters.opencode.go_relay import GoRelay
from test_go_relay import CANARY, SECRET, answer, payload
from test_go_relay_journal import authorization

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux namespace transport")


def _socket_path_with_length(root: Path, target_length: int) -> Path:
    base = root / "path"
    base.mkdir()
    index = 0
    while True:
        path = base / ("a" * index) / "inference.sock"
        actual = len(str(path))
        if actual == target_length:
            path.parent.mkdir(parents=True, exist_ok=True)
            return path
        if actual > target_length:
            raise ValueError("PATH_LENGTH_TARGET_UNREACHABLE")
        index += 1


def test_unix_relay_path_length_bounds(tmp_path):
    relay = GoRelay(SECRET, CANARY)
    try:
        allowed = _socket_path_with_length(tmp_path, 107)
        relay.start(unix_socket=allowed)
        assert len(str(allowed)) == 107
        assert relay.close()["status"] == "closed"
        assert not allowed.exists()
    finally:
        with contextlib.suppress(FileNotFoundError):
            relay.close()


def test_unix_relay_rejects_overlong_path(tmp_path):
    relay = GoRelay(SECRET, CANARY)
    overlong = _socket_path_with_length(tmp_path, 108)
    assert len(str(overlong)) == 108
    with pytest.raises(RuntimeError, match="UNIX_RELAY_PATH_TOO_LONG"):
        relay.start(unix_socket=overlong)
    assert not overlong.exists()
    assert relay.close()["status"] == "closed"


def test_unix_relay_keeps_the_same_fixed_endpoint_and_capability_boundary(tmp_path: Path) -> None:
    requests = []

    def receive(request):
        requests.append(request)
        return answer()

    relay = GoRelay(
        SECRET,
        CANARY,
        client_factory=lambda: httpx.Client(
            transport=httpx.MockTransport(receive),
            trust_env=False,
        ),
    )
    socket_path = tmp_path / "inference.sock"
    relay.start(unix_socket=socket_path)
    try:
        with httpx.Client(
            transport=httpx.HTTPTransport(uds=str(socket_path)), trust_env=False
        ) as client:
            headers = {
                "Authorization": f"Bearer {relay.capability}",
                "x-opencode-session": "native",
            }
            assert (
                client.post(
                    "http://relay/v1/chat/completions", json=payload(), headers=headers
                ).status_code
                == 200
            )
            assert (
                client.post("http://relay/v1/chat/completions", json=payload()).status_code == 403
            )
            assert (
                client.post("http://relay/arbitrary", json=payload(), headers=headers).status_code
                == 404
            )
        assert len(requests) == 1
        assert str(requests[0].url) == "https://opencode.ai/zen/go/v1/chat/completions"
        assert requests[0].headers["Authorization"] == "Bearer " + SECRET
        with pytest.raises(RuntimeError, match="UNIX_RELAY_HAS_NO_TCP_URL"):
            _ = relay.url
    finally:
        assert relay.close()["status"] == "closed"
    assert not socket_path.exists()


def test_unix_transport_consumes_the_durable_grant_and_preserves_replacement_files(tmp_path):
    auth = authorization(tmp_path)
    relay = GoRelay(
        SECRET,
        CANARY,
        authorization=auth,
        client_factory=lambda: httpx.Client(
            transport=httpx.MockTransport(lambda _: answer()),
            trust_env=False,
        ),
    )
    socket_path = tmp_path / "inference.sock"
    relay.start(unix_socket=socket_path)
    try:
        with httpx.Client(
            transport=httpx.HTTPTransport(uds=str(socket_path)), trust_env=False
        ) as client:
            response = client.post(
                "http://relay/v1/chat/completions",
                json=payload(),
                headers={
                    "Authorization": f"Bearer {relay.capability}",
                    "x-opencode-session": "native",
                },
            )
            assert response.status_code == 200
        socket_path.unlink()
        socket_path.write_text("replacement owned by another operation")
    finally:
        assert relay.close()["status"] == "closed"
    assert socket_path.read_text() == "replacement owned by another operation"
    assert auth.journal.snapshot("grant")["calls"][0]["state"] == "response_received"


def test_unix_start_never_overwrites_an_existing_path(tmp_path):
    socket_path = tmp_path / "occupied"
    socket_path.write_text("existing file")
    relay = GoRelay(SECRET, CANARY)
    with pytest.raises(RuntimeError, match="RELAY_SOCKET_PATH_EXISTS"):
        relay.start(unix_socket=socket_path)
    assert socket_path.read_text() == "existing file"
    assert relay.close()["status"] == "closed"
