# Independent #90 lifecycle and one-time Host control review

Review base: `e00f619` (PR91). Candidate HEAD: `c2c4b145473a259582d0d37a01e423ca03f03b65`. Scope: the new lifecycle interfaces in `backend/karajan/orchestration/go_execution_intent.py`, plus `RunnerHost.initialize_control_once`. Products remained read-only. The reviewer did not author these interfaces.

**Standards: 0 findings. Spec: 0 findings in this bounded scope.** The review follows `AGENTS.md`, `docs/agents/issue-tracker.md`, the code-review skill's Fowler judgment baseline and the #90 scope snapshot `docs/planning/go-task-execution-issue.md`. It does not review the reviewer's own Collector implementation or claim completion of #90's native or remote acceptance.

## What was checked

`read_operation` and `open_existing` read the original approved operation and recover its retained source, without constructing a replacement execution or requiring a new executable/credential. Missing execution may invoke the explicitly lazy source compiler; invalid existing execution does not fall back to a new identity. Ownership is checked before observation or mutation. Reopened reads and repeated observations return detached data.

`freeze_launch` binds the original intent, Host manifest, activation ID/expiry, grant, process specification and bootstrap digest. Replay recomputes the controller specification and rejects a conflict; cleanup reads the original frozen record with `current_source=False`. A recorded Capacity command is a historical observation. `activation_guard`, `launch_preparation_guard`, `startup_guard` and the downstream business gates are still required before new effects. Restoring the old successful activation after cancellation does not reopen these gates.

`capture_recorded` requires the original intent/Workspace/projection/author/grant binding and committed runner claim. A first capture also requires current source and no cancellation. Exact already-committed replay can be read after cancellation/source change, but a different runner is rejected and the collection guard remains closed. `candidate_recorded` reads exact Candidate identity through the injected store and only links that existing object; it does not clear cancellation, release Capacity or pass validation/review.

`observe_execution` records owned Host and Journal snapshots, distinguishes mismatches/unavailable resources, retains native/provider stop as unknown, and does not stop a process, revoke a grant or refund a request. Observations with equal content do not append another record.

`initialize_control_once` uses the existing Host database and an immediate transaction to validate the prepared start ID, manifest fence and authorization. It inserts only if no control exists; an existing exact disabled control stays disabled, and a later/different control is rejected. Replays return `activation_allowed=False`. The caller still owns current business authorization.

Lock inspection: the intent methods check Run owner in a short read before acquiring the operation transaction. Lifecycle guards hold operation first; downstream consumers then acquire Run/Project/Capacity/Host as specified. `freeze_launch` invokes its trusted metadata compiler while holding operation, and does not call Host/provider. Observation holds operation while reading Host/Journal; cancellation commits the operation first and releases it before external cleanup. No new reverse acquisition was found in the reviewed methods.

## Independent public-interface matrix

The nine cases use real approved Run/Project/Capacity/operation stores, real SQLite and Git/CAS where required. Qualification and source producers are explicit existing synthetic fixtures. The capture case uses a synthetic `GoTaskResult`/Host identity solely to inspect persistence: it does not qualify native stopping or model behavior. No ProcessSpec is executed.

1. Capacity activation commits, its reply is not recorded in the operation, then cancellation and source change occur. Reopen restores only the exact old receipt and expiry; cancelled activation/preparation/start guards reject; resource ledgers stay byte-identical.
2. Frozen launch response is mutated by a reader; after cancellation, source change and removal of the live launch compiler, cleanup/reconcile still return the exact original detached launch without writes.
3–4. Original operation database disappears before `read_operation` or `open_existing`; each fails without creating a replacement database or altering the saved original.
5. Host start key and Journal grant binding are independently mismatched. Observation records unknown/binding_mismatch without mutating either resource or clearing cancellation, and exact replay is stable.
6. Initial control response is lost; control is withdrawn, Host is reopened, and replay still reports disabled without changing bytes or starting a supervisor.
7. Real concurrent withdrawal and initialization serialize to a disabled final control. A later fence/auth change cannot be overwritten by replay.
8. Capture commits and Candidate commits before their acknowledgement is linked; cancellation/source change happen first. Exact capture replay remains history, another runner cannot replay it, collection remains blocked, and the exact Candidate is linked while cancellation and resource history are preserved.
9. Another principal cannot read, clean up, observe or freeze the operation; the operation database is unchanged.

## Results and limitations

2026-09-06: **Windows 9 passed, 0 skipped, 9.57s**; **WSL/Linux 9 passed, 0 skipped, 8.12s**. The first independent run had 8 passed and one reviewer expectation error: an unstarted Host's documented phase is `prepared`, not the reviewer's invented `unattempted`. `test-initial.py.txt` and `initial.xml` retain that evidence. The final assertion uses `prepared` and additionally requires no supervisor; no product change was made or product finding opened for this correction.

The initial sandbox WSL launch was denied by WSL service access before tests ran; the authorized local WSL test command then ran successfully. This is not counted as a product test failure. No provider, key, native runtime, GitHub operation or project Git mutation occurred. The fixture creates ordinary temporary Git test repositories to exercise existing public baseline APIs.

These are C/P persistence and process-control metadata observations. Actual Host child identity, namespace stopping, per-send business gating and real service qualification remain covered by separate author/integration evidence. Missing validation or Reviewer qualification remains a waiting condition; no delivery or whole-issue completion is asserted here. No static-tool checks were duplicated as review findings.

## Commands

From this worktree:

```powershell
& C:/Users/Chooo/Playground/Karajan/.venv/Scripts/python.exe -m pytest .cache/task-intent-independent/test_intent_boundaries.py -o 'pythonpath=backend tests/runs tests/projects tests/candidates tests/routing tests/capacity tests/execution tests/web' --junitxml=.cache/task-intent-independent/final-windows.xml -q
wsl --cd /mnt/c/Users/Chooo/Playground/Karajan/.cache/go-task-execution-v2 /tmp/karajan-candidate-mode-qy6_mqo2/venv/bin/python -m pytest .cache/task-intent-independent/test_intent_boundaries.py -p no:cacheprovider -o 'pythonpath=backend tests/runs tests/projects tests/candidates tests/routing tests/capacity tests/execution tests/web' --junitxml=.cache/task-intent-independent/final-linux.xml -q
```

`review.json` and `source-before.json` record the scope and exact hashes. Both reviewed product hashes are unchanged before and after the two final executions. Original failure history is retained separately from final passing evidence.
