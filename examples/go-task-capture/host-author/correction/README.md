# Existing-ledger correction

`HOST-CAPTURE-001` was independently found by the Host reviewer: after an existing Host ledger was retained under another name, entering `current_fence_guard` created an empty replacement SQLite file before failing. The independent original source and red test result remain in `.cache/host-capture-review/host-before.py.txt` and `missing-ledger-before.xml`. The earlier author freeze and XML one directory above remain unchanged.

Only the guard now opens SQLite with `Path.as_uri() + '?mode=rw'`; the private connection helper defaults to its prior create-capable behavior for all other callers. `BEGIN IMMEDIATE` and `PRAGMA query_only=ON` still cover the yielded body. Missing-ledger failure propagates as `sqlite3.Error`, without creating a replacement ledger. No new authority, state mutation or Host dispatch behavior was added.

Author red: two real missing-ledger cases failed, while an existing-ledger control passed. The paths include spaces, `#`, `%` and Unicode to verify URI encoding. Correction green: these three cases plus the unmodified independent missing-ledger case passed. Windows execution regression: 65 passed. WSL capture suite: 21 passed. Ruff, formatting and mypy on the owned files passed. Exact counts and source hashes are recorded in `freeze.json`; these are author correction evidence, separate from the reviewer's subsequent independent full run.

Commands:

```text
python -m pytest tests/execution/test_capture_fence.py -k ledger -q --junitxml=.cache/capture-fence-evidence/correction/author-red.xml
python -m pytest tests/execution/test_capture_fence.py .cache/host-capture-review/test_host_capture_independent.py -o "pythonpath=backend tests/execution" -k ledger -q --junitxml=.cache/capture-fence-evidence/correction/ledger-green.xml
python -m pytest tests/execution -q --junitxml=.cache/capture-fence-evidence/correction/windows-final.xml
PYTHONPATH=<worktree>/backend:<worktree>/tests/execution python -m pytest -p no:cacheprovider <worktree>/tests/execution/test_capture_fence.py -q --junitxml=<worktree>/.cache/capture-fence-evidence/correction/wsl-final.xml
python -m ruff check backend/karajan/execution/host.py tests/execution/test_capture_fence.py
python -m ruff format --check backend/karajan/execution/host.py tests/execution/test_capture_fence.py
python -m mypy backend/karajan/execution/host.py
```
