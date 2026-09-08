# Planning repository snapshot: trusted Git and retained authority repair

Candidate: successor to `b43da8e2fce3c3f2bc7d07598bbf89fc151a8635`.

## Scope

This #142 repair changes only the snapshot producer, its
`PlanningExecution` factory boundary, owned regressions, and this evidence
record. It does not change `planning_admission.py`, Capacity, Host, native
runtime, Relay, Journal, qualification, CI, provider, or credential code.

## Repaired receiving boundaries

- Git is selected from fixed controller installation locations as an absolute
  regular executable; this receiver never uses `PATH` or a bare command name.
  Its `cwd` is the fresh controller-created bare reader, not the repository.
  This preserves the no-source-config, no-promisor/helper, alternate-object,
  SHA-1/SHA-256, and nested-path reader rules while preventing Windows
  current-directory/repository `git.exe` shadowing.
- `.git` pointer and `commondir` metadata are opened as regular files and read
  with an 8 KiB bound before UTF-8 decoding. Oversize metadata fails as
  `PLANNING_SNAPSHOT_BASE_UNAVAILABLE`; it cannot allocate an arbitrary file
  before the subprocess deadline.
- A trusted factory captures the bootstrap digest, validated settings document,
  and `(st_dev, st_ino)` identities for the protected descriptor, state root,
  and all authority stores. Every retained-factory transaction revalidates the
  descriptor and those identities. Normal SQLite writes retain identity; an
  alias, missing file, hardlink/reparse substitution, or pathname replacement
  fails closed with `PLANNING_ADMISSION_BOOTSTRAP_CHANGED`. The existing final
  Execution → Run → Project guard therefore also checks current private
  authority after slow Git preparation and before manifest publication.
- The Windows leader-exit regression now holds a read-only process handle and
  observes `WaitForSingleObject(..., 0)`. It first observes an intentionally
  live control process, then observes the exact inherited-stdout child through
  the held handle. It sends no signal and does not claim daemon-thread status
  alone proves physical cleanup. The still-limited scope is this owned reader;
  unrecoverable OS cleanup remains reported as unavailable.

## Targeted evidence

- Windows main `.venv`: `tests/orchestration/test_planning_snapshot.py` with
  a fresh repository-local base temp: `15 passed, 7 skipped` on 2026-09-08.
  The new Windows-only regression compiles a tiny repository-local `git.exe`,
  launches the real reader from that repository cwd with that repository first
  in `PATH`, verifies a normal commit read, and verifies that executable never
  writes its marker. It is skipped only on non-Windows.
- Linux shared candidate-mode venv: the protected public factory creates an
  unfrozen execution, copies its compatible execution DB, performs real Git
  preparation, cancels the original execution, substitutes the pathname with
  the old DB symlink, then releases preparation. The first freeze fails closed;
  the snapshot ledger has zero rows, Capacity is unchanged, and restoration
  proves the original cancellation remains durable. This regression passed in
  `3.31s` on 2026-09-08.
- Required three-module reruns with
  `KARAJAN_GO_TOKENIZER_DIRECTORY` fixed to the existing `go-context-artifacts`
  cache passed: Windows main `.venv` `92 passed, 22 skipped in 52.83s`; Ubuntu
  shared candidate-mode venv `113 passed, 1 skipped in 28.98s`. The Linux
  first attempt is retained as an environment failure: its subprocess did not
  inherit `PYTHONPATH`, so one killed-publisher fixture could not import
  `karajan`; it is not counted as product evidence.
- `python -m mypy backend --platform win32` and the shared Linux equivalent
  each reported `Success: no issues found in 151 source files`. Changed-scope
  `ruff check backend tests docs` passed. Full `ruff check .` remains blocked
  by preserved untracked historical pytest fixture scripts under
  `.pytest-planning142-local-snapshot*`; they are not source changes and were
  left intact rather than cleared.

After the final controlled-`PATH` tightening, fresh recorded reruns remained
green: Windows `92 passed, 22 skipped in 55.15s`; Linux `113 passed, 1 skipped
in 30.32s`. Both platform mypy runs again reported 151 clean source files.
- The old `b43` worktree was prepared for red comparison. Its direct attempt
  using the new regression was inconclusive because the test runner resolved a
  preloaded current package; it is not evidence that the static finding was
  reproduced. No static report is relabelled behavioural red here.

These are C/P local observations only. No native execution, provider,
credential, official Commander qualification, real service, PR, merge, push,
or Issue-state action is claimed.
