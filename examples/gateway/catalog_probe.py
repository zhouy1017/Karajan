"""Reproducible local walkthrough of the R8-P1-01 gateway catalog.

Run from the repository root:

    python examples/gateway/catalog_probe.py

It starts one loopback HTTP server that behaves like an OpenAI-compatible
catalog endpoint, registers a project, a connection and a strict binding
through the real authenticated HTTP boundary, and performs one catalog probe.
No provider, account credential or model is contacted, and no generation
request is issued: the script asserts that the upstream saw exactly one GET.
"""

import json
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from karajan.gateway.probe import LocalFileSecretResolver  # noqa: E402
from karajan.projects import ProjectRegistry  # noqa: E402
from karajan.web import create_app  # noqa: E402

ORIGIN = "http://127.0.0.1:8765"
BINDING_ID = "vendor-binding"
CONNECTION_ID = "local-gateway"


class Catalog(BaseHTTPRequestHandler):
    """Answer only the discovery surface; record everything it received."""

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        self.server.received.append(self.path)  # type: ignore[attr-defined]
        body = json.dumps(
            {"object": "list", "data": [{"id": "vendor-model-a"}, {"id": "vendor-model-b"}]}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Catalog)
    server.received = []  # type: ignore[attr-defined]
    server.daemon_threads = True
    Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix="karajan-gateway-example-") as temporary:
            root = Path(temporary)
            repository = root / "repository"
            repository.mkdir()
            import subprocess

            for arguments in (
                ["init", "--initial-branch=main", str(repository)],
                [
                    "-C",
                    str(repository),
                    "-c",
                    "user.name=Example",
                    "-c",
                    "user.email=example@example.invalid",
                    "commit",
                    "--allow-empty",
                    "-qm",
                    "example",
                ],
            ):
                subprocess.run(["git", *arguments], check=True, capture_output=True)
            state = root / "state"
            state.mkdir()
            projects = ProjectRegistry(state / "projects.sqlite", [root])
            project = projects.create(
                {
                    "name": "Gateway example",
                    "repository_path": str(repository),
                    "base_ref": "main",
                    "target_branch": "main",
                    "allowed_target_branches": ["main"],
                },
                command_key="project",
                principal="owner",
            )
            # The credential is controller-configured and never uploaded.
            key_file = root / "gateway.key"
            key_file.write_text("example-local-secret-material\n", encoding="utf-8")
            app = create_app(
                state,
                origin=ORIGIN,
                bootstrap_token="example-bootstrap",
                allowed_roots=[root],
                gateway_secret_resolver=LocalFileSecretResolver(
                    sources={(project["id"], "secret:local-gateway"): key_file}
                ),
            )
            with TestClient(app, base_url=ORIGIN) as client:
                login = client.post(
                    "/v1/session/bootstrap",
                    json={"token": "example-bootstrap"},
                    headers={"Origin": ORIGIN},
                )
                headers = {"Origin": ORIGIN, "X-CSRF-Token": login.json()["csrf_token"]}
                base = f"/v1/projects/{project['id']}"
                connection = client.post(
                    base + "/gateway-connections",
                    json={
                        "connection_id": CONNECTION_ID,
                        "base_url": f"http://127.0.0.1:{server.server_address[1]}",
                        "protocol": {"family": "openai_compatible", "protocol_version": "v1"},
                        "secret_ref": "secret:local-gateway",
                        "request_transformation": {
                            "policy_id": "no-transform",
                            "revision": 1,
                            "payload_override": False,
                            "prompt_rewrite": False,
                            "managed_tools_injected": False,
                        },
                    },
                    headers={**headers, "Idempotency-Key": "example-connection"},
                )
                print("connection:", connection.status_code, connection.json()["base_url"])
                binding = client.post(
                    base + "/gateway-bindings",
                    json={
                        "binding_id": BINDING_ID,
                        "connection_id": CONNECTION_ID,
                        "connection_revision": 1,
                        "model_alias": "vendor-model-a",
                        "declared": {
                            "provider_id": "vendor",
                            "account_id": "vendor-account",
                            "billing_path": "subscription_only",
                            "required_parameters": {"temperature": 0.2},
                        },
                        "request_transformation": {
                            "policy_id": "no-transform",
                            "revision": 1,
                            "payload_override": False,
                            "prompt_rewrite": False,
                            "managed_tools_injected": False,
                        },
                    },
                    headers={**headers, "Idempotency-Key": "example-binding"},
                )
                print(
                    "binding:",
                    binding.status_code,
                    binding.json()["declared_identity"],
                    binding.json()["verified"],
                )
                probe = client.post(
                    base + f"/gateway-connections/{CONNECTION_ID}/revisions/1/catalog-probes",
                    headers={**headers, "Idempotency-Key": "example-probe"},
                )
                catalog = probe.json()["catalog"]
                print("probe:", probe.status_code, catalog["status"], catalog["model_ids"])
                print("eligible:", catalog["execution_eligible"])
                assert catalog["status"] == "ok", catalog
                assert catalog["execution_eligible"] is False
                assert catalog["requests_sent"] == 1
            assert server.received == ["/v1/models"], server.received
            print("upstream requests:", server.received, "(one read, no inference)")
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
