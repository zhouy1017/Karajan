# Issue 90 independent fixed-factory/source review

Reviewer: `capacity_facts`. Scope is the root-authored fixed bootstrap/factory and Task runner source envelope. This reviewer authored the separate intent port; those persistence methods are fixture dependencies, not independently reviewed here. No model calls, keys or product edits occurred.

## Confirmed findings, independently closed

- **RUNTIME-001 (P2), incomplete controller source envelope.** The real `task_runner_source` compiler does not change when simulated deployment bytes for `routing/evaluator.py`, `routing/quotas.py` or `capacity/models.py` change. These affect the actual reserved-Task guard or its validation. Entry and Collector file changes are positive controls and do alter the envelope. The unrelated native sub-envelope is explicitly replaced by a fixed fixture; actual controller file reads are exercised. This is a deterministic omission check, not a claim to have changed a live process or exploited a permission bypass. Root chose to bind all backend Python files, conservatively rejecting any code deployment change rather than support hot replacement.
- **RUNTIME-002 (P2), missing Journal blocks available Host cancellation.** Public stores contain the original approved operation, successful Capacity activation, frozen launch and prepared Host/control. After moving the Journal file, the historical factory fails its eager path preflight before a facade can cancel the still-owned Host. No substitute Journal is requested or justified. Root chose deferred validation for existing-only historical Journal/Host/Candidate handles; effects retain complete preflight, and every method must still open only existing state and record unknown for unavailable observations.

`before-confirmed.xml`: actual Windows 4 failed / 2 passed, 2.48 seconds. This is three omitted-file variants, one real factory cancellation gap and two included-file controls. `runtime-before.py.txt`, `binding-before.py.txt`, and `test-confirmed.py.txt` retain the exact input/source. Only import sorting and formatting followed in the executable test.

`before.xml` and `test-initial.py.txt` retain the first attempt: the source omission cases already failed as above, but the fourth test stopped at synthetic Capacity expiry because the reopened factory used the wall clock. Matching the existing fixture clock to 1000 exposed the actual later missing-Journal failure. The first fourth failure is not counted as product evidence.

```text
python -m pytest .cache/runtime-independent/test_runtime_review.py -o "pythonpath=backend tests/runs tests/projects" -q --junitxml=.cache/runtime-independent/before-confirmed.xml
```

Code read also confirms the bootstrap has an exact field schema with no argv/provider/prompt transport override; the child receives only three identifiers with Python `-I`; history avoids accounting, credential resolution and native suite construction. Private-path checks and immutable launch identity remain required. No unlimited same-account filesystem tampering guarantee is claimed.

## Final verification

The fixed source compiler now deterministically records every `backend/karajan/**/*.py` relative path/hash. Both omitted-source cases and already-covered entry/Collector controls pass. Historical Journal/Host/Candidate construction is explicitly `existing_only=True, defer_validation=True`; effect construction retains eager checks. Deferred mode cannot bootstrap state, and the actual Journal/Host accesses still use existing-only connections. The original missing-Journal scenario now records the absent grant as unknown while cancelling the available exact Host. No missing file is created.

Actual independent final results: **Windows 6 passed, 2.35 seconds** (`windows-final.xml`); **WSL/Linux 6 passed, 9.69 seconds** (`wsl-final.xml`). The executable review test has SHA256 `f13fd1f9eea715fab637a9acfa7abd935bf046e113bf98c78ea66861daf2723e`.

The first after-fix run reached a previously unreachable assertion and exposed a reviewer-test error: existing Host semantics report process `state='exited'` and `business_status='cancelled'`, rather than `state='cancelled'`. `windows-after-fix-first.xml`, `wsl-after-fix-first.xml` and `test-before-host-status-correction.py.txt` retain that result. The assertion was corrected to both documented public Snapshot fields, with an additional unknown-Journal observation check; no product changed for this correction. Original product red evidence remains intact.

Final source SHA256, verified before and after both tests:

```text
orchestration/go_task_runtime.py a126e93c336199ca2d81da2cc3a6565fc1e11866106bc990b678b93ab0d9563c
orchestration/go_task_binding.py d2a021d8ffad3741b0c7ee03481a9d7c907b8c0be20dc593ca01efb92a1e959d
adapters/opencode/go_journal.py f1db11761a3cec3993e4c46ce4503e6148d0565d8b8a0e3a698c2fac33ad3134
execution/host.py e3c0f6467314d304293d0752a21226e99e89f0873552b9e3584553ea02a5068e
candidates/store.py 011aa22ba030564f1ef511fe923a47f75c48488ace40572d95c07503b9bc6da6
```

Ruff passed. This bounded review has no remaining findings. It does not independently review this reviewer's intent persistence implementation or prove actual native/model execution.
