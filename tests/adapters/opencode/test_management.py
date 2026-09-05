import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from karajan.adapters.opencode import ManagementClient, ManagementResponseError


@pytest.mark.parametrize("operation", ["request", "open_events"])
def test_management_redirect_is_rejected_before_forwarding_credentials(operation: str) -> None:
    received: list[str] = []

    class Peer(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            received.append(self.path)
            if self.path == "/redirect-target":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")
                return
            self.send_response(302)
            self.send_header("Location", "/redirect-target")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Peer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = ManagementClient(f"http://127.0.0.1:{server.server_port}", "synthetic-admin")
    try:
        with pytest.raises(ManagementResponseError) as rejected:
            if operation == "request":
                client.request("GET", "/global/health")
            else:
                client.open_events()
        assert rejected.value.status == 302
        assert "/redirect-target" not in received
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
