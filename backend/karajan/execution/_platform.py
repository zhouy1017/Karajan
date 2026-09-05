"""OS observations for the local, non-breakaway process probe."""

import ctypes
import os
import signal
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    birth: str


def _kernel() -> Any:
    if sys.platform != "win32":
        raise OSError("Windows API is unavailable.")
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
        "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        "GetProcessTimes": ([wintypes.HANDLE] + [ctypes.c_void_p] * 4, wintypes.BOOL),
        "WaitForSingleObject": ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
        "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
        "OpenJobObjectW": (
            [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR],
            wintypes.HANDLE,
        ),
        "GetCurrentProcess": ([], wintypes.HANDLE),
        "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
        "QueryInformationJobObject": (
            [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p],
            wintypes.BOOL,
        ),
        "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(kernel, name)
        function.argtypes = arguments
        function.restype = result
    return kernel


def _windows_error() -> OSError:
    if sys.platform != "win32":
        raise OSError("Windows API is unavailable.")
    return OSError(int(ctypes.get_last_error()), "Windows process observation failed")


def process_identity(pid: int) -> ProcessIdentity | None:
    """None proves absent/exited; inaccessible state raises instead of looking absent."""
    if sys.platform == "win32":
        kernel = _kernel()
        handle = kernel.OpenProcess(0x1000 | 0x100000, False, pid)
        if not handle:
            error = _windows_error()
            if error.errno == 87:  # ERROR_INVALID_PARAMETER: no such PID.
                return None
            raise error
        try:
            if kernel.WaitForSingleObject(handle, 0) == 0:
                return None
            creation, exit_time, system, user = (ctypes.c_ulonglong() for _ in range(4))
            if not kernel.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(system),
                ctypes.byref(user),
            ):
                raise _windows_error()
            return ProcessIdentity(pid, str(creation.value))
        finally:
            kernel.CloseHandle(handle)
    fields = _linux_stat(pid)
    if fields is None or fields[0] == "Z":
        return None
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    return ProcessIdentity(pid, f"{boot}:{fields[19]}")


def _linux_stat(pid: int) -> list[str] | None:
    try:
        content = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return None
    return content[content.rfind(")") + 2 :].split()


def observe_process(
    expected: ProcessIdentity,
) -> Literal["running", "exited", "identity_mismatch", "unknown"]:
    """Read-only diagnostic; a reused PID is never the original process."""
    try:
        current = process_identity(expected.pid)
    except OSError:
        return "unknown"
    if current is None:
        return "exited"
    return "running" if current == expected else "identity_mismatch"


class ProcessGroup:
    """Named Windows job or the supervisor's dedicated Linux process group."""

    def __init__(self, nonce: str, supervisor_pid: int, *, create: bool = False) -> None:
        self.pid = supervisor_pid
        self.handle = 0
        if sys.platform == "win32":
            kernel = _kernel()
            name = f"Local\\Karajan-{nonce}"
            if create:
                self.handle = int(kernel.CreateJobObjectW(None, name) or 0)
                if not self.handle:
                    raise _windows_error()
                if not kernel.AssignProcessToJobObject(self.handle, kernel.GetCurrentProcess()):
                    self.close()
                    raise _windows_error()
            else:
                self.handle = int(kernel.OpenJobObjectW(0x0004 | 0x0008, False, name) or 0)
                if not self.handle and _windows_error().errno != 2:
                    raise _windows_error()
        elif create and os.getpgrp() != supervisor_pid:
            os.setsid()

    def members(self) -> list[ProcessIdentity]:
        if sys.platform == "win32":
            if not self.handle:
                return []
            kernel = _kernel()
            count = 16
            while True:
                size = 8 + ctypes.sizeof(ctypes.c_size_t) * count
                buffer = ctypes.create_string_buffer(size)
                if kernel.QueryInformationJobObject(self.handle, 3, buffer, size, None):
                    listed = ctypes.c_uint32.from_buffer(buffer, 4).value
                    pids = [
                        ctypes.c_size_t.from_buffer(
                            buffer, 8 + index * ctypes.sizeof(ctypes.c_size_t)
                        ).value
                        for index in range(listed)
                    ]
                    break
                if _windows_error().errno != 234 or count >= 65536:
                    raise _windows_error()
                count *= 2
        else:
            pids = []
            for entry in Path("/proc").iterdir():
                if entry.name.isdecimal():
                    fields = _linux_stat(int(entry.name))
                    if fields is not None and int(fields[2]) == self.pid:
                        pids.append(int(entry.name))
        identities = [process_identity(pid) for pid in pids]
        return [identity for identity in identities if identity is not None]

    def close(self) -> None:
        if self.handle:
            _kernel().CloseHandle(self.handle)
            self.handle = 0

    def terminate(self) -> None:
        if sys.platform == "win32":
            if self.handle and not _kernel().TerminateJobObject(self.handle, 125):
                raise _windows_error()
        elif self.pid == os.getpid():
            os.killpg(self.pid, signal.SIGKILL)
        else:
            raise OSError("Only the live Linux supervisor may terminate its own process group.")
