"""Run committed fixture inputs and retain compact evidence plus full local reports."""

import argparse
import hashlib
import json
import platform
from datetime import UTC, datetime
from pathlib import Path

from karajan.adapters.deepseek.offline import DeepSeekOfflineProbe


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    source = root / "backend" / "karajan" / "adapters" / "deepseek"
    results = []
    for path in sorted((root / "examples" / "deepseek" / "cases").glob("*.json")):
        report = DeepSeekOfflineProbe(args.runtime, args.directory / path.stem).run_file(path)
        results.append(
            {
                "case": path.name,
                "input_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "status": report["status"],
                "conditions": report["conditions"],
                "runtime_version": report["runtime_version"],
                "cleanup": report["cleanup"],
                "attempt_ids": sorted({r["attempt_id"] for r in report["receipts"]}),
                "provider_request_count": len(report["provider_requests"]),
                "receipts": [
                    {
                        k: receipt.get(k)
                        for k in (
                            "receipt_id",
                            "call_id",
                            "state",
                            "reason_code",
                            "profile_id",
                            "profile_revision",
                            "profile_digest",
                        )
                    }
                    for receipt in report["receipts"]
                ],
                "budgets": report["ledger"]["budgets"],
                "tool_output_observed": report["tool_output_observed"],
                "response_observations": [
                    {
                        k: item.get(k)
                        for k in (
                            "call_id",
                            "status",
                            "reason_codes",
                            "model",
                            "usage",
                            "usage_status",
                            "actual_charge",
                        )
                    }
                    for item in report["response_observations"]
                ],
            }
        )
    summary = {
        "schema_version": "karajan.deepseek.offline.suite.v1",
        "observed_at": datetime.now(UTC).isoformat(),
        "os": platform.system(),
        "python_version": platform.python_version(),
        "status": "passed"
        if results and all(r["status"] == "passed" for r in results)
        else "failed",
        "source_sha256": {
            str(p.relative_to(root)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(source.glob("*.py"))
        },
        "suite_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "live_qualification": "not_run",
        "profile_enabled": False,
        "cash_api_calls": 0,
        "billing_scope": "synthetic-flat-CNY-price-not-a-DeepSeek-token-price",
        "cases": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, indent=2, ensure_ascii=True), encoding="utf-8")
    print(
        json.dumps(
            {
                "status": summary["status"],
                "case_count": len(results),
                "output": str(args.output.resolve()),
                "cash_api_calls": 0,
            }
        )
    )
    return 0 if summary["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
