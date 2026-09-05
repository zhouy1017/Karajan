"""Keep partial messages and final counters separate; neither proves billed usage."""

from typing import Any

from .native import Assistant, Result


class UsageEvidence:
    def __init__(self) -> None:
        self.partial: dict[str, dict[str, int | None]] = {}
        self.final: dict[str, int | None] | None = None
        self.client_cost: float | None = None
        self.model_usage: dict[str, Any] = {}
        self.result_reports = 0
        self.snapshots: list[dict[str, Any]] = []
        self.child_snapshots: list[dict[str, Any]] = []

    def assistant(self, record: Assistant) -> bool:
        message = record.message
        if message.usage is None:
            return True
        if record.parent_tool_use_id is not None:
            # These rejected-Profile observations are never combined with the main
            # loop or summed as billable calls. UUID replay was deduplicated upstream.
            self.child_snapshots.append(
                {
                    "parent_tool_use_id": record.parent_tool_use_id,
                    "message_id": message.id,
                    "model_id": message.model,
                    "reported_usage": message.usage.model_dump(),
                    "output_counter_basis": "assistant_placeholder",
                }
            )
            return True
        partial = message.usage.model_dump(exclude={"output_tokens"})
        if message.id in self.partial and self.partial[message.id] != partial:
            return False
        self.partial[message.id] = partial
        return True

    def result(self, record: Result) -> None:
        self.result_reports += 1
        self.snapshots.append(
            {
                "usage": record.usage.model_dump(),
                "client_cost_estimate_usd": record.total_cost_usd,
                "model_usage": {
                    key: value.model_dump() for key, value in record.modelUsage.items()
                },
            }
        )
        # Single text input: the first terminal snapshot is the accepted turn's final usage.
        # Later snapshots are retained as observations, never summed as new billable calls.
        if self.result_reports == 1:
            self.final = record.usage.model_dump()
            self.client_cost = record.total_cost_usd
            self.model_usage = {key: value.model_dump() for key, value in record.modelUsage.items()}

    def report(self) -> dict[str, Any]:
        partial: dict[str, int | None] | None = None
        if self.partial:
            partial = {}
            for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
                counts = [row[key] for row in self.partial.values()]
                partial[key] = (
                    None
                    if any(value is None for value in counts)
                    else sum(value for value in counts if value is not None)
                )
        return {
            "coverage": "native_reported_only",
            "partial_main_input": partial,
            "child_message_snapshots": self.child_snapshots,
            "main_loop_final": self.final,
            "model_usage": self.model_usage,
            "terminal_observations": self.result_reports,
            "terminal_snapshots": self.snapshots,
            "client_cost_estimate_usd": self.client_cost,
            "cash_charged_usd": None,
            "account_remaining": None,
            "hidden_retry_usage": "unknown",
        }
