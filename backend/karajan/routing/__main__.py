"""Offline JSON entry point. There are deliberately no account or execution options."""

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .compiler import RoutingError, canonical, compile_rulebook
from .evaluator import evaluate_route
from .fixture import fixture_from_configuration
from .models import CapacitySnapshot, PolicySnapshot, Rulebook, TaskSnapshot


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RoutingError("ROUTING_INPUT_INVALID")
        result[key] = value
    return result


def _load(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        if len(raw) > 2_000_000:
            raise RoutingError("ROUTING_INPUT_INVALID")
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
        canonical(value)
        if not isinstance(value, dict):
            raise RoutingError("ROUTING_INPUT_INVALID")
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise RoutingError("ROUTING_INPUT_INVALID") from None


def main() -> int:
    parser = argparse.ArgumentParser(description="Karajan offline Rulebook compiler and simulator")
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name in ("compile", "evaluate", "fixture"):
        command = subcommands.add_parser(name)
        command.add_argument("--input", required=True, type=Path)
        command.add_argument("--output", type=Path)
        if name == "fixture":
            command.add_argument("--as-of", type=float, required=True)
    schema = subcommands.add_parser("schema")
    schema.add_argument("--kind", choices=("rulebook", "task", "policy", "capacity"), required=True)
    schema.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    try:
        if arguments.command == "schema":
            models: dict[str, type[BaseModel]] = {
                "rulebook": Rulebook,
                "task": TaskSnapshot,
                "policy": PolicySnapshot,
                "capacity": CapacitySnapshot,
            }
            result = models[arguments.kind].model_json_schema()
        else:
            source = _load(arguments.input)
            if arguments.command == "compile":
                result = compile_rulebook(source)
            elif arguments.command == "fixture":
                task, policy, capacity = fixture_from_configuration(source, as_of=arguments.as_of)
                # Validate the generated fixture through the same public seam.
                evaluate_route(task, policy, capacity)
                result = {"task": task, "policy": policy, "capacity": capacity}
            else:
                if set(source) != {"task", "policy", "capacity"}:
                    raise RoutingError("ROUTING_INPUT_INVALID")
                result = {
                    "schema_version": "karajan.routing.replay.v1",
                    "model_calls": 0,
                    "source_sha256": {
                        path.relative_to(Path(__file__).parents[1]).as_posix(): hashlib.sha256(
                            path.read_bytes()
                        ).hexdigest()
                        for path in sorted(Path(__file__).parents[1].rglob("*.py"))
                    },
                    "result": evaluate_route(source["task"], source["policy"], source["capacity"]),
                }
        encoded = (
            json.dumps(result, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False) + "\n"
        ).encode("utf-8")
        if arguments.output is None:
            print(encoded.decode("utf-8"), end="")
        else:
            arguments.output.write_bytes(encoded)
        return 0
    except RoutingError as error:
        print(
            json.dumps({"error": error.code, "issues": error.issues, "activation_allowed": False})
        )
        return 2
    except (KeyError, TypeError, ValueError, OverflowError, RecursionError):
        print(
            json.dumps(
                {"error": "ROUTING_INPUT_INVALID", "issues": [], "activation_allowed": False}
            )
        )
        return 2
    except OSError:
        print(
            json.dumps(
                {"error": "ROUTING_FILE_UNAVAILABLE", "issues": [], "activation_allowed": False}
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
