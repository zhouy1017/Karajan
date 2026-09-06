"""Local sessions and HTTP boundary for the workbench."""

import hashlib
import secrets
import sqlite3
import time
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from karajan.capacity import CapacityStore
from karajan.projects import ProjectRegistry
from karajan.runs import RunPlanner

from .body_limit import BodyLimitMiddleware
from .projects import register_project_routes
from .resources import register_resource_routes
from .runs import register_run_routes
from .simulation import register_simulation_routes


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _error(code: str, status: int) -> JSONResponse:
    return JSONResponse(
        {"reason_code": code}, status_code=status, headers={"Cache-Control": "no-store"}
    )


class LoginRateLimited(Exception):
    pass


class SessionStore:
    def __init__(self, path: Path, bootstrap_token: str) -> None:
        self.path = path
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS bootstrap (
                    hash TEXT PRIMARY KEY, expires REAL NOT NULL, used INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions (
                    hash TEXT PRIMARY KEY, csrf TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS failed_bootstraps (at REAL NOT NULL);
            """)
            db.execute(
                "INSERT OR IGNORE INTO bootstrap VALUES (?, ?, 0)",
                (_digest(bootstrap_token), time.time() + 600),
            )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    def exchange(self, token: str) -> tuple[str, str] | None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM failed_bootstraps WHERE at<?", (time.time() - 60,))
            if db.execute("SELECT COUNT(*) FROM failed_bootstraps").fetchone()[0] >= 5:
                db.commit()
                raise LoginRateLimited
            updated = db.execute(
                "UPDATE bootstrap SET used=1 WHERE hash=? AND used=0 AND expires>?",
                (_digest(token), time.time()),
            ).rowcount
            if updated != 1:
                db.execute("INSERT INTO failed_bootstraps VALUES (?)", (time.time(),))
                db.commit()
                return None
            session, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            db.execute(
                "INSERT INTO sessions VALUES (?, ?, ?)",
                (_digest(session), csrf, time.time() + 43_200),
            )
            db.commit()
            return session, csrf

    def lookup(self, token: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT csrf, expires FROM sessions WHERE hash=? AND expires>?",
                (_digest(token), time.time()),
            ).fetchone()
            return dict(row) if row is not None else None

    def revoke(self, token: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE hash=?", (_digest(token),))


class BootstrapInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    token: Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[a-zA-Z0-9_-]+$")]


def create_app(
    state_directory: Path,
    *,
    origin: str,
    bootstrap_token: str,
    allowed_roots: Sequence[Path] = (),
    frontend_directory: Path | None = None,
) -> FastAPI:
    BootstrapInput(token=bootstrap_token)
    parsed_origin = urlsplit(origin)
    if (
        parsed_origin.scheme != "http"
        or parsed_origin.hostname not in {"localhost", "127.0.0.1"}
        or parsed_origin.username
        or parsed_origin.password
        or parsed_origin.path
        or parsed_origin.query
        or parsed_origin.fragment
        or parsed_origin.port == 0
    ):
        raise ValueError("A plain loopback origin is required")
    state_directory.mkdir(parents=True, exist_ok=True)
    sessions = SessionStore(state_directory / "sessions.sqlite", bootstrap_token)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(BodyLimitMiddleware)
    projects = ProjectRegistry(state_directory / "projects.sqlite", allowed_roots)
    register_project_routes(app, projects)
    register_simulation_routes(app, projects)
    register_run_routes(app, RunPlanner(state_directory / "runs.sqlite", projects))
    register_resource_routes(app, CapacityStore(state_directory / "capacity.sqlite"))

    @app.middleware("http")
    async def session_boundary(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.headers.getlist("host") != [parsed_origin.netloc]:
            return _error("HOST_REJECTED", 403)
        unsafe = request.method not in {"GET", "HEAD", "OPTIONS"}
        if unsafe and request.headers.getlist("origin") != [origin]:
            return _error("ORIGIN_REJECTED", 403)
        if request.url.path.startswith("/v1/") and request.url.path != "/v1/session/bootstrap":
            current = sessions.lookup(request.cookies.get("karajan_session", ""))
            if current is None:
                return _error("AUTHENTICATION_REQUIRED", 401)
            request.state.session = current
            if unsafe and not secrets.compare_digest(
                request.headers.get("x-csrf-token", "").encode(), current["csrf"].encode()
            ):
                return _error("CSRF_REJECTED", 403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request: Request, error: RequestValidationError) -> JSONResponse:
        return _error("INPUT_INVALID", 422)

    @app.post("/v1/session/bootstrap")
    def bootstrap(data: BootstrapInput) -> Response:
        try:
            exchanged = sessions.exchange(data.token)
        except LoginRateLimited:
            return _error("BOOTSTRAP_RATE_LIMITED", 429)
        if exchanged is None:
            return _error("BOOTSTRAP_INVALID", 401)
        session, csrf = exchanged
        response = JSONResponse({"csrf_token": csrf})
        response.set_cookie(
            "karajan_session", session, httponly=True, samesite="strict", max_age=43_200
        )
        return response

    @app.get("/v1/session")
    def read_session(request: Request) -> dict[str, Any]:
        return {"csrf_token": request.state.session["csrf"], "authenticated": True}

    @app.post("/v1/session/logout", status_code=204)
    def logout(request: Request) -> Response:
        sessions.revoke(request.cookies.get("karajan_session", ""))
        response = Response(status_code=204)
        response.delete_cookie("karajan_session", httponly=True, samesite="strict")
        return response

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    if frontend_directory is not None:
        app.mount("/assets", StaticFiles(directory=frontend_directory / "assets"), name="assets")

        @app.get("/")
        def workbench() -> FileResponse:
            return FileResponse(frontend_directory / "index.html")

    return app
