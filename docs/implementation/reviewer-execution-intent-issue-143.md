# #143 Reviewer execution intent

Candidate base: `b1bd5e9e58eba9dd6d5d4e3d63b815edadd265d2`.

This slice owns only the Reviewer execution bootstrap, binding, intent ledger,
and fixed runner. It does not invoke native Review, a provider, HTTP, Journal,
Evidence, parser, or delivery.

## Evidence terminology

- **C**: a product-behaviour test passed. It is not a static-source claim.
- **P**: that test ran locally in WSL/Linux against the stated executable/store
  boundary. A test-only frozen qualification or deployment port remains just a
  fixture and never qualifies a profile or proves service behaviour.
- **S**: real service evidence. None was collected for this slice.

## Original acceptance-condition matrix (2026-09-08)

| Original acceptance condition | Behavioural evidence and result | Remaining gap |
| --- | --- | --- |
| Real lineage/CAS/Checks/Capacity produce one complete intent; bad material has no new intent/effect | **C passed**: `test_prepare_is_durable_and_freezes_compiler_identity` uses the approved Reviewer/Worker/Candidate/Checks path and verifies the v2 input, immutable identity, and unchanged Capacity snapshot. `test_tampered_persisted_intent_is_rejected_without_a_host_effect` rejects a changed durable Candidate binding and observes no Host record. | No S/native/provider evidence. |
| Exact replay, concurrent use, reopen, and tamper retain/reject the same identity | **C/P passed**: `test_concurrent_independent_facades_replay_one_prepared_identity` synchronizes two independently constructed facades after compilation and proves same-key convergence on one execution while a different key rejects. The production existing-only factory reopens its seeded identity and leaves the execution DB unchanged. Existing schema-valid ledgers in the actual registered repository, its parent symlink alias, and an in-repository hardlink are rejected before SQLite opens them; the original bytes and modes remain unchanged. | No S/native/provider evidence. |
| Every prepare/claim re-enters current admission/compiler and source guard; changed current facts block effects | **C/P passed**: `test_current_qualification_change_blocks_next_host_prepare` changes current qualification after intent persistence and observes no Host preparation. `test_expiry_after_real_sqlite_writer_wait_blocks_the_actual_effect` holds real Host or execution SQLite writers until the retained reservation/window has expired, then proves no new Host/control write or observer claim. The factory uses the actual pinned Linux OpenCode binary and tokenizer through `deployment_source`, with no provider call. | No native Review/provider assertion. |
| Host prepare/control and read-only inspection are durable; no facade start/new session on recovery | **C passed**: `test_fixed_host_prepare_is_replayable_without_starting_native` verifies same prepared/start identity; `test_prepare_rejects_second_key_and_never_prepares_host` verifies no Host record before prepare. | No native execution claim. |
| A registered direct Host child alone gains the first effect claim; lost reply/reopen never gets a second right | **P passed**: normal, committed-lost-reply, and concurrent modes run in one actual registered direct child. The lost-reply test first observes the original Host terminal record, then replays while asserting its prepared/attempt/start, supervisor, and persisted runner identities plus exactly one persisted launch and child registration. Completion/usage observations are intentionally allowed to progress. Concurrent reopened facades yield exactly one claim right. | No native Review/provider assertion. |
| Cancellation permanently blocks future claim while history stays inspectable; delivery remains false/not_run | **C passed**: `test_cancel_serializes_with_actual_inspect_host_writer_and_stays_durable` races a real `inspect_host` ledger update with cancellation and retains the durable cancellation without a stale WAL upgrade; direct-child recovery then blocks a later claim. Prepared intent asserts `delivery={eligible:false,state:not_run}`. | No remote stop, settlement, or provider assertion. |
| Public state is non-sensitive; quality checks and no native/HTTP/model/Journal grant side effects | **C passed (bounded)**: the Linux factory test installs counters on the actual configured Host start, Go Journal grant/call, Candidate gate, and Evidence-save entry points; it runs facade prepare/freeze/failed-unregistered-claim/reopen/cancel. The required Candidate gate is observed only as its read-only current-context check, while Host start, Journal grant/call, and Evidence writes are zero and the real configured Journal bytes are unchanged. It also checks the public persisted intent lacks credential/seal labels. This is C evidence only: Relay/HTTP transport is not composed by this leaf, so it is not a claim about Relay implementation. | No S evidence and no broad claim about unrelated modules or provider/Journal delivery. |

## Commands and observed results

All WSL commands used `/tmp/karajan-candidate-mode-qy6_mqo2/venv/bin/python`,
`PYTHONPATH=backend:tests:tests/projects:tests/runs:tests/candidates`, and a
fresh `/tmp/karajan-dg01-*` base temp. The current factory regression uses the
CI default runtime discovery: with `KARAJAN_OPENCODE_LINUX_BINARY` unset it
uses `runtimes/opencode/node_modules/opencode-linux-x64/bin/opencode`, the
pinned v1.18.29 binary with SHA-256
`ca6c0e1f42be3120595bf6848937e7586ec862c87fa7aa111e89c7cc6e9a4650`.
It keeps the pinned tokenizer at
`/mnt/c/Users/Chooo/Playground/Karajan/.cache/go-context-artifacts` and sets
both required-artifact flags. Thus a missing or invalid runtime is a useful
test failure (`Prepared fixed Linux OpenCode artifact is required`), never a
silent skip. The test only opens/hashes existing descriptors and stores; it
does not invoke native Review, a provider, HTTP, Journal grant/call, Evidence,
or a real credential.

| Command | Result |
| --- | --- |
| Prior `43ebc45` / PR #150 CI 34219462874 Linux job 102038995726: CI main `uv run --frozen --extra dev pytest tests`, with global pinned tokenizer and `KARAJAN_REQUIRE_OPENCODE_ISOLATION=1` but **without** `KARAJAN_OPENCODE_LINUX_BINARY` | **Red (CI)**: `1 failed, 3070 passed, 7 skipped in 1385.87s`; only `test_existing_factory_reopens_identity_and_rechecks_own_descriptor` failed, at its direct `os.environ["KARAJAN_OPENCODE_LINUX_BINARY"]` lookup with `KeyError`. The later independent-boundary CI steps set that override, but the main pytest step does not. |
| Current no-override factory regression: `env -u KARAJAN_OPENCODE_LINUX_BINARY KARAJAN_REQUIRE_OPENCODE_ISOLATION=1 KARAJAN_REQUIRE_GO_TOKENIZER=1 KARAJAN_GO_TOKENIZER_DIRECTORY=/mnt/c/Users/Chooo/Playground/Karajan/.cache/go-context-artifacts PYTHONPATH=backend:tests:tests/projects:tests/runs:tests/candidates /tmp/karajan-candidate-mode-qy6_mqo2/venv/bin/python -m pytest tests/runs/test_reviewer_execution_bootstrap.py::test_existing_factory_reopens_identity_and_rechecks_own_descriptor -q --basetemp /tmp/karajan-dg01-reviewer-ci-default-factory-fixed` | **P passed**: `1 passed in 9.89s`. This runs the actual factory/source path cold with the standard npm runtime path, rather than a deployment-source/runtime substitute or a Windows executable. |
| Current no-override owned suite: `env -u KARAJAN_OPENCODE_LINUX_BINARY KARAJAN_REQUIRE_OPENCODE_ISOLATION=1 KARAJAN_REQUIRE_GO_TOKENIZER=1 KARAJAN_GO_TOKENIZER_DIRECTORY=/mnt/c/Users/Chooo/Playground/Karajan/.cache/go-context-artifacts PYTHONPATH=backend:tests:tests/projects:tests/runs:tests/candidates /tmp/karajan-candidate-mode-qy6_mqo2/venv/bin/python -m pytest tests/runs/test_reviewer_execution_bootstrap.py tests/runs/test_reviewer_execution_intent.py -q --basetemp /tmp/karajan-dg01-reviewer-ci-default-owned-captured` | **P passed**: `27 passed in 36.67s`. The full seven-module affected suite was not rerun because this is a test-fixture discovery repair: production behaviour and the other six modules are unchanged. |
| Candidate `fd3f765` targeted lost-reply assertion | **Red (historical)**: the earlier `replayed.state == before_replay.state` terminal-state assertion exposed recovery-state timing; this was retained as the baseline diagnostic rather than treated as product evidence. |
| Candidate `1d7fcd8` two-module command supplied by Commander | **Red (historical)**: `1 failed, 19 passed in 29.95s`; `replayed == reopened.host.inspect(...)` raced a legitimate `exit_code: None -> 0` completion update. This is a mutable-snapshot regression, not a second launch. |
| Current bounded candidate: lost-reply test, three fresh `/tmp/karajan-dg01-reviewer-green-{1,2,3}` directories after Host terminal observation | **P passed**: each run `1 passed, 15 deselected` (5.72s, 5.76s, 5.64s). This is the new deterministic regression feedback loop. |
| Current repaired candidate: bootstrap + intent modules with `/tmp/karajan-dg01-reviewer-owned-final4` | **P passed**: `27 passed in 38.72s`. This includes repository containment, cold historical reopen, writer-wait expiry, cancellation-WAL, and connected-boundary regressions. |
| Current repaired candidate: `tests/runs/test_reviewer_execution_bootstrap.py tests/runs/test_reviewer_execution_intent.py tests/runs/test_reviewer_binding.py tests/runs/test_reviewer_input_approved.py tests/runs/test_admission_guard.py tests/runs/test_task_admission.py tests/execution/test_runnerhost.py -q --basetemp /tmp/karajan-dg01-reviewer-affected-final` | **P passed**: `155 passed in 122.43s`. This result is for the repaired candidate, not retrospectively for an older candidate. |
| Current repaired candidate: `C:/Users/Chooo/Playground/Karajan/.venv/Scripts/python.exe -m pytest tests/runs/test_reviewer_execution_bootstrap.py tests/runs/test_reviewer_execution_intent.py -q --basetemp C:/Users/Chooo/AppData/Local/Temp/karajan-reviewer-windows-final` | **P passed**: `22 passed, 5 skipped in 44.52s`; the Linux-only factory/direct-child evidence and an unavailable Windows directory-symlink capability were skipped. |
| Current fixture repair: `C:/Users/Chooo/Playground/Karajan/.venv/Scripts/python.exe -m ruff check .` | `All checks passed!` |
| Current fixture repair (Windows): `C:/Users/Chooo/Playground/Karajan/.venv/Scripts/python.exe -m mypy backend` | `Success: no issues found in 154 source files` |
| Current fixture repair (WSL/Linux): `/tmp/karajan-candidate-mode-qy6_mqo2/venv/bin/python -m mypy backend` | `Success: no issues found in 154 source files` |

## Independent-review repairs on this candidate

### Final second-review repair (2026-09-08)

The final static Standards report recorded two P1 violations (the retained
Capacity quota fence was dropped after yielding to Host/claim writers, and the
factory allowed a Host root beneath an actual registered repository) and one
nonblocking duplicated-containment-walk smell. The final Spec report recorded
one P1 (the same dropped quota fence) and two P2s (lost Host-prepare reply had
no historical read-only recovery, and a first intent could use a stale
deployment source). The P3 duplication was intentionally not expanded into a
cross-module refactor.

- `reviewer_reserved_effect_guard` now yields an ephemeral,
  non-serializable final-effect callback captured by the actual held
  Admission/Capacity producer. Reviewer intent consumers call it only after
  their deployment-source read and after each real private-ledger, Host
  preparation, Host-control, or observer-claim writer wait. It retains quota
  freshness/reset, reservation, Reviewer qualification/estimate, and Run
  cumulative-deadline checks without reopening controller writers.
- Existing-only factory opening rejects a Host directory under an actual
  `ProjectRegistry` repository root, including resolved aliases and an existing
  Host SQLite hard link where applicable, before `RunnerHost` can open it.
- `RunnerHost.inspect_original_preparation` is a read-only correlation port.
  A null Reviewer `host_prepared_id` can inspect only a persisted matching
  start key and complete original Host manifest (including Attempt/fence);
  it does not save a receipt or enable control, start, session, or observer
  claim.
- First intent insertion now rechecks the current deployment source after its
  real ledger writer wait. The new regressions cover all three quota-wait
  writers, a source read crossing the Run deadline, stale first intent source,
  Host-in-repository rejection, and loss after Host commit before ledger save.

Candidate implementation source commit:
`b275567d22a09473112373163ad7832f8e8795be`. No native Review, provider,
credential, HTTP, Relay, Journal grant, or real-service evidence is claimed.

- Standards P1 repository-layout blocker: the ledger is checked against the
  actual existing `ProjectRegistry` roots before any SQLite open/write;
  direct, parent-symlink, and hardlink paths are covered where the platform
  permits them.
- Standards/Spec temporal blocker: optional Host callbacks run immediately
  before new Host prepare/control rows, and the claim rechecks retained
  reservation and Reviewer windows immediately after its execution writer is
  acquired. They use held scalar facts and do not reopen Admission/Project
  writers under the Host lock.
- Standards cancellation blocker: cancellation takes the execution writer
  before reading/updating its record, preventing a stale WAL read promotion.
- Spec historical-reopen blocker: `ReviewerExecutionHistory` is an explicit
  read-only interface returned when a current runtime, tokenizer, Journal, or
  credential-seal asset is absent; it creates nothing and exposes no effect
  methods.
- Spec zero-effect-evidence blocker: the connected-boundary regression records
  the read-only Candidate gate separately from zero native/Journal/Evidence
  writes and unchanged configured Journal storage. Relay is not composed here,
  so no Relay claim is made.

## Final repair commands and observed results (2026-09-08)

| Command | Result and scope |
| --- | --- |
| WSL2 Ubuntu, CI-default npm runtime (override unset), `file` and `opencode --version` | Actual binary was Linux ELF x86-64 and reported `1.18.29`; the pinned tokenizer directory existed. This is runtime-fixture evidence, not Profile qualification. |
| `env -u KARAJAN_OPENCODE_LINUX_BINARY KARAJAN_REQUIRE_OPENCODE_ISOLATION=1 KARAJAN_REQUIRE_GO_TOKENIZER=1 KARAJAN_GO_TOKENIZER_DIRECTORY=/mnt/c/Users/Chooo/Playground/Karajan/.cache/go-context-artifacts PYTHONPATH=backend:tests:tests/projects:tests/runs:tests/candidates /tmp/karajan-candidate-mode-qy6_mqo2/venv/bin/python -m pytest tests/runs/test_reviewer_execution_bootstrap.py tests/runs/test_reviewer_execution_intent.py tests/runs/test_reviewer_binding.py tests/runs/test_reviewer_input_approved.py tests/runs/test_admission_guard.py tests/runs/test_task_admission.py tests/execution/test_runnerhost.py -q --basetemp /tmp/karajan-dg01-reviewer-final-capability-*` | **P passed:** `162 passed in 124.49s`. Fresh private Linux base temp; no override and no Windows executable substituted. |
| Windows main `.venv`, same seven modules with `PYTHONPATH=backend;tests;tests/projects;tests/runs;tests/candidates` and fresh private base temp | **C passed:** `157 passed, 5 skipped in 196.47s`. The skips were one Linux-only factory test, three Linux direct-child tests, and an unavailable Windows directory-symlink privilege; they are not counted as Linux evidence. |
| Windows main `.venv`: `python -m ruff check .`; `python -m mypy backend/karajan` | `All checks passed!`; `Success: no issues found in 154 source files`. |
| WSL candidate venv: `python -m ruff check .`; `python -m mypy backend/karajan` | `All checks passed!`; `Success: no issues found in 154 source files`. |

These commands were run for this final repair only. Earlier full-suite and CI
results above remain historical records and are not attributed to this source
candidate.

The affected command ran in a local captured process because it exceeds the interactive 30-second yield; its exit code and final pytest summary above were observed before recording this result.

## CI-default relative-tokenizer fixture repair (2026-09-08)

The earlier `43ebc45` / PR #150 CI failure was a separate entry failure: the
test directly read the unset `KARAJAN_OPENCODE_LINUX_BINARY` variable and
raised `KeyError`. The no-override runtime lookup already repaired that case.

The subsequent current-head CI failure at `3cb14cf` (run `34224575469`,
Linux job `102055632775`) was a distinct `TASK_BOOTSTRAP_INVALID` failure in
`write_go_task_bootstrap` / `GoTaskSettings.from_document`: CI supplied the
literal relative tokenizer value `.cache/go-context-artifacts`, while the
fixture passed that relative path into the trusted absolute-path descriptor.
The remote result was `1 failed, 3077 passed, 7 skipped, 2 warnings` in
`1406.32s`; only
`test_existing_factory_reopens_identity_and_rechecks_own_descriptor` failed.

The fixture now resolves its already-existing, asserted tokenizer directory
before constructing `GoTaskSettings`. Product descriptor validation remains
unchanged, CI globals remain unchanged, and no production source is modified.

| Command | Result |
| --- | --- |
| `env -u KARAJAN_OPENCODE_LINUX_BINARY KARAJAN_REQUIRE_OPENCODE_ISOLATION=1 KARAJAN_REQUIRE_GO_TOKENIZER=1 KARAJAN_GO_TOKENIZER_DIRECTORY=.cache/go-context-artifacts HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONPATH=backend:tests:tests/projects:tests/runs:tests/candidates /tmp/karajan-candidate-mode-qy6_mqo2/venv/bin/python -m pytest tests/runs/test_reviewer_execution_bootstrap.py::test_existing_factory_reopens_identity_and_rechecks_own_descriptor -q -p no:cacheprovider --basetemp=/tmp/karajan-dg01-reviewer-relative-red` | **Red before repair:** `TASK_BOOTSTRAP_INVALID`, `1 failed in 5.31s`. |
| Same command with `--basetemp=/tmp/karajan-dg01-reviewer-relative-green` after the fixture-only repair | **Green:** `1 passed in 9.11s`. |

This follow-up is limited to the bootstrap fixture and this evidence record;
the broader candidate checks and any CI dispatch must use the resulting commit
instead of being attributed to the prior `f70bb0e` candidate.
