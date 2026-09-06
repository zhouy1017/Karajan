# Fixed Go suite independent Spec review

Scope: `backend/karajan/projects/go_suite.py` and its public behavior, with
`tests/projects/test_go_suite.py` read as author evidence. Baseline `8a3d4ea`.
This review excludes the reviewer's credential-source and Go journal implementations.
The journal is exercised through its real public SQLite API as a suite dependency;
that is not an independent approval of journal internals. Root qualification-store
persistence and the observer implementation have separate reviewers.

## Finding history

**GO-SUITE-SPEC-001, P2: cleanup revoked a conflicting grant owned by another
binding.** At the initial source hash recorded in `history/initial-freeze.json`, a real
SQLite grant preexisted with the same grant ID but a different qualification and
Attempt identity. Creation correctly failed with an identity conflict, but the
suite's unconditional cleanup revoked the foreign grant. Initial independent
execution: 1 failure, 5 passes, 2 native cases deselected. No key was revealed and
no upstream request occurred. The bug is a trusted-controller ID conflict/recovery
boundary; this report does not claim external users can choose grant IDs.

The author changed cleanup to read the immutable journal binding and compare the
entire expected binding before revocation. A conflicting record is reported as
`not_owned` with `GRANT_CLEANUP_BINDING_MISMATCH`. A matching grant whose creation
reply/capability was lost is still revoked, and no new send authority is produced.
The original failing assertion remains in the independent test. It passes on the final reviewed source; this P2 is closed.

## Stability investigation and clock boundary

Three later full groups each had one unexpected failure (first-stage failed in two
source/start cases; first-stage not_run in a journal-correlation case). The early
assertions retained only summarized result dictionaries, so the inner causes of
those three historical failures were not preserved. Their original JUnit files
remain in history. They must not be described as individually diagnosed or fixed.

After adding optional exception/source/clock/grant tracing, the bounded diagnostic
ran three complete groups: 10 passes in 100.04, 94.91 and 94.73 seconds. No unexpected
exception was reproduced. Those passing reruns do not explain the earlier failures.
The diagnostic plugin is archived separately and is not enabled by default in CI.

An independent 30-second WSL clock observation did establish an actual wall-clock
regression: 1788683964.3040302 to 1788683961.8273075 (-2.4767227172851562 seconds),
while monotonic time advanced by 0.020303784 seconds. A new deterministic public
suite test replays that magnitude between initial validation and the first effect,
requires both scenarios to remain not_run, and verifies that both matching durable
grants are revoked with zero sends. Production time rules were not changed.

The ten tests of other behavior now give the suite and issued start one explicit
monotonic-relative test clock. The journal still uses its real time.time clock and
ordinary expiry behavior. This removes wall-clock adjustment from those tests'
unstated input assumptions; it is not proof that all three historical failures
had the same cause. That historical stability limit remains documented.

## Independent behavior coverage

- All four credential identity axes reject before either durable grant exists.
- Foreign grant identity survives a rejected start; matching lost capabilities
  are revoked without revealing the synthetic key or sending, and directory replay
  cannot re-execute the suite.
- Actual Linux OpenCode, namespaces, Unix socket relay and durable journal execute
  the read/edit and denied-read fixtures using local synthetic HTTP responses.
  Both exact grant bindings exist before the first request. A caller's mutation of
  the input start cannot change the copied binding or fixture observation origin.
- Controller source changes or disappearance after the first actual model request
  stop the second scenario. The completed first scenario retains observed native
  process-tree shutdown and relay closure; both grants remain revoked.
- An independently changed response-byte count in an otherwise actual native
  observation fails comparison with the persisted journal outcome. This test wraps
  the real observer after it completes; it does not replace native execution with
  a fabricated passed report.
- The observed scope remains `fixed_native_tools_fixture`; general runtime tools,
  dispatch eligibility, budget/context capability and remote provider stop are
  never upgraded.

## Final result

The current 11 independent Linux cases passed in 90.67 seconds. No production source
was changed for the clock-specific test or the stable-clock fixture.

## Boundaries and reproduction

The credential and start DTO builders imported from `test_go_suite` are explicit
synthetic controller fixtures. They do not claim that this test called the public
qualification-registration command or authenticated a real provider. There are no
actual Go model calls or real key reads. Runtime paths and temporary journals use
native WSL `/tmp`, because DrvFS does not support these pathname Unix sockets or
private Linux permission semantics. Temporary state is removed automatically;
retained evidence contains only test/source digests, result counts and JUnit.

From this worktree in WSL, with the prepared pinned runtime artifact:

```sh
PYTHONPATH="$PWD/backend:$PWD/tests/projects" \
KARAJAN_OPENCODE_LINUX_BINARY=/mnt/c/Users/Chooo/Playground/Karajan/.cache/go-linux-runtime/package/bin/opencode \
/tmp/karajan-candidate-mode-qy6_mqo2/venv/bin/python -m pytest \
  examples/go-suite-independent-review/test_go_suite_review.py -q -p no:cacheprovider \
  --basetemp=.cache/go-suite-independent-review/replay-tmp
```

The actual pinned ELF hash is
`ca6c0e1f42be3120595bf6848937e7586ec862c87fa7aa111e89c7cc6e9a4650`.
Windows can collect these tests but correctly skips all Linux behavior cases;
Windows skips are not counted as behavior passes. Final independent results and
exact current source/test/evidence hashes are recorded in `review.json`. The parent
separately reported a successful actual-Go public qualification with 3 + 2 requests
and a replay without new sends; that run was not repeated by this reviewer.
