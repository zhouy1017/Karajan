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
| Real lineage/CAS/Checks/Capacity produce one complete intent; bad material has no new intent/effect | **C passed**: `test_prepare_is_durable_and_freezes_compiler_identity` uses the existing approved Reviewer/Worker/Candidate/Checks fixture path and verifies the v2 input, immutable identity, and unchanged Capacity snapshot. Existing Reviewer-binding negatives remain in the affected suite. | No S/native/provider evidence. |
| Exact replay, concurrent use, reopen, and tamper retain/reject the same identity | **C/P passed subset**: `test_existing_factory_reopens_identity_and_rechecks_own_descriptor` reopens a seeded identity through the production existing-only factory, reads the original identity, and byte-compares the execution DB before/after. | It is not an actual simultaneous two-facade race test; descriptor/tamper coverage is limited to this factory boundary. |
| Every prepare/claim re-enters current admission/compiler and source guard; changed current facts block effects | **C passed**: the new factory regression replaces its own valid Reviewer descriptor after facade construction and the next `freeze_launch` rejects `REVIEWER_EXECUTION_BOOTSTRAP_CHANGED`; historical `read` still returns the old intent. `ReviewerExecutionIntents` current-input tests cover the existing compiler/admission path. | Full sealed deployment source could not run: available WSL OpenCode files did not match the pinned runtime SHA. The test uses a clearly-labelled test-only deployment envelope while retaining real existing stores and the pinned tokenizer. |
| Host prepare/control and read-only inspection are durable; no facade start/new session on recovery | **C passed**: `test_fixed_host_prepare_is_replayable_without_starting_native` verifies same prepared/start identity; `test_prepare_rejects_second_key_and_never_prepares_host` verifies no Host record before prepare. | No native execution claim. |
| A registered direct Host child alone gains the first effect claim; lost reply/reopen never gets a second right | **P passed**: Linux direct-child test runs both normal reply and `lost-reply`: the actual registered child commits the claim then exits before writing a response; a fresh existing-only facade returns `claim_allowed=False`. The parent never supplies a child PID or runner guard. | No two-independent-child concurrent claim test: one Host attempt has one direct child, and this bounded fixture does not establish a separate multi-attempt concurrency protocol. |
| Cancellation permanently blocks future claim while history stays inspectable; delivery remains false/not_run | **C passed**: cancellation test blocks Host prepare; direct-child recovery test cancels after reopen and blocks a later claim. Prepared intent asserts `delivery={eligible:false,state:not_run}`. | No remote stop, settlement, or provider assertion. |
| Public state is non-sensitive; quality checks and no native/HTTP/model/Journal grant side effects | **C passed (bounded)**: fixture outputs contain only IDs/digests; factory construction has no model call and the runner only claims an existing Host identity. | No S evidence and no broad claim about unrelated modules or provider/Journal delivery. |

## Commands and observed results

All WSL commands used `/tmp/karajan-candidate-mode-qy6_mqo2/venv/bin/python`,
`PYTHONPATH=backend:tests:tests/projects:tests/runs:tests/candidates`, and a
fresh `/tmp/karajan-dg01-*` base temp. The factory test used
`KARAJAN_GO_TOKENIZER_DIRECTORY=/mnt/c/Users/Chooo/Playground/Karajan/.cache/go-context-artifacts`.

| Command | Result |
| --- | --- |
| `... python -m pytest tests/runs/test_reviewer_execution_bootstrap.py -q --basetemp /tmp/karajan-dg01-bootstrap-143` | `4 passed in 6.34s` |
| `... python -m pytest tests/runs/test_reviewer_execution_intent.py::test_existing_store_direct_child_claim_is_one_shot_and_cancelled_recovery_stays_blocked -q --basetemp /tmp/karajan-dg01-lost-143b` | `2 passed in 9.45s` (normal reply and committed-lost-reply) |
| `... python -m pytest tests/runs/test_reviewer_execution_bootstrap.py tests/runs/test_reviewer_execution_intent.py -q --basetemp /tmp/karajan-dg01-reviewer-focused-143` | `11 passed in 17.56s` |
| `C:/Users/Chooo/Playground/Karajan/.venv/Scripts/python.exe -m ruff check .` | `All checks passed!` |
| `C:/Users/Chooo/Playground/Karajan/.venv/Scripts/python.exe -m mypy backend` | `Success: no issues found in 154 source files` |

An attempted broader affected Reviewer/admission/Host pytest invocation exceeded the interactive 30-second command window before a final summary, so it is deliberately not counted as passed evidence.
