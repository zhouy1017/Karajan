# Commander qualification Relay/Journal slice

Candidate scope: issue #147 shared-boundary slice only. This document records
the implementation boundary, not an official Commander qualification result.

`karajan.go-commander-qualification-grant.v1` is a distinct Journal binding.
It preserves the existing qualification, attempt, fence, Profile, channel,
model, generation, expiry and request-limit identity, then binds the fixed
probe digest, exactly one `legal_plan` or `denied_tool` scenario, and the full
current `GoRequestAccounting` source digest plus fixed limits. It reuses
`GoCallJournal` and its existing `go_grants` / `go_calls` ledger; no second
ledger or recovery path exists.

`GoCommanderQualificationContext` accepts only I=12288, O=4096, C=16384,
fixed margin=2048, and ratio=2000 basis points. Before every send it checks the
full actual accounting source and measures the complete native request,
including messages, history, and tools. The Relay also requires explicit
`tools: []`, forbids tool history/calls and tool-choice overrides, and allows
at most the sealed scene-local `max_requests` (bounded by the pre-existing
Journal maximum of six). The future producer owns the cross-scene total of 12.

For this schema Relay rejects a bad constructor assembly unless it has both the
exact current Commander context and a nonempty `send_guard`, then repeats those
checks at each send. The guard spans the unique `Journal.begin_call` and the
upstream HTTP opening. A failed/missing/revoked guard, bad source/spec/scenario,
unsupported tools (including shell/MCP-shaped names), malformed accounting, or
expiry causes no Journal call and no upstream send. `begin_call` persists
`send_unknown` before HTTP; completion/lost-reply paths never refund or issue a
replacement send. Provider usage remains recorded when it was received even if
the overall protocol result is rejected.

The local SQLite plus localhost receiving-counter tests are C evidence only.
In particular, the `denied_tool` result is a scene-local Relay policy denial;
it is not an official source scene observation and cannot satisfy a future
official Commander qualification or current-producer acceptance condition.

Still outside this slice: the #147 store/start lifecycle, sealed Commander
source/probe construction, native probe execution, current Project/credential
guard, parser/observation evidence, official/fixture provenance matrix, and
cross-scene producer limit of 12.
