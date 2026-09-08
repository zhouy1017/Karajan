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
| Exact replay, concurrent use, reopen, and tamper retain/reject the same identity | **C/P passed**: `test_concurrent_independent_facades_replay_one_prepared_identity` synchronizes two independently constructed facades after compilation and proves same-key convergence on one execution while a different key rejects. The production existing-only factory reopens its seeded identity and leaves the execution DB unchanged. Empty, malformed, deleted-after-open, and hardlinked ledgers reject without repair. | No S/native/provider evidence. |
| Every prepare/claim re-enters current admission/compiler and source guard; changed current facts block effects | **C/P passed**: `test_current_qualification_change_blocks_next_host_prepare` changes current qualification after intent persistence and observes no Host preparation. Replacing the facade's own valid descriptor also makes the next `freeze_launch` reject while historical `read` remains available. The factory uses the actual pinned Linux OpenCode binary and tokenizer through `deployment_source`, with no provider call. | No native Review/provider assertion. |
| Host prepare/control and read-only inspection are durable; no facade start/new session on recovery | **C passed**: `test_fixed_host_prepare_is_replayable_without_starting_native` verifies same prepared/start identity; `test_prepare_rejects_second_key_and_never_prepares_host` verifies no Host record before prepare. | No native execution claim. |
| A registered direct Host child alone gains the first effect claim; lost reply/reopen never gets a second right | **P passed**: normal, committed-lost-reply, and concurrent modes run in one actual registered direct child. Lost-reply recovery accepts the actual canonical Host terminal state (`exited` is valid), while asserting the original prepared/attempt identity, acknowledged launch identity, supervisor/process identities, and no second start. Concurrent reopened facades yield exactly one claim right. | No native Review/provider assertion. |
| Cancellation permanently blocks future claim while history stays inspectable; delivery remains false/not_run | **C passed**: cancellation test blocks Host prepare; direct-child recovery test cancels after reopen and blocks a later claim. Prepared intent asserts `delivery={eligible:false,state:not_run}`. | No remote stop, settlement, or provider assertion. |
| Public state is non-sensitive; quality checks and no native/HTTP/model/Journal grant side effects | **C passed (bounded)**: fixture outputs contain only IDs/digests; factory construction has no model call and the runner only claims an existing Host identity. | No S evidence and no broad claim about unrelated modules or provider/Journal delivery. |

## Commands and observed results

All WSL commands used `/tmp/karajan-candidate-mode-qy6_mqo2/venv/bin/python`,
`PYTHONPATH=backend:tests:tests/projects:tests/runs:tests/candidates`, and a
fresh `/tmp/karajan-dg01-*` base temp. The factory test used
`KARAJAN_GO_TOKENIZER_DIRECTORY=/mnt/c/Users/Chooo/Playground/Karajan/.cache/go-context-artifacts`
and `KARAJAN_OPENCODE_LINUX_BINARY=/mnt/c/Users/Chooo/Playground/Karajan/.cache/go-linux-runtime/package/bin/opencode`.

| Command | Result |
| --- | --- |
| `wsl.exe bash -lc 'cd /mnt/c/Users/Chooo/Playground/Karajan/.cache/dg01-reviewer-20260908 && export PYTHONPATH=backend:tests:tests/projects:tests/runs:tests/candidates && export KARAJAN_OPENCODE_LINUX_BINARY=/mnt/c/Users/Chooo/Playground/Karajan/.cache/go-linux-runtime/package/bin/opencode && export KARAJAN_GO_TOKENIZER_DIRECTORY=/mnt/c/Users/Chooo/Playground/Karajan/.cache/go-context-artifacts && /tmp/karajan-candidate-mode-qy6_mqo2/venv/bin/python -m pytest tests/runs/test_reviewer_execution_bootstrap.py tests/runs/test_reviewer_execution_intent.py tests/runs/test_reviewer_binding.py tests/runs/test_reviewer_input_approved.py tests/runs/test_admission_guard.py tests/runs/test_task_admission.py tests/execution/test_runnerhost.py -q --basetemp /tmp/karajan-dg01-reviewer-affected-final5-143'` | `148 passed in 107.46s` |
| `C:/Users/Chooo/Playground/Karajan/.venv/Scripts/python.exe -m ruff check .` | `All checks passed!` |
| `C:/Users/Chooo/Playground/Karajan/.venv/Scripts/python.exe -m mypy backend` | `Success: no issues found in 154 source files` |

The affected command ran in a local captured process because it exceeds the interactive 30-second yield; its exit code and final pytest summary above were observed before recording this result.
