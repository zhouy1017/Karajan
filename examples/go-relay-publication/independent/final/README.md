# Final independent receipt-publication review

Reviewed the final uncommitted Spark candidate above HEAD `c63c7e36b17c2f8d8b041984e5bd3422bd72ffd6`. Products were read-only. The initial review, original independent red and transient follow-up snapshot remain in the parent evidence directory and are not overwritten.

**Standards: 0 findings. Spec: 0 open findings within this two-file correction.** SPARK-RECEIPT-001 is resolved by the actual completion barrier and explicit held-write watchdog. This conclusion follows source inspection, a real-socket boundary experiment and a historical negative control; it does not infer whole-project acceptance or GitHub CI status.

The final author regression obtains the full HTTP 200 response while the actual OS socket write is still held, verifies `protocol_passed=true`, null-name count 1, `relay_completed=false` and Journal `send_unknown`, and then explicitly releases the writer. Successful public `relay.close()` establishes handler completion before final receipt and Journal assertions. A separate supported HTTP transport cleanup barrier verifies that complete client delivery can still precede Journal completion. No Journal method is mocked.

## Results — Windows, 2026-09-06

- `final-windows.xml`: **29 passed, 0 failed, 0 skipped, 3.53s**: 3 independent public-boundary cases, 13 final author nullable/publication cases, 13 context-accounting cases. The fixed tokenizer directory was provided and `KARAJAN_REQUIRE_GO_TOKENIZER=1`, so missing artifacts could not silently skip context checks.
- `old-source-control.xml`: **1 expected negative-control failure, 0 unrelated failures, 0.41s**. The unchanged final author held-write test was bound only at its public `GoRelay` construction port to exact historical `cd457975075ea9657baccdf6544ca0c0d86aa2c0` source. It received the entire body and then failed specifically at the test's `protocol_passed is True` assertion (`test_go_relay_nullable_names.py:134`), observing false. This proves the corrected test detects the original defect; it is not a failure on the final product.
- Independent actual TCP reset: a real OS `ConnectionResetError` leaves `relay_completed=false`, provisional `protocol_passed=false`, `RELAY_TRANSPORT_ERROR`, and one durable call. Complete upstream facts are retained. Journal `response_received` describes the actually completed upstream response, with failed outcome; it does not claim successful downstream delivery, refund, or remote stop.

`test_final_publication.py` is a new copy of the original three-case independent test. Its completion-lag case now first asserts the observed pending state, then releases transport cleanup, requires public close to finish and checks the final Journal. The original failing expectation remains byte-preserved in the parent test and XML.

All executions use actual local HTTP sockets and SQLite with a synthetic HTTP upstream and synthetic credential. No provider, real key, native OpenCode or Git action occurred. WSL and full-suite/static revalidation are separately owned by root and are not counted here. Compatibility with the newer `send_guard` branch was inspected only; this evidence does not claim an executed merged version.

## Exact commands

From the Spark worktree:

```powershell
$env:KARAJAN_GO_TOKENIZER_DIRECTORY='C:/Users/Chooo/Playground/Karajan/.cache/go-task-execution/.cache/go-context-artifacts'
$env:KARAJAN_REQUIRE_GO_TOKENIZER='1'
& C:/Users/Chooo/Playground/Karajan/.venv/Scripts/python.exe -m pytest .cache/spark-independent/final/test_final_publication.py tests/adapters/opencode/test_go_relay_nullable_names.py tests/adapters/opencode/test_go_relay_context.py -o 'pythonpath=backend tests/adapters/opencode' -o junit_logging=all --junitxml=.cache/spark-independent/final/final-windows.xml -q
& C:/Users/Chooo/Playground/Karajan/.venv/Scripts/python.exe -m pytest .cache/spark-independent/final/test_old_source_control.py -o 'pythonpath=backend tests/adapters/opencode' -o junit_logging=all --junitxml=.cache/spark-independent/final/old-source-control.xml -q
```

The second command is intentionally red against historical code. Do not include it as an ordinary green CI gate without explicitly representing its negative-control role. `review.json` records source and evidence SHA256; both product/test hashes matched the notified freeze before and after execution.
