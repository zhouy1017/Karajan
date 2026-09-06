# Independent Host capture guard review

Base: `153482593380fdb5a8a5e16940f600c06acfd2ca`. Scope: `execution/host.py::RunnerHost.current_fence_guard`, against `docs/implementation/m3-go-task-capture.md`. Reviewer did not implement Host and did not read the author's new capture tests. No product edits, provider calls, real credentials or native Collector tests.

## Finding: HOST-CAPTURE-001 (P2, closed)

The new guard uses the existing ordinary SQLite `_connect` at `host.py:240`. If an already constructed Host's ledger subsequently goes missing, invoking this advertised read-only guard creates a fresh empty `runnerhost.sqlite3` before failing its SELECT. The absence of existing authorization correctly prevents capture, but the guard still modifies recovery state by manufacturing a new ledger.

Public reproduction creates a temporary RunnerHost, moves its database to a retained sibling, invokes the guard, and asserts no replacement was created. No SQL values or Host internals are fabricated. `missing-ledger-before.xml`: **1 failed**, 14 deselected. Original source SHA256: `dff2612c876e079d89225e6f5010d8ecedaa585a8d153fdb92c01c073491f6b1`.

The author applied the minimum change: `_connect(existing_only=False)` retains the existing default; only this guard passes true, using `Path.as_uri()` plus `mode=rw`, while keeping `BEGIN IMMEDIATE` and `query_only`. A normal `mode=ro` connection is unsuitable for an IMMEDIATE transaction that must exclude writers. The reviewer did not patch product code.

Independent final verification: **15 passed / 0 skipped / 3.23s**, `after.xml`, with the original test semantics unchanged. Fixed source SHA256: `d59de810fc5113b8e2f0d350a8446d0838244965083ce7cb19da7222b1edc184`. The missing-ledger case now rejects without creating a replacement; the real writer serialization controls still pass. No remaining finding in this bounded review.

## Other observed behavior

The initial **14 independent public cases passed** in 3.54 seconds (`final.xml`, before adding the missing-ledger case). Actual local Python supervisors and SQLite were used:

- Prepared, after_accept, and before_spawn crash states are denied without tool execution or database-byte changes.
- Finished launch identity matches manifest/activation/control; detached receipt grants no new activation and the guard never asks for a fresh clock value.
- Higher current fence, different authorization, disabled control and cancellation defeat a previously accepted/replayed result. Re-enabling control after cancellation does not erase the cancellation.
- Wrong caller fence/authorization and boolean fence are rejected.
- Real second-thread `set_control`/`cancel` wait until guard release, including a body exception; their new state is then enforced.
- A still-running trusted supervisor is allowed as documented, without claiming its native child stopped or a Candidate was accepted.

The guard is only Host identity serialization. Run/Project/operation approval, owned native stop/capture, source isolation and delivery validation remain separate duties. A lost start response alone is not acceptance: unresolved before-spawn states fail; persistent evidence of an actual started execution is what the new guard examines.

## Reproduction

From the worktree, using the repository's Python environment:

```text
python -m pytest .cache/host-capture-review/test_host_capture_independent.py -o "pythonpath=backend tests/execution" -q --junitxml=.cache/host-capture-review/after.xml
```

The independent script uses only `test_runnerhost.manifest` as existing sample data. Its actions and assertions are independent of `test_capture_fence.py`. Formatting only was applied after the original runs; semantics were unchanged. Existing XML files and `host-before.py.txt` are retained verbatim; final fix verification uses the separate `after.xml`.
