# Candidate reviewer policy rebind — bounded author evidence

Base: `624ad8b8490003f155baf7842ba91b9975b9526a` in the independent
`codex/m3-reviewer-candidate-rebind` worktree. This implements only the Candidate
storage preparation slice of #95. It does not establish Reviewer qualification,
select a Rulebook rule, reserve model capacity, invoke a provider, execute Review,
or complete #95. Every authority/qualification record in these tests is explicitly
synthetic; Git, CAS, SQLite, filesystem permissions and commit recovery are real.

## Public controller ports

```python
candidate = store.rebind_reviewers(binding, command_key="controller-fixed-key")
historical = store.lookup_review_rebind(binding, command_key="controller-fixed-key")
```

The caller must persist the exact command before the new effect and hold its
current Run/Project/Rulebook/qualification guards. These ports prove content and
policy lineage, not present authorization. They accept no substitute Freeze,
Checks, environment, authors, writer stop, task class or policy. No public HTTP
endpoint is added.

`binding` is the strict `ReviewerBinding` model:

- `schema_version = karajan.reviewer-binding.v1`, positive `revision`;
- `source_candidate`: `id`, `series_id`, `revision`, `repository_identity`,
  `base_sha`, `tree_sha`, `content_sha256`, `manifest_sha256`, `input_sha256`,
  `policy_sha256`, `baseline_id`, and `request_sha256 = digest(source['request'])`;
- `run_id`, `operation_id`, `reviewer_task_id`;
- `capture_digest`, `approval_digest`, `plan_digest`, `execution_policy_digest`,
  `reviewer_task_digest`, `rulebook_digest`;
- 1–64 `reviewer_sources`, each containing an existing `Reviewer` record and
  `qualification_source_digest`, `authentication_source_digest`. Repeated
  `(profile_id, profile_revision)` is rejected, even with different family data.

These source records are immutable controller statements. An unknown family
stays `None`; no declaration is promoted to an observed or qualified capability.
Exact JSON list order participates in idempotency, as it does for existing frozen
policies. The controller is responsible for constructing a stable order.

The new Candidate preserves the full original manifest/modes, registered
baseline, repository/base/tree/content/input identity, changed paths, allowed
paths, class, authors, writer stop and all Check definitions. Only
`request.policy.review.approved_reviewers` and the resulting `policy_sha256`
change, together with the new Candidate ID/revision/time and added lineage.
Policy ID/revision and Review revision/environment remain unchanged.

`review_rebind` retains the whole binding, command key, server-computed
`binding_sha256` and full command `request_sha256`. Its source identity points
to the immutable predecessor. It uses the existing `candidates` ledger and a
`BEGIN IMMEDIATE` transaction; no schema migration or second command ledger is
introduced. Global command-key lookup checks SQL ID/series/revision against the
JSON, source and derived request identities, manifest and Git tree metadata.

New commits require the exact current source revision and verify all Candidate
and baseline CAS bytes, hashes, regular-file/link constraints and Git blob/tree
identities, without consulting the original repository or a Worker workspace.
Exact replay opens the read-only path first and remains available after the
source is superseded or artifacts/Git disappear. The standalone lookup performs
no artifact read, Git command, clock observation, directory creation or DB write.
It returns only the exact key/binding result, never a latest/similar Candidate.
Different bindings under the same key conflict. Ambiguous or inconsistent
persisted receipts fail closed. A missing existing ledger is never initialized.

Old evidence is not copied. The original Candidate and its evidence remain
unchanged; a new Candidate waits for its own Check and Review evidence. No
cross-database atomicity or delivery eligibility is claimed.

## Verification

The first public Git/CAS test failed because the new public method was absent
(`first-red.xml`), then passed (`first-green.xml`). The test file was subsequently
expanded and formatted; the final source hashes apply to the final 42-case file,
not to a reconstructed historical test version.

Final Windows Candidate regression: **164 passed, 3 POSIX-only skips**, 123.55 s.
Final WSL/Linux Candidate regression: **167 passed, no skips**, 19.77 s.
Both runs include the same **42 new tests**, all passing. The first new test
checks the complete binary/empty/executable baseline and real Linux restored
`0755` mode. Existing mode-only tests explain the three Windows skips.

New tests cover every source identity field, forbidden replacement fields,
empty/duplicate reviewer sets, unknown family retention, full CAS and old
baseline tampering, missing/hardlinked/directory-linked artifacts, immutable
historical metadata, source supersession, chain rebinding, cross-series key
conflicts, concurrent single commit, and a connection that commits then loses
the return. Recovery uses a newly opened store. The no-assets history test also
forbids clock, Git and artifact helper calls and compares unchanged DB bytes.

Targeted Ruff/format and strict mypy for both Windows and Linux platform settings
passed. XML and static outputs are retained alongside this file. No test DB,
Git repository, raw runtime directory, environment image, key or credential
value is included in the evidence manifest.

Run from this worktree with the repository's Python dependencies installed:

```powershell
$env:PYTHONPATH='backend'
python -m pytest tests/candidates/test_review_rebind.py -q --basetemp=.cache/rebind-recheck-new
```

```bash
PYTHONPATH=backend python -m pytest tests/candidates -q -p no:cacheprovider \
  --basetemp=/tmp/karajan-rebind-recheck-new
```

All fixture helper imports are sibling Candidate tests, so no extra pytest
`pythonpath` override is required. Use a fresh basetemp to retain prior evidence.
The implementation diff in `store.py` is only two thin entries; integrating it
must preserve the separate VALID tree's later `lookup_evidence` addition.
