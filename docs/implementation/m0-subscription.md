# M0-04: Codex subscription protocol and permission binding

Scope: [M0-04 / #5](../planning/m0/issues/04-subscription.md). The implementation is an offline, version-pinned replay adapter and a reusable single-use permission gate. It does not start Codex, send protocol messages, read login files, invoke models, dispatch/retry work, change persistent policy, or create a PR.

## Reproduce the bounded results

```powershell
.\.venv\Scripts\python.exe -m karajan.adapters.codex replay examples/subscription/command-accept.json
.\.venv\Scripts\python.exe -m karajan.adapters.codex replay examples/subscription/command-decline.json
.\.venv\Scripts\python.exe -m karajan.adapters.codex replay examples/subscription/late-approval.json
.\.venv\Scripts\python.exe -m karajan.adapters.codex replay examples/subscription/binding-unknown.json
```

| Input | Replay result | Exit | Actual CLI output |
|---|---|---|---|
| [command-accept](../../examples/subscription/command-accept.json) | Exactly one proposed native `accept` response | 0 | [report](../../examples/subscription/command-accept.report.json) |
| [command-decline](../../examples/subscription/command-decline.json) | Native `decline`, `PERMISSION_DECLINED` | 1 | [report](../../examples/subscription/command-decline.report.json) |
| [late-approval](../../examples/subscription/late-approval.json) | Cancellation consumes pending callbacks; late acceptance rejected | 1 | [report](../../examples/subscription/late-approval.report.json) |
| [binding-unknown](../../examples/subscription/binding-unknown.json) | No accepted configuration observation; `not_run` | 1 | [report](../../examples/subscription/binding-unknown.report.json) |
| [turn-completed](../../examples/subscription/turn-completed.json) | Started turn reaches successful completion after command acceptance | 0 | [report](../../examples/subscription/turn-completed.report.json) |
| [turn-failed](../../examples/subscription/turn-failed.json) | Failed completion remains a failure after earlier command acceptance | 1 | [report](../../examples/subscription/turn-failed.report.json) |
| [turn-interrupted](../../examples/subscription/turn-interrupted.json) | Interrupted completion remains nonpassing | 1 | [report](../../examples/subscription/turn-interrupted.report.json) |

A replay `passed` means the supplied protocol fragment and controller decisions meet this adapter's checks. **Every report keeps `qualification.live_status = not_run` and `dispatch_eligible = false`.** The reported responses are data; they were not transmitted. Imported observations do not turn a replay into a live test.

## Pinned evidence and protocol subset

The local installed runtime was identified as **codex-cli 0.153.2**. Its public schema was generated offline by the root implementation task using:

```text
codex app-server generate-json-schema --out .cache/codex-schema/0.153.2
```

No `--experimental` flag was used. [protocol-source.json](../../examples/subscription/protocol-source.json) records the exact command, version, reviewed schema files, byte counts and SHA-256 values. The v2 bundle digest is `d3eace08be5dca386bfd1f1e8df650058b4113f1e10870a284d775d75517576a`. Replays reject another bundle digest or runtime version before producing a permission response. This pins the reviewed contract; a fixture's version declaration does not verify a running binary.

The [official app-server documentation](https://learn.chatgpt.com/docs/app-server#approvals), opened on 2026-09-05, supplies lifecycle context. A transport initializes once, acknowledges initialization, then starts a thread and turn. Command approvals occur between item start and completion, with a request-resolution notification after answering or cleanup. This adapter consumes the relevant server-side fragment starting at a `thread/start` response. It does not claim to implement or test transport initialization, item-output streaming, or the entire generated JSON Schema. The local nonexperimental schema takes precedence over optional fields described for other versions/modes.

| Reviewed local schema / JSON pointer | Consequence for this adapter |
|---|---|
| `v2/ThreadStartParams.json#/properties`, `v2/ThreadStartResponse.json#/properties` | Compare the explicit model/provider, cwd, approval policy/reviewer and sandbox against the response; command-line/requested values alone never establish acceptance |
| `v2/ThreadSettingsUpdatedNotification.json#/properties` | Later settings changes are checked; a changed or missing bound setting stops pending approval |
| `v2/TurnStartedNotification.json`, `v2/TurnCompletedNotification.json`, `#/definitions/TurnStatus` | Start requires the bound turn with `inProgress` and no error; completion requires an active, unclosed turn and closes the gate. Only `completed` without an error is a successful terminal observation |
| `CommandExecutionRequestApprovalParams.json#/properties` | Bind JSON-RPC request ID, thread/turn, item ID, optional approval ID and the complete request digest; missing command/cwd is rejected in this narrower subset |
| `CommandExecutionRequestApprovalResponse.json#/definitions/CommandExecutionApprovalDecision` | Emit only single-use `accept`, `decline` or `cancel`; never `acceptForSession`, execpolicy amendments or network-policy amendments |
| `PermissionsRequestApprovalResponse.json#/properties` | Additional-permission requests receive an empty grant with explicit `scope: turn`; no session or broader turn permission is granted |
| `FileChangeRequestApprovalParams.json#/properties/grantRoot` | This version describes a session-root grant with uncertain enforcement; file-change approval is unsupported and receives `cancel` |
| `v2/ServerRequestResolvedNotification.json` | A server-cleared callback cannot receive a later acceptance |
| `v2/ModelReroutedNotification.json` | Observe a reported model reroute, close pending permissions and fail the replay |
| `v2/ModelVerificationNotification.json#/definitions/ModelVerification` | The value is `trustedAccessForCyber`; it is not evidence of actual model identity |
| `v2/AccountUpdatedNotification.json#/definitions/AuthMode` | A reported mode other than official `chatgpt` login stops the gate; a matching mode does not prove account identity or billing isolation |
| `v2/ErrorNotification.json`, JSON-RPC error fields | Record native turn failure/internal retry, or bounded RPC code/category, without copying native error messages or details |
| `v2/ThreadTokenUsageUpdatedNotification.json`, `v2/AccountRateLimitsUpdatedNotification.json` | Preserve observed usage/quota with their native coverage; missing windows/reset times remain null |

## Public boundary and authorization ownership

The public entry points are `replay_file(Path)` and `PermissionGate` from `karajan.adapters.codex`. Typed input models live in `karajan.adapters.codex.models`; `request_digest(message)` computes the documented SHA-256 of sorted compact JSON, including the request ID and every native parameter. The replay document is `karajan.codex-replay.v1`; reports use `karajan.codex-replay-report.v1`.

The replay envelope contains:

- Pinned version/schema digest and a nonsecret configuration-source digest.
- Attempt ID/fence, Profile ID/revision/digest, and the bound thread/turn IDs.
- The current authorization hash, expiry and explicitly authorized native-request digests.
- Requested configuration and the correlated `thread/start` request ID.
- Timestamped native messages, controller decisions, cancellation/invalidation, and fixture/imported provenance.

The supported starting configuration is deliberately limited to `openai`, a fixed model/cwd, `on-request`, reviewer `user`, and a read-only sandbox with network disabled. This is a protocol probe, not a Worker profile qualified for autonomous repository writes. Stdin/remote-environment/managed-network approvals and unknown native fields or approval methods do not receive an `accept`.

**The controller owns authorization.** An exact request digest must already appear in the trusted authorization snapshot after scope review. Calculating a digest from a request does not authorize it. The supplied authorization hash is a reference to the controller's approved envelope, not a signature or a newly created approval. The replay cannot verify that an external approval store exists. Command parsing and `commandActions` are not used as proof of safe effects; there is no shell/path/symlink or OS-sandbox safety claim.

`PermissionGate(attempt, authorization)` snapshots those values. Its public operations are:

| Operation | Result and required caller behavior |
|---|---|
| `register(native_request, expires_at=..., now=...)` | Returns a pending ticket bound to the exact request and Attempt, or a refusal. The ticket includes request/authorization hashes; the outcome also records expiry and native item/approval IDs |
| `decide(PermissionDecision, now=...)` | Rechecks all binding fields and expiry, consumes the callback once, and returns a proposed native response. A decision cannot predate registration |
| `resolve(thread_id=..., request_id=...)` | Removes a callback already answered/cleared by the server, without responding a second time |
| `invalidate()` | Permanently closes the gate and returns cancellation responses for pending callbacks |
| `pending_count` | Supports an incomplete replay result when a decision never arrives |

Call these operations serially from the authoritative controller. Before delivering a decision, the controller must serialize it against cancellation, fence or authorization changes and invoke `invalidate()` for those changes. This in-memory module does not discover external database changes, provide crash durability, prove a process stopped, or preserve request-consumption history across restarts. Reconnection/restart reuse requires M0-06/07 and RunnerHost continuity evidence; it must not silently create a fresh live gate for an old Attempt. The caller must handle both `response` and any `additional_responses` returned by a decision. Native `cancel` also closes this gate; it does not certify process termination.

Only JSON-RPC response objects are emitted by the gate. It has no capability to call model dispatch, request a new model, select another account, modify `config.toml`/execpolicy, or create delivery operations.

## Unknown capabilities and remaining live work

The protocol's fixed configuration receipt is stored under `bindings.accepted`; `bindings.requested` remains separate. `provider_reported` remains null because this fragment provides no verified provider execution identity. There is no inference from a model's own text or from `model/verification`.

The reviewed protocol can announce model rerouting and native retries, but this probe has not established an effective control that excludes all internal model/account/billing fallback or extra delegation. `hidden_fallback_excluded`, `extra_delegation_disabled`, `provider_model_confirmed`, `billing_path_confirmed`, and `native_sandbox_enforced` therefore remain `not_run`. A detected reroute, authentication/configuration change or turn error fails the replay and invalidates pending approvals; absence of a notification is not evidence that fallback is disabled. No undocumented configuration switch is invented or marked effective.

Usage observations retain thread-total and last-turn counters. `model_call_count` remains null; the adapter does not derive hidden subscription calls from event counts. Quota notifications retain supplied windows, percent used and reset information; their account identity remains unknown in this narrow notification format. No supplied observation means `unknown`, not zero. Estimates are separately `not_provided`. These reports do not reserve provider quota or establish an exact remaining cash budget.

Every report lists four live cases still `not_run`: official login, actual inference, actual file tools, and actual cancellation. M0-07 must additionally test configuration-source isolation, prevention/detection of model and billing changes, delegation controls, late callback handling on the real transport, and process/sandbox effects. Official login stays with the official runtime; no subscription token is read or exported into a broker credential. This ticket performs no real cash or subscription inference. Codex remains **not qualified for strict dispatch**. A Claude alternative would require its own versioned probe if later evidence shows this Codex route cannot satisfy the contract; no Claude compatibility is claimed here.

## Failure/result semantics

The CLI exits zero only for a passing replay. Refusals and invalid records produce `failed`; absent binding evidence or an unanswered callback produce `not_run` when there is no failure. Broad or unsupported methods appear explicitly in permission outcomes as `blocked` with `NATIVE_METHOD_UNSUPPORTED`, `NATIVE_SCOPE_UNSUPPORTED`, or `DECISION_SCOPE_UNSUPPORTED`; none is promoted to a qualification pass.

Other stable reasons cover input/schema/version errors, binding mismatch, missing acceptance, inactive turn/Attempt, reversed time, expired permission, unauthorized/reused/nonpending requests, configuration/auth/model changes, and invalid usage/quota. JSON-RPC error categories include parse error, invalid request, method not found, invalid params, internal error and unknown. Native messages/details are omitted from error reports. Input-byte digest, Profile/Attempt context, source digest, ordered event timestamps, evidence references and limitations permit correlation with the original fixture without copying command content into reports.

Terminal observations preserve failure through `NATIVE_TURN_FAILED` or interruption through `NATIVE_TURN_INTERRUPTED`. Missing/invalid start or terminal status and inconsistent success with an error produce `TURN_STATUS_INVALID`; completion before start or after closure produces `EVENT_ORDER_INVALID`. Every completion invalidates pending callbacks, so a late decision cannot reopen the turn. An earlier valid command acceptance is retained as historical evidence even if the later turn fails. The replay may intentionally stop at a permission fragment; a passing fragment does not assert completed inference.

## Test-first evidence

Tests use the agreed public module CLI and public PermissionGate operations, never private-state assertions or mocked internal collaborators. Each numbered cycle was observed red before its production change and green afterward. The CLI is launched by the test harness; the adapter itself starts no process.

| Cycles | Behavior added | Observed red |
|---|---|---|
| 1 | Offline accepted-command report | Missing adapter module |
| 2-4 | Required model/version, exact authorized request, complete decision identity | Unexpected successful exit; 9 binding variants exposed missing checks |
| 5-8 | Expiry, cancel/fence invalidation, forbidden session/policy decisions, duplicate replay | Expired/wide/replayed approvals incorrectly succeeded |
| 9-12 | Native turn identity, unsupported methods, accepted configuration feedback, active turn | Wrong/missing binding and closed-turn requests incorrectly succeeded |
| 13-18 | Model reroute, auth change, malformed command, unknown environment/stdin/network, unrestricted sandbox, schema hash | Each unqualified condition incorrectly allowed the replay |
| 19-24 | Report provenance/unknown capabilities, safe I/O, time ordering, pending result, RPC errors, usage/quota | Missing output fields, file/encoding tracebacks, wrong pass/state or unclassified error |
| 25-26 | Public gate snapshots and registration time | Caller mutation changed cancellation ID to 999; pre-registration decision accepted |
| 27-28 | Complete pending ticket and empty observations | Missing ticket; empty replay incorrectly passed |
| 29-30 | Native cancel and rejected wide decisions close the gate | Later request returned replay status instead of inactive Attempt |
| 31-33 | Server callback cleanup, native turn error/retry, post-acceptance settings change | Late approvals incorrectly remained possible |
| 34-35 | Missing public request identity and malformed error-code sanitization | Missing ID raised KeyError; untrusted error code was echoed |
| 36 | Start only with `inProgress` and no error | `failed` and `completed` start variants both exited zero and allowed acceptance |
| 37 | Failed completion closes pending approval and reports a bounded failure | Failed completion lacked `NATIVE_TURN_FAILED` |
| 38 | Interrupted completion is nonpassing | Interrupted completion exited zero |
| 39 | Completion cannot report `inProgress` or a success with an error | `inProgress` completion exited zero |
| 40 | Completion requires an active turn and occurs at most once | Completion without start and duplicate completion both exited zero |

A supporting public-boundary audit test passed immediately and is not claimed as a red/green cycle. It observes process-launch and socket operations inside the replay CLI and fails on any such operation; the run measured **zero external effects**.

The terminal-outcome regression also checks a successful completion and completed/failed/interrupted variants after an earlier command acceptance; it passed after cycles 36-40 and is not a separate red/green claim. All six variants from the independent review are covered at the public CLI, with an additional repeated-completion check.

Final local validation: **41 tests and 35 subtests passed**, targeted Ruff/format checks and strict mypy passed for the five source files. Seven report artifacts were generated from actual offline CLI invocations, each containing the exact input-byte SHA-256. No live login, inference, file-tool or cancellation result is claimed.
