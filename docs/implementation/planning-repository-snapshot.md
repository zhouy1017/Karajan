# Planning repository snapshot

`PlanningRepositorySnapshotStore` freezes the registered Project repository's
`base_sha`, rather than a mutable checkout. It enumerates only the Run's
approved `authorization_ceiling.read_paths`; each entry can name one file or a
directory prefix. Only base-tree regular blobs are accepted. The sealed manifest
is keyed by the existing PlanningExecution binding digest; bytes are verified
SHA-256-addressed files in the protected state directory.

The public controller methods accept an execution ID, authenticated principal,
and command key only. They neither accept content nor a repository path. Missing
or historical snapshots remain unreadable for new native transport; existing
execution records still retain their prior read-only recovery semantics.

### Historical Commander handoff recovery

The v1 execution binding's term, principal, and profile are reconstructed from
the original durable planning intent: the controller-sealed creation identity,
not the mutable current `run.commander`.  Thus, after an approved public
`RunPlanner.propose_handoff` / `decide_handoff` changes the Commander term and
profile, a snapshot that was already committed remains readable and its original
freeze key can be replayed.  This holds both when the original snapshot commit
lost its PlanningExecution command receipt and when that receipt was saved.  A
later ProjectRegistry base/source transition still returns the old manifest and
bytes, with no new snapshot blobs or Capacity effects.  In contrast, an old
execution with no committed snapshot still fails its first freeze after the
handoff: fresh freezing rechecks the current intent/Commander authority.

This historical reconstruction does not accept execution JSON as authority. The
execution binding digest and every non-Commander identity field are still
matched against durable Run/intent state, so a tampered binding is rejected.

Production provisioning calls `provision_planning_repository_snapshots` using
the existing protected planning bootstrap. This creates the fixed manifest
ledger and CAS directory in the private state directory. The trusted factory
validates their original path spelling, link count, containment, and private
modes before opening either. The unmerged local `blobs.content` SQLite fixture
format is obsolete and intentionally has no fake migration; v1 execution
bindings and their historical read-only behavior remain supported.

## Evidence and limits

The snapshot source is the ProjectRegistry-recorded absolute repository root
and its recorded Git `base_sha`. Git runs with a minimal environment, disabled
replace objects, hooks, fsmonitor, credentials, and protocols. It never uses a
caller-selected worktree revision. The manifest records the existing execution
binding digest, requirement and authorization-ceiling digests, repository
identity/base, approved path digest, each regular blob's path/mode/size/digest,
the total byte count, and its own digest. A reader validates the whole manifest,
reference rows, and every CAS file before returning bytes; malformed, missing,
or changed state is rejected and is never repaired by a reader. WAL and foreign
keys protect the manifest/reference ledger.

Approved entries may be an exact file or a directory prefix. Absolute,
relative-alias, traversal, backslash, symlink, submodule, unsupported mode,
empty/unmatched approval, and more than 2,000 files or 8,000,000 bytes fail
closed. Blob sizes are queried before their bodies, and the reader checks the
regular CAS file's `st_size` against its manifest before a bounded
`manifest_size + 1` read. The registered root must be a non-aliased directory.
On POSIX, a publisher fsyncs both the temporary blob and its artifact directory
after linking/unlinking and before SQLite records references. On Windows, it
flushes the temporary blob, publishes it with same-directory `MoveFileExW` and
`MOVEFILE_WRITE_THROUGH` without replacement, then flushes the published blob
before SQLite records references. This is a platform-specific Windows commit
protocol; it does not claim that an injected I/O failure or the API contract
constitutes an observed physical power-loss recovery test. Windows reparse
behavior remains unsupported evidence, not a portability claim.

The CAS protocol permits only a competing publisher's brief two-link interval:
it waits for that temporary name to disappear, then requires a regular,
single-link file with exactly the original bytes. A persistent second hardlink
is still rejected. The manifest's repository identity, Git base object ID, and
approved-path digest must have their expected types and match the independently
sealed source digest in SQLite; recomputing the manifest's self-digest cannot
rebind them. A second independently stored manifest digest seals the complete
canonical manifest (including every path, mode, size, and blob digest); the
manifest self-digest remains only an internal consistency check. Git tree names
selected by an approved prefix are validated with exactly the reader protocol
rules before any CAS blob or reference is published, so a Git-valid backslash
name cannot consume an unreadable binding.

Before Git/CAS publication, PlanningExecution durably reserves the exact
`(principal, command_key, freeze execution_id)` identity. A lost reply leaves
that reservation pending; only that original command can recover and complete
it. The final reference commit holds Execution → Run → ProjectRegistry
authority and rechecks the Run-recorded Project revision. A real Project update
therefore waits through publication or makes a first freeze fail before
references are committed.

Candidate evidence for this snapshot-authority worktree is WSL Ubuntu with Python 3.12 and a private `/tmp`
pytest base directory: `tests/orchestration/test_planning_snapshot.py`,
`tests/runs/test_planning_execution.py`, and the protected-factory tests in
`tests/runs/test_planning_admission.py`. They cover actual ProjectRegistry /
RunPlanner / PlanningExecution provisioning, base bytes after worktree change,
reopen/replay, saved-command replay after CAS-file/manifest/deleted-ledger rejection without Capacity changes,
Git replace/environment poisoning, and repository-root aliases. This is local
production-bootstrap evidence only: the fixture's actual temporary
Qualification, Run, and Capacity ledgers are inspected. The protected factory
has no configured Journal, Host, native runtime, model adapter, or output
receiver, so no unconnected Journal is counted. Physical provider/native
observations are therefore unavailable (`not_run`), while the actual shared
process-creation boundary and sockets remain observable.

```bash
KARAJAN_GO_TOKENIZER_DIRECTORY=/mnt/c/Users/Chooo/Playground/Karajan/.cache/go-context-artifacts \
PYTHONPATH=backend:tests:tests/projects:tests/runs:tests/candidates \
/tmp/karajan-candidate-mode-qy6_mqo2/venv/bin/python -m pytest \
  --basetemp=/tmp/karajan-142-popen-linux-final \
  tests/orchestration/test_planning_snapshot.py \
  tests/runs/test_planning_execution.py tests/runs/test_planning_admission.py -q
```

On 2026-09-08, after the shared-Popen observer correction, this exact Linux
command completed with `89 passed in 22.85s`.
The first fresh Linux invocation omitted `KARAJAN_GO_TOKENIZER_DIRECTORY` and
failed two tokenizer-dependent admission tests; it is retained as an environment
failure, and the new basetemp rerun above is the applicable result. Windows used
the pinned `.venv/Scripts/pytest.exe`, the same three modules, and fresh
`.cache/142-popen-windows-final-rerun`, completing `75 passed, 14 skipped in 34.61s`.

## Original AC coverage

The independent static-review red candidate was
`ad7bd56dd27314ec1b3a27eb7098729b17bcb45f`: Standards recorded the selected
Git-path and unconnected-Journal P2s (plus nonblocking fixture duplication),
while Spec recorded command reservation, complete-manifest sealing, Project
publication revalidation, and connected zero-effect evidence P2s. Those red
findings are retained as provenance; predecessor test results do not override
them. The preserved review-fix attribution is
`87d8ed88345f7724f3d7808dabcb82f0f4771c02` (`fix: seal planning snapshot
publication`). Its PR #148 remote Ubuntu CI run `34218759910`, job
`102036772358`, failed at 2026-09-08 11:05:51 UTC before tests: Linux mypy
reported that its unguarded Windows FFI references lacked `ctypes.WinDLL`,
`ctypes.get_last_error`, and `ctypes.WinError`. The same push run
`34218754710` failed likewise. This is a candidate failure, not infrastructure
or a test-timeout result; the follow-up uses platform-guarded, typed ctypes
bindings while retaining the Windows write-through no-replace publication
protocol. CLI8 then ran `mypy --platform linux backend/karajan` and
`mypy --platform win32 backend/karajan`; each reported `Success: no issues
found in 151 source files` on the platform-guarded follow-up. That type-check
result repairs the original WindowsAPI CI failure; it is not a replacement for
fresh review or CI of a later candidate.

The rows below are candidate-local evidence and limitations, not a declaration
that the full original AC has passed before the required exact review and CI.

| Original acceptance condition | Actual evidence | Result |
| --- | --- | --- |
| Registered Project/Run/intent/execution create one persistent identity-bound snapshot, including paths, requirement/acceptance and modes/digests. | `test_factory_freezes_registered_base_bytes_and_reopens`; actual SQLite ProjectRegistry, RunPlanner, protected factory and Git base tree. | Linux local C/P passed |
| Replay, reopen, worktree changes, concurrent producers and a lost command reply recover precisely the original snapshot. | Public same-key/different-execution concurrent freeze rejects before a second publication; a public command reserves, publishes, loses its reply, then recovers the same snapshot. Handoff/source and paused-reader cancellation retain historical bytes. | Local C/P: final command below |
| Wrong identities, changed Run term/configuration/authorization, tampered execution binding or binding digest, manifest/blob corruption, and missing ledgers fail closed without repair. | The metadata matrix includes valid `100644 → 100755` with recomputed self-hash; the separately stored full-manifest digest rejects it. | Local C/P: final command below |
| Traversal, symlink/reparse, unapproved or empty paths, registered-root aliases/corrupt base, and fixed file/byte limits reject completely without clipping. | A Linux Git tree containing approved `src/a\\b.txt` rejects before blobs/references, preserves bytes/mode, and leaves zero manifest/references/artifacts. Other bounds and alias cases remain. | Local C/P: final command below; Windows reparse unsupported/not_run |
| Freeze/read/replay have no Capacity/native/Host/Journal/model/Plan/qualification effects. | The protected factory invokes public freeze/read/replay/failure and compares actual Project qualification records, Run plans, and Capacity reservations. It wraps the shared `subprocess.Popen` creation boundary (not merely `subprocess.run`): every observed child is attributed to the snapshot reader by Python provenance and count, without executable-argv formatting guesses; zero socket connects are also recorded. The bootstrap has no configured Journal, Host, native runtime, model adapter, or output receiver, so no unconnected Journal is counted and those unavailable physical P/S observers remain `not_run`. | Applicable controller-ledger C/P: final command below; Journal/Host/native/model/provider physical P/S not_run |
| #110/#111 binding, begin/replay, submitted receipt recovery, historical handoff/source recovery and concurrency regressions stay intact; checks pass. | The three-module command above includes cancellation/handoff, Project update, command reservation/recovery, and durable-publication regressions. | Linux 89 passed / 22.85s; Windows 75 passed, 14 scoped skips / 34.61s |

The initial Windows invocation could not enumerate its inherited
`C:/Users/Chooo/AppData/Local/Temp/pytest-of-Chooo` (`PermissionError` before
tests); it is recorded as an environment failure, not product evidence. At
predecessor `36a9764444a2820c1a7614c9ac25a94fff2d0259`, a fresh Windows
snapshot basetemp had `5 failed, 2 skipped in 1.96s`: each positive producer
case reached the intentional unsupported `_sync_artifacts` branch and raised
`PLANNING_REPOSITORY_SNAPSHOT_CHANGED`. On 2026-09-08, after the shared-Popen
observer correction, the current candidate's fresh Windows basetemp ran the
three modules with `75 passed, 14 skipped in 34.61s`. Its skips are test-account symlink permissions, the Linux-only
backslash-filename regression, POSIX-only hard-link overlap, and Linux-private
deployment tests; generic concurrent Store freezing passed
on Windows. This is local Windows P evidence for the durable no-replace
publication protocol, not physical power-loss evidence. Full native/provider
qualification remains **not_run**; this leaf does not claim S evidence or
transport authority for an historical snapshot.
