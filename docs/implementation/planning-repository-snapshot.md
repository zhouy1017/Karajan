# Planning repository snapshot

`PlanningRepositorySnapshotStore` freezes the registered Project repository's
`base_sha`, rather than a mutable checkout. It enumerates only the Run's
approved `authorization_ceiling.read_paths`; each entry can name one file or a
directory prefix. Only base-tree regular blobs are accepted. The sealed manifest
and bytes are keyed by the existing PlanningExecution binding digest.

The public controller methods accept an execution ID, authenticated principal,
and command key only. They neither accept content nor a repository path. Missing
or historical snapshots remain unreadable for new native transport; existing
execution records still retain their prior read-only recovery semantics.

Production provisioning calls `provision_planning_repository_snapshots` using
the existing protected planning bootstrap. This creates the fixed ledger in the
private state directory. The trusted factory opens it in `existing_only` mode.

## Evidence and limits

The snapshot source is the ProjectRegistry-recorded absolute repository root
and its recorded Git `base_sha`. Git runs with a minimal environment, disabled
replace objects, hooks, fsmonitor, credentials, and protocols. It never uses a
caller-selected worktree revision. The manifest records the existing execution
binding digest, requirement and authorization-ceiling digests, repository
identity/base, approved path digest, each regular blob's path/mode/size/digest,
the total byte count, and its own digest. A reader validates the whole manifest
shape and every blob before returning bytes; malformed, missing, or changed
state is rejected and is never repaired by a reader.

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
reopen/replay, blob/manifest/deleted-ledger rejection without Capacity changes,
Git replace/environment poisoning, and repository-root aliases. This is local
production-bootstrap evidence only: no native transport, Journal call, Host,
provider request, model call, qualification, or plan submission is exercised.

## Original AC coverage (candidate `f95fe89b0435b80a251213d18f7302df6850d555` + worktree)

| Original acceptance condition | Actual evidence | Result |
| --- | --- | --- |
| Registered Project/Run/intent/execution create one persistent identity-bound snapshot, including paths, requirement/acceptance and modes/digests. | `test_factory_freezes_registered_base_bytes_and_reopens`; actual SQLite ProjectRegistry, RunPlanner, protected factory and Git base tree. | C/P passed |
| Replay, reopen, worktree changes, concurrent producers and a lost command reply recover precisely the original snapshot. | Base-tree/reopen test; `test_real_store_instances_concurrently_preserve_one_original_snapshot`; `test_committed_snapshot_survives_lost_command_reply_cancel_and_source_change` commits the producer SQLite transaction before simulating the lost controller reply. | C/P passed |
| Wrong identities, changed Run term/configuration/authorization, tampered execution binding or binding digest, manifest/blob corruption, and missing ledgers fail closed without repair. | `test_changed_trusted_run_record_rejects_unfrozen_execution_without_snapshot`, `test_persisted_snapshot_binding_tamper_is_stable_and_does_not_create`, factory tamper/deleted-ledger tests, and malformed-manifest test. | C/P passed |
| Traversal, symlink/reparse, unapproved or empty paths, registered-root aliases/corrupt base, and fixed file/byte limits reject completely without clipping. | `test_unapproved_or_symlink_base_entry_is_rejected`, `test_repository_root_alias_is_rejected`, Git hardening test, and `test_limits_and_malformed_persisted_manifest_reject_without_partial_snapshot`. | C/P passed |
| Freeze/read/replay have no Capacity/native/Host/Journal/model/Plan/qualification effects. | Snapshot/execution tests compare the real Capacity SQLite snapshot before and after; these test modules do not construct native, Host, Journal, provider, qualification or submit effects. | C/P passed for local absence; S not run |
| #110/#111 binding, begin/replay, submitted receipt recovery, cancellation/source and concurrency regressions stay intact; checks pass. | `pytest --basetemp=/tmp/karajan-dg01-final3 tests/orchestration/test_planning_snapshot.py tests/runs/test_planning_execution.py tests/runs/test_planning_admission.py -q` on WSL Ubuntu, 2026-09-08: `77 passed in 18.95s`. | P passed |

The initial Windows invocation could not enumerate its inherited
`C:/Users/Chooo/AppData/Local/Temp/pytest-of-Chooo` (`PermissionError` before
tests); it is recorded as an environment failure, not product evidence. The
reproducible candidate command above uses the required fresh Linux `/tmp`
basetemp. Full native/provider qualification remains **not_run**; this leaf
does not claim S evidence or transport authority for an historical snapshot.
