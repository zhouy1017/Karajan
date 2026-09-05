"""Stable delivery refusal and uncertain-transport signals."""


class DeliveryError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class RemoteUnknown(Exception):
    """An external operation may have happened; callers must reconcile first."""
