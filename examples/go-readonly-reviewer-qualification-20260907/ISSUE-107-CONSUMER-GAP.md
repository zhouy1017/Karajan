# Remaining Issue 107 consumer control

The official record from `issue107-official-go-reviewer-20260907-attempt2` was intentionally revoked after the direct Store consumption check. A history-only read still returns that record, while a current `facts_for_profile(..., scope="readonly_reviewer_tools")` call returns `QUALIFICATION_REVOKED`. This is the required negative control. It also means `ApprovedReviewerBindings.current_locked` cannot now provide the required positive control from that record, and the record must not be revived, rewritten, or treated as current.

No further Go/provider call is authorized by this artifact. The controller fixture preparation is fixed as follows:

1. Create one controller-owned, explicitly labelled `issue107-fixed-plan` in the private Linux controller state. It contains one T1 Worker predecessor and one T1 Reviewer membership item using `readonly-reviewer-107`, tools `["read"]`, existing-file projection, the recorded 12,288/4,096/16,384 context limits, and the exact parser revision. It is not a Commander result, Candidate Review, Reviewer Task, capacity reservation, or Check execution.
2. Bind that Plan through the existing approved Run/operation lineage and call `ApprovedReviewerBindings.advance` only far enough to exercise its internal `current_locked` guard and produce the ordinary membership preparation receipt. Record the profile/source/qualification-reference digests, binding state, and the absence of a Reviewer attempt or Task effect.
3. Call the same consumer after qualification revocation and require `REVIEWER_QUALIFICATION_REQUIRED` (or its precise current-source failure code); then read historical qualification by its original ID. This does not send a model request.

Because the old record is revoked, step 2 requires one explicitly approved **new** `qualify_runtime_tools` command/start. It must use the same fixed source and only one three-scenario suite, with at most six requests per scenario and eighteen in total. The consumer positive control must run before that new record is revoked. If the new suite fails, has unknown sends, or the current binding cannot compile, preserve that record and stop; do not create a further start. This is a missing consumer acceptance step, not a way to restore the revoked attempt-2 record.

The existing attempt-2 six successful sends, same-key replay, direct Store-facts positive control, and revoke negative control remain immutable evidence. No product source change is required for this gap; it is an ordering error in the one-shot operator script.
