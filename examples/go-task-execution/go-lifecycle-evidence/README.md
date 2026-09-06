# Issue 90 lifecycle bridge author evidence

Scope: the original admission operation now owns fixed launch metadata, activation/Host-preparation guards, historical cleanup observations, a write-once internal capture receipt and an exact owned Candidate link. `RunnerHost.initialize_control_once` only inserts absent control; an existing disabled or superseded control cannot be re-enabled through this port. No new execution-state database, grant creation, Capacity refund, provider call or credential read is performed by these methods.

`GoExecutionIntents.open_existing` reuses the public `read_operation` owner/history path. An existing execution is reconstructed with its validated original source; only an unprepared operation invokes the injected lazy source compiler. Actual execution still requires the consumer's current business and Host guards.

The public `CandidateStore.lookup_projection_capture` port is implemented by the Collector author. Capture tests use that real Store, immutable Git/CAS baseline, real approval/admission/Journal state and an explicitly synthetic native-result/Host-authority producer. They do not prove actual native stop, real model behavior or Profile qualification. The root's fixed-child integration owns those checks.

## Actual red/green

- `control-red.xml`: 9 failures because `initialize_control_once` was absent. `control-green.xml`: 9 passed, 0.35 seconds.
- `intent-red.xml`: first public lifecycle input failed because `GoLaunchSpec` was absent. `intent-green.xml`: first guard case passed.
- `lifecycle-before.xml`: 2 failures, 13 passed. A fresh `ProcessSpec.document()` returned tuple argv while persisted JSON returned a list; identical launch replay conflicted and first-return history differed. `intent-before-launch-canonicalization.py.txt` preserves the original source. The launch is now canonically detached before its first return. `lifecycle-green.xml`: 15 passed, 16.10 seconds.
- `lifecycle-capture.xml`: 3 test-harness failures, 23 passed. The tests referenced a nonexistent `CandidateStore.database` attribute; no product defect. The checks now use its real `candidates.sqlite` path. This failure evidence is retained.
- `windows-final.xml`: 58 passed, 52.89 seconds.
- `wsl-final.xml`: the same 58 passed, 26.80 seconds.

The 58 include 26 new lifecycle cases, all 23 existing intent cases and 9 Host initialization cases. They cover cancellation serialization, fixed expiry/replay, no resource-ledger writes, original source restoration, unavailable/wrong grant separation, persistent unknown sends, exact capture identity and late Candidate linkage preserving cancellation. No live provider was called.

## Commands

Working directory is the repository root of this worktree. Windows uses the repository `.venv/Scripts/python.exe` with `PYTHONPATH=backend`.

```text
python -m pytest tests/runs/test_go_lifecycle.py tests/runs/test_go_execution_intent.py tests/execution/test_control_initialization.py -o "pythonpath=backend tests/runs tests/projects tests/execution" -q --junitxml=.cache/go-lifecycle-evidence/windows-final.xml
wsl.exe --cd /mnt/c/Users/Chooo/Playground/Karajan/.cache/go-task-execution-v2 -e /tmp/karajan-candidate-mode-qy6_mqo2/venv/bin/python -m pytest tests/runs/test_go_lifecycle.py tests/runs/test_go_execution_intent.py tests/execution/test_control_initialization.py -o "pythonpath=backend tests/runs tests/projects tests/execution" -p no:cacheprovider -q --junitxml=.cache/go-lifecycle-evidence/wsl-final.xml
python -m ruff check backend/karajan/orchestration/go_execution_intent.py tests/runs/test_go_lifecycle.py tests/execution/test_control_initialization.py
python -m ruff format --check backend/karajan/execution/host.py
python -m mypy backend/karajan/orchestration/go_execution_intent.py backend/karajan/execution/host.py
```

Ruff and mypy passed. The Host file is shared with the root's existing-only opening changes; only `initialize_control_once` is authored here. Source hashes in `freeze.json` bind the tested bytes; subsequent root-owned Host changes require their own hash/update validation.
