# Issue 107 attempt 3 ordering gap

The one explicitly authorized third start used command
`issue107-official-go-reviewer-20260907-attempt3`. Its immutable start completed
with the fixed `clean_review`, `defect_review`, and `denied_read` sequence. Each
scenario recorded two `response_received` calls: six actual requests in total,
within the per-scenario six-request and total eighteen-request limits. The
sanitized completion, same-key zero-request replay, fixed source/profile
generation, and history readback are retained beside this note.

The attempt ran through the prior controller's `run` routine. That routine
performs its direct Store read and revoke as part of the command before returning
to the operator. Therefore the successful record was already revoked before the
separately prepared production `ApprovedReviewerBindings.advance/current_locked`
positive consumer could be called. The post-revoke same-consumer negative control
correctly reports `QUALIFICATION_REVOKED`, but it cannot replace the missing
current positive consumer control.

This is an operator ordering gap, not a model or product result. No second
attempt3 command or extra Go request was sent. The controller-private Store and
Journal retain the raw start, grant, call, completion, revocation, and history
records; public evidence keeps only hashes, counts, statuses, token aggregates,
and the documented `provider_remote_stop: unknown` limitation.

Issue 107 remains incomplete: its required positive
`ApprovedReviewerBindings.current_locked` consumption was **not_run**. Neither
this record nor the prior revoked records may be revived, rewritten, or treated
as a current qualification.
