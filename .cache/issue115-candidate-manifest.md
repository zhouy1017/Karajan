# #115 candidate evidence manifest

Date: 2026-09-08 (Asia/Hong_Kong)

- Base: `6ae6023250726d3126b9486a40b9a5b28c97c777`
- Product candidate: `3c652733dd9ca2a30ef666215402b174586bc0b8`
- Required Capacity boundary dependency: `ea1c84996580607370aec670178fb2a7431c0431`
- Scope: C-level SQLite/Capacity Reviewer routing and admission. The candidate
  exposes no native Reviewer start, input compilation, provider send, Evidence,
  or remote-settlement claim; those remain #116 work.

## Original acceptance conditions and current evidence

| Original condition | Result | Evidence at the product candidate |
| --- | --- | --- |
| Reviewer receives a distinct operation, attempt/context, exact Capacity request, and one persisted approved Worker dependency | passed (C) | `test_reviewer_admission_uses_distinct_operation_identity_and_capacity_request`; `test_multiple_credible_worker_operations_are_rejected_before_capacity` rejects ambiguous Worker operations before a slot. |
| Admission uses current final validation subject, Checks, binding, candidate source/generation/window, and refuses changed/cancelled state before Capacity | passed (C) | `test_current_check_change_blocks_reviewer_admission_before_capacity`, `test_reviewer_generation_change_blocks_current_effect_before_capacity`, `test_reviewer_cancelled_before_advance_never_creates_a_reservation`, and `test_reviewer_window_change_blocks_the_stored_request`. |
| Public IDs cannot replace candidate facts or lower complexity; only captured facts determine the route | passed (C) | `test_public_reviewer_admission_rejects_uploaded_candidate_or_complexity` supplies forged public facts and observes no Capacity reservation. |
| Every captured author is independent; a collision involving only author two rejects before Capacity | passed (C) | `test_public_reviewer_admission_retains_every_actual_candidate_author_before_capacity` mutates a real collector capture, persists it through `CandidateStore`, then enters public admission with only the second author colliding. `test_reviewer_route_checks_independence_against_every_captured_author` and `test_second_author_collision_rejects_reviewer_before_capacity` provide narrower route-level regressions. |
| T3 same or unknown model family rejects before a slot; current Go scope does not claim T2/T3 execution support | passed / not_run (C / scope) | `test_t3_reviewer_same_or_unknown_family_is_rejected_before_capacity` proves the rejection. Native T2/T3 execution is not implemented or claimed. |
| Lost Reviewer admission and activation replies, and concurrent `advance`, recover the original request/receipt without another Capacity claim | passed (C) | `test_reviewer_admission_reuses_one_exact_request_after_lost_reply`, `test_concurrent_reviewer_advance_has_one_capacity_admission`, and `test_lost_reviewer_activate_reply_recovers_the_original_activation_receipt`. The activation test proves the persisted deterministic key, ID-only `get`/`reconcile_reviewer` recovery, and no second activate. |
| Current Project binding remains locked through Capacity; Reviewer effect is fenced to the original operation/Worker lineage/active exact request | passed (C) | `test_reviewer_effect_guard_keeps_project_binding_locked_through_capacity`, `test_reviewer_effect_guard_uses_only_the_stored_operation_and_active_hold`, and `test_reviewer_effect_guard_rejects_cancelled_or_changed_binding_without_new_admission`. |
| Original Run cumulative duration/attempt budget is current at Capacity reservation and pre-effect boundary; own final existing claim remains valid | passed (C) | `test_reviewer_admission_honors_existing_run_cumulative_budget_before_capacity`, `test_reviewer_admission_rechecks_run_deadline_after_capacity_lock_wait`, and `test_reviewer_effect_guard_keeps_its_last_legal_run_budget_claim`. Capacity callback tests separately prove recovery skips the callback and a raised boundary guard commits neither reservation nor receipt. |
| Existing public Reviewer-shaped task without #115 controller stays a persistent lineage blocker | passed (C) | `examples/task-admission/spec/test_public_admission.py::test_public_owner_and_task_identity_cannot_redirect_existing_operation` asserts `EXECUTION_LINEAGE_REQUIRED` and no reservations. |
| Native/send/input/Evidence/remote receipt chain | not_run; #116 | Outside #115 scope. |

## Commands and raw results

The Windows system pytest temp directory is unavailable. Every pytest command
below uses only `--basetemp` for the test temporary directory; it leaves
repository configuration and import behavior intact.

```powershell
C:\Users\Chooo\Playground\Karajan\.venv\Scripts\pytest.exe --collect-only -q tests --basetemp .cache\pytest-115-collect
# 2774 tests collected in 1.59s

C:\Users\Chooo\Playground\Karajan\.venv\Scripts\pytest.exe -q tests -k "public_reviewer_admission_retains_every_actual_candidate_author or reviewer_admission_rechecks_run_deadline_after_capacity_lock_wait or reviewer_effect_guard_keeps_its_last_legal_run_budget_claim or lost_reviewer_activate" --basetemp .cache\pytest-115-review4
# 4 passed, 2770 deselected, 2 third-party deprecation warnings in 8.88s

C:\Users\Chooo\Playground\Karajan\.venv\Scripts\pytest.exe -q tests -k reviewer --basetemp .cache\pytest-115-reviewer-all
# 173 passed, 108 skipped, 2493 deselected, 2 third-party deprecation warnings in 119.98s
# Skips are pinned tokenizer artifacts, fixed Linux native/readonly Store, or POSIX chmod semantics.

C:\Users\Chooo\Playground\Karajan\.venv\Scripts\pytest.exe -q -o "pythonpath=backend tests/runs tests/web" examples/task-admission/spec examples/task-admission/standards --basetemp .cache\pytest-115-ci
# 61 passed, 2 third-party deprecation warnings in 21.87s

C:\Users\Chooo\Playground\Karajan\.venv\Scripts\pytest.exe -q tests/capacity/test_command_receipts.py --basetemp .cache\pytest-capacity-boundary
# 22 passed in 0.60s

C:\Users\Chooo\Playground\Karajan\.venv\Scripts\pytest.exe -q -o pythonpath=backend tests/runs/test_reserved_execution_guard.py tests/runs/test_admission_guard.py tests/runs/test_task_admission.py --basetemp .cache\pytest-115-worker-regression
# 22 passed in 8.17s

C:\Users\Chooo\Playground\Karajan\.venv\Scripts\ruff.exe check .
# All checks passed!

C:\Users\Chooo\Playground\Karajan\.venv\Scripts\mypy.exe backend/karajan
# Success: no issues found in 146 source files
```

A full `pytest.exe -q tests --basetemp .cache\pytest-115-full` was started
against this candidate but did not produce a terminal pytest summary (the
separate process exited after progress output containing early failures). It is
incomplete, not a pass; its diagnostics were not used as #115 evidence. No
remote CI, provider call, native Reviewer process, or real account qualification
was run locally.
