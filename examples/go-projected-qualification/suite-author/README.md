# FixedGoSuite revision 2 and read-only grant authentication

Author evidence in the projected qualification worktree, base
`6133e9423a27c9139da3c6b4243d03498c8046d2`. No real credential was read and no
official provider request was made by this author. All upstream responses in
these tests are explicitly HTTP fixtures. Native Linux tools, tokenizer,
SQLite journals and CandidateStore are real.

## Interface and scope

`FixedGoSuite(..., suite_ref={id: opencode-go-native-read-edit-linux, revision: 2},
accounting=GoRequestAccounting(...))` explicitly selects the projected suite.
Omitting the reference preserves v1 source/report/validation shapes; accounting
does not silently upgrade a v1 suite. Registered native_settings must name the
same suite revision. The current complete source, credential identity and
scenario grants must match the persisted start before grants/process effects.

The v2 source uses `karajan.fixed-go-suite-source.v2`, exact native source and
fixed probe_spec. The result uses `karajan.fixed-go-suite-observation.v2`.
Only when both scenarios pass all cross-checks are
`validation.projected_native_tools`, `candidate_capture`, and
`context_accounting` passed. Fixture scope remains
`projected_native_tools_fixture`; this suite does not enable a Profile, approve
a Task, grant arbitrary file access, or claim a cash bound.

The Suite checks actual Journal receipts and strict ContextMeasurement arithmetic,
limits/source/request identity, provider-usage observations, retained input/history,
native read/edit/denial results and stopped cleanup. It opens the controller-fixed
CandidateStore, reads the complete baseline and candidate, compares manifests,
projection, writer/attempt/fence and source identities, and rechecks materialized
file hashes/modes. The changed target also goes through the existing bounded AST
clamp checker. Missing validation and reviewer evidence remains explicitly missing:
`CHECK_EVIDENCE_MISSING:fixture_check`, `REVIEW_EVIDENCE_MISSING`.

`GoCallJournal.authenticate_grant(grant_id, *, capability, binding)` authenticates
exact controller identity in one existing SQLite read transaction. It returns
detached current snapshot facts, without capability/digest or send permission.
Expired/revoked/unknown-send facts are not changed. The observer must inspect
active state and zero calls; only begin_call can authorize a send. A missing
ledger is opened in mode=ro and cannot be recreated.

## Red and retained history

- constructor-before.xml: one actual missing public suite_ref interface failure.
- authentication-before.xml: four invalid Windows fixture filename failures and
  four missing-method failures. The filename was corrected to legal URI-reserved
  characters; authentication-red.xml then records eight actual missing-method
  failures before implementation.
- authentication-after.xml: new eight plus old 53 Journal cases passed.
- fixture-cases-red.xml: one actual native two-scenario run had its report
  fixture_cases changed to false yet the Suite incorrectly passed. The exact
  source is go-suite-before-fixture-cases.py.txt. The fix checks reported cases
  and the actual materialized candidate; the same input is green in later runs.
- fixture-cases-before.xml and history/prefreeze retain a preliminary failure
  before any scenario began, with ValueError and revoked preallocated grants.
  No inner clock observations were captured, so its cause is not attributed to
  clock rollback.
- suite-wsl-final.xml retains 25 passed and one start-binding rejection before
  that measurement case reached its effect. measurement-final.xml re-executes
  that same case successfully without changing production time rules. Test-only
  clock/start recording now writes before/during calls to retain future failures.

## Checks

Use the worktree backend explicitly. Pythonpath includes backend, tests/projects,
tests/isolation and tests/adapters/opencode. Linux tests require the pinned ELF
and official tokenizer artifacts, with KARAJAN_REQUIRE_OPENCODE_ISOLATION=1,
KARAJAN_REQUIRE_GO_TOKENIZER=1, HF_HUB_OFFLINE=1 and TRANSFORMERS_OFFLINE=1.
The upstream is always httpx.MockTransport in these author tests.

Windows frozen-windows.xml: 133 passed, 21 Linux-only skips, 6.81 seconds.
Ruff check/format and mypy of the two owned production modules passed.
Final Linux completion and exact hashes are recorded in freeze.json.

The parent owns any official-provider evidence and its interpretation. A later
relay compatibility change requires a new complete source and a minimal normal
two-scenario recheck; earlier successful/failed reports remain historical.

Published repository references:
[implementation specification](../../../docs/implementation/m3-go-projected-qualification.md),
[Suite](../../../backend/karajan/projects/go_suite.py),
[Journal](../../../backend/karajan/adapters/opencode/go_journal.py),
[Suite public tests](../../../tests/projects/test_go_projected_suite.py),
[authentication public tests](../../../tests/adapters/opencode/test_go_grant_authentication.py).

The delegated final public Store test passed against the updated relay (one case, two
scenarios, six synthetic HTTP requests; four nullable-name continuations). See
[final Store XML](../store-author/native-final.xml) and
[Store source freeze](../store-author/freeze.json). The original full 26-case
source and reports above remain historical and were not rewritten to a later
relay identity. The parent separately owns official-provider reports.
