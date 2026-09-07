#115 Reviewer admission seam

`ApprovedTaskAdmission` derives Reviewer admission solely from persisted
`run_id`, task, operation and principal. It locates the one approved Worker
operation, and `ApprovedRunRouting` derives the Reviewer assessment from the
current validation subject, final Checks, locked binding, and the complete
captured author vector. The selected Reviewer gets a separate operation,
Attempt/context, capacity request, and reservation; neither a Worker operation
nor a provisional candidate is an execution identity.

The held order at Reviewer admission and effect is operation -> Run -> Project
-> Capacity. The Reviewer's original Run ledger remains held through the
Capacity admission callback, which checks the deadline at the write boundary.
`CapacityStore.admit` invokes its internal `before_reserve` callback only after
its write lock and admissibility decision; historical exact receipts skip it.

Reviewer activation persists the deterministic `reviewer-activate:<operation>`
intent before calling Capacity. `get` and `reconcile_reviewer` only read that
exact receipt. The future #116 consumer calls `reviewer_reserved_effect_guard`
by Reviewer operation ID; the guard rechecks original Worker lineage, current
subject/binding/source facts, cancellation, the exact active capacity hold, and
original Run budget. It creates no native process claim or provider effect.

C coverage is recorded precisely in `issue115-candidate-manifest.md`. T2/T3
native execution, real profile qualification, native input/send/Evidence and
remote settlement remain not_run and outside this #115 slice.
