"""Trusted PID-namespace init; control descriptor is not inherited by OpenCode."""

import base64
import http.client
import json
import os
import re
import socket
import struct
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING or __package__:
    from ._opencode_projection import projection_files
else:
    from _opencode_projection import projection_files


def validate_request(method: str, route: str, body: object, sessions: set[str]) -> None:
    valid = method == "GET" and route in {"/config", "/path", "/global/health"} and body is None
    if method == "POST" and route == "/session":
        valid = (
            isinstance(body, dict)
            and set(body) == {"title", "agent"}
            and body["agent"] == "probe"
            and isinstance(body["title"], str)
            and 0 < len(body["title"]) <= 256
            and len(sessions) < 8
        )
    match = re.fullmatch(r"/session/(ses_[A-Za-z0-9]{1,124})/(message|prompt_async|abort)", route)
    if match and match[1] in sessions:
        operation = match[2]
        valid = (method, operation) in {("GET", "message"), ("POST", "abort")} and body is None
        if method == "POST" and operation == "prompt_async" and isinstance(body, dict):
            parts = body.get("parts")
            valid = (
                set(body) == {"agent", "model", "parts"}
                and body["agent"] == "probe"
                and body["model"] == {"providerID": "opencode-go", "modelID": "glm-5.3-flash"}
                and isinstance(parts, list)
                and len(parts) == 1
                and isinstance(parts[0], dict)
                and set(parts[0]) == {"type", "text"}
                and parts[0]["type"] == "text"
                and isinstance(parts[0]["text"], str)
                and 0 < len(parts[0]["text"]) <= 8192
            )
    if not valid:
        raise ValueError("MANAGEMENT_REQUEST_NOT_ALLOWED")


def register_session(result: object, sessions: set[str]) -> None:
    if (
        not isinstance(result, dict)
        or not isinstance(result.get("id"), str)
        or not re.fullmatch(r"ses_[A-Za-z0-9]{1,124}", result["id"])
    ):
        raise ValueError("NATIVE_SESSION_ID_INVALID")
    sessions.add(result["id"])


def lifecycle_probe() -> dict[str, Any]:
    """Fixed diagnostic, deliberately outside the native tool interface."""
    report = Path("/tmp/lifecycle.json")
    heartbeat = Path("/tmp/lifecycle-heartbeat")
    if not report.exists():
        program = (
            "import json,os,subprocess,time; from pathlib import Path; os.setsid(); "
            "child=subprocess.Popen(['/usr/bin/python3','-c','import time; time.sleep(3600)'],"
            "start_new_session=True,close_fds=True,stdin=subprocess.DEVNULL,"
            "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
            "Path('/tmp/lifecycle.json').write_text(json.dumps({'leader_pid':os.getpid(),"
            "'leader_sid':os.getsid(0),'child_pid':child.pid,'child_sid':os.getsid(child.pid)})); "
            "[(Path('/tmp/lifecycle-heartbeat').write_text(str(i)),time.sleep(0.02)) "
            "for i in range(180000)]"
        )
        subprocess.Popen(
            ["/usr/bin/python3", "-c", program],
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
        )
    deadline = time.monotonic() + 2
    while not heartbeat.exists():
        if time.monotonic() >= deadline:
            raise RuntimeError("LIFECYCLE_CANARY_NOT_READY")
        time.sleep(0.01)
    before = heartbeat.read_text()
    time.sleep(0.08)
    return {**json.loads(report.read_text()), "heartbeat_growing": heartbeat.read_text() != before}


def receive(control: socket.socket) -> dict[str, Any]:
    def read(size: int) -> bytes:
        result = bytearray()
        while len(result) < size:
            block = control.recv(size - len(result))
            if not block:
                raise EOFError
            result.extend(block)
        return bytes(result)

    length = struct.unpack("!I", read(4))[0]
    if length > 1024 * 1024:
        raise ValueError("CONTROL_FRAME_TOO_LARGE")
    value = json.loads(read(length))
    if not isinstance(value, dict):
        raise ValueError("CONTROL_REQUEST_INVALID")
    return value


def send(control: socket.socket, value: dict[str, Any]) -> None:
    payload = json.dumps(value, allow_nan=False).encode()
    if len(payload) > 1024 * 1024:
        raise ValueError("CONTROL_FRAME_TOO_LARGE")
    control.sendall(struct.pack("!I", len(payload)) + payload)


def configuration(
    capability: str, projection: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = (
        projection_files(projection)
        if projection is not None
        else [{"path": "fixture.py", "writable": True}]
    )
    permissions = {
        "*": "deny",
        "read": {"*": "deny", **{"workspace/" + r["path"]: "allow" for r in rows}},
        "edit": {"*": "deny", **{"workspace/" + r["path"]: "allow" for r in rows if r["writable"]}},
    }
    return {
        "model": "opencode-go/glm-5.3-flash",
        "small_model": "opencode-go/glm-5.3-flash",
        "default_agent": "probe",
        "enabled_providers": ["opencode-go"],
        "autoupdate": False,
        "share": "disabled",
        "snapshot": False,
        "compaction": {"auto": False, "prune": False},
        "plugin": [],
        "mcp": {},
        "lsp": False,
        "formatter": False,
        "permission": permissions,
        "agent": {
            "probe": {"mode": "primary", "options": {}, "steps": 4, "permission": permissions},
            **{
                name: {"disable": True, "options": {}, "permission": {}}
                for name in (
                    "title",
                    "summary",
                    "compaction",
                    "explore",
                    "general",
                    "build",
                    "plan",
                )
            },
        },
        "provider": {
            "opencode-go": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Fixed local inference bridge",
                "whitelist": ["glm-5.3-flash"],
                "options": {
                    "baseURL": "http://127.0.0.1:5001/v1",
                    "apiKey": capability,
                    "timeout": 90000,
                },
                "models": {
                    "glm-5.3-flash": {
                        "name": "glm-5.3-flash",
                        "tool_call": True,
                        "limit": {"context": 16384, "output": 4096},
                    }
                },
            }
        },
    }


def management(method: str, route: str, body: object, password: str) -> object:
    connection = http.client.HTTPConnection("127.0.0.1", 5002, timeout=5)
    try:
        authorization = base64.b64encode(("probe:" + password).encode()).decode()
        connection.request(
            method,
            route,
            json.dumps(body).encode() if body is not None else None,
            {"Authorization": "Basic " + authorization, "Content-Type": "application/json"},
        )
        response = connection.getresponse()
        payload = response.read(1024 * 1024 + 1)
        if len(payload) > 1024 * 1024:
            raise ValueError("MANAGEMENT_RESPONSE_TOO_LARGE")
        if not 200 <= response.status < 300:
            raise OSError("MANAGEMENT_HTTP_" + str(response.status))
        return json.loads(payload) if payload else None
    finally:
        connection.close()


class InferenceConnection(http.client.HTTPConnection):
    """The only outbound peer is the controller's mounted pathname socket."""

    def connect(self) -> None:
        if sys.platform != "linux":
            raise ValueError("LINUX_NAMESPACES_REQUIRED")
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(90)
        self.sock.connect("/bridge/inference.sock")


class InferenceBridge(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_POST(self) -> None:
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        lengths = self.headers.get_all("Content-Length", [])
        if self.headers.get("Transfer-Encoding") or len(lengths) != 1:
            self.send_error(400)
            return
        try:
            length = int(lengths[0])
        except ValueError:
            self.send_error(400)
            return
        if not 0 < length <= 256 * 1024:
            self.send_error(413)
            return
        self.connection.settimeout(10)
        body = self.rfile.read(length)
        if len(body) != length:
            self.send_error(400)
            return
        headers = {
            key: self.headers[key]
            for key in ("Authorization", "x-opencode-session", "Content-Type")
            if key in self.headers
        }
        connection = InferenceConnection("fixed-inference", timeout=90)
        try:
            connection.request("POST", "/v1/chat/completions", body, headers)
            response = connection.getresponse()
            self.send_response(response.status)
            self.send_header("Content-Type", response.getheader("Content-Type", "application/json"))
            self.send_header("Connection", "close")
            self.end_headers()
            total = 0
            while block := response.read1(16384):
                total += len(block)
                if total > 1024 * 1024:
                    break
                self.wfile.write(block)
                self.wfile.flush()
        except OSError:
            self.close_connection = True
        finally:
            connection.close()
            self.close_connection = True


def main(control_fd: int) -> None:
    control = socket.socket(fileno=control_fd)
    control.set_inheritable(False)
    startup = receive(control)
    home = Path("/tmp/home")
    home.mkdir()
    version = subprocess.run(
        ["/opt/opencode", "--version"],
        cwd="/workspace",
        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": str(home)},
        close_fds=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=True,
        timeout=10,
    )
    if version.stdout.strip() != b"1.18.29":
        raise ValueError("RUNTIME_VERSION_MISMATCH")
    bridge = ThreadingHTTPServer(("127.0.0.1", 5001), InferenceBridge)
    bridge.daemon_threads = True
    threading.Thread(target=bridge.serve_forever, daemon=True).start()
    password = uuid.uuid4().hex
    environment = {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "HOME": str(home),
        "OPENCODE_CONFIG_CONTENT": json.dumps(
            configuration(
                startup["capability"], json.loads(Path("/control/projection.json").read_text())
            )
        ),
        "OPENCODE_SERVER_PASSWORD": password,
        "OPENCODE_SERVER_USERNAME": "probe",
        "TMPDIR": "/tmp",
        "OPENCODE_TEST_HOME": str(home),
        "OPENCODE_EXPERIMENTAL_DISABLE_FILEWATCHER": "true",
    }
    for kind in ("CONFIG", "DATA", "CACHE", "STATE"):
        target = Path("/tmp") / kind.lower()
        target.mkdir()
        environment[f"XDG_{kind}_HOME"] = str(target)
    for flag in (
        "MODELS_FETCH",
        "DEFAULT_PLUGINS",
        "PROJECT_CONFIG",
        "EXTERNAL_SKILLS",
        "AUTOUPDATE",
        "CLAUDE_CODE",
        "LSP_DOWNLOAD",
    ):
        environment["OPENCODE_DISABLE_" + flag] = "true"
    process = subprocess.Popen(
        ["/opt/opencode", "serve", "--hostname", "127.0.0.1", "--port", "5002"],
        cwd="/workspace",
        env=environment,
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=sys.stderr,
        stderr=sys.stderr,
    )
    deadline = time.monotonic() + 15
    while True:
        try:
            management("GET", "/global/health", None, password)
            break
        except OSError:
            if process.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError("NAMESPACE_SERVER_NOT_READY") from None
            time.sleep(0.05)
    status = dict(
        line.split(":", 1)
        for line in Path(f"/proc/{process.pid}/status").read_text().splitlines()
        if ":" in line
    )
    control_identity = f"socket:[{os.fstat(control.fileno()).st_ino}]"
    native_fds = []
    for fd in Path(f"/proc/{process.pid}/fd").iterdir():
        try:
            native_fds.append(os.readlink(fd))
        except FileNotFoundError:
            continue
    snapshot = {
        "state": "running",
        "runtime_version": version.stdout.decode().strip(),
        "runtime_tools_status": "not_run",
        "dispatch_eligible": False,
        "namespace_pid": os.getpid(),
        "namespaces": {
            name: os.readlink(f"/proc/self/ns/{name}") for name in ("user", "mnt", "pid", "net")
        },
        "host_mount_visible": Path("/mnt/c").exists(),
        "wsl_interop_visible": Path("/init").exists(),
        "native_control_fd_inherited": control_identity in native_fds,
        "network_interfaces": [name for _, name in socket.if_nameindex()],
        "ipv4_routes": Path("/proc/net/route").read_text().splitlines()[1:],
        "capabilities": {
            name: status[key].strip()
            for name, key in (
                ("effective", "CapEff"),
                ("permitted", "CapPrm"),
                ("bounding", "CapBnd"),
            )
        },
        "no_new_privileges": status["NoNewPrivs"].strip() == "1",
    }
    send(control, snapshot)
    sessions: set[str] = set()
    try:
        while True:
            request = receive(control)
            try:
                if request == {"operation": "probe_lifecycle"}:
                    send(control, {"result": lifecycle_probe()})
                    continue
                validate_request(request["method"], request["route"], request["body"], sessions)
                result = management(request["method"], request["route"], request["body"], password)
                if request["method"] == "POST" and request["route"] == "/session":
                    register_session(result, sessions)
                send(control, {"result": result})
            except (OSError, ValueError) as error:
                send(control, {"error": str(error)})
    except EOFError:
        pass


if __name__ == "__main__":
    main(int(sys.argv[1]))
