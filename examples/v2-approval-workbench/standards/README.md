# Independent Standards review

The final published test suite passes **11 tests, 0 failures, 0 errors**. The product source hashes are recorded in `review-final.json` against base `8329a1e028d53c6da5598f620c37aa8a2c62f0f2`. The existing final JUnit names this published test path; publication was already tested, so the final review did not repeat that run.

The review found one P2, now closed: the backend accepts and persists the decimal string `USD=0e0`, but the original UI rejected this valid authorization. The fix preserves the authenticated server string for display, including exponent notation, without converting it to a JavaScript number. The original public `RunPlanner` submission and HTTP-read fixture remain unchanged in `quantity-view.json`; the original failing input now passes in the full final suite.

The other checks cover exact v2 approval identity and CSRF, distinction between plan limits and original budget ceilings, ordered stage membership, conflict followed by failed refresh, uncertain retry identity, late responses after project/session changes, resetting review when changing Run, and refusal of incomplete or foreign authorization. There are no remaining actionable Standards findings in this reviewed scope.

## Reproduce

From the worktree/repository root, with the locked frontend dependencies installed:

```powershell
node frontend/node_modules/vitest/vitest.mjs run --config examples/v2-approval-workbench/standards/vitest.config.mjs --reporter=default --reporter=junit --outputFile=.cache/v2-ui-standards/replay.junit.xml
```

The replay writes a separate report and leaves original evidence intact. `standards-final.junit.xml` is the successful run from the published path.

## Evidence history and limits

- `quantity-before.junit.xml`: one actual product failure; the other ten cases were filtered out. This is the isolated original P2 reproduction.
- `standards-before.junit.xml`: the original eleven-case run had one product failure and one ambiguous text-selector failure. The selector was corrected to expect both matching task texts. That harness error is not a product red/green result.
- `standards-initial.json`: retained historical report, including the original source and input hashes. Its pending status describes that earlier point, not the final result.
- `valid-view.json`: unchanged v2 project/Run material from the original fixture, with only the unused top-level `v1_run` removed. The final published suite uses this reduced fixture. `quantity-view.json` and both before reports retain their original bytes.

These are independent component/JSDOM tests with controlled fetch responses. Only the original quantity fixture was produced through public domain submission and authenticated HTTP; this suite does not itself rerun that server setup. Real-browser verification belongs to the root browser reports in the parent directory. No credential, bootstrap/session state, model invocation, provider quota, execution qualification, or actual dispatch is verified or enabled by this evidence.
