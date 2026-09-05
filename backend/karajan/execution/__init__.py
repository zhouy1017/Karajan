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
    "observe_process",
]
