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
| Public state is non-sensitive; quality checks and no native/HTTP/model/Journal grant side effects | **C passed (bounded)**: the Linux test first opens and reopens the production existing-only factory descriptor, then installs counters on the configured Host start, Go Journal grant/call, Candidate gate, and Evidence-save entry points. Its prepare/freeze/lost-Host-reply/reopen/cancel sequence is performed by a separately constructed `seeded` facade with the fixture launch compiler; it does **not** call `claim_registered_observer`. The required Candidate gate is observed only as its read-only current-context check, while Host start, Journal grant/call, and Evidence writes are zero and the real configured Journal bytes are unchanged. It also checks the public persisted intent lacks credential/seal labels. This is C evidence only: Relay/HTTP transport is not composed by this leaf, so it is not a claim about Relay implementation. | No S evidence; no `claim_registered_observer` coverage; and no successful lifecycle run through the factory-composed facade. The attempted factory-facade prepare currently rejects `REVIEWER_RESERVED_ROUTE_NOT_CURRENT` before a Host effect, so it cannot be cited as lifecycle evidence. |

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

## CI-default hardlinked runtime fixture repair (2026-09-08)

PR CI34230588520 (`f02f378ca9370c989884761d36fa2bc729c07991`) exposed a third,
separate fixture failure on Linux: the existing factory reached the pinned
runtime at
`runtimes/opencode/node_modules/opencode-linux-x64/bin/opencode`, then
production `_plain()` rejected it with `TASK_DEPLOYMENT_PATH_INVALID` at the
`st_nlink != 1` check. The CI trace does not report the runtime inode or link
count, so this record does not claim that CI observed a particular `st_nlink`.
The runtime was present and the tokenizer had already been made absolute.

The local reproduction used the pinned v1.18.29 ELF
(`ca6c0e1f42be3120595bf6848937e7586ec862c87fa7aa111e89c7cc6e9a4650`) and an
exact temporary hardlink on the same filesystem. `stat` reported the package
path and alias as the same inode, `nlink=2`, mode `777`, size `184666240`, and
the same digest. This models the legitimate npm postinstall shape without
removing or unlinking any shared runtime asset.

The fixture now treats the package path as input only. It copies the fixed
binary into `tmp_path/private-deployment/opencode`, preserves and verifies the
source mode and size, verifies equal streaming SHA-256 digests, verifies the
new target is a regular standalone file with `nlink=1`, and verifies the
source inode/link metadata is unchanged. The existing-only production factory
then receives that private staged path through the real Go-task descriptor;
production private-path and hardlink checks remain unchanged. The controlled
override is used only to inject the isolated hardlink input for the diagnostic
run, not as a factory bypass or test boolean.

| Command | Result and limits |
| --- | --- |
| Current pre-repair candidate `afa7f5b7c496778fcb670df1552dff6fda18e365`, controlled `KARAJAN_OPENCODE_LINUX_BINARY` pointing to the temporary `nlink=2` alias, relative `.cache/go-context-artifacts` tokenizer, required isolation/tokenizer and offline flags; one factory test | **Red (behavioral):** `1 failed in 5.6s` with `TASK_DEPLOYMENT_PATH_INVALID` from production `go_task_runtime._plain`. This is the current-candidate fixture failure, not historical CI success. |
| Repaired candidate, override unset, relative `.cache/go-context-artifacts` tokenizer, same required flags and a fresh WSL `/tmp` basetemp; one factory test | **P passed:** `1 passed in 9.58s`. |
| Repaired candidate, same flags with the controlled `nlink=2` alias as input and a fresh WSL `/tmp` basetemp; one factory test | **P passed:** `1 passed in 10.21s`. The helper staged a standalone copy; the aliased input remained denied if passed directly to production. |
| Repaired candidate, override unset and relative tokenizer; full `tests/runs/test_reviewer_execution_bootstrap.py` module with fresh `/tmp/karajan-dg01-reviewer-bootstrap-final-20260908` basetemp | **P passed:** `4 passed in 10.25s`. |
| Repaired candidate, same flags; `tests/runs/test_reviewer_execution_bootstrap.py tests/runs/test_reviewer_execution_intent.py` with fresh `/tmp/karajan-dg01-reviewer-bootstrap-intent-final2-20260908` basetemp | **P passed:** `39 passed in 50.26s`. |
| Windows main `.venv`, same relative tokenizer/isolation/offline configuration; full bootstrap module with fresh private basetemp | **C passed:** `3 passed, 1 skipped in 1.00s`; the Linux-only factory test was skipped by its platform marker. |
| Windows main `.venv`: `python -m ruff check .`; `python -m mypy backend` | **Passed:** Ruff clean; mypy `Success: no issues found in 154 source files`. |
| WSL candidate venv: `python -m mypy backend` | **Passed:** `Success: no issues found in 154 source files`. |

No production file, shared runtime directory, CI workflow, provider, account,
credential, Journal, native Review, or HTTP operation was changed or invoked.
The Commander-reported Windows/Linux Capacity, reserved execution guard, and
Go intent independent-module results remain shared-source evidence and are not
relabelled as proof of this fixture repair. The earlier f02, 3cb, and 43eb
failures and their static-review attribution remain preserved above.

## P1 final-effect repair (2026-09-08)

The fresh independent Standards and Spec reports for candidate
`f02f378ca9370c989884761d36fa2bc729c07991` both left the candidate blocked.
They found two P1 omissions in the producer path. Both are direct failures of
the original AC3: “每次 Host 准备/observer effect claim 前重进当前 admission
guard、重编译完整 input 并比较；取消、批准/资格/禁用/source/generation/窗口/CAS/Checks/input变化阻止新效果，原 unknown 占账不清零。”
Old passing tests and the Commander-attested f02 seven-module results are
historical only; they do not authorize this candidate.

- **Reservation-only expiry:** `CapacityStore.pre_effect_guard` now yields a
  non-serializable `CapacityEffectCapability` which retains the exact held
  reservation `expires_at` and Capacity's completed temporal fence. The
  Reviewer capability invokes that owned authority at the new-intent, Host
  prepare, Host control, and observer-claim writers; it does not reconstruct
  fields, reopen Capacity, or acquire a control writer.
- **Current credential material:** the final Reviewer callback reuses the
  held Project source recheck before any final scalar-clock observations. That
  is the same qualification `_facts` / configured credential reader that
  validates the sealed material, not a persisted generation or caller fact.
  It remains inside the existing operation → Run → Project → Capacity lock
  ordering and is never serialized.

The regression holds each real receiving SQLite writer. The expiry case moves
only to `reservation.expires_at + 0.001`; qualification, estimate, quota
freshness/reset, and Run windows remain valid. The credential case replaces
only bytes in the configured temporary key file under its original path and
source identity while the writer waits. It observes
`REVIEWER_CAPACITY_REVALIDATION_FAILED` or
`REVIEWER_QUALIFICATION_SOURCE_CHANGED`, respectively. New intent has no
ledger row; Host prepare has no Host row; control has no control row; and
claim has no effect claim (earlier applicable Host identity remains historical).
No provider, native Review, HTTP, Relay, Journal, Evidence, or external
credential is used.

| Command | Result and limits |
| --- | --- |
| Fresh independent `reviewer143-standards-static3-final.md` and `reviewer143-spec-static3-final.md` against f02 | **Red (static):** two independent P1 findings: the final capability omitted retained reservation expiry, and it did not rerun external credential material after the writer wait. This establishes the pre-repair failure identity, but is not behavioural/CI evidence. |
| Windows main `.venv`, literal `.cache/go-context-artifacts`, override unset, required tokenizer/isolation/offline flags, `python -m pytest tests/runs/test_reviewer_execution_intent.py::test_reservation_only_expiry_after_real_sqlite_writer_wait_blocks_the_actual_effect tests/runs/test_reviewer_execution_intent.py::test_actual_credential_material_change_after_real_writer_wait_blocks_each_effect -q --basetemp <fresh-private>` | **C passed:** `8 passed in 21.44s`. This is the real writer-wait green regression for new intent, Host, control, and claim. |
| Windows main `.venv`, same literal tokenizer/flags and `PYTHONPATH=backend;tests;tests/projects;tests/runs;tests/candidates`, seven affected modules with a fresh private base temp | **C passed:** `162 passed, 5 skipped in 229.34s`. Skips: Linux-only factory/direct-child P evidence and unavailable Windows directory-symlink privilege. |
| WSL Ubuntu `/tmp/karajan-candidate-mode-qy6_mqo2/venv/bin/python`, override unset, literal `.cache/go-context-artifacts`, `KARAJAN_REQUIRE_GO_TOKENIZER=1`, `KARAJAN_REQUIRE_OPENCODE_ISOLATION=1`, `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`, colon-separated `PYTHONPATH`, same seven modules and fresh `/tmp/karajan-reviewer143-final-linux-*` base temp | **P passed:** `167 passed in 137.80s`. The standard npm runtime remained the existing Linux ELF v1.18.29; no provider/native Review operation was requested. |
| Windows main `.venv`: `python -m ruff check .`; `python -m mypy backend/karajan --platform win32` | `All checks passed!`; `Success: no issues found in 154 source files`. |
| WSL candidate venv: `python -m mypy backend/karajan --platform linux` | `Success: no issues found in 154 source files`. |

## Commander retained-capability follow-up (2026-09-08)

This is a narrow follow-up to the original #143 **C/P: resources and
withdrawal** acceptance condition: each actual Reviewer effect boundary must
re-enter current approval/qualification/source/generation/window/Capacity
guard, while a cancelled, disabled, expired, or changed source must have no
next effect. It neither expands the slice to native Review/HTTP/Relay nor
changes the original S/not-run limits.

### Clock-floor diagnosis

The exported `CapacityEffectCapability` captures its private temporal fence
before Capacity executes `before_effect_yield`'s final scalar callback. That
class alone would not retain a later callback observation. The required real
Reviewer receiver, however, does not accept the proposed rollback: its
`ReviewerFinalEffectCapability.assert_current()` first invokes the retained
Reviewer scalar check, and `capacity_quota_fence.assert_current(as_of=...)`
rejects `as_of < floor` in `karajan.routing.quotas.QuotaTemporalFence`.

A local temporary probe against `ab09f36` used the real `CapacityStore`
guard, mutable fixture clock, retained Reviewer capability, and an actual Host
SQLite `BEGIN IMMEDIATE` writer wait. The final callback advanced Capacity from
1000 to 1002; while the Host writer waited, the clock was rolled back only to
1001.5. Reservation expiry, quota freshness/reset, qualification, estimate,
and Run windows remained valid. The receiving callback rejected with
`REVIEWER_CAPACITY_REVALIDATION_FAILED` before `RunnerHost.prepare` wrote.
The retained quota fence's observed floor was 1002. This is an existing
behavioural rejection, so this follow-up makes no speculative Capacity-store
floor change. The original direct Capacity regression tests still cover
callback-preparation and evaluation clock regression in
`tests/capacity/test_pre_effect_guard.py`.

### Callback-phase repair

`CapacityStore.pre_effect_guard` documents the successful post-`prepared_at`
callback as O(1), with no JSON/database scan. Before this follow-up,
`check_reviewer_final_effect_boundary` deferred `current.recheck_source()`
into that scalar callback; the real qualification source can reread configured
credential material. The repair makes `recheck_source()` the explicit
preparation phase and returns a scalar-only closure. The retained typed
Reviewer capability stores that preparation callable and repeats it after each
actual new-intent, Host, control, or observer-claim writer wait, before the
same scalar Reviewer/Capacity checks. Thus material revalidation remains
post-wait and Capacity's callback contract is restored without reopening a
controller writer or changing lock order.

| Command | Result |
| --- | --- |
| Isolated local `ab09f36` checkout plus the new phase regression, Windows main `.venv` and the same flags: `python -m pytest tests/runs/test_reviewer_execution_intent.py::test_reviewer_material_recheck_prepares_before_capacity_scalar_callback -q -p no:cacheprovider --basetemp C:/Users/Chooo/AppData/Local/Temp/karajan-reviewer143-material-phase-red-ab09f36` | **Red:** `1 failed in 4.36s`; the callback preparation saw source-read counts `(3, 3)` and `(7, 7)`, proving no material reread before the deferred scalar closure on `ab09f36`. |
| Current source, Windows main `.venv`, literal `.cache/go-context-artifacts`, override unset, required tokenizer/isolation/offline flags: `python -m pytest tests/capacity/test_pre_effect_guard.py tests/runs/test_reviewer_execution_intent.py::test_reviewer_material_recheck_prepares_before_capacity_scalar_callback tests/runs/test_reviewer_execution_intent.py::test_reservation_only_expiry_after_real_sqlite_writer_wait_blocks_the_actual_effect tests/runs/test_reviewer_execution_intent.py::test_actual_credential_material_change_after_real_writer_wait_blocks_each_effect -q -p no:cacheprovider --basetemp C:/Users/Chooo/AppData/Local/Temp/karajan-reviewer143-focused-final-3ba0e9` | **C passed:** `35 passed in 21.56s`. The phase regression is green, and the eight parameterized real-writer checks retain reservation-only expiry and actual credential-material rejection for new intent, Host, control, and claim. |

The previous f02 record remains deliberately limited: its independent reports
were **static red** findings. Although an old-backend/current-tests behavioral
attempt had been planned in commentary, this author did not run it; no
behavioural red result is claimed retroactively here.

## PR150 zero-effect boundary evidence follow-up (2026-09-09)

The bounded Linux factory regression now installs all boundary counters before
the first production `open_reviewer_execution_intents` construction. The
counters separately wrap `RunnerHost.start`,
`observe_go_reviewer_tools`, the actual `IsolatedOpenCode.start` runtime entry,
`httpx.Client.send`, both the Reviewer suite's imported
`parse_review_output` consumer alias and the defining
`karajan.candidates.review_output.parse_review_output`,
`GoCallJournal.create_grant`, `GoCallJournal.begin_call`, Candidate `gate`,
and Candidate `_save_evidence` entries. Transport, runtime, parser, Host start,
and Journal entry wrappers fail immediately if reached. Candidate gate and
Evidence-save wrappers delegate and count calls; the final assertion requires
zero Evidence writes while permitting the existing read-only Candidate gate.

The test opens the actual existing-only factory, reopens history, exercises the
descriptor rejection, and runs the seeded real Run/Candidate/Host lifecycle.
It snapshots the execution ledger, Host ledger, configured Journal, and copied
state stores around the first factory construction and checks unchanged bytes.
The observed result is `gate > 0` and zero for observer, Host start, native
runtime start, HTTP send, both parser entry points, Journal grant, Journal
call, and Evidence-save counters; the configured Journal bytes also remain
unchanged. This remains C/P bounded evidence: the
factory-composed `prepare` still rejects with
`REVIEWER_RESERVED_ROUTE_NOT_CURRENT` because the persistent factory
qualification reader and seeded fixture source differ. That failure is
retained as `failed`, and no successful factory qualification or native/HTTP
service lifecycle is claimed; the existing seeded facade evidence remains
fixture-owned and `not_run` for those capabilities.

| Command | Result |
| --- | --- |
| WSL2 Ubuntu, `env -u KARAJAN_OPENCODE_LINUX_BINARY KARAJAN_REQUIRE_OPENCODE_ISOLATION=1 KARAJAN_REQUIRE_GO_TOKENIZER=1 KARAJAN_GO_TOKENIZER_DIRECTORY=/tmp/karajan-pr155-tree/.cache/go-context-artifacts HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONPATH=backend:tests:tests/projects:tests/runs:tests/candidates /tmp/karajan-candidate-mode-qy6_mqo2/venv/bin/python -m pytest tests/runs/test_reviewer_execution_bootstrap.py::test_existing_factory_reopens_identity_and_rechecks_own_descriptor -q -p no:cacheprovider --basetemp=/tmp/karajan-dg01-pr150-boundary-green-20260909` | **P passed:** `1 passed in 10.30s`; direct `IsolatedOpenCode.start` and parser-definition interception were installed before factory construction, all forbidden counters remained zero, and no provider/model call was enabled. |

## Historical verification retained from candidate 35c5b00

The following results were already recorded in `35c5b00`; they are historical
evidence, not runs of the September 9 scalar/counter follow-ups. Those later
changes use the targeted commands above and current PR quick CI; full testing
now runs only in the daily nightly workflow.

That earlier verification completed with the CI-relative
tokenizer value, runtime override unset, and required isolation/tokenizer and
offline flags: Windows main `.venv` ran the seven affected modules with
**163 passed, 5 skipped in 244.76s**; WSL2's shared candidate venv ran the
same modules with **168 passed in 137.70s**. The Windows skips are the
Linux-only factory/direct-child cases and unavailable directory-symlink
privilege. Ruff passed on both OSes; mypy passed on `backend/karajan` with
`--platform win32` and `--platform linux` (154 source files each).
