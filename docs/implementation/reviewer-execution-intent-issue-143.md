# #143 Reviewer execution intent

Candidate base: `b1bd5e9e58eba9dd6d5d4e3d63b815edadd265d2`.

This slice owns only the Reviewer execution bootstrap, binding, intent ledger,
and fixed runner. It does not invoke native Review, a provider, HTTP, Journal,
Evidence, parser, or delivery.

## Evidence terminology

- **C**: a product-behaviour test passed. It is not a static-source claim.
- **P**: that test ran locally in WSL/Linux against the stated executable/store
  boundary. A test-only frozen qualification or deployment port remains just a
  fixture and never qualifies a profile or proves service behaviour.
- **S**: real service evidence. None was collected for this slice.

## Original acceptance-condition matrix (2026-09-08)

| Original acceptance condition | Behavioural evidence and result | Remaining gap |
| --- | --- | --- |
| Real lineage/CAS/Checks/Capacity produce one complete intent; bad material has no new intent/effect | **C passed**: `test_prepare_is_durable_and_freezes_compiler_identity` uses the approved Reviewer/Worker/Candidate/Checks path and verifies the v2 input, immutable identity, and unchanged Capacity snapshot. `test_tampered_persisted_intent_is_rejected_without_a_host_effect` rejects a changed durable Candidate binding and observes no Host record. | No S/native/provider evidence. |
| Exact replay, concurrent use, reopen, and tamper retain/reject the same identity | **C/P passed**: `test_concurrent_independent_facades_replay_one_prepared_identity` synchronizes two independently constructed facades after compilation and proves same-key convergence on one execution while a different key rejects. The production existing-only factory reopens its seeded identity and leaves the execution DB unchanged. Existing schema-valid ledgers in the actual registered repository, its parent symlink alias, and an in-repository hardlink are rejected before SQLite opens them; the original bytes and modes remain unchanged. | No S/native/provider evidence. |
| Every prepare/claim re-enters current admission/compiler and source guard; changed current facts block effects | **C/P passed**: `test_current_qualification_change_blocks_next_host_prepare` changes current qualification after intent persistence and observes no Host preparation. `test_expiry_after_real_sqlite_writer_wait_blocks_the_actual_effect` holds real Host or execution SQLite writers until the retained reservation/window has expired, then proves no new Host/control write or observer claim. The factory uses the actual pinned Linux OpenCode binary and tokenizer through `deployment_source`, with no provider call. | No native Review/provider assertion. |
| Host prepare/control and read-only inspection are durable; no facade start/new session on recovery | **C passed**: `test_fixed_host_prepare_is_replayable_without_starting_native` verifies same prepared/start identity; `test_prepare_rejects_second_key_and_never_prepares_host` verifies no Host record before prepare. | No native execution claim. |
| A registered direct Host child alone gains the first effect claim; lost reply/reopen never gets a second right | **P passed**: normal, committed-lost-reply, and concurrent modes run in one actual registered direct child. The lost-reply test first observes the original Host terminal record, then replays while asserting its prepared/attempt/start, supervisor, and persisted runner identities plus exactly one persisted launch and child registration. Completion/usage observations are intentionally allowed to progress. Concurrent reopened facades yield exactly one claim right. | No native Review/provider assertion. |
| Cancellation permanently blocks future claim while history stays inspectable; delivery remains false/not_run | **C passed**: `test_cancel_serializes_with_actual_inspect_host_writer_and_stays_durable` races a real `inspect_host` ledger update with cancellation and retains the durable cancellation without a stale WAL upgrade; direct-child recovery then blocks a later claim. Prepared intent asserts `delivery={eligible:false,state:not_run}`. | No remote stop, settlement, or provider assertion. |
| Public state is non-sensitive; quality checks and no native/HTTP/model/Journal grant side effects | **C passed (bounded)**: the Linux factory test installs counters on the actual configured Host start, Go Journal grant/call, Candidate gate, and Evidence-save entry points; it runs facade prepare/freeze/failed-unregistered-claim/reopen/cancel. The required Candidate gate is observed only as its read-only current-context check, while Host start, Journal grant/call, and Evidence writes are zero and the real configured Journal bytes are unchanged. It also checks the public persisted intent lacks credential/seal labels. This is C evidence only: Relay/HTTP transport is not composed by this leaf, so it is not a claim about Relay implementation. | No S evidence and no broad claim about unrelated modules or provider/Journal delivery. |

## Commands and observed results

All WSL commands used `/tmp/karajan-candidate-mode-qy6_mqo2/venv/bin/python`,
`PYTHONPATH=backend:tests:tests/projects:tests/runs:tests/candidates`, and a
fresh `/tmp/karajan-dg01-*` base temp. The factory test used
`KARAJAN_GO_TOKENIZER_DIRECTORY=/mnt/c/Users/Chooo/Playground/Karajan/.cache/go-context-artifacts`
and `KARAJAN_OPENCODE_LINUX_BINARY=/mnt/c/Users/Chooo/Playground/Karajan/.cache/go-linux-runtime/package/bin/opencode`.

| Command | Result |
| --- | --- |
| Candidate `fd3f765` targeted lost-reply assertion | **Red (historical)**: the earlier `replayed.state == before_replay.state` terminal-state assertion exposed recovery-state timing; this was retained as the baseline diagnostic rather than treated as product evidence. |
| Candidate `1d7fcd8` two-module command supplied by Commander | **Red (historical)**: `1 failed, 19 passed in 29.95s`; `replayed == reopened.host.inspect(...)` raced a legitimate `exit_code: None -> 0` completion update. This is a mutable-snapshot regression, not a second launch. |
| Current bounded candidate: lost-reply test, three fresh `/tmp/karajan-dg01-reviewer-green-{1,2,3}` directories after Host terminal observation | **P passed**: each run `1 passed, 15 deselected` (5.72s, 5.76s, 5.64s). This is the new deterministic regression feedback loop. |
| Current repaired candidate: bootstrap + intent modules with `/tmp/karajan-dg01-reviewer-owned-final4` | **P passed**: `27 passed in 38.72s`. This includes repository containment, cold historical reopen, writer-wait expiry, cancellation-WAL, and connected-boundary regressions. |
| Current repaired candidate: `tests/runs/test_reviewer_execution_bootstrap.py tests/runs/test_reviewer_execution_intent.py tests/runs/test_reviewer_binding.py tests/runs/test_reviewer_input_approved.py tests/runs/test_admission_guard.py tests/runs/test_task_admission.py tests/execution/test_runnerhost.py -q --basetemp /tmp/karajan-dg01-reviewer-affected-final` | **P passed**: `155 passed in 122.43s`. This result is for the repaired candidate, not retrospectively for an older candidate. |
| Current repaired candidate: `C:/Users/Chooo/Playground/Karajan/.venv/Scripts/python.exe -m pytest tests/runs/test_reviewer_execution_bootstrap.py tests/runs/test_reviewer_execution_intent.py -q --basetemp C:/Users/Chooo/AppData/Local/Temp/karajan-reviewer-windows-final` | **P passed**: `22 passed, 5 skipped in 44.52s`; the Linux-only factory/direct-child evidence and an unavailable Windows directory-symlink capability were skipped. |
| `C:/Users/Chooo/Playground/Karajan/.venv/Scripts/python.exe -m ruff check .` | `All checks passed!` |
| `C:/Users/Chooo/Playground/Karajan/.venv/Scripts/python.exe -m mypy backend` | `Success: no issues found in 154 source files` |

## Independent-review repairs on this candidate

- Standards P1 repository-layout blocker: the ledger is checked against the
  actual existing `ProjectRegistry` roots before any SQLite open/write;
  direct, parent-symlink, and hardlink paths are covered where the platform
  permits them.
- Standards/Spec temporal blocker: optional Host callbacks run immediately
  before new Host prepare/control rows, and the claim rechecks retained
  reservation and Reviewer windows immediately after its execution writer is
  acquired. They use held scalar facts and do not reopen Admission/Project
  writers under the Host lock.
- Standards cancellation blocker: cancellation takes the execution writer
  before reading/updating its record, preventing a stale WAL read promotion.
- Spec historical-reopen blocker: `ReviewerExecutionHistory` is an explicit
  read-only interface returned when a current runtime, tokenizer, Journal, or
  credential-seal asset is absent; it creates nothing and exposes no effect
  methods.
- Spec zero-effect-evidence blocker: the connected-boundary regression records
  the read-only Candidate gate separately from zero native/Journal/Evidence
  writes and unchanged configured Journal storage. Relay is not composed here,
  so no Relay claim is made.

The affected command ran in a local captured process because it exceeds the interactive 30-second yield; its exit code and final pytest summary above were observed before recording this result.
