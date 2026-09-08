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
