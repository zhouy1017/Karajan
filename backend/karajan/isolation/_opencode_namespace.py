"""Fixed OpenCode mount setup; never changes the existing canary launcher."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ._namespace import READ_ONLY_ROOTS, drop_capabilities, mount
    from ._opencode_projection import projection_files, verify_projected_file
else:
    from _namespace import READ_ONLY_ROOTS, drop_capabilities, mount
    from _opencode_projection import projection_files, verify_projected_file


def main(directory: Path, runtime: Path, upstream: Path, control_fd: int) -> None:
    if sys.platform != "linux":
        raise ValueError("LINUX_NAMESPACES_REQUIRED")
    projection = projection_files(json.loads((directory / "projection.json").read_text()))
    root = directory / "root"
    root.mkdir(mode=0o700)
    for name in ("workspace", "tmp", "proc", "dev", "control", "opt", "bridge"):
        (root / name).mkdir()
    for source in READ_ONLY_ROOTS:
        if Path(source).is_dir():
            (root / source.lstrip("/")).mkdir(parents=True, exist_ok=True)
    for name, target in (("bin", "usr/bin"), ("lib", "usr/lib"), ("lib64", "usr/lib64")):
        (root / name).symlink_to(target, target_is_directory=True)
    for name in ("null", "zero", "urandom"):
        (root / "dev" / name).touch()
    for name in ("opt/opencode", "bridge/inference.sock"):
        (root / name).touch()
    for row in projection:
        target = root / "workspace" / row["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch()
    (root / "control/inner.py").write_bytes(
        Path(__file__).with_name("_opencode_inner.py").read_bytes()
    )
    (root / "control/_opencode_projection.py").write_bytes(
        Path(__file__).with_name("_opencode_projection.py").read_bytes()
    )
    (root / "control/projection.json").write_text(json.dumps(projection))
    mount("--bind", str(root), str(root))
    for source in READ_ONLY_ROOTS:
        if Path(source).is_dir():
            target = str(root / source.lstrip("/"))
            mount("--bind", source, target)
            mount("-o", "remount,bind,ro", target)
    for bound_source, bound_target in (
        (runtime, root / "opt/opencode"),
        (upstream, root / "bridge/inference.sock"),
    ):
        mount("--bind", str(bound_source), str(bound_target))
        mount("-o", "remount,bind,ro", str(bound_target))
    with (root / "opt/opencode").open("rb") as binary:
        if hashlib.file_digest(binary, "sha256").hexdigest() != (
            "ca6c0e1f42be3120595bf6848937e7586ec862c87fa7aa111e89c7cc6e9a4650"
        ):
            raise ValueError("RUNTIME_ARTIFACT_MISMATCH")
    for row in projection:
        source = verify_projected_file(directory / "workspace", row)
        target = root / "workspace" / row["path"]
        mount("--bind", str(source), str(target))
        verify_projected_file(root / "workspace", row)
        if not row["writable"]:
            mount("-o", "remount,bind,ro", str(target))
    mount("-t", "tmpfs", "-o", "size=128m,mode=1777,nosuid,nodev", "tmpfs", str(root / "tmp"))
    mount("-t", "proc", "-o", "nosuid,nodev,noexec", "proc", str(root / "proc"))
    for name in ("null", "zero", "urandom"):
        mount("--bind", f"/dev/{name}", str(root / "dev" / name))
    subprocess.run(["/usr/bin/ip", "link", "set", "lo", "up"], check=True, capture_output=True)
    mount("-o", "remount,bind,ro", str(root))
    os.chroot(root)
    os.chdir("/workspace")
    drop_capabilities()
    os.execve(
        "/usr/bin/python3",
        ["/usr/bin/python3", "/control/inner.py", str(control_fd)],
        {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": "/tmp/home"},
    )


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), int(sys.argv[4]))
