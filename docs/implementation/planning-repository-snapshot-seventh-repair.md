# Planning repository snapshot: SHA-256 and literal-alternate repair

Candidate: this local successor to `01a23ef5c16bd82ba3b1b03f537f74f4efd4d195`.

## Scope and decision

This repair stays within #142's Planning snapshot producer, its public
PlanningExecution integration tests, and owned evidence. It does not modify
admission, Capacity, Host/native, Go Relay/Journal, qualification, provider,
credential, CI, or runtime-pin code. `tests/runs/test_planning_admission.py`
was not modified.

The two blocking independent-review findings were real:

1. `_git_tree_pair` sends child OIDs through stdin, so argument-length
   inference made an SHA-1 bare reader for an SHA-256 registered base.
2. A raw `GIT_ALTERNATE_OBJECT_DIRECTORIES` value lets Git parse a POSIX colon
   in a legitimate repository pathname as a second alternate.

The reader now derives the object format from the already validated registered
base OID once in `freeze`, and passes it explicitly to every commit, tree,
blob, and stdin-batch read. The alternate directory is one Git C-quoted path
member: backslash and quote are escaped, controls use Git's three-digit octal
form, and the full member is quoted. This is deliberately not JSON escaping.
The reader remains a fresh bare repository with source configuration, replace
refs, hooks, promisor configuration, transport, and caller Git environment
excluded.

## Bounded reader finding

The pre-repair limited reader did `stdout.read(limit + 1)` before
`wait(timeout=10)`. A no-output child could therefore block before the wall
time bound was applied. The repair starts a reader thread, enforces the fixed
monotonic deadline while it waits for at most `limit + 1` bytes, then waits
only for the remaining deadline. On POSIX the fresh session's process group is
killed on either timeout or byte excess; on Windows the owned process tree is
terminated with `taskkill /T /F`. The test fixture launches an actual temporary
`git` replacement that spawns a sleeping child. One variant emits no output;
another emits 101 bytes against a 100-byte cap. Both prove prompt rejection
and disappearance of the exact owned child PID. This applies to the snapshot
reader boundary; it does not make claims about unrelated subprocess owners.

## New behavioral evidence

- The public `ProjectRegistry -> RunPlanner -> PlanningExecution` producer
  creates an SHA-1 and an SHA-256 repository with approved nested `src/` and
  `tests/` entries, freezes, validates original binding/base/mode/digest and
  bytes, then reopens the execution/store and reads the same bytes.
- The POSIX public producer uses a registered root whose actual name contains
  `:`, `"`, and `\\`; freeze succeeds. The quoted alternate therefore retains
  filesystem identity instead of admitting a second object directory.
- The valid-zlib, same-size loose-object corruption regression covers commit,
  tree, and blob objects for SHA-1 and SHA-256. It continues to reject changed
  content under the original OID before any snapshot publication.
- Existing root-alias, hostile Git environment/local config, replace-ref,
  missing-promisor, traversal, link, publication, process-death recovery,
  manifest/SQL seal, and historical-read regressions remain in the required
  modules and were run below.

## Commands and results — 2026-09-08

All Windows commands used
`PYTHONPATH=backend;tests;tests/projects;tests/runs;tests/candidates` and
`C:/Users/Chooo/Playground/Karajan/.venv/Scripts/python.exe`.

| Evidence | Command/result |
| --- | --- |
| Focused Windows | `pytest tests/orchestration/test_planning_snapshot.py tests/runs/test_planning_execution.py -k 'public_snapshot_flow or freeze_rejects_same_length_replaced_loose_git_object' -q --basetemp=.pytest-planning142-gap-target-win-2`: `8 passed, 1 skipped` (the colon-root check is Linux-only). |
| Focused Linux | Ubuntu, shared `/tmp/karajan-candidate-mode-qy6_mqo2/venv/bin/python`: equivalent selection plus `bounded_git_reader`: `11 passed`. |
| Required Windows modules | `pytest tests/orchestration/test_planning_snapshot.py tests/runs/test_planning_execution.py tests/runs/test_planning_admission.py -q --basetemp=.pytest-planning142-full-win-evidence2`: `89 passed, 21 skipped in 44.01s`. Skips are the named POSIX-link/path/process-group/transport cases and Linux-only deployment modes. |
| Required Linux modules | Ubuntu: `KARAJAN_GO_TOKENIZER_DIRECTORY=/mnt/c/Users/Chooo/Playground/Karajan/.cache/go-context-artifacts PYTHONPATH=backend:tests:tests/projects:tests/runs:tests/candidates /tmp/karajan-candidate-mode-qy6_mqo2/venv/bin/python -m pytest tests/orchestration/test_planning_snapshot.py tests/runs/test_planning_execution.py tests/runs/test_planning_admission.py -q --basetemp=/tmp/planning142-full-linux-current-with-tokenizer`: `110 passed in 27.58s`. |
| Ruff | `python -m ruff check .`: passed. |
| mypy | `python -m mypy backend --platform win32` and `python -m mypy backend --platform linux`: each `Success: no issues found in 151 source files`. |

The first Linux full invocation omitted the documented tokenizer environment;
only two admission tests failed with `KeyError: KARAJAN_GO_TOKENIZER_DIRECTORY`
before their assertions. The rerun above supplies the fixed existing cache and
is the applicable result. A first redirected Windows evidence invocation put
live log files inside `--basetemp`, so pytest failed while clearing that base
before product test setup; it is not counted as product evidence. Its preserved
output is quarantined in the private untracked test directory.

## Scope limitations

This is C/P local evidence for the snapshot producer. It does not claim native
execution, real provider credentials, qualification, Capacity admission,
Host/native activation, Go Journal effects, model calls, plan submission, or
real-source S eligibility. No push, PR, merge, Issue state, or GitHub action
was performed.
