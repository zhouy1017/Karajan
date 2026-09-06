# Independent Go context / relay / journal review

Reviewer: `/root/qualification_integration`. Worktree: `C:\Users\Chooo\Playground\Karajan\.cache\go-task-execution`.
Date: 2026-09-06. Reviewed the new `go_context.py` accounting boundary and the relay/journal changes that consume and persist measurements. The review does not independently re-review the previously committed Task grant implementation from the PR #55 baseline (PR remains unmerged).

## Final result

**Two P2 findings were reproduced and repaired by the author. No open blocking finding remains in this slice.** Final independent execution: **10 passed in 3.12 seconds**, with Ruff passing. All HTTP traffic used a synthetic local transport/upstream, and all journals used temporary SQLite files. No actual provider, credential file, native model call, namespace change or CI repair was performed.

### GO-CONTEXT-REVIEW-001 — unresolved durable calls left Task grants usable (closed)

Two public fault cases reproduced the same boundary failure:

1. `begin_call` committed its real SQLite send intent and then its return was lost. The relay did not recover the recorded call ID, so its error path skipped revocation.
2. The relay received and forwarded a valid response, but `complete_call` failed before persisting completion. The journal retained `send_unknown`, while the completion-error handler only changed the in-memory receipt.

Both cases reopened the real journal and observed one measured `send_unknown` call with the grant still `active`. `before.xml` preserves both failures. The author now recovers a lost begin through a read-only snapshot matching the full grant binding, the controller-generated call UUID and the exact measurement; this is not a second begin/send attempt. Recovered calls and completion failures revoke the exact grant and close the relay to new sends. Both original cases pass without weakening their assertions, and no request count is refunded.

Additional post-fix checks show that a wrong fence or wrong capability does not revoke another grant. A transient revocation-write failure still prevents this relay from sending again. If the lost-begin ownership lookup itself is unavailable, the relay closes locally and retains the durable unknown call without claiming it successfully revoked a grant. The controller must reconcile that existing grant before creating any recovery transport; this module alone does not implement that future recovery coordinator.

### GO-CONTEXT-REVIEW-002 — later usage frames could hide a reported exceedance (closed)

A synthetic SSE stream first reported `prompt_tokens=5000`, then supplied a usage-only frame with `prompt_tokens=20`. The original parser's `usage.update` discarded the higher observation, resulting in actual HTTP 200 forwarding instead of the context failure. `before.xml` preserves this third failure.

The author now retains each counter's highest observation, including nested usage counters. The original input case passes; an additional public output case reports `completion_tokens=300` followed by `2` against requested output 256 and is rejected with `CONTEXT_PROVIDER_OUTPUT_EXCEEDED`. Its durable result retains 300 and the exact Task grant is revoked.

## Other reviewed behavior

- GoRelay requires controller-supplied accounting for a Task subject, checks the supplied policy digest against that grant, measures the complete supported request and tool history, and persists the allowlisted measurement in the same transaction that records send intent, before upstream request I/O.
- The independent complete-history test reopens SQLite from inside the actual upstream HTTP callback, sees `send_unknown`, and compares its full measurement against the request bytes delivered to that callback. Historical reasoning and tool arguments/results contribute to measurement; their raw text is absent from the public journal snapshot.
- Accounting uses fixed local tokenizer/template artifacts and explicit library versions. It labels the result `reference_tokenizer_estimate` / `local_estimate`; declared model capacity is separate, and its source descriptor explicitly grants no qualification or server-exact accounting claim.
- Input, requested output, reserved output, margin and operating context constraints are checked before a send slot is consumed. The measurement schema rejects raw prompt/body fields. Unsupported request shapes, duplicate or non-object tool arguments, missing tool results, and incomplete tool histories are rejected rather than silently omitted.
- Missing provider input/output usage, exceeded input/output observations and transport failures fail the Task observation and withdraw remaining sends after authenticated durable intent. Response receipt and local closure do not claim remote stopping or refund prior requests.
- The independent legacy qualification test still returns HTTP 200 without Task accounting or provider usage. Its reopened call retains the old JSON shape without a `request_context` field. Existing qualification behavior is deliberately separate from Task accounting enforcement.

## Authorization limits

`GoRelayContext` and `GoRelayAuthorization` are internal controller ports. Their parameters are not accepted from a native HTTP body. The relay compares a policy digest and verifies a grant/capability; it does not independently load an approved Run or establish that a Python caller derived numeric limits from that Run. The controller/execution consumer must resolve and bind the actual approved policy, context source, workspace, Profile/runtime, credential generation and current Attempt/fence. The journal remains a trusted bookkeeping API, not a model-facing authorization endpoint; a direct internal `begin_call` is not proof of a metered production model request. This review does not claim the complete approved-Run execution chain has been activated.

## Evidence and reproduction

| Evidence | Result |
| --- | --- |
| `before.xml` | Original 5-case group: 3 failed, 2 passed; 1.92 s |
| `after.xml` | Original 5 plus 5 additional independent cases: 10 passed; 3.12 s |

The author separately reported its larger regression runs. Those counts are not included in the independent result.

Run the independent group from this worktree, with the already provisioned `.cache/go-context-artifacts` (or an explicit `KARAJAN_GO_TOKENIZER_DIRECTORY`) and pinned Python dependencies:

```powershell
$env:PYTHONPATH = (Resolve-Path backend).Path
python -m pytest examples/go-task-context/relay-review -o 'pythonpath=backend tests/adapters/opencode tests/projects tests/runs tests/web' --basetemp=.cache/go-context-review/public-rerun-tmp --junitxml=.cache/go-context-review/public-rerun.xml -q
```

## Source binding

The initial failing relay SHA was `6e6e74db3c2419873bbaabee780b48f80d5ff5498c9d3d21d77b89a16821fd6d`. Accounting and journal source did not change during this review. The final relay hash below was read before and after the independent post-fix group.

| File | Final SHA-256 |
| --- | --- |
| `backend/karajan/adapters/opencode/go_context.py` | `ab351b634e38351bed631db97734d1e519fc7e4577b0fb094f2651fa112d0428` |
| `backend/karajan/adapters/opencode/go_relay.py` | `2d90f224f0b33e04adc693c33ac58ffd3904296b596c5c23c155a65cab7c992a` |
| `backend/karajan/adapters/opencode/go_journal.py` | `a63f3f4d314f2a64bdd9a4f8e7dd43b2c24d5bc9388a9404976e6200a05bbfdd` |
| `test_independent_context_relay.py` | `53710bd94fdd639be6ed55e97acbb703fbed59d95ce869af9c63d9237f7b69b4` |
| `test_revocation_failure.py` | `0978f29055cedd39997bbcac288e412014304408accf0e6af744e431b5be0f2d` |
| `test_recovery_and_compatibility.py` | `3ee82174cc99300d1470f1b9bc560108cb4992c6014955860391fccb4e4e972e` |
| `before.xml` | `5b32df37e24447a2f83dd1177c746cd6728c4be7529cc10eb5c14038fd8bb9cd` |
| `after.xml` | `477763c2cc963ac6a76f94aac85907cf4468fc365e307dbb209f6904d78af6f2` |

This directory deliberately contains no SQLite fixture state, credentials, or raw model messages. The three tests reuse repository test helpers; use the explicit pytest pythonpath above.
The published directory was independently rerun with that import configuration: 10 passed in 3.13 seconds. Original before/after XML bytes are preserved.
