"""Pure DeepSeek protocol conversion; importing this module performs no I/O."""

from .protocol import PreparedRequest, ProtocolError, prepare_request
from .response import ResponseObservation, observe_response

__all__ = [
    "PreparedRequest",
    "ProtocolError",
    "ResponseObservation",
    "prepare_request",
    "observe_response",
]
