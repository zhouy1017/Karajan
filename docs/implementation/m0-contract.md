# M0-01: offline probe contract

Scope: [M0-01 / #2](../planning/m0/issues/01-contract.md), implementing PRD FR02 and FR07 at the agreed public boundary: `python -m karajan probe <file>`. This is an offline document inspector. It does not resolve credentials, approve budgets, enforce a sandbox, launch an executor, contact a provider, or enable a Profile.

## Run the examples

After the repository's development environment has been installed, run from the repository root:

```powershell
.\.venv\Scripts\python.exe -m karajan probe examples/probes/fixture-passed.json
.\.venv\Scripts\python.exe -m karajan probe examples/probes/fixture-rejected.json
```

The first command exits `0` with `status: passed`. The second exits `1` with `status: failed` and `ACCEPTED_BINDING_MISMATCH`: its scripted acceptance switches the requested subscription billing path to cash. Neither command performs that switch or makes a real call.

Reusable artifacts:

- [Passing input](../../examples/probes/fixture-passed.json) and [its actual CLI report](../../examples/probes/fixture-passed.report.json).
- [Rejected input](../../examples/probes/fixture-rejected.json) and [its actual CLI report](../../examples/probes/fixture-rejected.report.json).
- [Generated input JSON Schema](../../examples/probes/probe.schema.json). Pydantic also enforces cross-record identity, evidence and binding rules described below; JSON Schema alone is insufficient.

## Input contract

`karajan.probe.v1` is a single UTF-8 JSON document; a UTF-8 BOM is accepted. Fields not in this version are rejected. Identifiers must be nonempty, whitespace-free strings; revisions/fences must be positive integers, not booleans. The format makes no compatibility claim for an executor that has not implemented it.

| Field | Required content |
|---|---|
| `case_id` | Stable identity of this probe case |
| `profile` | ID/revision, full fixed binding, `auth_ref`, required permissions, declared admission granularity and usage coverage |
| `profile.binding` | Model, channel, account, runtime kind/version, authentication mode, billing path and provider-native settings |
| `attempt` | ID/fence, role, exact Profile ID/revision, authorization and budget references, granted permissions and full requested binding |
| `required_capabilities` | At least one capability identifier; each must have an observation to pass |
| `events` | Typed observations, each bound to the Attempt ID/fence and Profile ID/revision |
| `provenance` | Fixture or imported observation; runtime version, OS/isolation, timezone-aware observation date, evidence references and limitations |

References are opaque nonsecret identifiers. The inspector checks presence and shape, not the existence or validity of a real credential, authorization or reservation. Required permissions must be a subset of the Attempt's granted permissions. Model names and native settings are checked for a consistent declared binding, not against a provider's current model catalog.

Supported event types:

- `binding.accepted`: complete execution-adapter acceptance receipt. Every field, including native settings and JSON value types, must match the request exactly.
- `binding.provider_reported`: partial provider observation. Only supplied, non-null binding fields and supplied native-setting keys are compared. Missing keys remain unknown. Nested setting objects are compared only at reported keys; lists/scalars are compared in full. Provider observations never substitute for adapter acceptance.
- `capability.result`: capability ID, one of four states, evidence references and limitations. A claimed pass requires nonempty evidence references declared in provenance. References are recorded, never fetched or authenticated.

Events with identical IDs and normalized JSON content are coalesced and counted. A duplicate ID with conflicting content fails; the first observation is retained for diagnostics. Events for another identity/fence fail and cannot confirm this Attempt. Different events with contradictory states for the same capability fail. Repeated accepted receipts cannot conceal a mismatch, even if a later receipt matches.

## Output and qualification boundary

The CLI writes one JSON object to stdout, using `karajan.qualification.v1`. Parsed valid documents include an exact input-byte SHA-256; Profile revision; Attempt/fence/role; requested, accepted and provider-reported binding summaries; event counts; per-capability observations, evidence and limitations; and supplied provenance. Missing required capability observations are synthesized as `not_run`. Parse/read failures return the common report envelope and safe validation paths/codes, without echoing input values, file-error details or a traceback.

Native setting values are replaced in reports by a SHA-256 of normalized JSON. Provider summaries include parameter names actually reported. Authentication, authorization and budget references are not copied to output. Operator-supplied public metadata and evidence references remain visible; do not place secrets in those fields. Digests correlate evidence; they do not verify who produced an observation.

| Overall state | Meaning | Exit |
|---|---|---|
| `passed` | Supplied records are consistent, acceptance is present, and all observed/required capabilities pass with referenced evidence | `0` |
| `failed` | Invalid input, identity/configuration conflict, or an observed failed capability | `1` |
| `unsupported` | An observed capability is unsupported and there is no failure | `1` |
| `not_run` | Required observations, acceptance or passing evidence are missing, or a capability was not run; no higher-priority state applies | `1` |

Aggregation precedence: `failed > unsupported > not_run > passed`. Every nonpass observation contributes, including capabilities beyond the required set. Sorted, deduplicated `reason_codes` preserve all applicable reasons. The summary agrees with final status.

Every report has `qualification_scope: offline_contract`, `live_qualified: false`, and `profile_enabled: false`, including `imported_observation`. A fixture cannot declare itself live: unsupported provenance kinds/extra fields fail validation. Imported observations do not establish trusted provenance, validate a signature or prove that a live runtime worked.

`coverage.source` is `profile_declaration`. Admission granularity (`attempt` or `model_call`) and usage coverage (`attempt`, `model_call` or `unknown`) reproduce the Profile declaration; they are not measurements. `observed_model_call_count` is always `null` in M0-01. M0-03 supplies actual per-call observations. Subscription-internal calls are never inferred from event counts.

## Stable reasons

| Codes | Interpretation |
|---|---|
| `INPUT_UNREADABLE`, `INPUT_ENCODING_INVALID`, `INPUT_INVALID` | File access, UTF-8, JSON or schema failure; schema diagnostics use `REQUIRED_FIELD`, `UNKNOWN_FIELD`, or `INVALID_VALUE` and a field path |
| `REQUIRED_PERMISSION_MISSING` | A declared required permission is not granted |
| `PROFILE_IDENTITY_MISMATCH`, `EVENT_IDENTITY_MISMATCH` | Profile revision or event Attempt/fence identity does not match |
| `REQUESTED_BINDING_MISMATCH`, `ACCEPTED_BINDING_MISMATCH`, `PROVIDER_BINDING_MISMATCH` | Request, acceptance or reported parameter conflicts with expected binding |
| `PROVENANCE_RUNTIME_MISMATCH` | Observation runtime version differs from bound runtime |
| `BINDING_UNCONFIRMED` | No acceptance receipt for this Attempt |
| `EVENT_ID_CONFLICT`, `CAPABILITY_RESULT_CONFLICT` | Replayed content or capability state contradicts another observation |
| `CAPABILITY_MISSING`, `CAPABILITY_EVIDENCE_MISSING` | Required result or evidence for a claimed pass is unavailable |
| `CAPABILITY_FAILED`, `CAPABILITY_NOT_RUN`, `CAPABILITY_UNSUPPORTED` | Effective capability result contributes a nonpass |

## TDD evidence

Tests use the public CLI's exit code and JSON output with temporary input files. No private-function mocks are used. Every numbered cycle below was observed red before its production change and green afterward. Initial cycles used `python -m unittest discover -s tests/contract -p test_probe_cli.py`; the final suite also runs under pytest.

| Cycle | Behavior | Observed red | Green suite |
|---|---|---|---|
| 1 | Offline fixture pass only | `No module named karajan` | 1 test |
| 2 | Missing model and safe diagnostic | Unexpected exit `0` | 2 tests |
| 3 | Required permission | Unexpected exit `0` | 3 tests |
| 4 | Exact Profile revision | Unexpected exit `0` | 4 tests |
| 5 | Requested native settings fixed | Unexpected exit `0` | 5 tests |
| 6 | Accepted billing path fixed | Unexpected exit `0` | 6 tests |
| 7 | Event fence | Unexpected exit `0` | 7 tests |
| 8 | Missing acceptance stays unknown | Unexpected exit `0` | 8 tests |
| 9 | Preserve three nonpass states | Three unexpected exit `0` subcases | 9 tests |
| 10 | Missing capability is not run | Unexpected exit `0` | 10 tests |
| 11 | Empty capability requirement rejected | Unexpected exit `0` | 11 tests |
| 12 | Pass needs declared evidence | Two unexpected exit `0` subcases | 12 tests |
| 13 | Coalesce and count duplicate events | Missing `event_summary` | 13 tests |
| 14 | Reject conflicting event IDs | Unexpected exit `0` | 14 tests |
| 15 | Contradictory capability results | `unsupported` instead of `failed` | 15 tests |
| 16 | Partial provider report cannot replace acceptance | `failed` instead of `not_run` | 16 tests |
| 17 | Provider model mismatch | Unexpected exit `0` | 17 tests |
| 18 | Reproducible metadata, unknown call count | Missing `profile` | 18 tests |
| 19 | Runtime provenance version | Unexpected exit `0` | 19 tests |
| 20 | Safe file/encoding/JSON errors | File and encoding tracebacks | 20 tests |
| 21 | Verifiable provider summaries | Missing observation `values` | 21 tests |
| 22 | Summary agrees with result | Incorrectly said `passed` | 22 tests |
| 23 | Empty provider feedback is unknown | `observed` instead of `unknown` | 23 tests |
| 24 | Additional nonpass cannot yield pass | Unexpected exit `0` | 24 tests |
| 25 | JSON types cannot coerce | Boolean equalled integer | 25 tests |
| 26 | Another fence cannot satisfy acceptance | Existing fence test saw `observed` | 25 tests |
| 27 | Compare only reported native-setting keys | Matching partial report failed | 28 tests |
| 28 | Per-capability evidence/limitations retained | Missing capability `event_id` | 29 tests |

Between cycles 26 and 27, two supporting regression tests passed immediately: required reference/type variants, and an external-boundary audit of valid/invalid CLI runs. The audit hook counts process-launch and socket operations inside the CLI process and rejects any attempt; both paths measured **zero external effects**. These already-passing checks are not claimed as red/green cycles.

Final local validation on Python 3.12.14 / Pydantic 2.13.5: **29 tests and 14 subtests passed** under pytest; targeted Ruff checks passed; strict mypy passed for four owned source files. Passing/rejected reports were generated from actual CLI runs with exits `0`/`1`. No real account, credential, subscription or cash API was used.
