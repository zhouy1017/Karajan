# Planning snapshot fifth repair — #142

Date: 2026-09-08.  Candidate parent SHA:
`dfef9328b116b8302226da66d5942b017b75901b`.

This repair addresses the fifth independent Standards/Spec review packet for
the Planning snapshot/execution/bootstrap boundary only.  The Standards report
was read separately from the Spec report.  Its P1 finding was source-repository
Git configuration executing a helper for a missing promisor blob; its P3 test
fixture-duplication observation remains nonblocking.  The Spec report's two P2
findings were execution JSON substitution of another same-owner Run/intent and
post-factory private path substitution.  The reports are findings, not evidence
of passing behavior or CI approval.

## Repair and observed behavior

- The Git reader now builds a fresh bare reader with controller-written config
  and exposes the registered source only as an object alternate.  It never
  invokes Git with the source working tree/configuration.  The POSIX regression
  creates a real missing promisor blob and a repository-local
  `protocol.ext.allow=always` plus `ext::touch` helper.  It observes the helper by
  its local marker (no network endpoint or credentials are configured) and
  requires the marker to remain absent while the snapshot fails closed.
- `PlanningExecution._load` now compares every durable indexed execution field
  (`id`, `run_id`, `intent_id`, and `state`) with its embedded JSON before any
  owner, Run, intent, binding, freeze, replay, read, or submission path can
  reconstruct authority.  The direct SQLite regression retains A's execution
  ID, substitutes B's same-owner Run/intent/binding, recomputes the binding
  digest, and proves A has zero new snapshot rows/files or Capacity effects;
  B's distinct-base valid path and wrong-owner rejection remain valid.
- A production `PlanningRepositorySnapshotStore` now rechecks its private root,
  ledger and artifact spelling immediately before every SQLite connection,
  freeze/publish/sync, and blob materialization.  A retained trusted factory is
  exercised after replacement of the ledger, artifact directory, and private
  root with compatible symlinked copies.  Existing reads and command replay
  freeze reject before use, the external copies remain byte-identical, and the
  original inode/directory restores to a valid full-content read.  These
  private-owner/mode checks are Linux-specific; Windows retains scoped skips
  because it cannot provide the POSIX private-mode/symlink setup.

## Original acceptance coverage

| Original #142 acceptance condition | Current behavioral evidence |
| --- | --- |
| Real registered Project/Run/intent snapshot | Factory registered-base freeze/read and real SQLite assertions. |
| Replay, reopen, immutable historic bytes and concurrent recovery | Existing freeze receipt, handoff, cancellation, lost-reply and concurrent-store regressions. |
| Wrong identity/tampering is read-only and fails closed | Direct A/B same-owner SQLite substitution, manifest/blob/receipt checks, wrong-owner paths. |
| Approved base-tree scope rejects hostile entries without source mutation | Real Git base enumeration, symlink/path/limit regressions, plus missing-promisor `ext::` helper marker. |
| Snapshot activity has no admission/native/provider/plan effects | Existing real Capacity/Plan/project-record counters and Popen/network-boundary tests; no provider was configured. |
| Prior #110/#111 behavior remains | The three Planning snapshot/execution/admission modules retain their historic recovery, source, cancel, publication and command-reservation cases. |

## Commands and actual results

The WSL command used Ubuntu 24.04, Python at
`/tmp/karajan-candidate-mode-qy6_mqo2/venv/bin/python`, and the pinned
`/mnt/c/Users/Chooo/Playground/Karajan/.cache/go-context-artifacts` tokenizer
directory.  Windows used `C:/Users/Chooo/Playground/Karajan/.venv` and the same
artifact directory under its native spelling.

```text
WSL: KARAJAN_GO_TOKENIZER_DIRECTORY=.../go-context-artifacts \
  PYTHONPATH=$PWD/backend /tmp/karajan-candidate-mode-qy6_mqo2/venv/bin/python \
  -m pytest tests/orchestration/test_planning_snapshot.py \
  tests/runs/test_planning_execution.py tests/runs/test_planning_admission.py -q
# 98 passed in 23.93s

Windows: C:/Users/Chooo/Playground/Karajan/.venv/Scripts/python.exe -m pytest \
  tests/orchestration/test_planning_snapshot.py \
  tests/runs/test_planning_execution.py tests/runs/test_planning_admission.py -q
# 81 passed, 17 scoped skips in 37.22s

C:/Users/Chooo/Playground/Karajan/.venv/Scripts/python.exe -m ruff check .
# All checks passed

C:/Users/Chooo/Playground/Karajan/.venv/Scripts/python.exe -m mypy --platform win32 backend/karajan
WSL: /tmp/karajan-candidate-mode-qy6_mqo2/venv/bin/python -m mypy --platform linux backend/karajan
# Success: no issues found in 151 source files (each)
```

This is C/P evidence only.  No official provider, model credential, native
Host, Go Journal receiver, remote CI, push, PR, merge, or real qualification
was invoked; those remain `not_run` rather than being inferred from source or
local static checks.
