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
| Real lineage/CAS/Checks/Capacity produce one complete intent; bad material has no new intent/effect | **C passed**: `test_prepare_is_durable_and_freezes_compiler_identity` uses the approved Reviewer/Worker/Candidate/Checks path and verifies the v2 input, immutable identity, and unchanged Capacity snapshot. The affected binding/input/admission suite also passed. | No S/native/provider evidence. |
| Exact replay, concurrent use, reopen, and tamper retain/reject the same identity | **C/P passed**: the production existing-only factory reopens a seeded identity from the real fixed source, reads the same record, and leaves the real execution DB byte-for-byte unchanged. Empty, malformed, deleted-after-open, and hardlinked ledgers reject without initialization or repair. | No S/native/provider evidence. |
| Every prepare/claim re-enters current admission/compiler and source guard; changed current facts block effects | **C/P passed**: replacing the facade's own valid descriptor after construction makes the next `freeze_launch` reject `REVIEWER_EXECUTION_BOOTSTRAP_CHANGED`; historical `read` remains available. The factory uses the actual pinned Linux OpenCode binary and tokenizer through `deployment_source`, with no monkeypatch or provider call. | No native Review/provider assertion. |
| Host prepare/control and read-only inspection are durable; no facade start/new session on recovery | **C passed**: `test_fixed_host_prepare_is_replayable_without_starting_native` verifies same prepared/start identity; `test_prepare_rejects_second_key_and_never_prepares_host` verifies no Host record before prepare. | No native execution claim. |
| A registered direct Host child alone gains the first effect claim; lost reply/reopen never gets a second right | **P passed**: normal, committed-lost-reply, and concurrent modes run in one actual registered direct child. In concurrent mode two independently reopened `ReviewerExecutionIntents` facades synchronize and contend for the same original observer claim: exactly one returns `claim_allowed=true`, the other false, with the same persistent execution identity and no extra Host launch/Capacity/effect. Reopen and lost reply never receive a second right. | No native Review/provider assertion. |
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
| `wsl.exe bash -lc 'cd /mnt/c/Users/Chooo/Playground/Karajan/.cache/dg01-reviewer-20260908 && export PYTHONPATH=backend:tests:tests/projects:tests/runs:tests/candidates && export KARAJAN_OPENCODE_LINUX_BINARY=/mnt/c/Users/Chooo/Playground/Karajan/.cache/go-linux-runtime/package/bin/opencode && export KARAJAN_GO_TOKENIZER_DIRECTORY=/mnt/c/Users/Chooo/Playground/Karajan/.cache/go-context-artifacts && /tmp/karajan-candidate-mode-qy6_mqo2/venv/bin/python -m pytest tests/runs/test_reviewer_execution_bootstrap.py tests/runs/test_reviewer_execution_intent.py tests/runs/test_reviewer_binding.py tests/runs/test_reviewer_input_approved.py tests/runs/test_admission_guard.py tests/runs/test_task_admission.py tests/execution/test_runnerhost.py -q --basetemp /tmp/karajan-dg01-reviewer-affected-final2-143'` | `144 passed in 103.05s` |
| `C:/Users/Chooo/Playground/Karajan/.venv/Scripts/python.exe -m ruff check .` | `All checks passed!` |
| `C:/Users/Chooo/Playground/Karajan/.venv/Scripts/python.exe -m mypy backend` | `Success: no issues found in 154 source files` |

An attempted broader affected Reviewer/admission/Host pytest invocation exceeded the interactive 30-second command window before a final summary, so it is deliberately not counted as passed evidence.
