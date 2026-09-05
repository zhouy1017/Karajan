"""Replay fixed synthetic Spec inputs through the public, pure routing interface."""

import argparse
import hashlib
import json
import platform
import sys
from pathlib import Path
from typing import Any

from karajan.routing import RoutingError, evaluate_route

DIRECTORY = Path(__file__).resolve().parent
ROOT = DIRECTORY.parents[2]


def source_hashes() -> dict[str, str]:
    result = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((ROOT / "backend/karajan/routing").glob("*.py"))
    }
    backend = ROOT / "backend/karajan"
    for name, module in tuple(sys.modules.items()):
        filename = getattr(module, "__file__", None)
        if not name.startswith("karajan.") or filename is None:
            continue
        path = Path(filename).resolve()
        if path.is_relative_to(backend) and not path.is_relative_to(backend / "routing"):
            result[path.relative_to(backend).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--case",
        required=True,
        choices=(
            "unknown-estimate",
            "lead-reserve-denied",
            "fx-normalized-one",
            "credential-native-setting",
            "mixed-pool-pressure",
        ),
    )
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    if arguments.output.exists():
        raise FileExistsError("Choose a new report path; historical evidence is preserved")
    input_path = DIRECTORY / f"spec-{arguments.case}.input.json"
    input_bytes = input_path.read_bytes()
    document = json.loads(input_bytes)
    before = source_hashes()
    try:
        result: dict[str, Any] = evaluate_route(
            document["task"], document["policy"], document["capacity"]
        )
    except RoutingError as error:
        result = {"error": error.code, "issues": error.issues, "activation_allowed": False}
    after = source_hashes()
    if before != after:
        raise RuntimeError("Source changed during replay; retry against a frozen revision")
    report = {
        "input_sha256": hashlib.sha256(input_bytes).hexdigest(),
        "source_sha256": after,
        "scope": "public evaluate_route; fixed synthetic inputs; no model or ledger access",
        "runtime": {"python": platform.python_version(), "system": platform.system()},
        "result": result,
    }
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "case": arguments.case,
                "input_sha256": report["input_sha256"],
                "selected_profile": result.get("selected_profile"),
                "reason_codes": result.get("reason_codes"),
                "error": result.get("error"),
            }
        )
    )


if __name__ == "__main__":
    main()
