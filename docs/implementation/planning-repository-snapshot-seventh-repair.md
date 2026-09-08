# Planning repository snapshot: SHA-256 and literal-alternate repair

Candidate: local successor to `772b50b6575821b4b7f3d16ecebc9d739f01b5cc`.

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

## Bounded reader finding and leader-exit repair

The earlier limited reader did `stdout.read(limit + 1)` before
`wait(timeout=10)`. A no-output child could therefore block before the wall
time bound was applied. The first repair moved the read into a thread, but its
cleanup still had a real leader-exit hole: it ran group/tree cleanup only while
`process.poll()` was `None`, then could call buffered `stdout.close()` while
that reader held the stream lock.

The actual receiving-boundary fixture now makes the temporary `git` leader
spawn an inherited-stdout child and exit immediately. Before this repair its
0.2-second deadline took 2.03 seconds (the child only slept two seconds), so
this is a reproduced failure, not an unrun claim. On POSIX cleanup always
signals the dedicated fresh-session group, including after its leader exits.
On Windows the reader creates the Git process suspended, assigns its process
handle to a private unnamed Job Object, then resumes its primary thread. This
removes the child-before-assignment race and preserves ownership after the
leader exits; `TerminateJobObject` ends the exact Job rather than using a
leader-PID `taskkill /T` tree walk. A pending Windows synchronous pipe read is
cancelled before its bounded join. On both platforms `stdout.close()` happens
only after the reader is observed stopped. If Job assignment, termination,
pipe cancellation, wait, or reader exit cannot be proved, the boundary returns
`PLANNING_SNAPSHOT_GIT_UNAVAILABLE`, does not close the contended stream, and
does not report a successful read or physical cleanup.

The regression records the child PID; on POSIX it also records and checks that
the child's original process group is the original Git leader PID. It observes
the elapsed bound, exact child disappearance, and reader-thread termination.
The Windows batch wrapper is routed only because bare `git` CreateProcess
resolution does not select `.cmd`; the real `_git` Popen, pipe, Job, deadline,
and cleanup path are exercised. This remains a claim about this owned snapshot
reader boundary, not unrelated subprocess owners.

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
| Leader-exit red | Ubuntu: `... python -m pytest tests/orchestration/test_planning_snapshot.py -k reaps_child_after_its_leader_exits -q --basetemp=/tmp/karajan-142-leader-exit-red`: `1 failed, 19 deselected in 2.88s`; the asserted 0.2-second boundary measured 2.03 seconds before this repair. |
| Leader-exit green | Ubuntu: the same shared Python, `-k bounded_git_reader`: `3 passed, 17 deselected in 0.90s`; Windows main `.venv`, `-k reaps_child_after_its_leader_exits`: `1 passed, 19 deselected in 0.50s`. Both runs used fresh private base temps outside the repository base temp. |
| Required Windows modules | Windows main `.venv`, fixed tokenizer cache, and fresh external `--basetemp=C:/Users/Chooo/Playground/Karajan/.cache/pytest-142-leader-final-win-3`: `90 passed, 21 skipped in 65.10s`. Skips are the named POSIX-link/path/process-group/transport cases and Linux-only deployment modes. Standard output/error logs were outside that base temp. |
| Required Linux modules | Ubuntu: `KARAJAN_GO_TOKENIZER_DIRECTORY=/mnt/c/Users/Chooo/Playground/Karajan/.cache/go-context-artifacts PYTHONPATH=backend:tests:tests/projects:tests/runs:tests/candidates /tmp/karajan-candidate-mode-qy6_mqo2/venv/bin/python -m pytest tests/orchestration/test_planning_snapshot.py tests/runs/test_planning_execution.py tests/runs/test_planning_admission.py -q --basetemp=/tmp/karajan-142-leader-final-linux`: `111 passed in 28.42s`. |
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
