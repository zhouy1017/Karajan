"""Fixed offline test entry; it is not the production bootstrap."""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path[:0] = [
    str(ROOT / "backend"),
    str(HERE),
    str(ROOT / "tests/projects"),
    str(ROOT / "tests/web"),
]

if __name__ == "__main__":
    from karajan.orchestration.go_task_execution import consume_go_task
    from karajan.runs import RunError
    from task_execution_fixture import open_fixture

    assert len(sys.argv) == 4
    services = open_fixture(Path.cwd())
    assert services.client_factory is not None
    try:
        consume_go_task(services, sys.argv[1], sys.argv[2], principal=sys.argv[3])
    except Exception as error:
        # All data in this harness is synthetic, but retain only known stable codes.
        code = error.code if isinstance(error, RunError) else type(error).__name__
        (Path.cwd() / "fixture-error.json").write_text(__import__("json").dumps({"code": code}))
        raise SystemExit(1) from None
