# Stopped projection capture and metered qualification grants

This slice follows PR #57. It supplies the remaining local capture mechanisms
and a distinct metered qualification grant. It does not yet qualify an arbitrary
Task, enable a Profile, launch an approved Run or pass its tests/review.

## Capture from the owned native runtime

`IsolatedOpenCode.capture_projection()` accepts no path or stop report. Only an
explicit projection that successfully started is eligible. The runtime closes
its own namespace and independently checks its owned process identities and PID
namespace again before reading. An unstarted runtime or uncertain stop produces
no capture.

The controller pins the original projected file and parent-directory identities
at startup. Collection checks the exact tree, original identities, regular
unshared files, unchanged read-only contents and bounded stable reads. Added,
missing, linked, replaced or out-of-scope paths are rejected. Limits are 8 MiB per
file, 64 MiB per capture and 4,096 projected files/directories combined. Normal
startup retains support for unprojected diagnostic files; collection requires an
exact tree. A qualification canary must therefore live outside the captured tree.

Successful output is an immutable `StoppedProjection`: original projection,
in-memory file bytes, fixed runtime artifact digest and detached local stop
evidence. Repeated calls retain the same bytes; they do not reread a subsequently
modified directory. A failed capture remains failed for that runtime. Local stop
does not prove that a provider stopped remote work.

## Full candidate reconstruction

`CandidateStore.freeze_projection(projection, contents, request)` is an internal
trusted-controller port. It is not a model-facing report importer. The caller
supplies bytes from the owned runtime capture and holds the relevant current
authority through the call.

The Store validates every original projected digest against its registered
baseline. The writable set must equal the exact approved file list. Read-only
changes, extra/missing contents and new files are rejected before reconstruction.
It restores the complete baseline from verified local artifacts into a fresh
private directory, overlays the approved bytes and calls the existing freeze
implementation. Unprojected files, binary bytes and executable modes survive.
The source checkout and agent Git metadata are never consulted.

Existing candidate content identities, replay, revision and gate behavior are
unchanged. `approved_reviewers: []` is valid: code can be retained while no eligible
reviewer exists. That candidate still lacks required check/review evidence and
cannot pass the delivery gate. This is not a qualification workaround.

## Current writer identity

`RunnerHost.current_fence_guard(attempt_id, fence=..., authorization_ref=...)`
holds a read-only SQLite transaction through publication. It requires a
persistently accepted launch and the same current manifest/activation/control
identity. A changed fence, withdrawn authorization, cancellation or disabled
control rejects collection. Concurrent control updates wait until the guard is
released. Prior result acceptance never overrides a newer control state.
The guard opens only an existing ledger: a missing database is not silently
recreated. Independent review reproduced and closed that failure before publication.

The trusted runner may remain alive while collecting its stopped native child.
The Host guard therefore does not require the entire supervisor group to exit.
Activation expiry is the deadline for starting, not a collection deadline.
The guard grants no new dispatch and does not write success or settle usage.

The approved Task runner must additionally hold the current Run/Project/operation
guards and bind the owned runtime to its durable execution intent. These local
ports do not establish that business binding by themselves. The qualification
consumer instead uses its own persisted qualification start; no Task reservation
or synthetic Task grant is needed for a fixed qualification.

## Metered qualification identity

The new explicit `karajan.go-qualification-grant.v2` retains `qualification_id`
and the common Attempt/fence/runtime/Profile/channel/generation/request limit.
It adds a fixed `probe_spec_digest`, `edit` or `denied_read` scenario, and a
context record with tokenizer source, input/output/window limits and margins.
It does not borrow a Task's execution-policy digest.

`GoQualificationContext` must match the complete qualification context, spec and
scenario. A v2 grant without accounting, mismatched limits/source or a Task
context is rejected before an upstream send. The journal requires the matching
numeric measurement before recording a new v2 call. Existing relay usage,
unknown-send, revocation and request-slot rules still apply. Legacy qualification
and Task grant JSON/replay behavior remain separate and compatible.

The fixed suite v2 producer and its trusted facts consumer still need to bind
the new probe spec, actual projection, accounting and this Collector source.
This change alone adds no `candidate_capture=passed` or production routing fact.

## Validation

Tests use temporary Git repositories, real SQLite/RunnerHost processes, the
pinned Linux OpenCode binary and local HTTP fixtures. No provider credential is
needed. The published evidence records the exact source revision and each check;
fixture observations are not official Go observations. CI remains required on
both supported platforms. CI failures follow the current
[repair assignment](testing-gates.md#ci-失败的修复分工): local `gpt-5.3-codex-spark`,
without new `@copilot` comments. If its quota is unavailable, retain the
reproduction and handoff materials and report the issue as unfixed; do not
substitute another model or weaken checks.
