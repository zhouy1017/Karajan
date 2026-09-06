# Candidate Reviewer policy rebind

This is the internal storage slice in [#96](https://github.com/zhouy1017/Karajan/issues/96),
under [#95](https://github.com/zhouy1017/Karajan/issues/95). Its base is
`624ad8b8490003f155baf7842ba91b9975b9526a`; the implementation commit is `58eafc337b7a2350c9e983d550e14ce50b2dc5e9`.
It proves local C/P storage behavior, with synthetic authorization and qualification
declarations. It does not establish a qualified Reviewer or invoke a model.

## Contract

`CandidateStore.rebind_reviewers(binding, command_key=...)` creates a same-content
Candidate revision in the original series. `lookup_review_rebind(...)` recovers
the exact historical commit. Both are internal controller ports; there is no
request-facing endpoint or model-start authority.

The strict binding includes the original Candidate identity and full Freeze
request digest; Run, operation and Reviewer Task IDs; capture, approval, Plan,
ExecutionPolicy, task and Rulebook digests; and the nonempty Reviewer/source set.
Duplicate Profile/revision pairs and extra fields are rejected. Unknown family
information remains unknown. Source declarations must be compiled and checked by
the future trusted caller under its current Run/Project/rule/qualification guards.
This primitive does not resolve those declarations against live authority.

The new revision preserves baseline, repository/base/tree/content/input identity,
the complete manifest and file modes, allowed paths, task class, authors, Writer
stop and all Check definitions. Only Review's allowed Reviewer set and its
derived policy digest change, alongside the new Candidate ID/revision/time and
immutable `review_rebind` lineage. Policy and Review revision/environment fields
retain their original values. The original Candidate and its evidence stay intact.

The existing Candidate ledger stores the complete binding, command key and
server-computed binding/request digests. New effects use `BEGIN IMMEDIATE` to
check exact source identity, current series revision and command uniqueness
together. They verify all Candidate and baseline CAS bytes, hashes, regular-file
constraints and Git blob/tree identities before inserting. No Worker workspace,
Git command, source repository or second command ledger is required.

Exact replay checks the read-only path first. Historical lookup reads ledger
metadata only, without artifacts, Git, clock observations, directory creation or
DB writes. A superseded source or missing assets cannot erase an exact receipt;
new commands still require the current source and available CAS. Different
bindings under one key conflict, including across series. Damaged or ambiguous
receipts fail closed. A missing ledger is not initialized by either entry point.

## Evidence and integration limits

The [publication directory](../../examples/reviewer-candidate-rebind/README.md)
contains the original author freeze and input, red/green and final XML, static
checks and an independent root review. Original files keep their byte hashes;
the publication map records the exact implementation commit and verifies
its source bytes against the original freeze.

- C/P: all 42 new public cases pass against real temporary Git/CAS/SQLite.
- P: Windows Candidate regression is 164 passed and 3 POSIX-only skips; WSL is
  167 passed with no skips. Binary, empty and executable files are preserved.
- C/P: independent Linux execution of seven existing public cases passed. This
  is an independent execution, not seven additional test designs.
- G: commit, PR, current CI and merge remain separate publication work.

The new Candidate has no copied Check or Review evidence and remains ineligible
for delivery. #95 still owns the real qualified Reviewer-set compiler, dependency
routing, Capacity, read-only model execution, structured Review evidence and S
acceptance. The #94/#95 integration still must persist and recover the validation
subject handoff, stop old Checks, rerun every required Check and preserve the
same cumulative budget. This slice does not change the original Worker capture
pointer or demonstrate that handoff. It does not close either parent scope.

The `store.py` patch is two thin entries. Integration with the separate Check
worktree must preserve that worktree's `lookup_evidence` addition and validate
the combined source instead of replacing its CandidateStore wholesale.
