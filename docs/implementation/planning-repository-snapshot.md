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
