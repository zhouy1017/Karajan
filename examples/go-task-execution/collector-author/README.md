# Collector / Candidate author evidence

Issue: https://github.com/zhouy1017/Karajan/issues/90. Worktree: `codex/m3-go-task-execution-v2`, starting at `858bd5a`.

The fixed child can hand its own `GoTaskResult` to `ApprovedGoCollector`. No public request accepts reports, paths, prompt, Candidate policy or stop claims. The compiler checks the original approved Workspace, input/source/grant identity, complete current Journal, actual captured projection and stop result. Freeze policy/check argv/environment hashes and author Attempt/context/Profile/family come from the immutable approved records.

Capture metadata commits before Candidate freeze. The collection order is operation, Run, Project, Host, Candidate; fresh controller source is checked twice. A failed current collection gate preserves the capture receipt but creates no Candidate. Capacity startup expiry is not used as a deadline to capture already stopped files. The existing reserved-route gate may separately reject stale approval/qualification/estimate facts. That does not erase the captured observation.

`CandidateStore.lookup_projection_capture` compares the complete Freeze request and complete expected baseline-plus-captured tree identity in a read-only transaction. It selects an exact historical candidate, including an older series revision, or returns no match; multiple exact commits fail as ambiguous. It does not read the source repository, Git objects or artifact files. This is evidence of a commit, not evidence that artifact bytes remain available today. Recovery may link that historical ID into the operation while preserving cancellation; it never restarts a native worker or retries a missing freeze from mutable files.

`CandidateStore(existing_only=True)` never initializes or migrates storage. Reconnection uses existing-only SQLite modes and validates the required tables/columns. Missing/empty ledgers fail without creating replacements. Default initial bootstrap remains available for owner setup.

## Results and reproducible commands

- Windows: **135 passed, 3 POSIX-only skips** (`final-windows.xml`), complete Candidate tests plus Collector tests.
- Linux/WSL: **63 passed, 0 skips** (`final-linux.xml`), projection, baseline, exact recovery and Collector tests.
- Ruff and formatting passed for the 5 owned files; mypy passed for the 3 product files with `--follow-imports=silent`.
- No provider or real credential was used. Real Git/CAS/SQLite/Journal behavior is exercised; qualification, planning admission, Host current-child authority, native result and current source callback are explicit test doubles in Collector unit cases. These results do not qualify native termination or model capability. The consumer's separate actual Host/native/HTTP fixture test must provide that integration evidence.

```text
python -m pytest tests/candidates tests/runs/test_go_task_collector.py -q -o "pythonpath=backend tests/candidates tests/runs tests/projects tests/routing tests/capacity tests/web" --junitxml=.cache/collector-author/final-windows.xml

python -m pytest tests/candidates/test_capture_recovery.py tests/candidates/test_projected_capture.py tests/candidates/test_baseline_materialization.py tests/runs/test_go_task_collector.py -q -o "pythonpath=backend tests/candidates tests/runs tests/projects tests/routing tests/capacity tests/web" -p no:cacheprovider --basetemp /tmp/karajan-collector-final --junitxml=.cache/collector-author/final-linux.xml

python -m ruff check backend/karajan/candidates/store.py backend/karajan/candidates/_capture_lookup.py backend/karajan/orchestration/go_task_collector.py tests/candidates/test_capture_recovery.py tests/runs/test_go_task_collector.py
python -m ruff format --check backend/karajan/candidates/store.py backend/karajan/candidates/_capture_lookup.py backend/karajan/orchestration/go_task_collector.py tests/candidates/test_capture_recovery.py tests/runs/test_go_task_collector.py
python -m mypy backend/karajan/candidates/store.py backend/karajan/candidates/_capture_lookup.py backend/karajan/orchestration/go_task_collector.py --follow-imports=silent
```

`freeze.json` binds all source/test hashes and original red/green/final XML bytes. The red files record missing public interfaces before their implementation. Formatting occurred before final runs; intermediate green reports are historical, not a claim that later code was tested at those earlier hashes.

Candidate checks and independent review remain missing. Empty Reviewer authorization does not pass a gate, release capacity or complete a Run. An authorized plan with no ordinary validation check cannot be compiled to Candidate Policy; no fallback argv is invented. Future validation execution, qualified Reviewer continuation, repairs and delivery belong to subsequent work.
