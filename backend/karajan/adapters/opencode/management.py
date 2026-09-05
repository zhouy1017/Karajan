"""A trusted fixed-loopback connection; no proxy discovery or redirect following."""

import base64
import http.client
import json
import socket
from typing import Any
from urllib.parse import urlsplit


class ManagementResponseError(OSError):
    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__(f"MANAGEMENT_HTTP_{status}")


class ManagementClient:
    def __init__(self, base_url: str, credential: str) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.port is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("FIXED_LOOPBACK_MANAGEMENT_REQUIRED")
        self.port = parsed.port
        token = base64.b64encode(f"probe:{credential}".encode()).decode()
        self.headers = {"Authorization": f"Basic {token}", "Content-Type": "application/json"}
        self._event_connection: http.client.HTTPConnection | None = None
        self._event_response: http.client.HTTPResponse | None = None
        self._event_socket: socket.socket | None = None

    def request(self, method: str, route: str, body: object = None) -> Any:
        if not route.startswith("/") or route.startswith("//") or "#" in route:
            raise ValueError("MANAGEMENT_PATH_REQUIRED")
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request(
                method, route, json.dumps(body).encode() if body is not None else None, self.headers
            )
            response = connection.getresponse()
            if not 200 <= response.status < 300:
                raise ManagementResponseError(response.status)
            payload = response.read()
            return json.loads(payload) if payload else None
        finally:
            connection.close()

    def open_events(self) -> http.client.HTTPResponse:
        if self._event_connection is not None:
            raise ValueError("EVENT_CONNECTION_ALREADY_OPEN")
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        self._event_connection = connection
        try:
            connection.request("GET", "/event", headers=self.headers)
            self._event_socket = connection.sock
            response = connection.getresponse()
            self._event_response = response
            if response.status != 200:
                raise ManagementResponseError(response.status)
            return response
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        if self._event_socket is not None:
            try:
                self._event_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if self._event_response is not None:
            self._event_response.close()
        if self._event_connection is not None:
            self._event_connection.close()
        self._event_socket = None
        self._event_response = None
        self._event_connection = None
