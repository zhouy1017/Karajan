"""Controller-owned Linux Python image; no request-supplied assets or downloads."""

import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

KIND = "python312-stdlib"
MAX_LOG_BYTES = 8 * 1024 * 1024
LOG_HEADER = b"karajan-check-log.v1\nmerged_stdout_stderr:\n"


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def regular(path: Path, *, max_bytes: int = 256 * 1024 * 1024) -> bytes:
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ValueError("CHECK_ASSET_NOT_REGULAR")
    if before.st_size > max_bytes:
        raise ValueError("CHECK_ASSET_LIMIT")
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError("CHECK_ASSET_CHANGED")
        content = stream.read(max_bytes + 1)
        if len(content) > max_bytes:
            raise ValueError("CHECK_ASSET_LIMIT")
        after = os.fstat(stream.fileno())
    if (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ):
        raise ValueError("CHECK_ASSET_CHANGED")
    return content


def manifest(root: Path) -> list[dict[str, Any]]:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("CHECK_IMAGE_UNAVAILABLE")
    rows: list[dict[str, Any]] = []
    total = 0
    for directory, directories, files in os.walk(root, followlinks=False):
        for name in directories:
            if (Path(directory) / name).is_symlink():
                raise ValueError("CHECK_IMAGE_LINK_UNSUPPORTED")
        for name in sorted(files):
            path = Path(directory) / name
            data = regular(path)
            total += len(data)
            if total > 256 * 1024 * 1024 or len(rows) >= 10000:
                raise ValueError("CHECK_IMAGE_LIMIT")
            rows.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "mode": stat.S_IMODE(path.stat().st_mode),
                    "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            )
    return sorted(rows, key=lambda row: row["path"])


def supported_platform() -> None:
    if sys.platform != "linux" or platform.machine() != "x86_64":
        raise ValueError("CHECK_ENVIRONMENT_UNSUPPORTED")


class PythonCheckEnvironment:
    """Explicitly provision once, then re-observe every byte before an execution.

    The private image contains only the fixed distro Python 3.12 stdlib and its
    ELF dependencies. It has no pip, Git, shell, sitecustomize or host config.
    This is an environment implementation, never a model qualification.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory.absolute()
        if self.directory.is_symlink() or self.directory.resolve() != self.directory:
            raise ValueError("CHECK_IMAGE_PATH_INVALID")
        self.root = self.directory / "root"

    @classmethod
    def provision(cls, directory: Path) -> "PythonCheckEnvironment":
        supported_platform()
        directory.mkdir(mode=0o700, parents=True, exist_ok=False)
        root = directory / "root"
        root.mkdir(mode=0o700)
        python = Path("/usr/bin/python3.12")
        stdlib = Path("/usr/lib/python3.12")
        if not python.is_file() or not stdlib.is_dir():
            raise ValueError("CHECK_ENVIRONMENT_UNSUPPORTED")

        def copy(source: Path, target: Path) -> None:
            resolved = source.resolve(strict=True)
            if not resolved.is_relative_to("/usr") and not resolved.is_relative_to("/lib"):
                raise ValueError("CHECK_SYSTEM_ASSET_UNSUPPORTED")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(resolved, target)
            target.chmod(0o755 if os.access(resolved, os.X_OK) else 0o644)

        for name in ("python", "python3", "python3.12"):
            copy(python, root / "usr/bin" / name)
        for path in stdlib.rglob("*"):
            if (
                "__pycache__" in path.parts
                or path.suffix == ".pyc"
                or path.name == "sitecustomize.py"
                or path.is_dir()
            ):
                continue
            copy(path, root / path.relative_to("/"))
        # ldd sees only trusted system ELF inputs; candidate bytes are never supplied.
        dependencies: set[str] = set()
        for binary in [python, *sorted((root / "usr/lib/python3.12").rglob("*.so"))]:
            result = subprocess.run(
                ["/usr/bin/ldd", str(binary)],
                capture_output=True,
                check=False,
                text=True,
                timeout=10,
                env={"PATH": "/usr/bin:/bin", "LANG": "C"},
            )
            if result.returncode:
                raise ValueError("CHECK_SYSTEM_DEPENDENCIES_UNAVAILABLE")
            dependencies.update(re.findall(r"(?:=>\s+|^\s*)(/[^\s]+)", result.stdout, re.M))
        for dependency in sorted(dependencies):
            copy(Path(dependency), root / dependency.lstrip("/"))
        for name in ("workspace", "tmp", "proc", "dev"):
            (root / name).mkdir(exist_ok=True)
        for name in ("null", "zero", "urandom"):
            (root / "dev" / name).touch()
        (directory / "manifest.json").write_text(json.dumps(manifest(root)))
        return cls(directory)

    def source(self) -> dict[str, Any]:
        supported_platform()
        if sys.platform == "linux":
            for directory in (self.directory, self.root):
                info = directory.stat()
                if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
                    raise ValueError("CHECK_IMAGE_PERMISSIONS_INVALID")
        actual = manifest(self.root)
        if any(row["mode"] & 0o022 for row in actual):
            raise ValueError("CHECK_IMAGE_PERMISSIONS_INVALID")
        expected = json.loads(regular(self.directory / "manifest.json"))
        if actual != expected:
            raise ValueError("CHECK_ENVIRONMENT_SOURCE_CHANGED")
        here = Path(__file__).parent
        sources = {
            name: hashlib.sha256(regular(here / name)).hexdigest()
            for name in (
                "check_runner.py",
                "_check_environment.py",
                "_check_namespace.py",
                "_namespace.py",
            )
        }
        sources["execution/_platform.py"] = hashlib.sha256(
            regular(here.parent / "execution/_platform.py")
        ).hexdigest()
        system = {
            name: hashlib.sha256(Path(name).resolve().read_bytes()).hexdigest()
            for name in (
                "/usr/bin/unshare",
                "/usr/bin/mount",
                "/usr/bin/python3",
                "/usr/bin/python3.12",
            )
        }
        value: dict[str, Any] = {
            "schema_version": "karajan.python-check-environment.v1",
            "runtime_kind": KIND,
            "platform": "linux_x64",
            "kernel_release": platform.release(),
            "filesystem": "candidate_copy",
            "network": "none",
            "image_manifest_sha256": digest(actual),
            "image_files": len(actual),
            "image_bytes": sum(row["size"] for row in actual),
            "controller_sources": sources,
            "launcher_assets": system,
            "isolation": "user-mount-pid-net-chroot-no-capabilities",
            "limits": {
                "max_log_bytes": MAX_LOG_BYTES,
                "workspace_bytes": 128 * 1024 * 1024,
                "temporary_bytes": 32 * 1024 * 1024,
            },
        }
        return value | {"environment_sha256": digest(value)}
