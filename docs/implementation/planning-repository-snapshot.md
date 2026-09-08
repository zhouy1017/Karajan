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
