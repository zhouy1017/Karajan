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
| 3 — invalid/missing/cross authority rejects before slot or upstream | `test_business_relay_rejects_invalid_authority_before_journal_or_upstream` |
| 4 — Planning/Reviewer wire-tool policy and invalid response/accounting evidence | `test_business_planning_rejects_every_request_tool_variant_before_send` covers each declaration plus structurally valid assistant `tool_calls`/returned-tool history; `test_business_planning_rejects_every_returned_tool_variant_after_accounted_send`; `test_business_reviewer_allows_read_through_relay_journal_and_stream_identity`; `test_business_reviewer_rejects_nonread_tool_identities_with_correct_send_boundary`; `test_business_response_validation_failures_are_accounted_and_withdraw_future_send` |
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
| Focused business AC suite | `$env:KARAJAN_GO_TOKENIZER_DIRECTORY = 'C:/Users/Chooo/Playground/Karajan/.cache/go-context-artifacts'; C:/Users/Chooo/Playground/Karajan/.venv/Scripts/python.exe -m pytest tests/adapters/opencode/test_go_business_relay_grants.py -q --basetemp C:/Users/Chooo/Playground/Karajan/.cache/dg01-business-relay-20260908/.pytest-business-144-repair-2` | `85 passed in 8.05s` |
| Affected OpenCode adapter suite | `$env:KARAJAN_GO_TOKENIZER_DIRECTORY = 'C:/Users/Chooo/Playground/Karajan/.venv/Scripts/python.exe -m pytest tests/adapters/opencode/test_go_business_relay_grants.py tests/adapters/opencode/test_go_journal.py tests/adapters/opencode/test_go_relay_context.py tests/adapters/opencode/test_go_relay_journal.py -q --basetemp C:/Users/Chooo/Playground/Karajan/.cache/dg01-business-relay-20260908/.pytest-affected-144-final` | `155 passed in 11.56s` |
| Ruff | `C:/Users/Chooo/Playground/Karajan/.venv/Scripts/ruff.exe check .` | `All checks passed!` |
| Backend mypy | `C:/Users/Chooo/Playground/Karajan/.venv/Scripts/mypy.exe backend` | `Success: no issues found in 150 source files` |

| Evidence layer | Result and scope |
| --- | --- |
| C | passed — the focused suite above exercises real loopback Relay HTTP and real SQLite durable/reopen behavior against a synthetic upstream. |
| P | not_run — native Planning/Reviewer consumer behavior is outside #144. |
| S | not_run — no provider request was made; `MockTransport` is local synthetic upstream evidence only. |
