# Approved Go Task execution — author evidence

Issue: https://github.com/zhouy1017/Karajan/issues/90
Base: 858bd5a (execution-v2 worktree).

The public facade accepts only original Run/operation/principal IDs. It resumes the original Capacity activation receipt, initializes the original Host control once, launches a fixed direct child, and consumes exactly one persistent effect claim and Task grant. The child derives its prompt/files from frozen approved CAS. Actual native start and each Journal/HTTP send hold operation → Run → Project → Capacity → Host guards, rechecking complete source and current qualification/material facts. Credential resolve_exact follows the first successful claim and runs outside Project locks. Cancellation is persisted before cleanup; reconcile can continue the same grant/Host cleanup and query the original activation receipt without a new activation, grant or native run.

Final author results:

- **11 public facade tests passed on Windows** (10.66 s), `.cache/task-activation-recovery-green.xml`.
- **11 public facade tests passed on Linux** (8.43 s), `.cache/task-facade-final-linux.xml`.
- **3 actual Linux Host/native integration cases passed** (162.71 s), `.cache/task-full-native-d.xml`.
- Ruff passed for the two product files and four test/harness files; mypy passed for the two product files with Windows and Linux platform settings. Linux platform typing used Windows mypy `--platform linux`; the WSL runtime itself lacks mypy.

The actual Linux cases use a fixed controller-owned **test entry**, explicit SyntheticSuite external qualification/planning/quota facts, real Project/Run/Capacity/Host/Journal/Candidate stores, the fixed Linux OpenCode ELF and pinned local tokenizer artifacts. The test entry and configuration are bound in the fixture source. Its HTTP transport accepts the fixed provider host and forwards **only to loopback**; it never forwards the synthetic credential in HTTP headers. It is not the production bootstrap and is not provider/S evidence.

1. **Normal**: actual Host direct child → native read/edit → stopped immutable projection → complete baseline Candidate. Three HTTP requests were durably counted before sending; all finished as response_received, and the grant was revoked. Original repository bytes stayed unchanged; unprojected binary and executable mode were retained. Candidate check/review evidence remains missing, so it is not deliverable. The child exited with code 0 and no owned Host group members remained. The operation keeps its pending business state with `phase=candidate_recorded`.
2. **Lost grant reply**: a test-only Journal crash boundary commits the original grant and loses its create reply. The original claim remains consumed, the exact grant is revoked, and there are zero HTTP calls and no native directory. Repeated facade advance only reconciles history.
3. **Cancel after first send**: the loopback server sends response headers, releases the bounded send gate, withholds the body, and calls the real facade cancel. The original grant is revoked and Host child stopped. The first send remains counted as send_unknown; no second request or replacement grant occurs. Native/provider stop without a capture remains unknown rather than inferred from Host status.

Every case recorded its complete controller source manifest before effects. `native/{none,grant_reply_lost,cancel_after_send}/` contains only public operation/source JSON; `native/summary.json` records the content-free results. All recorded controller source hashes matched current files after the run. Private SQLite, credential files, raw HTTP/model text and mutable native directories were not copied.

Public reproduction (WSL/Linux; required fixed artifacts must already exist):

```text
PYTHONPATH=backend:tests/runs:tests/projects:tests/web:tests/isolation:tests/adapters/opencode
KARAJAN_REQUIRE_OPENCODE_ISOLATION=1
KARAJAN_OPENCODE_LINUX_BINARY=<fixed ELF>
KARAJAN_GO_TOKENIZER_DIRECTORY=<pinned local tokenizer directory>
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
python -m pytest tests/runs/test_go_task_execution.py tests/runs/test_go_task_execution_native.py -p no:cacheprovider --basetemp=/tmp/k90r -q
```

Use a short private basetemp (the actual run used `/tmp/kt90a`); the relay's Unix socket path must fit the OS limit. The original fixture state is retained in `/tmp/kt90a`, outside normal pytest retention.

Preserved red/diagnosis history:

- Initial public module/entry/history/source tests failed before their corresponding implementation; original XML is retained in `.cache/task-*.xml`.
- `task-cancel-recovery-red.xml`: reconcile originally observed cancellation without revoking its grant. The private cleanup path fixed this; the subsequent test-only intermediate Host-state expectation was corrected to the actual public `exited`/cancelled result.
- `task-activation-recovery-red.xml`: reconcile did not read an already committed original Capacity activate receipt. Green now uses only command_receipt and leaves Capacity DB bytes unchanged.
- `task-full-native-a.xml`: test used nonexistent top-level Project base_ref; corrected the fixture to declared main.
- `task-full-native-b.xml`: synthetic evidence_ref contained spaces; schema rejected it before execution.
- `task-full-native-c.xml`: initial source preflight rejected during parent source edits, with zero Host/HTTP. The parent confirmed existing-only source fixes overlapped this attempt. Later read-back found current source and qualified mechanism equal. This is retained as a conservative source-change refusal, not successful native evidence.

This is author C/P evidence, not independent review or official provider qualification. Check/Review execution, PR delivery and account-window settlement remain outside this slice.

After this d-round capture, parent changed storage.py connect compatibility and is integrating a separately reviewed Relay timing fix. Independent CONSUMER-001 also found incomplete Host cleanup matching; the subsequent exact transactional cancellation repair is recorded under consumer-001/. The complete d-round manifests preserve the original successful input; final combined-source native verification remains pending parent coordination.
