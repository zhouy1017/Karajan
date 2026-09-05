"""Only local generated targets; no real broker, delivery service or provider."""

import http.client
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Self

ENDPOINTS = ("/control", "/broker-admin", "/provider", "/delivery")


class NetworkCanary(ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), Handler)
        self.receipts: list[str] = []
        self.thread = threading.Thread(
            target=lambda: self.serve_forever(poll_interval=0.01), daemon=True
        )

    def __enter__(self) -> Self:
        self.thread.start()
        return self

    def positive_control(self) -> int:
        for endpoint in ENDPOINTS:
            connection = http.client.HTTPConnection("127.0.0.1", self.server_port, timeout=2)
            try:
                connection.request("GET", endpoint)
                response = connection.getresponse()
                if response.status != 200:
                    raise ValueError("Local positive control failed")
                response.read()
            finally:
                connection.close()
        return len(self.receipts)

    def __exit__(self, *arguments: object) -> None:
        self.shutdown()
        self.server_close()
        self.thread.join(timeout=2)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        assert isinstance(self.server, NetworkCanary)
        self.server.receipts.append(self.path)
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        pass
