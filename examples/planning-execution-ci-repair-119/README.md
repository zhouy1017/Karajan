# PR #119 planning-execution CI collection repair

Date: 2026-09-07. Scope: the test-import correction at
`ca0366fc1c822d9c6f797fc20f6ea20bc7f33eeb`, based on failed candidate
`9911f1a0a0cd20246adc53cd3be29b31f0da5daf`.

The failed GitHub runs were [PR 34097329556](https://github.com/zhouy1017/Karajan/actions/runs/34097329556)
and [push 34097326425](https://github.com/zhouy1017/Karajan/actions/runs/34097326425). Their actual command was:

```text
uv run --frozen --extra dev pytest tests
```

Its public stdout summary on both platforms was:

```text
collected 2368 items / 31 errors
E   ModuleNotFoundError: No module named 'tests'
```

`uv` was not installed on the local worker. The following commands invoke the same
`pytest` console-script that `uv run` dispatches after resolving the frozen dev
environment; they do not use `python -m pytest`.

| Platform | Command | Public stdout summary | Result |
| --- | --- | --- | --- |
| Windows | `& 'C:\Users\Chooo\Playground\Karajan\.venv\Scripts\pytest.exe' tests --collect-only -q` | `2748 tests collected in 1.55s` | passed |
| WSL Ubuntu | `/tmp/karajan-candidate-mode-qy6_mqo2/venv/bin/pytest tests --collect-only -q` | `2748 tests collected in 7.72s` | passed |
| Windows | `pytest.exe tests/runs/test_planning_execution.py tests/runs/test_routing_authorization.py -q --basetemp .cache\\terra-planning-execution-pytest-windows` | `49 passed in 15.03s` | passed |
| Windows | `ruff check backend tests` | `All checks passed!` | passed |
| Windows | `mypy backend/karajan` | `Success: no issues found in 147 source files` | passed |

The correction moves `tests/orchestration/test_planning_execution.py` into
`tests/runs/test_planning_execution.py` and restores flat imports from the existing
`tests/runs` fixture domain. It does not package `tests`, alter CI, or change product
code. The previous independent-review archive and its historical commands remain
unchanged.
