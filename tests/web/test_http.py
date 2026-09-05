import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from karajan.web import create_app


def test_local_bootstrap_is_one_use_and_authenticated_reads_require_the_session(
    tmp_path: Path,
) -> None:
    app = create_app(
        tmp_path / "state", origin="http://127.0.0.1:8765", bootstrap_token="test-bootstrap"
    )
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        assert client.get("/v1/session").status_code == 401
        response = client.post(
            "/v1/session/bootstrap",
            json={"token": "test-bootstrap"},
            headers={"Origin": "http://127.0.0.1:8765"},
        )
        assert response.status_code == 200
        assert response.json()["csrf_token"]
        assert "HttpOnly" in response.headers["set-cookie"]
        assert "SameSite=strict" in response.headers["set-cookie"]
        assert client.get("/v1/session").status_code == 200
        assert (
            client.post(
                "/v1/session/bootstrap",
                json={"token": "test-bootstrap"},
                headers={"Origin": "http://127.0.0.1:8765"},
            ).status_code
            == 401
        )


@pytest.mark.parametrize(
    "headers",
    [
        {"Host": "attacker.invalid", "Origin": "http://127.0.0.1:8765"},
        {"Host": "127.0.0.1:9999", "Origin": "http://127.0.0.1:8765"},
        {"Origin": "http://attacker.invalid"},
        {},
    ],
)
def test_bootstrap_rejects_untrusted_host_and_cross_site_writes(
    tmp_path: Path, headers: dict[str, str]
) -> None:
    app = create_app(tmp_path / "state", origin="http://127.0.0.1:8765", bootstrap_token="private")
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.post("/v1/session/bootstrap", json={"token": "private"}, headers=headers)
        assert response.status_code == 403
        assert "private" not in response.text


def test_logout_requires_current_csrf_and_invalidates_the_persisted_session(tmp_path: Path) -> None:
    app = create_app(
        tmp_path / "state", origin="http://127.0.0.1:8765", bootstrap_token="bootstrap"
    )
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        login = client.post(
            "/v1/session/bootstrap",
            json={"token": "bootstrap"},
            headers={"Origin": "http://127.0.0.1:8765"},
        )
        cookie = client.cookies.get("karajan_session")
        assert (
            client.post(
                "/v1/session/logout", headers={"Origin": "http://127.0.0.1:8765"}
            ).status_code
            == 403
        )
        assert client.get("/v1/session").status_code == 200
        response = client.post(
            "/v1/session/logout",
            headers={"Origin": "http://127.0.0.1:8765", "X-CSRF-Token": login.json()["csrf_token"]},
        )
        assert response.status_code == 204
        assert (
            client.get("/v1/session", headers={"Cookie": f"karajan_session={cookie}"}).status_code
            == 401
        )


def test_validation_errors_do_not_echo_secrets_and_sensitive_responses_are_not_cached(
    tmp_path: Path,
) -> None:
    app = create_app(
        tmp_path / "state", origin="http://127.0.0.1:8765", bootstrap_token="bootstrap"
    )
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.post(
            "/v1/session/bootstrap",
            json={"token": {"private": "SECRET-CANARY"}},
            headers={"Origin": "http://127.0.0.1:8765"},
        )
        assert response.status_code == 422
        assert "SECRET-CANARY" not in response.text
        assert response.headers["cache-control"] == "no-store"
        assert client.get("/v1/events").status_code == 401
        assert client.get("/v1/artifacts/private").status_code == 401
        assert client.get("/v1/session").headers["cache-control"] == "no-store"


def test_bootstrap_brute_force_limit_persists_across_app_restarts(tmp_path: Path) -> None:
    for turn in range(2):
        app = create_app(
            tmp_path / "state", origin="http://127.0.0.1:8765", bootstrap_token="bootstrap"
        )
        with TestClient(app, base_url="http://127.0.0.1:8765") as client:
            for _ in range(5 if turn == 0 else 1):
                response = client.post(
                    "/v1/session/bootstrap",
                    json={"token": "wrong"},
                    headers={"Origin": "http://127.0.0.1:8765"},
                )
            assert response.status_code == (401 if turn == 0 else 429)


@pytest.mark.parametrize("chunked", [False, True])
def test_request_limit_is_enforced_before_parsing_even_without_content_length(
    tmp_path: Path, chunked: bool
) -> None:
    app = create_app(
        tmp_path / "state", origin="http://127.0.0.1:8765", bootstrap_token="bootstrap"
    )
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        content = iter([b"x" * 32768, b"x" * 32769]) if chunked else b"x" * 65537
        response = client.post(
            "/v1/session/bootstrap",
            content=content,
            headers={"Origin": "http://127.0.0.1:8765", "Content-Type": "application/json"},
        )
        assert response.status_code == 413


def test_project_registration_is_authenticated_persistent_and_command_idempotent(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repositories" / "sample"
    repository.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(repository)], check=True, capture_output=True
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=",
            "commit",
            "--allow-empty",
            "-m",
            "fixture",
        ],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    app = create_app(
        tmp_path / "state",
        origin="http://127.0.0.1:8765",
        bootstrap_token="bootstrap",
        allowed_roots=[repository.parent],
    )
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        assert client.get("/v1/projects").status_code == 401
        login = client.post(
            "/v1/session/bootstrap",
            json={"token": "bootstrap"},
            headers={"Origin": "http://127.0.0.1:8765"},
        )
        headers = {
            "Origin": "http://127.0.0.1:8765",
            "X-CSRF-Token": login.json()["csrf_token"],
            "Idempotency-Key": "create-project",
        }
        payload = {
            "name": "Sample",
            "repository_path": str(repository),
            "base_ref": "main",
            "target_branch": "main",
            "allowed_target_branches": ["main"],
        }
        invalid = client.post(
            "/v1/projects",
            json={**payload, "base_ref": "main\x00"},
            headers={**headers, "Idempotency-Key": "invalid-base"},
        )
        assert invalid.status_code == 422
        assert invalid.headers["cache-control"] == "no-store"
        assert client.get("/v1/projects").json()["items"] == []
        first = client.post("/v1/projects", json=payload, headers=headers)
        assert first.status_code == 201
        assert first.headers["etag"] == '"1"'
        assert client.post("/v1/projects", json=payload, headers=headers).json() == first.json()
        assert len(client.get("/v1/projects").json()["items"]) == 1
        assert (
            client.post(
                "/v1/projects", json={**payload, "name": "Changed"}, headers=headers
            ).status_code
            == 409
        )
        assert first.json()["configuration"]["dispatch_eligible"] is False
        project_id = first.json()["id"]
        configuration = json.loads(
            Path("examples/projects/offline-configuration.json").read_text(encoding="utf-8")
        )
        preview_headers = {**headers, "Idempotency-Key": "preview-project"}
        preview = client.post(
            f"/v1/projects/{project_id}/configuration/preview",
            json=configuration,
            headers=preview_headers,
        )
        assert preview.status_code == 200
        assert (
            client.post(
                f"/v1/projects/{project_id}/configuration/preview",
                json=configuration,
                headers=preview_headers,
            ).json()
            == preview.json()
        )
        apply_payload = {"preview_id": preview.json()["preview_id"]}
        apply_headers = {**headers, "Idempotency-Key": "apply-project", "If-Match": '"1"'}
        applied = client.post(
            f"/v1/projects/{project_id}/configuration/apply",
            json=apply_payload,
            headers=apply_headers,
        )
        assert applied.status_code == 200
        assert applied.headers["etag"] == '"2"'
        assert applied.json()["configuration"]["dispatch_eligible"] is False
        saved_configuration = client.get(f"/v1/projects/{project_id}/configuration")
        assert saved_configuration.status_code == 200
        assert saved_configuration.json()["configuration"] == configuration
        assert (
            client.post(
                f"/v1/projects/{project_id}/configuration/apply",
                json=apply_payload,
                headers=apply_headers,
            ).json()
            == applied.json()
        )
        update_payload = {key: value for key, value in payload.items() if key != "repository_path"}
        update = client.patch(
            f"/v1/projects/{project_id}",
            json={**update_payload, "name": "stale"},
            headers={**apply_headers, "Idempotency-Key": "stale-update"},
        )
        assert update.status_code == 409
        assert client.get(f"/v1/projects/{project_id}").json()["name"] == "Sample"


def test_compiled_workbench_is_served_but_local_files_are_not_exposed(tmp_path: Path) -> None:
    assets = tmp_path / "frontend"
    (assets / "assets").mkdir(parents=True)
    (assets / "index.html").write_text("<html><h1>Workbench fixture</h1></html>")
    (assets / "assets" / "app.js").write_text('console.log("fixture")')
    (tmp_path / "private.txt").write_text("PRIVATE-CANARY")
    app = create_app(
        tmp_path / "state",
        origin="http://127.0.0.1:8765",
        bootstrap_token="bootstrap",
        frontend_directory=assets,
    )
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "Workbench fixture" in response.text
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        assert client.get("/assets/app.js").status_code == 200
        assert client.get("/assets/%2e%2e/%2e%2e/private.txt").status_code == 404
        assert client.get("/sessions.sqlite").status_code == 404


def test_invalid_unicode_bootstrap_is_a_redacted_client_error(tmp_path: Path) -> None:
    app = create_app(
        tmp_path / "state", origin="http://127.0.0.1:8765", bootstrap_token="bootstrap"
    )
    with TestClient(app, base_url="http://127.0.0.1:8765", raise_server_exceptions=False) as client:
        response = client.post(
            "/v1/session/bootstrap",
            content=b'{"token":"\\ud800"}',
            headers={"Content-Type": "application/json", "Origin": "http://127.0.0.1:8765"},
        )
        assert response.status_code == 422
        assert response.json() == {"reason_code": "INPUT_INVALID"}
        assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "action,content",
    [
        ("apply", b'{"preview_id":"\\ud800"}'),
        ("preview", b'{"nested":{"\\ud800":"canary"}}'),
        ("preview", b'{"number":NaN}'),
    ],
)
def test_authenticated_json_rejects_non_unicode_strings_and_nonfinite_numbers(
    tmp_path: Path, action: str, content: bytes
) -> None:
    app = create_app(
        tmp_path / "state", origin="http://127.0.0.1:8765", bootstrap_token="bootstrap"
    )
    with TestClient(app, base_url="http://127.0.0.1:8765", raise_server_exceptions=False) as client:
        login = client.post(
            "/v1/session/bootstrap",
            json={"token": "bootstrap"},
            headers={"Origin": "http://127.0.0.1:8765"},
        )
        response = client.post(
            f"/v1/projects/missing/configuration/{action}",
            content=content,
            headers={
                "Content-Type": "application/json",
                "Origin": "http://127.0.0.1:8765",
                "X-CSRF-Token": login.json()["csrf_token"],
                "Idempotency-Key": "invalid-json",
                "If-Match": '"1"',
            },
        )
        assert response.status_code == 422
        assert response.json() == {"reason_code": "INPUT_INVALID"}
        assert response.headers["cache-control"] == "no-store"
