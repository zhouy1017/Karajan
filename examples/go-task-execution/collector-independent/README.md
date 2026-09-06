# Issue 90 independent Collector/Candidate review

Reviewer: `capacity_facts`. Reviewed `go_task_collector.py`, `_capture_lookup.py`, and CandidateStore's new recovery/opening behavior against `docs/planning/go-task-execution-issue.md` and the preceding Collector design. The reviewer coordinated interfaces and authored the separate intent persistence ports; those ports are **not independently reviewed here**. The behavior assertions below were written independently. Shared fixture constructors provide real approval/SQLite/Git/CAS/Journal state with explicitly synthetic native results and Host authority. No provider or credential access occurred.

## Confirmed findings, closed after independent verification

- **COLLECTOR-001 (P2): reopening historical recovery requires unavailable artifacts.** After a public projection freeze, moving `artifacts` and `objects.git` while retaining the valid DB prevents `CandidateStore(existing_only=True)` from opening. Therefore restart recovery cannot use the intentionally artifact-independent lookup, although the original in-memory Store can. The design separates historical commit identity from present artifact availability. Keep existing-only schema validation and no bootstrap; validate physical artifacts at effects/materialization instead.
- **COLLECTOR-002 (P2): successful freeze can have an invalid exact recovery key.** The public freeze accepts repeated identical `allowed_paths` and commits; exact lookup rejects that complete original Freeze request because it requires a unique list. The current Workspace compiler emits unique paths, so this is the deep module's paired contract rather than a normal Task compiler output. Reject duplicates before committing, or support the exact old request at lookup without relabeling/normalizing its identity.

`before.xml`: actual Windows 2 failed / 9 passed, 13.62 seconds. `test-before.py.txt`, `store-before.py.txt`, and `lookup-before.py.txt` retain the exact first-run inputs/source. Only formatting changes were applied to the executable review test afterward.

The nine passing controls cover original revision selection, multiple exact commits rejected as ambiguous, full reviewer environment/reviewer/writer-stop identity, actual Journal identity and stop mismatches despite a completed report, and cancellation recovery without a live source check or fabricated Review success.

Command from worktree root:

```text
python -m pytest .cache/collector-independent/test_review.py -o "pythonpath=backend tests/runs tests/projects tests/candidates" -q --junitxml=.cache/collector-independent/before.xml
```

Before-source SHA256:

```text
go_task_collector.py a9d3149f33ae026ae4e4dbc5e74a5daed9aaebac75066005da57d84c07f699bc
_capture_lookup.py ea524d6973801a7583648357252070ff28657f2454730a4c063240daab3cd608
store.py 8f69dfdd1364d6a33cd5351f28785a445fa36f213ce4a10dffcef8c7153a758f
```

## Final verification

The author removed artifact/Git availability from existing-only construction and added an explicit duplicate-allowed-path rejection before projection materialization or commit. Existing SQLite schema/ro/rw checks remain. The added `defer_validation` constructor mode is accepted only with `existing_only`; actual access still validates original state and cannot bootstrap it.

The original eleven independently authored cases pass on Windows and WSL/Linux. `windows-final.xml` and `wsl-final.xml` retain the exact runs; the WSL run completed in 6.23 seconds. Neither report overwrites the original red evidence. No additional findings remain in this bounded Collector/Candidate review. No actual native/provider or validation/Reviewer qualification is claimed.

Final source SHA256:

```text
go_task_collector.py a9d3149f33ae026ae4e4dbc5e74a5daed9aaebac75066005da57d84c07f699bc
_capture_lookup.py ea524d6973801a7583648357252070ff28657f2454730a4c063240daab3cd608
store.py 011aa22ba030564f1ef511fe923a47f75c48488ace40572d95c07503b9bc6da6
test_review.py 8906838bb753238728ae6d45b3bfc4a3fa5c1ba76a82599851c55bc854997ee1
```
