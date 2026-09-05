"""Fixed payload executed after confinement; it only targets generated fake data."""

import http.client
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path


def main() -> None:
    inputs = json.loads(Path(sys.argv[1]).read_text())
    permitted = Path("/workspace/allowed.txt")
    before = permitted.read_text()
    permitted.write_text("allowed update")
    reads: dict[str, bool] = {}
    writes: dict[str, bool] = {}
    for name, target in inputs["protected"].items():
        try:
            Path(target).read_bytes()
            reads[name] = True
        except OSError:
            reads[name] = False
        try:
            Path(target).write_text("unexpected canary write")
            writes[name] = True
        except OSError:
            writes[name] = False
    escapes = {}
    for name, path in {
        "symlink": "/workspace/outside-link",
        "host_proc": inputs["host_proc_path"],
    }.items():
        try:
            Path(path).read_bytes()
            escapes[name] = True
        except OSError:
            escapes[name] = False
    try:
        inherited = os.fstat(inputs["inherited_fd"])
        leaked_fd = [inherited.st_dev, inherited.st_ino] == inputs["inherited_identity"]
    except OSError:
        leaked_fd = False
    status = dict(
        line.split(":", 1)
        for line in Path("/proc/self/status").read_text().splitlines()
        if ":" in line
    )
    try:
        Path("/probe/readonly-canary").write_text("unexpected readonly write")
        readonly_write = True
    except OSError:
        readonly_write = False
    network = {}
    for endpoint in ("/control", "/broker-admin", "/provider", "/delivery"):
        connection = http.client.HTTPConnection("127.0.0.1", inputs["network_port"], timeout=0.5)
        try:
            connection.request("GET", endpoint)
            response = connection.getresponse()
            network[endpoint] = response.status == 200
            response.read()
        except OSError:
            network[endpoint] = False
        finally:
            connection.close()
    git_environment = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}
    git_read = subprocess.run(
        ["/usr/bin/git", "-C", "/workspace", "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        env=git_environment,
        timeout=5,
        check=False,
    )
    git_push = subprocess.run(
        ["/usr/bin/git", "-C", "/workspace", "push", inputs["git_remote"], "HEAD:refs/heads/main"],
        capture_output=True,
        env=git_environment,
        timeout=5,
        check=False,
    )
    interop_executed = False
    if inputs["wsl_pe_available"]:
        try:
            pe = subprocess.run(
                ["/workspace/interop-canary.exe", "/D", "/Q", "/C", "echo KARAJAN_WSL_CANARY"],
                capture_output=True,
                timeout=5,
                check=False,
            )
            interop_executed = pe.returncode == 0 and b"KARAJAN_WSL_CANARY" in pe.stdout
        except OSError:
            pass
    chroot = subprocess.run(
        ["/usr/bin/python3", "-c", "import os; os.chroot('/tmp')"],
        capture_output=True,
        timeout=5,
        check=False,
    )
    mount = subprocess.run(
        [
            "/usr/bin/python3",
            "-c",
            "import ctypes,os; os.mkdir('/tmp/mount-canary'); "
            "lib=ctypes.CDLL(None); "
            "result=lib.mount(b'tmpfs',b'/tmp/mount-canary',b'tmpfs',0,None); "
            "raise SystemExit(0 if result==0 else 1)",
        ],
        capture_output=True,
        timeout=5,
        check=False,
    )
    subprocess.Popen(
        [
            "/usr/bin/python3",
            "-c",
            "from pathlib import Path; import time; "
            "p=Path('/workspace/heartbeat'); "
            "[(p.open('a').write('tick\\n'), time.sleep(0.02)) for _ in range(1000)]",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    deadline = time.monotonic() + 2
    while not Path("/workspace/heartbeat").exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    print(
        json.dumps(
            {
                "allowed_read": before == "allowed input",
                "allowed_write": permitted.read_text() == "allowed update",
                "protected_reads": reads,
                "protected_writes": writes,
                "escapes": escapes,
                "environment_leaked": "KARAJAN_FAKE_SECRET" in os.environ,
                "inherited_fd_leaked": leaked_fd,
                "capabilities": {
                    key: status[key].strip()
                    for key in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")
                },
                "no_new_privs": status["NoNewPrivs"].strip(),
                "readonly_write": readonly_write,
                "network_reachable": network,
                "git_read_usable": git_read.returncode == 0 and git_read.stdout.strip() == b"true",
                "git_push_exit": git_push.returncode,
                "interop_executed": interop_executed,
                "chroot_succeeded": chroot.returncode == 0,
                "mount_succeeded": mount.returncode == 0,
                "runtime_version": sys.version.split()[0],
                "runtime_executable": sys.executable,
                "kernel_release": platform.release(),
            }
        ),
        flush=True,
    )
    time.sleep(20)


if __name__ == "__main__":
    main()
