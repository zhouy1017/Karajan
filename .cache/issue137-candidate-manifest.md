# Issue #137 candidate evidence

Source delta: `6a5153d..c282a5dfe276e88b0a04e6f1928d611c927e7e8f`.

## Original failure and fixed behaviour

The failing PR130 Linux node was
`tests/runs/test_candidate_subjects_native.py::test_active_old_namespace_blocks_ready_subject_and_concurrent_cancel`
in `.cache/phase3-ci/pr130-691-pr-linux.log`.  Its former twelve-second loop
accepted `phase=cancelled` after one observation, before the persisted cleanup
had converged from `local_stop=unknown` to confirmed Host/native receipts.

`59c00d3` makes the existing twelve-second deadline wait for those owned
terminal facts.  `c282a5d` makes the permanent-unknown fixture persist and
return the same unknown receipt from run, inspect, and cancel, so the C
negative path cannot manufacture a confirmed stop.

## Local C evidence

The c282 controlled checks ran on Windows with the repository pytest setup:

```text
C:\Users\Chooo\Playground\Karajan\.venv\Scripts\pytest.exe -q tests -k
'public_cancel_retains_host_until_native_receipt_arrives or
permanent_unknown_cancel_never_becomes_confirmed' --basetemp
.cache/pytest-137-control-fixed
```

Result: `2 passed, 2773 deselected`.  This covers pending-to-confirmed
convergence, a stable permanent unknown cleanup receipt, no Evidence, one
start, and zero second claim.  `ruff check tests/runs/test_candidate_checks.py`
also passed.

Before c282, a private WSL source copy at
`/tmp/karajan-137-native-src-187239` ran:

```text
PATH=/mnt/c/Users/Chooo/Playground/Karajan/.cache/go-linux-runtime/package/bin:$PATH
/tmp/karajan-candidate-mode-qy6_mqo2/venv/bin/pytest -q tests -k
'candidate_subjects_native' --basetemp .cache/pytest-137-wsl-native
```

with the recorded result `2 passed, 2773 deselected` in 21.49 seconds.  That
copy was created after `59c00d3` and before `c282a5d`; its native test blob is
the same `c5007ed6cc34d765bcd5992bd4482afff9ea900a` at both commits, so it is
valid evidence for the unchanged native wait test.  The original tool output
was not redirected to a filesystem log.

## Later WSL rerun and #138 boundary

A later rerun of that exact source copy was saved at
`.cache/issue137-wsl-native.log`.  It passed the #137 old-namespace/cancel
node, but its sibling
`test_rebound_subject_reruns_all_checks_in_real_fixed_host_namespace` failed
with `RUN_EXECUTION_CLOCK_REGRESSED`: persisted `started_at`
`1788804883.797772`, then check claim `now` `1788804882.6302676`.  This is
tracked separately as #138; this manifest does **not** count the entire native
file as a final green result.  No #138-owned source or fixture is changed here.

The original c282 source file changes are restricted to
`tests/runs/test_candidate_checks.py`; the native test file remained the blob
above.
