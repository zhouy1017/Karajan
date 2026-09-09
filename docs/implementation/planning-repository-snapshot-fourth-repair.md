# Planning snapshot fourth repair — #142

Date: 2026-09-08.  Repair candidate base: `248198f686cae714b7ade783e7912f195d0d0d8f`.

This is a narrow owner repair to the Planning snapshot/execution/bootstrap
boundary.  It does not change Reviewer, Host, Relay, Journal, Capacity,
qualification-producer, provider, or native controller behavior.  In
particular, it does not turn the local C/P checks below into a provider or
production-qualification claim.

## Original red findings repaired

1. The trusted factory now distinguishes an absent, post-#110/#111 snapshot
   ledger from a present ledger that is corrupt or aliased.  The former opens
   the existing-only execution/admission controller with no snapshot port;
   history reads and an already committed Run submission receipt recover
   without creating a ledger, artifact, Capacity reservation, Plan, or effect.
   `freeze_repository_snapshot` and `read_repository_snapshot` still fail
   closed with `PLANNING_REPOSITORY_SNAPSHOT_UNAVAILABLE`.  A present ledger,
   including a dangling symlink spelling, is still opened through the private
   existing-only validation and fails the factory if invalid.
2. `PlanningExecution._load` now checks the requested SQLite execution primary
   key against the embedded execution ID before any owner/Run/binding authority
   reconstruction.  The direct SQLite regression creates two same-owner
   executions, freezes both actual stores, substitutes B's full JSON row into
   A, and verifies every public A path rejects without writes, repair, Capacity
   activity, or B content.  Valid same-owner B history and wrong-principal
   rejection remain covered.
3. Snapshot command replay now returns the manifest reconstructed and verified
   by the immutable store, after exact equality with the saved command receipt.
   The ordinary replay and the final concurrent-receipt branch both reject a
   changed base SHA, file metadata, or snapshot ID with
   `PLANNING_REPOSITORY_SNAPSHOT_CHANGED`; neither rewrites the corrupted
   receipt.  Verification remains outside the short execution writer, so it
   does not hash files while that writer is held.

The prior factory-missing-ledger assertion was corrected to test the original
scoped behavior: the factory remains available for historical reads while each
snapshot operation remains unavailable.  The existing committed-snapshot test
also covers removal after publication under that same operation-level rule.

## Actual commands and results

All commands used the fixed artifact/tokenizer directory and
`PYTHONPATH=backend:tests:tests/projects:tests/runs:tests/candidates`.

```text
wsl.exe bash -lc "cd /mnt/c/Users/Chooo/Playground/Karajan/.cache/dg01-planning-20260908 && KARAJAN_GO_TOKENIZER_DIRECTORY=/mnt/c/Users/Chooo/Playground/Karajan/.cache/go-context-artifacts PYTHONPATH=backend:tests:tests/projects:tests/runs:tests/candidates /tmp/karajan-candidate-mode-qy6_mqo2/venv/bin/python -m pytest --basetemp=/tmp/karajan-142-fourth-linux-rerun tests/orchestration/test_planning_snapshot.py tests/runs/test_planning_execution.py tests/runs/test_planning_admission.py -q"
# 96 passed in 23.25s

C:/Users/Chooo/Playground/Karajan/.venv/Scripts/python.exe -m pytest --basetemp .pytest-planning142-windows-final-final tests/orchestration/test_planning_snapshot.py tests/runs/test_planning_execution.py tests/runs/test_planning_admission.py -q
# 80 passed, 16 scoped platform skips in 36.76s

C:/Users/Chooo/Playground/Karajan/.venv/Scripts/python.exe -m ruff check backend tests
# All checks passed

C:/Users/Chooo/Playground/Karajan/.venv/Scripts/python.exe -m mypy --platform linux backend/karajan
C:/Users/Chooo/Playground/Karajan/.venv/Scripts/python.exe -m mypy --platform win32 backend/karajan
# Success: no issues found in 151 source files (each)
```

The Linux and Windows suite remains the three original snapshot modules.  It
therefore retains their real SQLite, Git, Popen/process, network-connect,
Run/Project/cancel/handoff, immutable artifact, command-reservation and
receipt-loss checks.  Windows skips are only the documented platform/private
deployment cases (symlink/backslash/hard-link mechanics and Linux-private
factory cases); they are not treated as passes.
