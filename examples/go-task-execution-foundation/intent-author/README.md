# Durable Go execution intent author evidence

Base: `f2d639559e738dfbb951163c6e8d83b460d758fb` in the `go-task-runner`
worktree. This slice owns only the new intent module, the admission state bridge,
and its public tests. No provider calls, real credential reads, git commits or
additional Task state database were performed.

## Result

- Public intent tests: **23 passed**, Windows, 22.20 seconds (`author-final.xml`).
- Existing admission/workspace regression: **26 passed**, Windows, 16.13 seconds
  (`admission-workspace-regression.xml`).
- Ruff check/format and mypy for the two owned source modules passed.
- The same **23 cases passed on WSL Linux**, 13.06 seconds, no skips
  (`author-linux.xml`). `linux-check.json` verifies unchanged source hashes and
  references the preserved original freeze. Pytest emitted one cache-write
  permission warning for the existing Windows cache; execution and JUnit output
  completed normally.

The tests use real Run, Project, Admission, Capacity, Candidate and Host SQLite
stores plus a temporary Git repository. Existing `projected` qualification data
is an explicitly synthetic producer fixture; these tests create no actual Go
qualification. Claim tests inject a synthetic typed ProcessIdentity at this
trusted internal seam. They verify durable claim behavior, not Host runner
authentication; the separate Host port must provide and recheck that identity.
One test uses real Host.start with `after_accept` crash injection, matching its
clock to the synthetic Capacity clock, and stops before spawning a child.

The meaningful boundaries exercised are one original operation and reservation;
no implicit Workspace capture; owner and command identity; source changes;
missing-ledger reads; detached no-clock/no-write status; original activation
receipt/expiry recovery after a lost response; wrong Host start identity;
prepared and unknown launch states not conferring effect authority; racing
claims; lost claim responses; stale PID births; late Host observations;
cancellation persistence without refunds or remote-stop claims; and read-only
operation guards serializing cancellation.

## Preserved development observations

- `prepare-red.xml`: actual initial public test failed because the new module did
  not exist; its real-store fixture completed successfully.
- `prepare-green.xml`: that original behavior passed after implementation.
- `author-first.xml`: 20 passed, one test failed because it expected NOT_FOUND for
  the existing planner owner check's USER_DECISION_REQUIRED. The test expectation
  was corrected; the product authorization behavior was unchanged.
- `author-final.xml`: final 23 cases passed after adding startup_guard and the
  real Host lost-acceptance boundary.

## Public controller interface

Construct `GoExecutionIntents(admissions, source=GoExecutionSource(...), host=host)`.
The frozen source dependency contains actual `runner_source_sha256` and
`native_source_sha256` from the controller's fixed compiler. Public command
arguments identify run/operation/principal, plus a prepare command key. They do
not supply Profile, prompt, budget, credentials or replacement grant JSON.

`prepare_intent` stores `execution` inside the existing operation. `read` and
`reconcile` return detached historical state. Repeating prepare returns current
state and cannot restore pre-cancellation state. `activation_recorded` reads the
original Capacity activation command only. `record_host_prepared` and
`host_started` use the owned Host's inspect result; they accept no caller
Snapshot. `mark_start_unknown` commits the one Host launch intent first.

`effect_start_claim` accepts the typed identity read from Host's current runner
guard after releasing that Host lock. It commits once and returns an ephemeral
`claim_allowed=True` only on the first live call. Replays, including the original
PID/birth, return false without a ledger write. The flag is never persisted.

`startup_guard` and `effect_claim_guard` hold the operation transaction read-only
before Run, Project, Capacity and Host guards. The actual native-start boundary
must recheck the exact claimed PID/birth in Host.current_runner_guard. A failed
recheck consumes the claim without effects. Never wait for child registration
while holding these guards. No method in this slice launches or sends anything.

`cancel_intent` uses the common admission cancellation entry. The operation and
its nested execution both retain cancel_requested; the execution coordinator
must perform owned grant/Host cleanup. No generic result payload or fabricated
Candidate/usage/remote-stop evidence is accepted. That concrete integration is
the next controller step.

## Reproduction

From the worktree, with the repository development environment:

```text
python -m pytest tests/runs/test_go_execution_intent.py -o "pythonpath=backend tests/runs tests/projects" -q
python -m pytest tests/runs/test_task_admission.py tests/runs/test_task_workspace.py -o "pythonpath=backend tests/runs tests/projects" -q
python -m ruff check backend/karajan/orchestration/go_execution_intent.py backend/karajan/orchestration/admission.py tests/runs/test_go_execution_intent.py
python -m ruff format --check backend/karajan/orchestration/go_execution_intent.py backend/karajan/orchestration/admission.py tests/runs/test_go_execution_intent.py
python -m mypy backend/karajan/orchestration/go_execution_intent.py backend/karajan/orchestration/admission.py
```

The executed Windows interpreter was
`C:\Users\Chooo\Playground\Karajan\.venv\Scripts\python.exe` with the explicit
pythonpath override shown above. `freeze.json` binds final product and test bytes.
