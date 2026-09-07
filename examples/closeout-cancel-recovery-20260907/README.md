# Current integration and cancelled Check receipt recovery

Tested product integration: `444fed2abdd32a4335a5da6320965da7ff640750`. It includes Reviewer implementation `1a000c5`, the current heads of PR102/103/105, accepted dev `f3704e6` (including PR97/98), and Luna's correction `9d2ee17551361f0b451f36a0ab188a76c03681f5`. Only `candidate_checks.py` and its public test file changed after the separately validated Reviewer implementation/dependency combination.

## Problem and final behavior

PR103's two Linux CI runs failed because cancellation could stop the Check Host before its native result was saved. The namespace had exited but the retained Check observation was still missing. `original-ci-failure.txt` preserves the actual failure; 2184 other tests passed in that original main test step. The old CI result is not relabeled as successful.

The final correction leaves native cancellation immediate. When a native claim has no result yet, Host cleanup waits for a later reconciliation so the publisher can finish. Once the runner result is available or the original observation is already persisted, Host cleanup proceeds. Historical cleanup still works without loading a current runner. Missing results remain unknown and do not produce successful evidence.

The initial `cc81f8a` proposal added a wait inside the runner's own cancellation path; independent review rejected that self-wait approach. The final tree restores the original runner implementation. The accepted change is in the coordinating cleanup order. No test timeout, skip or stop assertion was weakened.

## Validation and independent review

- Author: the explicitly authorized `gpt-5.6-luna` fallback after Spark exhaustion. The author reports a deterministic public-boundary failure against the original `37daff5` source and two passes after correction. These original local outputs existed only in tool output; `author-history.md` is an attestation, not reconstructed raw logs. Its earlier native timing differs from the author's interim summary; the acceptance below uses root's saved final-combination result.
- Root independently ran the original real Linux namespace/concurrent-cancel test and both new public recovery tests on the integrated source: **3 passed, 25.99 seconds**, with original stdout/XML here. The public tests create their native claim through `advance` and `consume_check`; only the runner/Host observation boundary is a named double. The separate native test supplies real process evidence.
- Root's affected Windows integration: **554 passed / 3 POSIX-only skips, 310.92 seconds**. It includes Candidate storage, Checks, validation subjects, bindings, scope and both factories. Linux covers the applicable native cancellation boundary. Full Ruff and mypy on **145 modules** pass. The exact commands and report mappings are retained here; overlapping groups are not summed.
- Independent reviewer root, Standards: no unresolved findings. Original runner cancellation, immutable identities, operation locking and receipt persistence are preserved. Spec: no unresolved findings after adding the historical persisted-observation branch. The public late-result and history tests exercise the actual coordination methods; the real Linux failure case passes. This review is independent of Luna's product/test authorship.

The [Reviewer archive](../go-readonly-reviewer-qualification-20260907/README.md) retains its earlier complete Store/native mechanism evidence and source boundary. The cleanup correction does not change that Suite, observer, runtime, parser, Journal or relay; final G tests the actual entire repository checkout. Current required GitHub checks and actual dev inclusion remain mandatory before closing the associated Issues. The final PR/Issues carry those remote results.

`closeout-source.json` records the actual local source bytes. As in the Reviewer archive, `projects/qualification.py` retains CRLF locally while Git stores LF; normalized bytes and AST are identical. This is not a claim that its raw local hash equals the commit blob. Other source differences must be independently accounted for before accepting a later candidate.

There were no official model calls or paid API tests in this closeout. #107 official mechanism qualification, #95 actual Reviewer Task/business quality/Review Evidence, and the broader #13/#14 requirements remain outside these completed implementation slices.
