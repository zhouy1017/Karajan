# Planning model input

`compile_planning_input(executions, accounting, *, execution_id, principal)` is
a read-only compiler. It obtains the execution and Run from the supplied
`PlanningExecution` and obtains the immutable registered-base bytes only via
`read_repository_snapshot`; callers cannot provide a prompt, profile, path,
source, digest, or token estimate.

The returned `PlanningModelInput` (`karajan.planning-model-input.v1`) contains
the original requirement and acceptance list, original intent and
authorization ceiling, the original execution binding, the complete frozen v2
execution policy, the complete snapshot manifest, and every approved file as
base64. `request_bytes` is canonical UTF-8 JSON and is measured, hashed, and
size-recorded. The fixed system instruction marks repository content as
untrusted data; the user message contains the complete delimited payload and
the exact `PlanV2.model_json_schema()` contract used by
`parse_planning_output(version="v2")`.

The compiler requires a current `karajan.run-planning.v2` run with
`karajan.execution-policy.v2`, frozen `max_context_tokens`, and
`reserved_output_tokens`; unsupported versions are rejected rather than
represented by a combined schema. Manifest path, mode, size, and SHA-256 are
checked against the persisted blob before base64 encoding. It uses the pinned
Go accounting source with fixed margin 2048 and ratio margin 1000 basis points;
bytes and token/output/context limits therefore fail explicitly without
truncation. The artifact digest proves this local artifact only. Native Relay
adds session/system/history and must measure its complete wire request again;
this module creates no admission, ledger, capacity, host, journal, model,
qualification, plan, or transport effect.
