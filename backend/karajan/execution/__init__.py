"""Persistent local process supervision through a narrow public interface."""

from ._platform import ProcessIdentity, observe_process
from .host import (
    Activation,
    Cancellation,
    LaunchDenied,
    ProbeCrash,
    ProcessSpec,
    RunnerHost,
    Snapshot,
    StartConflict,
)
from .manifest import (
    CheckAttemptManifest,
    HostManifest,
    parse_host_manifest,
    parse_host_manifest_json,
)

__all__ = [
    "Activation",
    "Cancellation",
    "LaunchDenied",
    "ProbeCrash",
    "ProcessSpec",
    "ProcessIdentity",
    "RunnerHost",
    "Snapshot",
    "StartConflict",
    "CheckAttemptManifest",
    "HostManifest",
    "observe_process",
    "parse_host_manifest",
    "parse_host_manifest_json",
]
