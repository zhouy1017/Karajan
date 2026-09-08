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
after linking/unlinking and before SQLite records references. Windows has no
equivalent directory-fsync implementation in this boundary, so publication
fails closed there; Windows reparse behavior remains unsupported evidence, not
a portability claim.

The CAS protocol permits only a competing publisher's brief two-link interval:
it waits for that temporary name to disappear, then requires a regular,
single-link file with exactly the original bytes. A persistent second hardlink
is still rejected. The manifest's repository identity, Git base object ID, and
approved-path digest must have their expected types and match the independently
sealed source digest in SQLite; recomputing the manifest's self-digest cannot
rebind them.

Candidate evidence for this snapshot-authority worktree is WSL Ubuntu with Python 3.12 and a private `/tmp`
pytest base directory: `tests/orchestration/test_planning_snapshot.py`,
`tests/runs/test_planning_execution.py`, and the protected-factory tests in
`tests/runs/test_planning_admission.py`. They cover actual ProjectRegistry /
RunPlanner / PlanningExecution provisioning, base bytes after worktree change,
reopen/replay, saved-command replay after CAS-file/manifest/deleted-ledger rejection without Capacity changes,
Git replace/environment poisoning, and repository-root aliases. This is local
production-bootstrap evidence only: the fixture's actual temporary Journal,
Qualification, Run, and Capacity ledgers are inspected, but no native transport,
Host, provider request, model call, qualification operation, or plan submission
is exercised.

```bash
KARAJAN_GO_TOKENIZER_DIRECTORY=/mnt/c/Users/Chooo/Playground/Karajan/.cache/go-context-artifacts \
PYTHONPATH=backend:tests:tests/projects:tests/runs:tests/candidates \
/tmp/karajan-candidate-mode-qy6_mqo2/venv/bin/python -m pytest \
  --basetemp=/tmp/karajan-142-final \
  tests/orchestration/test_planning_snapshot.py \
  tests/runs/test_planning_execution.py tests/runs/test_planning_admission.py -q
```

## Original AC coverage

The independent static-review red candidate was
`44d1aa88aed38a2d2f1183b0c9568e3d233e8489`. The green implementation candidate
whose source/tests are covered below is
`5158aab22ca493100c1f13f129da9e76d1e96322`; this evidence-report commit is a
documentation-only follow-up.

| Original acceptance condition | Actual evidence | Result |
| --- | --- | --- |
| Registered Project/Run/intent/execution create one persistent identity-bound snapshot, including paths, requirement/acceptance and modes/digests. | `test_factory_freezes_registered_base_bytes_and_reopens`; actual SQLite ProjectRegistry, RunPlanner, protected factory and Git base tree. | Linux local C/P passed |
| Replay, reopen, worktree changes, concurrent producers and a lost command reply recover precisely the original snapshot. | Base-tree/reopen test; forced `linkcount==2` interleaving in `test_concurrent_publish_waits_only_for_its_temporary_link`; real approved Commander handoff/source-transition recovery; and a saved command replay whose reader is paused while cancellation completes, retaining original bytes. | Linux local C/P passed |
| Wrong identities, changed Run term/configuration/authorization, tampered execution binding or binding digest, manifest/blob corruption, and missing ledgers fail closed without repair. | Existing binding/ledger corruption cases plus recomputed-self-hash metadata matrix (`repository_identity_sha256`, `base_sha`, `read_paths_sha256`) in `test_base_tree_snapshot_is_immutable_and_directory_paths_are_expanded`. | Linux local C/P passed |
| Traversal, symlink/reparse, unapproved or empty paths, registered-root aliases/corrupt base, and fixed file/byte limits reject completely without clipping. | Direct traversal, unmatched approval, corrupt Git base, unchanged original bytes/mode and zero-manifest tests; Linux symlink and root-alias tests; sparse oversized replacement checks `st_size` before bounded consumption. | Traversal/unapproved/corrupt-base/limits/Linux symlink C/P passed; Windows reparse unsupported/not_run |
| Freeze/read/replay have no Capacity/native/Host/Journal/model/Plan/qualification effects. | The protected-factory test snapshots actual Run plans and Capacity reservations, an actual temporary `GoCallJournal` call ledger, and the copied `ProfileQualificationStore` record ledger before/after freeze, read, saved replay and failures. The fixture intentionally supplies no Host or provider adapter: zero Journal rows is a receiving-boundary result only, while physical native/model/provider absence remains not_run. | Local ledger C/P passed; Host/native/model/provider physical P/S not_run |
| #110/#111 binding, begin/replay, submitted receipt recovery, historical handoff/source recovery and concurrency regressions stay intact; checks pass. | Linux command below, 2026-09-08, after the blockers: `84 passed in 19.04s`. `test_snapshot_publication_holds_authority_after_final_check` retains the short FINAL PUBLICATION interval: cancellation/handoff wait only through the final manifest commit. | Linux P passed |

The initial Windows invocation could not enumerate its inherited
`C:/Users/Chooo/AppData/Local/Temp/pytest-of-Chooo` (`PermissionError` before
tests); it is recorded as an environment failure, not product evidence. The
reproducible candidate command above uses the required fresh Linux `/tmp`
basetemp. Full native/provider qualification remains **not_run**; this leaf
does not claim S evidence or transport authority for an historical snapshot.
