"""Fixed Check mount setup, separate from every OpenCode qualification."""

import json
import os
import resource
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ._check_environment import digest, manifest
    from ._namespace import drop_capabilities, mount
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _check_environment import digest, manifest
    from _namespace import drop_capabilities, mount


def main(directory: Path) -> None:
    if sys.platform != "linux":
        raise ValueError("CHECK_ENVIRONMENT_UNSUPPORTED")
    config = json.loads((directory / "launch.json").read_text())
    # Before replacing proc, its self entry exposes this namespace init's host PID.
    raw = Path("/proc/self/stat").read_text()
    host_pid = int(raw.split(" ", 1)[0])
    fields = raw[raw.rfind(")") + 2 :].split()
    identity = {
        "pid": host_pid,
        "birth": Path("/proc/sys/kernel/random/boot_id").read_text().strip() + ":" + fields[19],
        "execution_digest": config["execution_digest"],
    }
    with (directory / "namespace-init.json").open("x") as stream:
        json.dump(identity, stream)
        stream.flush()
        os.fsync(stream.fileno())
    root = directory / "root"
    root.mkdir(mode=0o700)
    mount("--bind", config["image_root"], str(root))
    mount("-o", "remount,bind,ro,nosuid,nodev", str(root))
    if digest(manifest(root)) != config["image_manifest_sha256"]:
        raise ValueError("CHECK_ENVIRONMENT_SOURCE_CHANGED")
    for name, size in (("workspace", "128m"), ("tmp", "32m")):
        mount("-t", "tmpfs", "-o", f"size={size},mode=1777,nosuid,nodev", "tmpfs", str(root / name))
    if digest(manifest(directory / "snapshot")) != config["snapshot_manifest_sha256"]:
        raise ValueError("CHECK_SNAPSHOT_CHANGED")
    shutil.copytree(directory / "snapshot", root / "workspace", dirs_exist_ok=True)
    if digest(manifest(root / "workspace")) != config["snapshot_manifest_sha256"]:
        raise ValueError("CHECK_SNAPSHOT_CHANGED")
    mount("-t", "proc", "-o", "nosuid,nodev,noexec", "proc", str(root / "proc"))
    for name in ("null", "zero", "urandom"):
        mount("--bind", "/dev/" + name, str(root / "dev" / name))
    os.chroot(root)
    os.chdir("/workspace")
    Path("/tmp/home").mkdir(mode=0o700)
    drop_capabilities()
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
    resource.setrlimit(resource.RLIMIT_FSIZE, (128 * 1024 * 1024, 128 * 1024 * 1024))
    environment = {
        "PATH": "/usr/bin",
        "HOME": "/tmp/home",
        "LANG": "C.UTF-8",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    environment.update(config["env"])
    argv = config["argv"]
    executable = argv[0] if argv[0].startswith("/") else "/usr/bin/" + argv[0]
    os.execve(executable, argv, environment)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
