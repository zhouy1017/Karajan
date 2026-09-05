"""Pinned official server process and its trusted management connection."""

import json
import os
import socket
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, BinaryIO

from .management import ManagementClient


class OfficialServer:
    def __init__(
        self,
        runtime: Path,
        directory: Path,
        provider_url: str,
        capability: str,
        binding_headers: dict[str, str],
    ) -> None:
        self.runtime = runtime.resolve(strict=True)
        self.directory = directory
        self.workspace = directory / "workspace"
        self.workspace.mkdir(parents=True)
        self.events: list[dict[str, Any]] = []
        self.password = uuid.uuid4().hex
        self.config: dict[str, Any] = {
            "model": "fixture/fixture-model",
            "small_model": "fixture/fixture-model",
            "default_agent": "probe",
            "enabled_providers": ["fixture"],
            "autoupdate": False,
            "share": "disabled",
            "snapshot": False,
            "compaction": {"auto": False, "prune": False},
            "plugin": [],
            "mcp": {},
            "lsp": False,
            "formatter": False,
            "permission": {"*": "deny", "read": "allow"},
            "agent": {
                "probe": {
                    "mode": "primary",
                    "options": {},
                    "steps": 3,
                    "permission": {"*": "deny", "read": "allow"},
                },
                **{
                    name: {"disable": True, "options": {}, "permission": {}}
                    for name in [
                        "title",
                        "summary",
                        "compaction",
                        "explore",
                        "general",
                        "build",
                        "plan",
                    ]
                },
            },
            "provider": {
                "fixture": {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": "Karajan local fixture",
                    "options": {
                        "baseURL": provider_url,
                        "apiKey": capability,
                        "timeout": 5000,
                        "headers": binding_headers,
                    },
                    "models": {
                        "fixture-model": {
                            "name": "Fixture",
                            "tool_call": True,
                            "limit": {"context": 8192, "output": 256},
                        }
                    },
                }
            },
        }
        self.environment = {
            key: value
            for key, value in os.environ.items()
            if key.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP"}
        }
        for kind in ("CONFIG", "DATA", "CACHE", "STATE"):
            target = directory / kind.lower()
            target.mkdir()
            self.environment[f"XDG_{kind}_HOME"] = str(target)
        isolated_home = directory / "home"
        isolated_home.mkdir()
        isolated_temp = directory / "temp"
        isolated_temp.mkdir()
        self.environment.update(
            {
                "TMPDIR": str(isolated_temp),
                "TEMP": str(isolated_temp),
                "TMP": str(isolated_temp),
                "HOME": str(isolated_home),
                "USERPROFILE": str(isolated_home),
                "OPENCODE_TEST_HOME": str(isolated_home),
                "OPENCODE_CONFIG_CONTENT": json.dumps(self.config),
                "OPENCODE_SERVER_PASSWORD": self.password,
                "OPENCODE_SERVER_USERNAME": "probe",
                "NPM_CONFIG_REGISTRY": "http://127.0.0.1:1",
                "HTTP_PROXY": "http://127.0.0.1:1",
                "HTTPS_PROXY": "http://127.0.0.1:1",
                "NO_PROXY": "127.0.0.1,localhost",
            }
        )
        for flag in (
            "MODELS_FETCH",
            "DEFAULT_PLUGINS",
            "PROJECT_CONFIG",
            "EXTERNAL_SKILLS",
            "AUTOUPDATE",
            "CLAUDE_CODE",
            "LSP_DOWNLOAD",
        ):
            self.environment[f"OPENCODE_DISABLE_{flag}"] = "true"
        self.environment["OPENCODE_EXPERIMENTAL_DISABLE_FILEWATCHER"] = "true"
        self.process: subprocess.Popen[bytes] | None = None
        self.log: BinaryIO | None = None
        self.stream: Any = None
        self.event_thread: threading.Thread | None = None
        self.management: ManagementClient | None = None
        self.version = "unknown"

    def start(self) -> str:
        version = subprocess.run(
            [str(self.runtime), "--version"],
            env=self.environment,
            cwd=self.workspace,
            capture_output=True,
            timeout=10,
            check=True,
        )
        result = version.stdout.decode().strip()
        self.version = result
        if result != "1.18.29":
            raise ValueError("RUNTIME_VERSION_MISMATCH")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        self.management = ManagementClient(self.url, self.password)
        self.log = (self.directory / "server.log").open("wb")
        self.process = subprocess.Popen(
            [str(self.runtime), "serve", "--hostname", "127.0.0.1", "--port", str(port)],
            env=self.environment,
            cwd=self.workspace,
            stdout=self.log,
            stderr=self.log,
        )
        until = time.monotonic() + 15
        while time.monotonic() < until:
            try:
                self.request("GET", "/global/health")
                break
            except OSError:
                if self.process.poll() is not None:
                    raise RuntimeError(f"SERVER_EXITED: {self.directory / 'server.log'}") from None
                time.sleep(0.05)
        else:
            raise TimeoutError("SERVER_READINESS_TIMEOUT")
        self.stream = self.management.open_events()
        self.event_thread = threading.Thread(target=self._read_events, daemon=True)
        self.event_thread.start()
        return result

    def request(self, method: str, route: str, body: object = None) -> Any:
        if self.management is None:
            raise RuntimeError("MANAGEMENT_NOT_STARTED")
        return self.management.request(method, route, body)

    def _read_events(self) -> None:
        try:
            for line in self.stream:
                if line.startswith(b"data:"):
                    self.events.append(json.loads(line[5:]))
        except (OSError, ValueError):
            pass

    def close(self) -> dict[str, Any]:
        errors: list[str] = []
        facts: dict[str, Any] = {
            "scope": "server_process_only",
            "status": "not_started",
            "forced_kill": False,
            "pid": self.process.pid if self.process is not None else None,
            "errors": errors,
        }
        if self.process is not None:
            facts["status"] = "unknown"
            try:
                if self.process.poll() is None:
                    self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    facts["forced_kill"] = True
                    self.process.kill()
                    self.process.wait(timeout=2)
                facts.update(status="exited", exit_code=self.process.returncode)
            except (OSError, subprocess.TimeoutExpired) as error:
                errors.append(type(error).__name__)
        try:
            if self.management is not None:
                self.management.close()
        except OSError as error:
            errors.append(type(error).__name__)
        if self.event_thread is not None:
            self.event_thread.join(timeout=2)
            if self.event_thread.is_alive():
                errors.append("EVENT_READER_NOT_JOINED")
        try:
            if self.log is not None:
                self.log.close()
        except OSError as error:
            errors.append(type(error).__name__)
        return facts
