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

## C/P acceptance matrix

| Boundary | Local evidence | Result | Scope |
| --- | --- | --- | --- |
| Original command/record reopen | real read-only SQLite fixtures with actual Store-shaped start, record and separate revocation fields | passed | C/P |
| Consumer success before local receipt | original membership operation is read back through `positive_history`; the consumer callback is not called again | passed | C/P |
| Revoke commits but reply is lost | exact Store revoke readback is persisted as `recovered`; no second revoke | passed | C/P |
| Public Store revoke reply loss | real `ProfileQualificationStore` / SQLite local-fixture record commits revoke before injected reply loss; resume reads it once | passed | C/P |
| Qualification reply loss | fixed command is read back and resumed; no second qualification call | passed | C/P |
| Receipt publication failure | `os.replace` failure leaves no partial stage receipt | passed | C/P |
| Expired or unknown original state | no consumer or revoke call; status remains expired/unknown | passed | C/P |

Run the local evidence without a Go suite:

```text
PYTHONPATH=backend C:/Users/Chooo/Playground/Karajan/.venv/Scripts/python.exe -m pytest tests/tools/test_issue107_qualification_recovery_driver.py -q
```

The local tests use controlled Store adapters where an effect must be injected,
real SQLite admission data and receipt files, and one real public
`ProfileQualificationStore` backed by a local fixture. They prove C/P control
flow and recovery boundaries only. They do not perform or prove #107's binding
positive control, official Go qualification, or any S/G acceptance.
