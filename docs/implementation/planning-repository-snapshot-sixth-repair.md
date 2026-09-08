# Planning snapshot sixth repair — #142

Date: 2026-09-08.  This repair started from candidate
`a29125a864247a4de6b6d2c8ad3b25d3ac8336fd`.  The complete supplied source
packet was `969743` bytes with SHA-256
`4339b17e1997973f939611a604e0c5b948085fc4915bbe4da94ac3330d8971cd`.
Both independent reports were read separately:
`dg01-commander-20260908/planning142-standards-static6-final.md` and
`dg01-commander-20260908/planning142-spec-static6-final.md`.  They identified
two P2 blockers; their earlier approval status was not applied to this changed
candidate.

## Repair

- Base materialization now reads the registered commit, root/selected trees,
  and selected blobs by their original object IDs.  Each bounded object is
  checked with the Git object hash of `"<type> <size>\\0<content>"` before its
  bytes can receive the separate SHA-256 CAS seal.  The hash algorithm follows
  the registered base ID format (SHA-1 or SHA-256); the isolated bare reader is
  configured from that controller-held format and never reads source Git
  configuration, refs, replace refs, or transports.  A bounded batch is used
  only for up to two known child tree IDs; it validates the returned names,
  types, sizes and object hashes.
- Linux CAS publication uses `renameat2(RENAME_NOREPLACE)`: a flushed temporary
  file becomes the final name atomically, with no two-hardlink interval.  An
  unsupported POSIX kernel fails closed rather than returning to a link/unlink
  fallback.  Windows retains its existing write-through no-replace move.  The
  artifact is still checked and synced before its SQLite reference commits.
- The object-corruption regression replaces the actual loose blob, tree, and
  commit pathname with a valid zlib stream carrying same-length altered bytes;
  it uses no replace ref or repository config.  Freeze rejects before any
  snapshot/file row or CAS entry, then succeeds once the original object is
  restored.  The execution-path case is represented by a real child controller:
  it pauses after atomic publication, only that owned child is killed, and cold
  stores recover the original command receipt and a second valid execution with
  the same content.  The process-kill case does not claim physical power-loss
  coverage.

## Original #142 acceptance coverage and evidence

| Original acceptance condition | Current evidence | Level / limit |
| --- | --- | --- |
| Registered Project/Run/intent base-tree freeze | Real Project/Run/execution fixture, verified commit/tree/blob chain, normal-base recovery | C/P; local Git only |
| Corrupt/missing/hostile source rejects without source mutation | Same-length actual loose object substitutions, malformed entries, path/size and missing-promisor regressions | C/P; no source repair/download |
| Immutable replay, reopen, concurrent recovery | Existing receipt/reopen/cancel/lost-reply cases plus owned-child publication death recovery | C/P; process termination, not power loss |
| No foreign alias or altered bytes become history | CAS type/size/content checks, single-name publication, persistent-alias rejection and SQL assertions | C/P; filesystem primitives scoped by OS |
| No Admission/Capacity/provider/native side effects | Existing controller boundary/count assertions stayed intact | C/P; provider/credential/native/Host/Journal qualification remains not_run |

## Commands and actual results

```text
WSL Ubuntu, Python 3.12.3 at /tmp/karajan-candidate-mode-qy6_mqo2/venv/bin/python:
KARAJAN_GO_TOKENIZER_DIRECTORY=/mnt/c/Users/Chooo/Playground/Karajan/.cache/go-context-artifacts \
PYTHONPATH=backend:tests:tests/projects:tests/runs:tests/candidates \
python -m pytest --basetemp=/tmp/karajan-142-seventh-linux-final \
  tests/orchestration/test_planning_snapshot.py \
  tests/runs/test_planning_execution.py tests/runs/test_planning_admission.py -q
# 102 passed in 24.35s

Windows, C:/Users/Chooo/Playground/Karajan/.venv/Scripts/python.exe:
python -m pytest --basetemp .pytest-planning142-windows-seventh-final \
  tests/orchestration/test_planning_snapshot.py \
  tests/runs/test_planning_execution.py tests/runs/test_planning_admission.py -q
# 84 passed, 18 scoped skips in 37.26s

Windows: python -m ruff check .
# All checks passed
Windows: python -m mypy --platform win32 backend/karajan
WSL: python -m mypy --platform linux backend/karajan
# Success: no issues found in 151 source files (each)
```

These are C/P evidence for this local candidate.  No provider, credentials,
real service qualification, push, PR, merge, remote CI, native Host, or Journal
receiver was invoked; those S/G claims remain `not_run`.  Fresh root checks,
dual review, and CI are still required for the resulting commit.
