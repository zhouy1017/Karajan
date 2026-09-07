"""Project Issue 107's existing qualification observations without exposing raw text.

Run only with the controller's prescribed virtual environment.  The script
opens the two existing controller databases using SQLite URI mode=ro, filters
the three published record digests, and emits the narrow JSON allowlist needed
to review request/history-to-Journal linkage.  It never invokes a runtime,
provider, qualification API, or consumer API.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any


RECORDS = {
    "0576491aab6106cbb485b088d68289600f55d24a21067bcd96d8eb78e0251e77": "attempt2",
    "bb4c42d0921726457c16e0bdb4ea022053c0f3dd35c0222d0a864cc710925019": "attempt3",
    "8e162b7af73b060e507a3c61677bfdc25cc16603abbec4bf7845acb4f2eeb58a": "attempt4",
}


def digest(value: object) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def connect_read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def journal_index(connection: sqlite3.Connection) -> dict[tuple[str, str], list[dict[str, Any]]]:
    result: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for grant_id, call_id, receipt_raw in connection.execute(
        "SELECT grant_id, call_id, receipt FROM go_calls"
    ):
        receipt = json.loads(receipt_raw)
        request_digest = receipt["request_context"]["request_digest"]
        item = {
            "journal_call_id_sha256": digest(call_id),
            "journal_grant_id_sha256": digest(grant_id),
            "journal_request_digest": request_digest,
            "journal_state": receipt["state"],
            "journal_outcome_state": receipt["outcome"]["state"],
            "journal_completed_at": receipt["completed_at"],
        }
        result.setdefault((grant_id, request_digest), []).append(item)
    return result


def project_record(record_raw: str, record_digest: str, journal: dict[tuple[str, str], list[dict[str, Any]]]) -> dict[str, Any]:
    record = json.loads(record_raw)
    scenarios: list[dict[str, Any]] = []
    for scenario_entry in record["observation"]["scenarios"]:
        observation = scenario_entry["observation"]
        grant_id = scenario_entry["grant_id"]
        retention_rows: list[dict[str, Any]] = []
        for retained in observation["retention"]["requests"]:
            matches = journal.get((grant_id, retained["request_digest"]), [])
            retention_rows.append(
                {
                    "sequence": retained["sequence"],
                    "request_digest": retained["request_digest"],
                    "messages_digest": retained["messages_digest"],
                    "message_count": retained["message_count"],
                    "initial_input_retained": retained["initial_input_retained"],
                    "prior_messages_retained": retained["prior_messages_retained"],
                    "denied_canary_present": retained["denied_canary_present"],
                    "read_results": [
                        {
                            "path": item["path"],
                            "content_sha256": item["content_sha256"],
                            "tool_result_sha256": item["tool_result_sha256"],
                        }
                        for item in retained["read_results"]
                    ],
                    "journal_match_count": len(matches),
                    "journal_matches": matches,
                }
            )
        parsed = observation["parsed_review"]
        findings = [
            {
                key: finding[key]
                for key in ("blocking", "severity", "file", "line", "behavior", "trigger", "acceptance_ref")
            }
            for finding in parsed["findings"]
        ]
        scenarios.append(
            {
                "scenario": scenario_entry["scenario"],
                "scenario_status": scenario_entry["status"],
                "attempt_id_sha256": digest(scenario_entry["attempt_id"]),
                "grant_id_sha256": digest(grant_id),
                "final_request_digest": observation["retention"]["final_request_digest"],
                "parsed_review": {"verdict": parsed["verdict"], "findings": findings},
                "retention_requests": retention_rows,
            }
        )
    return {
        "attempt": RECORDS[record_digest],
        "record_sha256": record_digest,
        "record_status": record["status"],
        "scenarios": scenarios,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--controller-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with connect_read_only(args.controller_root / "projects.sqlite") as projects, connect_read_only(
        args.controller_root / "journal.sqlite"
    ) as journal_db:
        journal = journal_index(journal_db)
        rows = projects.execute("SELECT record, digest FROM profile_qualification_records").fetchall()
    projected = [
        project_record(record, record_digest, journal)
        for record, record_digest in rows
        if record_digest in RECORDS
    ]
    projected.sort(key=lambda item: item["attempt"])
    if [item["attempt"] for item in projected] != ["attempt2", "attempt3", "attempt4"]:
        raise SystemExit("expected the three published Issue 107 records")
    payload: dict[str, Any] = {
        "schema_version": "issue107-retention-journal-whitelist-v1",
        "source": "existing controller records; SQLite mode=ro",
        "new_go_calls": 0,
        "new_qualification_starts": 0,
        "new_consumer_effects": 0,
        "records": projected,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    payload["payload_sha256"] = hashlib.sha256(canonical).hexdigest()
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
