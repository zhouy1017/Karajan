# Approved Candidate Checks (#94)

The controller derives every ordinary check from the original approved Run,
ExecutionPolicy v2 and captured Candidate. `ApprovedCandidateChecks` accepts only
Run/operation IDs and the principal. It records its subject, process intents,
observations and Evidence links inside the original admission operation. It does
not introduce a second scheduling database or accept caller commands, paths,
logs, exit codes, environment images or Candidate JSON.

This slice covers deterministic Checks. A passed batch retains
`review=not_run`, `local_gate_passed=false` and `delivery_eligible=false` while
independent Review is missing. It does not complete parent #13, PR delivery #14,
or the real Commander planning bridge #93.

## Public stages and subject

`advance(run_id, operation_id, principal=...)` advances one durable stage.
`get(...)` reads historical state; `reconcile(...)` inspects original effects and
exact Evidence; `cancel(...)` first persists the shared operation cancellation
and then performs optional cleanup without business locks. The fixed Host child
alone calls internal `consume_check(..., check_run_id, runner_identity=...)`.

The initial `validation.subject` has schema
`karajan.candidate-validation-subject.v1`, revision 1. It retains the original
capture digest, complete source/validation Candidate identities, approval,
Plan and ExecutionPolicy digests. Candidate identity includes repository/base,
tree/content/manifest/input/policy digests and baseline ID. The original
`execution.collection.candidate` pointer is unchanged.

The controller resolves all approved ordinary check IDs to exact versioned argv
and environment definitions. `independent_review` resolves to the separate Review
convention. A completed nonzero check does not suppress another independent
required check: the batch gathers its results before becoming `blocked`.
Inconclusive, unavailable, invalidated or cancelled execution prevents subsequent
new effects.

The same-content policy/Candidate handoff is implemented by the #100 trusted
binding producer, #96 CAS primitive and #101 subject consumer. The producer
derives Reviewer membership from the approved plan and current sources; the
consumer accepts only its durable operation transition and exact receipt. A CAS
rebind record by itself is not authority. The original capture cannot be replaced
by a caller Candidate. See the [versioned subject contract](candidate-validation-subject.md).

`subject_transition` progresses through `prepared`, `rebind_claimed`, `ready`
and `installed`. Pending transition fences old-cycle effects. Installation checks
current sources and actual quiescence, archives the old cycle in `validation.history`,
and compiles every original approved check with new Attempt/start/Evidence IDs.
`validation.review_binding` retains the full installed transition while the next
incoming intent is prepared. A→B→C preserves A as the capture anchor and B as the
direct predecessor. An unclaimed prepared intent may be archived and replaced;
claimed/ready work may only recover its exact receipt.

## Claims, current authority and process supervision

Each required check has stable check-run, local Check Attempt, fence, start,
activation and Evidence IDs. Its immutable execution document binds all four
entry IDs, root task, approval authorization, complete Candidate identity,
versioned check/environment, controller/runner source and time limits. Changing
lifecycle fields are excluded from that execution digest.

The stages are `prepared` → `claimed` → `host_prepared` →
`host_start_claimed` → `host_started` → `native_claimed` → `observed` →
`evidence_submit_claimed` → `recorded`. A committed uncertain claim is consumed;
reconciliation never returns it to an earlier phase or allocates a new ID.

The local `CheckAttemptManifest` has no fictitious model Profile, account or
billing binding. Host prepare and one-time control initialization precede the
durable Host-start claim. The child waits up to 10 seconds, outside business
locks, for the Host's actual direct-child registration and compares PID/birth.
Its native claim is committed before calling the runner.

At current effect admission, lock order is operation → Run → Project → Host.
The exact original approval, task, registered ExecutionPolicy, repository,
Candidate subject, environment/controller sources and shared budget are checked.
For rebound subjects the configured producer also rechecks current Reviewer
binding facts inside that same Project transaction. A missing producer or
unsupported current role qualification cannot enable Check effects.
The native Popen callback repeats current Host identity and the deadline after
the Host guard is acquired. Long process waiting and log reads occur outside
these business locks. No model Capacity admission, grant, provider credential or
settlement is used by a deterministic check.

The fixed runner materializes the complete CAS tree into a new isolated copy and
executes the approved command in its supported Linux Python 3.12 stdlib image.
The fixed child and private bootstrap reopen only existing controller stores.
Actual namespace isolation and environment evidence are provided by the fixed
runner's separate implementation and P tests; Host process control alone is not
presented as a sandbox.

## One shared Run process budget

`run_execution_budgets` lives in the admission database. Its first Writer
execution enqueue establishes `started_at`; it is not Run creation, owner
approval or the later Check stage. A committed Writer/Check/Reviewer process
claim counts conservatively toward `max_total_attempts`. This matches the
legacy supervised-process definition and is not a model-call or provider usage
counter. Check Host supervision and its namespace child belong to one Check
Attempt, not two independent task attempts.

The ledger retains original Run/root/operation/Plan ownership. Later operations
cannot reset the starting time or enlarge the original total limits. Replaying
the exact claim does not count twice; current authority separately rechecks the
claim and the strictest bound. No infrastructure retry or quality-repair cycle is
introduced by this slice. Different mandatory check IDs are not infrastructure
retries. Such future cycles must consume their own approved retry/quality
allowances as well as this shared total; the stored root ID is not proof that a
cycle was authorized.

An older v2 database without a trusted shared budget remains readable and
cancel/reconcile remains available. New effects fail with
`RUN_EXECUTION_HISTORY_RECONCILIATION_REQUIRED`; opening an existing store does
not add the budget table or fabricate an old start time. Default initialization
creates the table for new deployments.

Writer new-effect guards also recheck this ledger. Their final controller guard
compares the nonpersistent bound deadline after current source/Host checks.
Stopped Candidate collection uses a distinct historical writer-identity guard,
so expiry does not discard already produced, safely stopped output. These gates
bound controller effect admission. They do not establish an end-to-end hard
deadline across later native setup, Journal fsync and transport internals, nor
promise to terminate an already running Writer at the shared deadline.

## Evidence and recovery

Only the internal actual `CheckObservation` is accepted, with exact execution
and environment digests. A confirmed stop, complete log and actual completed
exit are required for a conclusive result. Log text saying “passed” is not an
exit observation. Unknown stop, incomplete/missing logs, timeout or unknown exit
cannot produce passed Evidence.

Before `CandidateStore.record_check`, the operation persists the complete
CheckResult request, its digest and actual log SHA/size. The fixed runner reads
only its exact controller-owned persisted log. The empty-output metadata header
is part of the runner's trusted capture format, not fabricated command output.

| Lost boundary | Recovery | Forbidden action |
| --- | --- | --- |
| Host start claim/reply | Inspect original Host Attempt and child facts | Repeat Host start or allocate another Attempt |
| Native claim/start/result reply | Inspect the fixed runner's original execution result | Repeat native Popen |
| Observation persisted before Evidence claim | First Evidence submission may be claimed once | Re-run the check |
| Evidence claim/commit reply | `lookup_evidence` with the full request and log identity | Re-submit uncertain Evidence or rewrite its artifacts |
| Cancellation with late Evidence | Retain the exact committed historical record | Promote it to current authorization or release Worker usage |
| Subject installation commit/reply | Read the same installed revision and Check IDs | Allocate another cycle or rebind Candidate |
| Old child/observation/Evidence after a switch | Resolve the original cycle by Check ID and retain its evidence | Restart the old child or overwrite the new cycle |

Absent facts remain `reconciliation_required` or `cancellation_pending`.
Cleanup of one available resource proceeds even when another optional Host,
runner result or Evidence store is unavailable. Candidate current gates still
evaluate the availability and validity of their evidence independently.

## Verification and current limits

`tests/runs/test_candidate_checks.py` uses real Project/Run/operation/Candidate
and Host persistence, with explicit native/Host-identity boundary doubles for
fault ordering. It also includes an actual local Host child. The separate
`test_candidate_checks_native.py` exercises the fixed production entry, actual
Host, namespace, CAS materialization and two owner-approved commands. Its
planning/model author remains an explicit fixture; no real Commander, model
qualification or provider call is claimed by these Checks tests.

The original Check implementation evidence remains in
[candidate-checks](../../examples/candidate-checks/README.md). The subsequent
handoff implementation is fixed at `1fc97849697cfe89a79595cba07e9ec028c6d0b2`; its
[publication index](../../examples/reviewer-validation-subject/README.md) retains
16 consumer C cases, the 26 existing Checks regression cases, producer and
independent reviews, and the two actual Linux namespace P cases. The latter run
A's two approved checks and rerun both for B, then independently exercise a running
A refusing a ready transition followed by concurrent advance/cancel. These are
distinct evidence scopes, not additive counts of unique tests.

The handoff P uses a separate fixed test child with explicit Reviewer qualification
and planning/Writer doubles. Its source bytes are recorded in each execution;
the production factory has no fixture flag. The normal factory with real current
qualification storage rejects unsupported Reviewer authority before Host.prepare.
Positive production Reviewer suite/credential configuration, actual read-only
Reviewer execution and Review Evidence, real planning, S, and the current PR's
G remain separate requirements. Passing local Checks does not complete them.
