# PR92 Linux CI diagnosis

The frozen PR92 head has a reproducible pathname-dependent failure before native startup: its default pytest fixture constructs a 126-byte Unix socket path. The Linux public relay accepts 107 bytes but rejects 108 bytes and above. The earlier `/tmp/kt90b` run constructed a 106-byte path. No source, formal test, CI configuration, credential, or Git state was changed by this investigation.

The audited head is `825248a29c4dcdb4f432157fdf0979f26ed9c9b9`. The original CI log records GitHub's tested merge ref `50b479897746aeeafd4ada0a03e03ad48925b9c6`; these are different revisions. `source-manifest.json` binds the relevant frozen head files and original CI log. This diagnosis reproduces the head's behavior; the CI did not retain the inner exception itself.

## Evidence

| Check | Actual result |
| --- | --- |
| Public `GoRelay.start`, 106 / 107 byte pathname | Started and closed; zero HTTP clients |
| Public `GoRelay.start`, 108 / 126 byte pathname | `OSError: AF_UNIX path too long`; zero HTTP clients |
| Public `execute_go_task`, real accounting/source checks, 126 byte pathname | Failed with `OSError`; capture absent; native not started; grant revoked; zero sends |
| Unmodified original normal integration test under CI-length temporary root | `TASK_STOPPED_CAPTURE_REQUIRED`, phase `effect_claimed` |
| Unmodified original cancel-after-send test under the same root | Request count `0`, expected `1`; cancellation handler never reached |

The independent five-case public group passed in 4.80 seconds (`uds.junit.xml`). The original two cases reproduced both CI assertions in 72.13 seconds (`original-long-path.junit.xml`). They used actual Host children with explicitly synthetic qualification and credentials, and the existing loopback-only HTTP factory. The failing path never launched OpenCode or created an outbound HTTP client. No provider was called.

`path-lengths.json` records exact path shapes. Both original-test reproduction operations have a real owned directory but no native directory or socket; their detached public receipt summaries are in `original-long-path-summary.json`. The original operation receipts remain under `/tmp/pytest-of-review/pytest-1`; no database or private fixture material is copied here.

## Causal chain

1. `task_execution_fixture.open_fixture` chooses `tmp_path / 'w'` for `work_root`.
2. `consume_go_task` appends `operation-<UUID36>`; `execute_go_task` appends `inference.sock`.
3. `GoRelay.start` resolves the parent and calls the real `socket.bind` before creating the native object.
4. Bind raises on the 126-byte pathname. The producer returns a failed, content-free report, revokes the grant, and reports `local_stop=not_started`; it has no `StoppedProjection`.
5. The Collector correctly refuses the missing capture with `TASK_STOPPED_CAPTURE_REQUIRED`. The test entry saves only that outer stable code.
6. The cancel test's callback is inside the loopback HTTP handler. Since no request is made, its expected one send cannot occur. The lost-grant-reply scenario stops before invoking the producer, so it does not encounter the Unix socket path.

The initial hypotheses were path-length rejection, current source/time rejection, and runtime/fixture timing. The real kernel boundary, public producer report, actual directory presence, and unchanged-test reproduction isolate the pathname defect. No retry against an official provider is needed. The original CI omitted its inner error report, so its exact exception was not retrospectively observed; the same path construction deterministically reproduces both reported assertions on the frozen head.

## Diagnostic retention gap

The producer already has a content-free `error_type`, reason codes, source digests, request receipts and cleanup observations. These are held in memory. If collection rejects, the test entry persists only the final exception code, and `public-operation.json` has no capture or inner producer report. `namespace.log` cannot help when bind fails before the namespace exists. Host supervisor logs do not restore the discarded report.

The current CI invokes `pytest tests` without a bounded basetemp, without JUnit output, and without an `always()` artifact-upload step. Later test steps are skipped on this failure. Temporary public receipts disappear with the runner. A future fixture diagnostic should preserve a small allowlisted report before collection and on exceptions: failure stage/code/type, resolved socket byte length, whether native started, request count, grant state, source digests, and cleanup status. Never upload the whole tmp tree, journal, credential-private directory, bootstrap credentials, or raw model requests.

## Minimal repair for Spark

Keep the actual integration assertions and owned cleanup. Allocate a bounded short Linux fixture work root (preferable to relying on pytest's generated test-name length), or use an explicit short Linux basetemp plus an assertion over the final resolved socket byte length. The current UUID/path suffix uses 62 bytes, leaving at most 45 bytes for the resolved work root. A lexical symlink shortcut is insufficient because the relay resolves its parent.

Retain a fast kernel boundary test and rerun the unchanged normal/cancel/lost-grant cases through the CI command. Preserve allowlisted failure artifacts under `if: always()`. The production bootstrap can also accept a long task work root; a separately scoped preflight or transport-location change should report that configuration failure before consuming the one-shot effect claim. Do not weaken the Collector, grant accounting, or native-stop requirements to make these tests pass.

## Reproduction and harness notes

`test_uds_diagnosis.py` is the independent five-case test. Run it using the frozen worktree's explicit `-c pyproject.toml -o pythonpath=backend`, its existing test-helper paths, fixed Linux ELF, and pinned offline tokenizer. `run-original-long-path.sh` records the exact original-test reproduction command; use a fresh, same-length basetemp when rerunning so prior evidence is retained. The parent `/tmp/pytest-of-review` must exist. No rerun is necessary to support this report.

Three preliminary harness mistakes are retained separately: wrong pytest root selected an older root package (`harness-import-before.xml`), missing basetemp parent caused setup errors (`harness-basetemp-before.xml`), and a mistyped tokenizer path skipped two cases (`harness-tokenizer-before.xml`). They are not product results. The final command explicitly requires tokenizer artifacts and produced the two expected failures. The five-case independent test and successful original reproduction used the correct immutable source.
