# Issue 90 independent Task consumer review

Reviewer: `capacity_facts`. Scope is `orchestration/go_task_execution.py` and `_go_task_runner.py`, against `docs/planning/go-task-execution-issue.md`. The reviewer did not author these two files. The reviewer **did** author the separate intent-persistence port; this review does not independently endorse that port. Fixtures construct actual approved Project/Run/Capacity/Host/Journal/CAS records. Callback tests explicitly replace only trusted Host runner identity and native start/transport; credentials are synthetic test material. There is no provider invocation or access to a real key.

## CONSUMER-001 — P2, closed after independent replay

Cleanup checks only Host Attempt id and prepared/start key. If an existing Host record uses those same two identifiers but a different frozen manifest (fence, budget ref or Profile revision), `facade.cancel` cancels it and changes its control/business state. Exact ownership requires the original frozen manifest and ProcessSpec, with the check and cancellation in the same Host transaction. An ordinary current-fence guard cannot substitute for that check, because historical/finished/cancelled instances must remain cleanable.

Independent public negative cases use `Host.prepare` to establish each mismatched immutable Host record; no database corruption is injected. Three variants fail with a confirmed Host DB write. The exact original-binding positive control cancels successfully.

`before.xml`: actual Windows **3 failed / 7 passed, 11.69 seconds**. `test-before.py.txt`, `consumer-before.py.txt` and `entry-before.py.txt` preserve the first run's exact source/input. Only import sorting/formatting followed in the executable test. Root assigned the product fix to its author; this reviewer does not edit it.

Other passing independent controls cover: cancellation before native start; cancellation or source change before send; a second invocation of the native-start callback rejected; loss of the committed effect-claim reply; loss of the committed grant-creation reply. Each preserves the original claim/grant, no provider slot is sent, and replay never resolves credentials or re-creates the grant.

```text
python -m pytest .cache/task-consumer-independent/test_consumer_review.py -o "pythonpath=backend tests/runs tests/projects" -q --junitxml=.cache/task-consumer-independent/before.xml
```

Before-source SHA256:

```text
go_task_execution.py fb2e726ba7639f27c04a054f7115c4a49ad563424e7af44e66b457ccc29677b1
_go_task_runner.py 0a43cf7b1924b40a66ea35e354e15a6cce697b954c243c8808a7bf7500343822
```

Entry read confirms fixed three-identity arguments, trusted package loading under Python `-I`, content-free failure output, historical early reconciliation for cancelled/already-claimed operations and no caller argv/provider/report ingress. Its actual native child behavior is covered separately by the author's running Linux integration, not claimed by this lightweight review.

## Final verification

The author added optional `Host.cancel(expected_binding=...)` and the consumer supplies the original frozen manifest, ProcessSpec and start key. Host compares all three in the same `BEGIN IMMEDIATE` transaction before writing cancellation or control state. It does not require current dispatch permission to clean up an exact historical execution. The existing no-binding Host API remains unchanged.

The same ten independent cases passed on Windows (**10 passed, 11.72 seconds**) and WSL Linux (**10 passed, 8.57 seconds**). See `windows-final.xml`, `wsl-final.xml` and `final.json`. The three mismatched manifests leave the Host database bytes unchanged; the original-binding control still cancels. All callback and lost-response controls remain green. Ruff passed for the executable independent test. No product files or original red evidence were edited by this reviewer.

```text
python -m pytest .cache/task-consumer-independent/test_consumer_review.py -o "pythonpath=backend tests/runs tests/projects" -q --junitxml=.cache/task-consumer-independent/windows-final.xml
wsl.exe --cd /mnt/c/Users/Chooo/Playground/Karajan/.cache/go-task-execution-v2 -e /tmp/karajan-candidate-mode-qy6_mqo2/venv/bin/python -m pytest .cache/task-consumer-independent/test_consumer_review.py -o "pythonpath=backend tests/runs tests/projects" -p no:cacheprovider -q --junitxml=.cache/task-consumer-independent/wsl-final.xml
```

This is a bounded review of the facade/consumer and entry, with a targeted review of the Host cancellation repair. It does not independently establish native containment, provider behavior, complete Profile qualification, or the reviewer's own intent persistence implementation.
