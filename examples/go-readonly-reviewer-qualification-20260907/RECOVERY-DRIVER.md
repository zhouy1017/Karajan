# Issue 124 qualification stage driver

`qualification_recovery_driver.py` is the only future ordered start path. It
persists an atomic, redacted JSON receipt for `preflight`, fixture/source
preparation, start intent, the qualification claim and its exact readback,
membership positive, record revoke, negative/history, and completion. The legacy
`run_issue107_ordered_consumer.py execute` is sealed and cannot be reused.

The fixed WSL command must use the existing offline environment and checkout:

```text
wsl.exe -d Ubuntu -- bash -lc 'cd /mnt/c/Users/Chooo/Playground/Karajan/.cache/dispatch-qualification-driver && /tmp/karajan-candidate-mode-qy6_mqo2/venv/bin/python examples/go-readonly-reviewer-qualification-20260907/qualification_recovery_driver.py preflight --private-root /home/chow/karajan-issue107-controller --receipts /home/chow/issue107-receipts'
```

`resume` has no qualification call. It reads only the fixed command/start and
record, including the Store's separate revoke receipt and the source-bound
execution expiry. If a Store effect committed but its local receipt did not,
resume reopens that exact original identity: a completed qualification is found
by command key, a ready membership is read from its original admission SQLite
operation, and a revoke is read from its original Store receipt. It never
rebuilds a Plan, creates a start, or repeats an ambiguous effect. If the
original record expired before any durable positive membership receipt, it
records `QUALIFICATION_EXPIRED`. If a durable positive receipt is recoverable
and the original record was revoked, it performs only the same consumer's
negative/history observation; it never represents that as a current
qualification. `execute` is a future separately authorized effect and is not
run by this C/P slice.

Before a membership positive callback, the driver writes `positive_claimed`
under a process-wide receipt lock. Only the invocation that committed that claim
may call the consumer. A lost reply, an old `positive_observed=unknown`, or a
pre-existing claim can only be reconciled by the original admission and
CandidateStore receipt; absence remains unknown. Legacy JSON receipts are
validated and imported into SQLite before any SQLite stage is written, so an
older unknown receipt cannot be hidden by a newer passed value. The negative
observer reopens the original fixture stores in `existing_only` mode and checks
the current consumer guard under its operation, Run, and Project locks. A ready
history alone is never a revoke refusal.

The fixed start's `expires_at` bounds the suite call window. A completed record
is consumed only through its own `valid_until`; recovery does not extend either
deadline. Its membership receipt is compared through the canonical Candidate
identity, and qualification ownership is persisted before the membership effect.

## C/P acceptance matrix

| Boundary | Local evidence | Result | Scope |
| --- | --- | --- | --- |
| Original command/record reopen | real read-only SQLite fixtures with actual Store-shaped start, record and separate revocation fields | passed | C/P |
| Consumer success before local receipt | original membership operation is read back through `positive_history`; the consumer callback is not called again | passed | C/P |
| Revoke commits but reply is lost | exact Store revoke readback is persisted as `recovered`; no second revoke | passed | C/P |
| Public Store revoke reply loss | real `ProfileQualificationStore` / SQLite local-fixture record commits revoke before injected reply loss; resume reads it once | passed | C/P |
| Qualification reply loss | fixed command is read back and resumed; no second qualification call | passed | C/P |
| Receipt publication failure | injected `os.link` publication failure leaves the already committed SQLite stage readable | passed | C/P |
| Ambiguous membership positive | durable `positive_claimed` and a two-process resume race observe exactly one consumer callback | passed | C/P |
| Legacy receipt migration | an old JSON `unknown` is imported before SQLite and rejects a conflicting `passed` write | passed | C/P |
| Expired or unknown original state | no consumer or revoke call; status remains expired/unknown | passed | C/P |

Run the local evidence without a Go suite:

```text
PYTHONPATH=backend C:/Users/Chooo/Playground/Karajan/.venv/Scripts/python.exe -m pytest tests/tools/test_issue107_qualification_recovery_driver.py -q

$env:PYTHONPATH='backend;tests/runs;tests/projects'; C:/Users/Chooo/Playground/Karajan/.venv/Scripts/python.exe -m pytest tests -q -k test_issue107_identity_precedes_real_ready_reply_loss
```

The local tests use controlled Store adapters where an effect must be injected,
real SQLite admission data and receipt files, and one real public
`ProfileQualificationStore` backed by a local fixture. They prove C/P control
flow and recovery boundaries only. They do not perform or prove #107's binding
positive control, official Go qualification, or any S/G acceptance.
