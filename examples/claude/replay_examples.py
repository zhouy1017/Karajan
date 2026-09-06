"""Regenerate synthetic cases and reports through the public, network-free replay API."""

import copy
import json
from pathlib import Path

from karajan.adapters.claude import replay_file


def main() -> None:
    directory = Path(__file__).resolve().parent
    base = json.loads((directory / "completed.json").read_text(encoding="utf-8"))
    cases = {"completed": base}

    cancelled = copy.deepcopy(base)
    cancelled["steps"].insert(
        1,
        {
            "kind": "controller",
            "at": "2026-09-05T13:30:00.500Z",
            "event_id": "cancel-1",
            "action": "cancel_requested",
            "attempt_id": base["attempt"]["id"],
            "fence": 1,
        },
    )
    cases["cancelled-late-result"] = cancelled

    denied = copy.deepcopy(base)
    denied["steps"][-1]["message"]["permission_denials"] = [
        {"tool_name": "Read", "tool_use_id": "denied-read", "tool_input": {"file_path": "/fake"}}
    ]
    cases["permission-denied"] = denied

    quota = copy.deepcopy(base)
    quota["steps"].insert(
        1,
        {
            "kind": "native",
            "at": "2026-09-05T13:30:00.500Z",
            "message": {
                "type": "system",
                "subtype": "api_retry",
                "uuid": "retry-1",
                "session_id": base["session_id"],
                "attempt": 1,
                "max_retries": 3,
                "retry_delay_ms": 500,
                "error_status": 429,
                "error": "rate_limit",
            },
        },
    )
    quota["steps"].insert(
        2,
        {
            "kind": "native",
            "at": "2026-09-05T13:30:00.500Z",
            "message": {
                "type": "rate_limit_event",
                "uuid": "quota-1",
                "session_id": base["session_id"],
                "rate_limit_info": {
                    "status": "allowed_warning",
                    "rateLimitType": "five_hour",
                    "utilization": 0.8,
                    "resetsAt": 1788624000,
                },
            },
        },
    )
    cases["retry-quota"] = quota

    truncated = copy.deepcopy(base)
    truncated["steps"][1:] = [
        {
            "kind": "native",
            "at": "2026-09-05T13:30:00.500Z",
            "message": {
                "type": "assistant",
                "uuid": "partial-1",
                "session_id": base["session_id"],
                "parent_tool_use_id": None,
                "message": {
                    "id": "message-1",
                    "model": "claude-fixture-model-v1",
                    "content": [],
                    "usage": {
                        "input_tokens": 12,
                        "output_tokens": 1,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 2,
                    },
                },
            },
        }
    ]
    cases["truncated"] = truncated

    mismatch = copy.deepcopy(base)
    mismatch["steps"][0]["message"]["model"] = "unrequested-model"
    cases["binding-mismatch"] = mismatch

    newer = copy.deepcopy(quota)
    newer["steps"][1]["message"]["no_response"] = {"waited_ms": 1000, "retry_wait_ms": 2000}
    cases["261-extension-unsupported"] = newer

    reports = directory / "reports"
    reports.mkdir(exist_ok=True)
    for name, document in cases.items():
        document["case_id"] = name
        source = directory / f"{name}.json"
        source.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        report = replay_file(source)
        (reports / f"{name}.report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"{name}: replay={report['status']}; live={report['qualification']['live_status']}")


if __name__ == "__main__":
    main()
