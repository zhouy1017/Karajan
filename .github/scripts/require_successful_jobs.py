"""Fail a workflow aggregation job unless every named dependency succeeded."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Iterable, Mapping
from typing import Any


def unsuccessful_jobs(
    results: Mapping[str, object], required: Iterable[str]
) -> dict[str, str]:
    """Return failures, skipped jobs, and malformed or absent required jobs."""
    required_names = set(required)
    if set(results) != required_names:
        return {
            "dependency-list": (
                f"expected {sorted(required_names)!r}, got {sorted(results)!r}"
            )
        }

    unsuccessful: dict[str, str] = {}
    for job in required_names:
        outcome = results.get(job)
        if not isinstance(outcome, Mapping):
            unsuccessful[job] = "missing"
            continue
        result = outcome.get("result")
        if result != "success":
            unsuccessful[job] = str(result) if result is not None else "missing"
    return unsuccessful


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--required", action="append", required=True)
    parser.add_argument(
        "--results",
        default=os.environ.get("NEEDS_RESULTS"),
        help="GitHub needs JSON; defaults to NEEDS_RESULTS.",
    )
    args = parser.parse_args(argv)
    if args.results is None:
        print("NEEDS_RESULTS is not set.", file=sys.stderr)
        return 1
    try:
        raw_results: Any = json.loads(args.results)
    except json.JSONDecodeError as error:
        print(f"NEEDS_RESULTS is invalid JSON: {error}", file=sys.stderr)
        return 1
    if not isinstance(raw_results, dict):
        print("NEEDS_RESULTS must be a JSON object.", file=sys.stderr)
        return 1

    unsuccessful = unsuccessful_jobs(raw_results, args.required)
    if unsuccessful:
        print(f"Required checks did not succeed: {unsuccessful}", file=sys.stderr)
        return 1
    print("All required checks succeeded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
