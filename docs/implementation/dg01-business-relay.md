# DG01 business Relay grant boundary

Issue #144 adds two explicitly versioned, durable business grant bindings to
the existing local OpenCode Go relay path:

- `karajan.go-planning-native-grant.v1` binds a planning execution, planning
  binding, admission, trusted input digest, authentication source, tokenizer
  limits, and a `none` tool policy.
- `karajan.go-reviewer-native-grant.v1` binds the independent reviewer
  execution identity, review/candidate/checks digests, authentication source,
  the same tokenizer limits, and a `read`-only tool policy.

They are intentionally not aliases for the historical planning, Task, or
qualification schemas. The journal validates exact schema/context/binding
shapes, writes `send_unknown` with the locally measured wire request digest
before the local HTTP relay begins its upstream request, and gives send
permission only in the first successful `begin_call` result. A repeated call
ID is read-only history; different call IDs consume the original grant cap.

Business sends require both the matching typed context and a non-empty,
controller-owned `send_guard`. The guard surrounds SQLite begin through HTTP
send startup, then releases before response streaming. Its current controller
facts therefore control the next send; guard failures, source/window/accounting
rejection, malformed SSE, absent or excessive provider usage, and tool policy
failure do not create an additional upstream request. A persisted unknown send
is never refunded or retried automatically.

When a completed stream has already supplied valid cumulative provider usage,
that observation is persisted even if its returned tool subsequently fails the
business policy. Such a call remains `protocol_passed: false` and is revoked;
an invalid, malformed, non-finite, or absent usage value is never promoted into
successful final evidence.

Planning rejects declarations, historical tool messages, and returned tool
calls. Reviewer accepts only structurally valid `read` declarations/history and
returned `read` calls; edit, shell, MCP, and unknown names are rejected.

## Evidence boundary

The focused tests use a real loopback HTTP `GoRelay`, real SQLite reopen/read,
and `httpx.MockTransport` as a synthetic upstream. They provide C-level local
relay/journal evidence, including the provider-observed counter before request
handling. They do not make an upstream socket request and therefore are not S
or native/P evidence. No provider/model request, native runtime, qualification,
Planning, Reviewer, Admission, Capacity, or delivery effect is asserted here.

## Original #144 acceptance coverage

The following maps each original acceptance criterion (AC) to its focused
evidence.  The parametrized business tests run each case for both the planning
and reviewer schemas.  The loopback Relay and SQLite journal are real; the
upstream is an `httpx.MockTransport` and is counted by the fixture.

| Original AC | Specific focused tests |
| --- | --- |
| 1 — durable schemas, strict binding/context, legacy recovery and tamper rejection | `test_business_bindings_are_durable_and_context_bound`; `test_business_context_tamper_is_pre_send_and_legacy_is_readable` |
| 2 — Relay HTTP ordering, one `send_unknown`, measured wire digest, completion/reopen | `test_business_grant_uses_relay_journal_and_actual_accounting` — in the actual `MockTransport` callback, independently measures JSON decoded from received `request.content`, compares every `ContextMeasurement` field and the SHA-256 of those bytes before send, then reopens SQLite to compare the original call ID, grant binding, digest, provider usage, and sanitized receipt outcome. |
| 3 — invalid/missing/cross authority rejects before slot or upstream | `test_business_relay_rejects_invalid_authority_before_journal_or_upstream`; `test_business_and_qualification_authority_cannot_mix_through_relay` (both mixing directions); `test_legacy_planning_grant_cannot_send_with_business_context`; `test_business_authority_seals_limits_and_actual_source_reject_before_send` (each planning/reviewer seal, context limit, and matching false source digest). The helper authorizes each fixture's actual `grant_id` (`target` or `legacy`), each test asserts its intended rejection code, observes that actual grant before send, and proves zero slots/upstream; each also authenticates a separate valid unrelated grant and proves its snapshot did not change. |
| 4 — Planning/Reviewer wire-tool policy and invalid response/accounting evidence | `test_business_planning_rejects_every_request_tool_variant_before_send` covers each declaration plus structurally valid assistant `tool_calls`/returned-tool history; `test_business_planning_rejects_every_returned_tool_variant_after_accounted_send`; `test_business_reviewer_allows_read_through_relay_journal_and_stream_identity`; `test_business_reviewer_rejects_nonread_tool_identities_with_correct_send_boundary`; `test_business_response_validation_failures_are_accounted_and_withdraw_future_send`; `test_forbidden_returned_tool_persists_valid_observed_usage_despite_protocol_failure` reopens SQLite for both schemas and proves cumulative valid observed input/output overruns survive a forbidden returned tool while the outcome remains failed/revoked and public receipts retain no forbidden identity. The legacy Relay regression `test_unapproved_tool_name_is_rejected_without_retaining_its_text` also proves valid usage persists without retaining the returned name. |
| 5 — guard recheck and enter/exit/begin/complete lost-return counterexamples | `test_business_guard_rechecks_current_withdrawal_before_each_send`; `test_business_guard_lifecycle_faults_do_not_refund_or_repeat_send`; `test_business_lost_begin_reply_keeps_one_unknown_slot_without_replay_or_refund`; `test_business_lost_completion_acknowledgement_keeps_unknown_without_a_retry_or_refund` |
| 6 — cap, expiry/revoke, replay and concurrent distinct calls | `test_business_grant_limits_preserve_history_and_never_reauthorize_replay`; `test_business_grant_concurrent_distinct_calls_stop_at_original_cap` |
| 7 — business Relay/Journal/context/send-guard regression boundary | `test_go_business_relay_grants.py`; affected OpenCode adapter suite; Ruff; backend mypy |

`test_business_response_validation_failures_are_accounted_and_withdraw_future_send` is
not evidence for a lost send: its persisted call is `response_received` with a
durable protocol/accounting failure.  In contrast,
`test_business_lost_begin_reply_keeps_one_unknown_slot_without_replay_or_refund`
injects an `OSError` only after `begin_call` commits, reopens the Journal, and
proves exactly one durable `send_unknown` call, zero upstream sends and no
replayed permission.  Its second Relay HTTP request is deliberately a new
request: the Relay has revoked/closed after the uncertain original call, so it
gets `503` and does not consume the unused second cap slot.

The guard-withdrawal cases use a synthetic controller permission flag named for
the withdrawal reason.  They prove that the shared Relay invokes its required
guard before every send; they do not claim native cancellation or qualification
implementation, which remains with the native consumers.

## Frozen Reviewer contract mapping

This leaf deliberately does not invent a native caller DTO.  The reviewed #143
sources establish the names a future native producer must map while it holds its
current Reviewer guard:

| Relay binding field | Existing #143 / compiler source |
| --- | --- |
| `subject.project_id`, `subject.run_id`, `subject.reviewer_operation_id`, `subject.worker_operation_id`, `subject.reviewer_task_id` | `compiler_binding()` returns `project_id`, `run_id`, `reviewer_operation_id`, `worker_operation_id`, and `task_id` respectively. |
| common `attempt_id` | `compiler_binding()` returns `planned_attempt_id`; it is the controller attempt identity, not a newly supplied native value. |
| `subject.execution_id` | `ReviewerExecutionIntents.prepare()` creates durable `execution_id`; the future native producer must read this exact intent record. |
| `review_binding_sha256` | the durable intent's `binding_digest`, built from the controller-owned compiler binding plus execution source facts. |
| `reviewer_input_sha256` | `compiler_binding()["reviewer_input"]["sha256"]`, which is `ReviewerInput.content_sha256` from `compile_reviewer_input()`. |
| `candidate_checks_sha256` | a future native producer must digest the exact compiler binding `candidate` identity together with `reviewer_input.check_evidence_ids`; no existing DTO exposes a precomputed replacement key. |

The compiler input artifact is not the full native wire: OpenCode can add
session/system/history fields.  The producer must therefore construct the
typed context and binding from those durable controller records, while the relay
continues to measure and persist the actual completed HTTP request.  This is a
documented future-native responsibility, not C/P evidence supplied by this
leaf.

## Reproducible validation record — 2026-09-08

All commands run from this worktree using
`C:/Users/Chooo/Playground/Karajan/.venv/Scripts/python.exe`, with
`KARAJAN_GO_TOKENIZER_DIRECTORY=C:/Users/Chooo/Playground/Karajan/.cache/go-context-artifacts`
and a fresh `--basetemp` below this worktree.

| Check | Exact command | Actual result |
| --- | --- | --- |
| Focused receipt/authority regressions | `$env:KARAJAN_GO_TOKENIZER_DIRECTORY='C:/Users/Chooo/Playground/Karajan/.cache/go-context-artifacts'; C:/Users/Chooo/Playground/Karajan/.venv/Scripts/pytest.exe tests/adapters/opencode/test_go_relay.py::test_unapproved_tool_name_is_rejected_without_retaining_its_text tests/adapters/opencode/test_go_business_relay_grants.py::test_business_and_qualification_authority_cannot_mix_through_relay tests/adapters/opencode/test_go_business_relay_grants.py::test_legacy_planning_grant_cannot_send_with_business_context tests/adapters/opencode/test_go_business_relay_grants.py::test_business_authority_seals_limits_and_actual_source_reject_before_send tests/adapters/opencode/test_go_business_relay_grants.py::test_forbidden_returned_tool_persists_valid_observed_usage_despite_protocol_failure --basetemp .pytest-focused-144-green-3 -q` | `19 passed in 2.78s`. The unchanged first receipt regression was red at baseline: `1 failed` because `unapproved-tool` was in the receipt. |
| Full relevant Relay/Journal/context/task/planning/qualification/reviewer-qualification/business modules | Same tokenizer environment; first: `pytest.exe tests/adapters/opencode/test_go_relay.py tests/adapters/opencode/test_go_journal.py tests/adapters/opencode/test_go_relay_context.py tests/adapters/opencode/test_go_context.py tests/adapters/opencode/test_go_task_grants.py --basetemp .pytest-144-relevant-a -q`; second: `pytest.exe tests/adapters/opencode/test_go_business_relay_grants.py tests/adapters/opencode/test_go_planning_grants.py tests/adapters/opencode/test_go_qualification_relay.py tests/adapters/opencode/test_go_qualification_grants.py tests/adapters/opencode/test_go_reviewer_qualification_relay.py tests/adapters/opencode/test_go_reviewer_qualification_grants.py --basetemp .pytest-144-relevant-b -q` | `218 passed in 16.86s`; `236 passed in 17.25s` (454 total). |
| Complete OpenCode adapter suite | Same tokenizer environment and a workspace-only junction to the already-installed local `runtimes/opencode/node_modules`; `pytest.exe tests/adapters/opencode --basetemp .pytest-144-opencode-all-installed/tmp -q` | `518 passed, 5 skipped in 91.47s`; skips are Windows-inapplicable Linux namespace transport tests. |
| Ruff | `C:/Users/Chooo/Playground/Karajan/.venv/Scripts/ruff.exe check .` | `All checks passed!` |
| Backend mypy | `C:/Users/Chooo/Playground/Karajan/.venv/Scripts/mypy.exe backend/karajan` | `Success: no issues found in 150 source files` |

| Evidence layer | Result and scope |
| --- | --- |
| C | passed — the focused suite above exercises real loopback Relay HTTP and real SQLite durable/reopen behavior against a synthetic upstream. |
| P | not_run — native Planning/Reviewer consumer behavior is outside #144. |
| S | not_run — no provider request was made; `MockTransport` is local synthetic upstream evidence only. |
