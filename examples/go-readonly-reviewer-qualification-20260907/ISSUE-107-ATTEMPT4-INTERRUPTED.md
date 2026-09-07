# Issue 107 attempt 4 interrupted before consumer control

The reviewed ordered command
`issue107-official-go-reviewer-20260907-ordered-attempt4` created one immutable
start using the frozen official source, `readonly-reviewer-107@1`, and its
current controller credential generation.  Its three fixed scenarios completed
with two `response_received` Journal calls each: six actual official requests
total.  This remains within the per-scenario six-request and per-start
eighteen-request limits.  The qualification record is `passed`; the suite's
three own grants are revoked as its normal cleanup.

The process did not persist the ordered driver's final evidence, a qualification
record revoke, or an `ApprovedReviewerBindings` transition.  The only durable
consumer operation remains `controller_fixture_only`, with no transition and no
actual reviewer attempt.  Consequently there is no evidence that
`positive_result` completed, nor that the required `prepared -> ready`
membership-only positive control ran.  The absence of a retained command exit
code or stderr prevents attributing that interruption to a specific consumer or
product failure; it is recorded as an operator/execution-stage unknown whose
upper bound is before the first persisted binding transition.

An attempted no-model continuation script subsequently failed during Python
module import because it used the system interpreter without `pydantic`.  The
failure occurred before controller construction, Store access, consumer access,
or any Go effect.  It did not create a new start, qualification call, request,
or record mutation.

The immutable start expired at `1788766446.7067192`.  A later read-only
observation at `1788766617.0048356` found it expired, still unrevokeed, and with
the Journal still at exactly six calls.  No attempt was made to bypass the
current qualification guard, extend validity, change the clock, invoke the
consumer, revoke the record, or make a fifth start.  Its consumer positive,
post-positive revoke, same-consumer negative, and historical consumer readback
are therefore **not_run / expired**, not a passing S/C result.

The public, redacted supporting records are:

- `issue107-attempt4-prestart-freeze.json` for the source, Profile, generation,
  and asset freeze before official effect;
- `issue107-attempt4-readonly-reconciliation.json` for the completed record and
  six-call reconciliation before expiry;
- `issue107-attempt4-consumer-readonly-trace.json` for the pre-existing fixture
  operation state; and
- `issue107-attempt4-final-readonly-state.json` for the final expired state.

These files contain only hashes, fixed identifiers, statuses, aggregate counts,
and white-listed state; they do not contain provider key material, raw private
database values, or user review content.
