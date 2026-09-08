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
closed. Blob sizes are queried before their bodies, so a rejected blob is not
first loaded into memory. The registered root must be a non-aliased directory.

Candidate C/P evidence is WSL Ubuntu with Python 3.12 and a private `/tmp`
pytest base directory: `tests/orchestration/test_planning_snapshot.py`,
`tests/runs/test_planning_execution.py`, and the protected-factory tests in
`tests/runs/test_planning_admission.py`. They cover actual ProjectRegistry /
RunPlanner / PlanningExecution provisioning, base bytes after worktree change,
reopen/replay, CAS-file/manifest/deleted-ledger rejection without Capacity changes,
Git replace/environment poisoning, and repository-root aliases. This is local
production-bootstrap evidence only: no native transport, Journal call, Host,
provider request, model call, qualification, or plan submission is exercised.

## Original AC coverage (current candidate worktree)

| Original acceptance condition | Actual evidence | Result |
| --- | --- | --- |
| Registered Project/Run/intent/execution create one persistent identity-bound snapshot, including paths, requirement/acceptance and modes/digests. | `test_factory_freezes_registered_base_bytes_and_reopens`; actual SQLite ProjectRegistry, RunPlanner, protected factory and Git base tree. | C/P passed |
| Replay, reopen, worktree changes, concurrent producers and a lost command reply recover precisely the original snapshot. | Base-tree/reopen test; `test_real_store_instances_concurrently_preserve_one_original_snapshot`; `test_committed_snapshot_survives_commander_handoff_and_source_change` exercises a real approved Commander handoff plus later ProjectRegistry source transition for both a lost and saved original freeze receipt, returning the old manifest/bytes without new CAS references or Capacity effects. | C/P passed |
| Wrong identities, changed Run term/configuration/authorization, tampered execution binding or binding digest, manifest/blob corruption, and missing ledgers fail closed without repair. | `test_changed_trusted_run_record_rejects_unfrozen_execution_without_snapshot`, `test_persisted_snapshot_binding_tamper_is_stable_and_does_not_create`, factory tamper/deleted-ledger tests, and malformed-manifest test. | C/P passed |
| Traversal, symlink/reparse, unapproved or empty paths, registered-root aliases/corrupt base, and fixed file/byte limits reject completely without clipping. | `test_unapproved_or_symlink_base_entry_is_rejected`, `test_repository_root_alias_is_rejected`, Git hardening test, and `test_limits_and_malformed_persisted_manifest_reject_without_partial_snapshot`. | C/P passed |
| Freeze/read/replay have no Capacity/native/Host/Journal/model/Plan/qualification effects. | Snapshot/execution tests compare the real factory-reopened Capacity ledger before and after, while directly counting persisted manifest/reference records. Native, Host, Journal, provider, qualification and submit stores are not opened by this local path. | C passed; P/S not_run |
| #110/#111 binding, begin/replay, submitted receipt recovery, historical handoff/source recovery and concurrency regressions stay intact; checks pass. | `KARAJAN_GO_TOKENIZER_DIRECTORY=/mnt/c/Users/Chooo/Playground/Karajan/.cache/go-context-artifacts PYTHONPATH=backend:tests:tests/projects:tests/runs:tests/candidates /tmp/karajan-candidate-mode-qy6_mqo2/venv/bin/python -m pytest --basetemp=/tmp/karajan-142-impact-tokenizer tests/orchestration/test_planning_snapshot.py tests/runs/test_planning_execution.py tests/runs/test_planning_admission.py -q` on WSL Ubuntu, 2026-09-08: `79 passed in 18.18s`. | P passed |

The initial Windows invocation could not enumerate its inherited
`C:/Users/Chooo/AppData/Local/Temp/pytest-of-Chooo` (`PermissionError` before
tests); it is recorded as an environment failure, not product evidence. The
reproducible candidate command above uses the required fresh Linux `/tmp`
basetemp. Full native/provider qualification remains **not_run**; this leaf
does not claim S evidence or transport authority for an historical snapshot.
