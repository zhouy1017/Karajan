# Issue 107 fixed consumer negative control

`prepare_issue107_consumer.py negative` constructs a controller-owned, durable
fixed Plan and Candidate fixture and calls the production
`ApprovedReviewerBindings.advance` consumer. It contains no transport, relay,
credential-value, Reviewer Task, Candidate Review, Review Evidence, capacity
reservation, or model-execution path.

The fixture is deliberately labelled as a controller Plan/Candidate fixture.
The Plan API's required planning fields are structural lineage only; they do not
claim a Commander or Worker execution. The Candidate uses the real
`CandidateStore` and its frozen static private controller file, solely so the
consumer can verify an exact immutable Candidate identity. The fixture's static
permission metadata is discarded by `ApprovedReviewerBindings._compiled` before
membership: only the actual `ProfileQualificationStore._facts` result can make a
Reviewer eligible.

With the existing `issue107-official-go-reviewer-20260907-attempt2` record
revoked, the real Store returns `QUALIFICATION_REVOKED` for
`readonly-reviewer-107`. The consumer result is `blocked`, with
`REVIEWER_QUALIFICATION_REQUIRED` and `NO_ELIGIBLE_PROFILE`; no subject
transition was staged and `actual_reviewer_attempt` is null. The corresponding
sanitized record is `issue107-consumer-negative.json`.

This is a negative preparation control only. It does not establish the missing
current positive binding and it did not begin another official suite or send any
Go request.
