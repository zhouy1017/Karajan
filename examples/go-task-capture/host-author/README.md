# RunnerHost current fence guard author evidence

Base `1534825`; owner files `backend/karajan/execution/host.py` and `tests/execution/test_capture_fence.py`. The public seam was agreed with root before implementation. No provider or credential was used.

`current_fence_guard(attempt_id, *, fence, authorization_ref)` validates persisted manifest/activation integrity and their matching identity/budget, the accepted supervisor state and current enabled control. Missing execution remains the existing KeyError convention; insufficient start state or withdrawn/mismatched control raises LaunchDenied. Its real SQLite `BEGIN IMMEDIATE` is query-only and spans the caller's capture. It creates no success/start fact and does not alter usage or business state.

The guard does not require the trusted RunnerHost supervisor to stop: actual native namespace stop is independently required by the Collector. Activation expiry remains the start TTL, not an expiration of collection rights for an already running attempt. The guard is neither Run authorization nor Candidate acceptance. A `finished` row without an exit code is the supervisor's denied-start path and is not accepted as a successful native execution identity merely because control is re-enabled.

Actual TDD: the real live Python writer positive case failed first because the interface did not exist (`current-writer-red.xml`, 1 failed), then passed (`current-writer-green.xml`, 1 passed). The later boundary suite reached 17 passes (`author-final.xml`), followed by one additional real elapsed-start-TTL case. These are author results, not independent review.

Final verification:

- Windows: `python -m pytest tests/execution tests/orchestration -q --junitxml=.cache/capture-fence-evidence/windows-final.xml` — 112 passed, 1 existing POSIX-only canary skipped / 96.73 s. The new Host file contributes 18 cases.
- WSL: existing isolated Python, `PYTHONPATH=<worktree>/backend`, `python -m pytest -p no:cacheprovider <worktree>/tests/execution/test_capture_fence.py -q --junitxml=<worktree>/.cache/capture-fence-evidence/wsl-final.xml` — 18 passed / 11.24 s.
- Ruff and format checks: both owner files passed. Mypy: `--follow-imports=silent backend/karajan/execution/host.py`, passed.

Tests use real temporary SQLite and real local Python supervisor/child processes. Coverage includes live and normally finished execution, no preparation/activation or only crash-before-spawn intent, wrong caller/current fence or authorization, cancellation/disabled control, historical result replay not reauthorizing capture, blocking concurrent public control/cancel writes, body failure unlocking without changing usage, detached evidence and strict boolean-fence rejection. Every live fixture cleans up through the public Host cancel API.

Historical red test source was not separately captured; raw JUnit retains the actual first missing-method error. The final source hashes and evidence hashes are recorded in `freeze.json`. No Git commit or product changes outside the two owner files occurred in this author task.
