# Independent execution-intent review

Scope: `orchestration/go_execution_intent.py` and the execution lifecycle bridge in `orchestration/admission.py`, in `codex/m3-go-task-runner` at base `f2d639559e738dfbb951163c6e8d83b460d758fb`. Reviewed against `.cache/go-execution-intent-design.md` (including its final implemented-contract section), `.cache/go-task-runner-acceptance.md`, CONTEXT.md, issue-tracker.md and the code-review skill's Standards/Fowler baseline. No product or shared author tests were changed.

## Standards

**0 actionable findings.** No documented hard-rule violation or actionable Fowler judgement was identified in this bounded change. The internal shared admission-row helpers, original Workspace validation and typed controller source input are consistent with the expressly chosen one-record design. The repeated Run/operation/principal identifiers match existing public domain boundaries; extracting them solely to reduce argument repetition would not improve the current seam. Lint/type checks are not counted as review findings.

## Spec

**0 actionable findings.** Independent Windows final: 9 passed, 0 skipped, 8.74s. Independent Linux final: 9 passed, 0 skipped, 6.07s. Public tests use real Project/Run/Workspace/Admission/Capacity/Host stores and actual temporary Git baselines. Qualification production, source digests and the claimed Host ProcessIdentity are explicitly synthetic at this component seam; no test calls Host.start, native or a provider.

Verified behavior:

- Four concurrent preparations yield one original operation/Attempt/intent without Capacity changes. Replaying an earlier command after cancellation returns current cancellation state.
- Three different claimed runner identities race through real SQLite; exactly one live result has claim_allowed=true. That permission is never persisted, and reopening cannot reclaim it.
- An actual committed Capacity activation can be adopted after its original expiry, but the combined startup/Capacity guard still rejects the effect and does not refresh expiry or rewrite Capacity history.
- Reconstructed read/reconcile/prepare calls do not recreate a renamed-away Run or Admission database.
- The original Admission.cancel endpoint persists both cancellation flags, retains pending cleanup and blocks all later claim/guard paths without refunding Capacity. Late Host observation and prepare replay do not clear cancellation.
- Mutating a yielded guard value and raising an exception does not mutate the stored intent or leave a lock behind.
- Real Operation→Run→Project→Capacity guard composition serializes cancellation and allows it to finish after release, without lock inversion.
- Changed controller sources may read/cancel history but cannot reclaim or advance that old source into a new effect claim.

The first independent Windows run also passed all 9 cases. Final runs followed only test formatting; the later module docstring shortening is nonsemantic. Product hashes are recorded before/after and were unchanged through review.

Limits: startup_guard and effect_claim_guard intentionally do not revalidate current business/capacity/Host authority by themselves. The actual consumer must compose them with fresh routing, Capacity and exact Host-owned runner guards. The ProcessIdentity input at this trusted component is not caller proof. Cancellation deliberately remains pending until the owner completes actual grant/Host cleanup. Read/reconcile return persisted history, not refreshed external state. These are explicit component boundaries, not newly found missing product capabilities.

The root controller must still bind the complete runner source/manifest/grant, commit claim before Popen, require the per-send guard and connect stop/capture/validation/review outcomes. This review does not claim the complete approved Task runner or v1 platform has been delivered.

Reproduce:

```text
python -m pytest .cache/go-intent-independent/test_independent.py -q -o "pythonpath=backend tests/runs tests/projects tests/routing tests/capacity tests/web tests/adapters/opencode"
```
