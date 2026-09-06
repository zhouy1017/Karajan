# Independent review of the initial Spark receipt patch

Scope: uncommitted `go_relay.py` and `test_go_relay_nullable_names.py` above HEAD `c63c7e36b17c2f8d8b041984e5bd3422bd72ffd6` in `codex/ci-spark-relay`. This is the initial Spark candidate, not a later follow-up or merged source. Author and reviewer are separate. Products remain unmodified by this reviewer.

Standards: **0 findings** against `AGENTS.md`, `docs/agents/issue-tracker.md`, and the code-review skill's Fowler judgment baseline. No lint findings are repeated here.

Spec: **1 open P2**, concerning the new regression's completion synchronization. No demonstrated new product defect in the bounded source change. The original COPILOT-RECEIPT-001 publication boundary is satisfied by this candidate in the deterministic experiment below; this does not close the whole CI correction while its own regression remains racy.

## SPARK-RECEIPT-001 — wait for handler completion before asserting final Journal state

`tests/adapters/opencode/test_go_relay_nullable_names.py:130–131` assumes that `future.result()` also completes the server's post-write receipt update and final Journal transaction. It does not: the client can return after reading exactly Content-Length while the handler continues. An independently injected HTTP transport cleanup barrier, with real loopback HTTP and SQLite, records HTTP 200 / all 446 bytes / `relay_completed=true` but Journal still `send_unknown`, outcome null. The new test's final `response_received` assertion deterministically fails under that valid ordering. This is a behavioral test correctness issue, not a requirement to mark the Journal complete before it actually commits.

Its unchecked `resume.wait(timeout=1.0)` at line 112 is also an unreliable held-state boundary: the server may continue after the timer even if the main test thread has not performed its intermediate assertions. This is the same synchronization finding, not separately counted.

Suggested correction: hold the actual socket writer until explicit release (a bounded watchdog may fail explicitly), obtain and verify the complete client response while it remains held, then release it. Use the public `relay.close()` result `closed` as the handler-completion barrier before asserting final `relay_completed` and Journal outcome. Never move Journal completion earlier merely to satisfy an immediate client-side assertion.

## Observed product semantics

- `publication-observed.json`: original real socket schedule; the entire HTTP 200 body is received while `wfile.write` is still paused after the OS send. Public `protocol_passed=true` and null-name count 1 are already visible. `relay_completed=false` and Journal `send_unknown` are accurate pending observations. After explicit release and successful close, delivery and Journal completion are both visible.
- `completion-lag-observed.json`: the independent red described above pauses only the supported injected HTTP transport's `close`. It does not mutate or simulate receipts or Journal persistence.
- `write-reset-observed.json`: actual client TCP reset after headers and before body causes an actual `ConnectionResetError` from the OS socket write. The previously published `protocol_passed=true` becomes false, `relay_completed=false`, and reason `RELAY_TRANSPORT_ERROR`. Complete parsed upstream facts remain, including stream termination and null-name count. The real Journal retains one call and records `response_received` with a failed outcome: this state means the upstream response was received, not that downstream delivery succeeded. It is not a refund or remote-stop assertion.

Resetting `protocol_passed` therefore makes it a provisional, conservative outcome flag rather than an immutable fact of upstream syntax validation. This is consistent with the already-existing client-close and Journal-completion failure paths, and preserves the previous final false result for a downstream write failure. No separate defect is asserted without a stronger immutable-field contract. Consumers must use the finalized receipt, reasons and Journal together for overall success.

The new pre-response publication is after the send/response boundary, and does not change `send_guard` lock ordering. Inspection of the other branch's guard implementation found no semantic conflict; an actually merged send-guard version was not executed by this review.

## Reproduction and evidence

Run from this worktree with the shared Python 3.12 environment:

```powershell
& C:/Users/Chooo/Playground/Karajan/.venv/Scripts/python.exe -m pytest .cache/spark-independent/test_response_publication.py -o 'pythonpath=backend tests/adapters/opencode' -o junit_logging=all --junitxml=.cache/spark-independent/independent.xml -q
& C:/Users/Chooo/Playground/Karajan/.venv/Scripts/python.exe -m pytest tests/adapters/opencode/test_go_relay.py tests/adapters/opencode/test_go_relay_journal.py tests/adapters/opencode/test_go_relay_nullable_names.py -o 'pythonpath=backend tests/adapters/opencode' -o junit_logging=all --junitxml=.cache/spark-independent/regression.xml -q
```

2026-09-06 Windows: independent **2 passed, 1 intentionally retained red, 0 skipped** in 0.74s; author regressions **85 passed, 0 skipped** in 9.82s. The independent red explicitly reproduces the unguaranteed completion expectation; it is not a claim that pending Journal state is itself a product failure. No blind repeats were used to erase it.

Evidence is C/P: actual local sockets, OS TCP reset and SQLite, synthetic HTTP upstream only. No native OpenCode, provider, real credential, GitHub write or Git mutation occurred. `before-sources.json`, byte copies of both initial files and `review.json` pin the initial source. Any later Spark correction needs its own source-bound review result; these initial results remain history.
