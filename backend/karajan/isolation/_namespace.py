"""Trusted namespace setup; every mount exists only in the child mount namespace."""

import ctypes
import os
import subprocess
import sys
from pathlib import Path

READ_ONLY_ROOTS = (
    "/usr/bin",
    "/usr/lib/x86_64-linux-gnu",
    "/usr/lib/python3.12",
    "/usr/lib/git-core",
    "/usr/lib64",
)


def mount(*arguments: str) -> None:
    result = subprocess.run(["/usr/bin/mount", *arguments], check=False, capture_output=True)
    if result.returncode:
        raise OSError(result.stderr.decode(errors="replace"))


def drop_capabilities() -> None:
    # Constants and ABI from the installed linux/prctl.h and linux/capability.h.
    library = ctypes.CDLL(None, use_errno=True)
    last_capability = int(Path("/proc/sys/kernel/cap_last_cap").read_text())
    for capability in range(last_capability + 1):
        if library.prctl(24, capability, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "Cannot clear capability bounding set")
    if library.prctl(47, 4, 0, 0, 0) != 0 or library.prctl(38, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "Cannot clear ambient capabilities or set no_new_privs")

    class Header(ctypes.Structure):
        _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]

    class Data(ctypes.Structure):
        _fields_ = [
            ("effective", ctypes.c_uint32),
            ("permitted", ctypes.c_uint32),
            ("inheritable", ctypes.c_uint32),
        ]

    header = Header(0x20080522, 0)
    data = (Data * 2)()
    if library.capset(ctypes.byref(header), ctypes.byref(data)) != 0:
        raise OSError(ctypes.get_errno(), "Cannot clear process capabilities")


def main(directory: Path) -> None:
    if (directory / "marker").read_text() != "karajan-generated-canary-v1":
        raise ValueError("Only a generated canary directory is supported")
    root = directory / "root"
    root.mkdir(mode=0o700)
    for name in ("workspace", "tmp", "proc", "dev", "probe"):
        (root / name).mkdir()
    for source in READ_ONLY_ROOTS:
        if Path(source).is_dir():
            (root / source.lstrip("/")).mkdir(parents=True, exist_ok=True)
    for name, target in (("bin", "usr/bin"), ("lib", "usr/lib"), ("lib64", "usr/lib64")):
        (root / name).symlink_to(target, target_is_directory=True)
    for name in ("null", "zero", "urandom"):
        (root / "dev" / name).touch()
    (root / "probe/canary.py").write_bytes(Path(__file__).with_name("_canary.py").read_bytes())
    (root / "probe/input.json").write_bytes((directory / "input.json").read_bytes())
    (root / "probe/readonly-canary").write_text("generated readonly canary")
    mount("--bind", str(root), str(root))
    for source in READ_ONLY_ROOTS:
        if Path(source).is_dir():
            target = str(root / source.lstrip("/"))
            mount("--bind", source, target)
            mount("-o", "remount,bind,ro", target)
    mount("--bind", str(directory / "workspace"), str(root / "workspace"))
    mount("-t", "tmpfs", "-o", "size=16m,mode=1777,nosuid,nodev", "tmpfs", str(root / "tmp"))
    mount("-t", "proc", "-o", "nosuid,nodev,noexec", "proc", str(root / "proc"))
    for name in ("null", "zero", "urandom"):
        mount("--bind", f"/dev/{name}", str(root / "dev" / name))
    mount("-o", "remount,bind,ro", str(root))
    if sys.platform == "linux":
        os.chroot(root)
    else:
        raise OSError("Linux namespaces are required")
    os.chdir("/workspace")
    drop_capabilities()
    os.execve(
        "/usr/bin/python3",
        ["/usr/bin/python3", "/probe/canary.py", "/probe/input.json"],
        {"PATH": "/usr/bin:/bin", "HOME": "/tmp/empty", "LANG": "C.UTF-8"},
    )


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
