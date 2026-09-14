"""Stable, content-free failures for the gateway catalog boundary."""


class GatewayError(ValueError):
    """Reason-code only: no caller payload, path or credential is echoed.

    ``current_revision`` is present exactly when a conditional write lost a
    compare-and-set, so a caller can retry against the durable head without
    reading another project's state.
    """

    def __init__(self, code: str, *, current_revision: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.current_revision = current_revision
