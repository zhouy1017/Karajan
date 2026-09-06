# Qualification v2 grant + relay context

Author implementation against `153482593380fdb5a8a5e16940f600c06acfd2ca`, in `go-task-capture`. Only the two adapter sources and the two new adapter test files were edited. No credentials, provider calls, qualification facts, Git writes, or tokenizer algorithm changes.

## Public contract

`go_journal.GoQualificationLimits` is a strict content-free Contract:

```text
source_sha256: lowercase SHA256
approved_input_tokens: strict int 1..1_000_000
reserved_output_tokens: strict int 1..131_072
operating_context_tokens: strict int 1..1_000_000
fixed_margin: strict int 0..1_000_000
ratio_margin_basis_points: strict int 0..10_000
```

The limits follow GoRequestAccounting's supported numeric range. They are not measured maximum capacity or Profile qualification. Existing relay max_tokens <= 4096 remains in force. Every actual request still must satisfy input/output/window arithmetic.

A v2 grant contains the existing qualification_id and common fields, plus:

```text
schema_version = "karajan.go-qualification-grant.v2"
probe_spec_digest: lowercase SHA256
scenario: "edit" | "denied_read"
context: GoQualificationLimits
```

Unknown versions and mixed Task/qualification shapes are invalid. Existing legacy normalization, key order and canonical binding do not acquire default fields. No database migration is needed.

`GoCallJournal.begin_call` authenticates the persisted grant, preserves existing call replay behavior, and for a new v2 call requires a valid `ContextMeasurement` matching all six context fields exactly. Missing context returns `QUALIFICATION_CONTEXT_REQUIRED`; mismatch returns `QUALIFICATION_CONTEXT_MISMATCH`. Invalid inputs consume no durable slot. A call already persisted remains history with send_allowed=false; changing/omitting its context still returns the existing `CALL_CONTEXT_CONFLICT`.

```python
from karajan.adapters.opencode.go_relay import GoQualificationContext

context = GoQualificationContext(
    accounting=accounting,
    source_sha256=grant_binding["context"]["source_sha256"],
    probe_spec_digest=grant_binding["probe_spec_digest"],
    scenario=grant_binding["scenario"],
    approved_input_tokens=grant_binding["context"]["approved_input_tokens"],
    reserved_output_tokens=grant_binding["context"]["reserved_output_tokens"],
    operating_context_tokens=grant_binding["context"]["operating_context_tokens"],
    fixed_margin=grant_binding["context"]["fixed_margin"],
    ratio_margin_basis_points=grant_binding["context"]["ratio_margin_basis_points"],
)
```

This frozen dataclass exposes `limits()` (a detached, strictly validated context dict) and `measure(payload)` (the existing actual accounting implementation, source-checked each call). Invalid constructor data produces the fixed `QUALIFICATION_CONTEXT_INVALID` error. It has no endpoint/prompt/report input or authority to select a suite.

Relay requires this concrete context for v2 qualification and matches spec/scenario/all limits before measuring. Task GoRelayContext continues to match only Task grants. Legacy+context, Task+qualification context, qualification+Task context, missing context and unknown schema all reject before upstream send. V2 errors are `QUALIFICATION_CONTEXT_ACCOUNTING_REQUIRED` and `QUALIFICATION_CONTEXT_BINDING_MISMATCH`; actual journal ownership remains separately authenticated before send. Forged binding or bad capability must not revoke another active grant.

The existing durable measurement, usage checks, lost response, remaining-send withdrawal and cleanup implementation is reused. The producer still must derive the spec from a persisted trusted qualification start, pass the actual credential generation only to the relay, verify fixed projected native behavior, and produce the qualification record. None of that is claimed by this port alone.

## Actual Windows checks

Artifacts were loaded from the already downloaded official cache under `go-task-execution/.cache/go-context-artifacts`, with `KARAJAN_REQUIRE_GO_TOKENIZER=1`, `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`. No runtime download or upstream model request occurred; HTTP uses MockTransport only beyond the real local relay server.

- `journal-before.xml`: first public v2 create rejected as unsupported; 1 failed.
- `journal-green.xml`: that create/required-context/first-send case passed after Journal implementation.
- `relay-before.xml`: missing public GoQualificationContext; 1 failed.
- `relay-green.xml`: first public Journal+HTTP end-to-end case passed (2 total).
- `journal-boundaries.xml`: 20 passed, including six-slot real SQLite concurrency, exact limits/source, scope identity, unknown replay, expiry tombstone and legacy shape.
- `qualification-boundaries.xml`: 51 passed / 4 failed because the new tests assumed final journal completion precedes the HTTP error response. The response correctly followed revocation, while final outcome is written in handler finally.
- `qualification-after-synchronization.xml`: same 55 cases passed after tests first assert revocation/next-request denial, then join relay.close before reading the final outcome. No product fix was made for that test synchronization issue.
- `final-regression.xml`: 231 passed / 0 skipped / 19.62s, including existing Journal, Task grant, relay, relay+Journal and Task context tests, plus new 55 cases.
- Ruff check and format --check of the four owned files passed. Mypy passed for both adapter source files.

Commands from the worktree (repository root venv Python):

```text
python -m pytest tests/adapters/opencode/test_go_journal.py tests/adapters/opencode/test_go_task_grants.py tests/adapters/opencode/test_go_relay.py tests/adapters/opencode/test_go_relay_journal.py tests/adapters/opencode/test_go_relay_context.py tests/adapters/opencode/test_go_qualification_grants.py tests/adapters/opencode/test_go_qualification_relay.py -o "pythonpath=backend tests/adapters/opencode" -q --junitxml=.cache/qualification-context-evidence/final-regression.xml
python -m ruff check backend/karajan/adapters/opencode/go_journal.py backend/karajan/adapters/opencode/go_relay.py tests/adapters/opencode/test_go_qualification_grants.py tests/adapters/opencode/test_go_qualification_relay.py
python -m ruff format --check backend/karajan/adapters/opencode/go_journal.py backend/karajan/adapters/opencode/go_relay.py tests/adapters/opencode/test_go_qualification_grants.py tests/adapters/opencode/test_go_qualification_relay.py
python -m mypy backend/karajan/adapters/opencode/go_journal.py backend/karajan/adapters/opencode/go_relay.py
```

No Linux runtime, official Go acceptance, suite-v2 or Profile qualification is claimed by these Windows/local HTTP results. Independent review and later producer integration remain the root task's responsibility.
