# Go per-call business guard: author evidence

Scope: `backend/karajan/adapters/opencode/go_relay.py` and `tests/adapters/opencode/test_go_send_guard.py`, base `f2d639559e738dfbb951163c6e8d83b460d758fb`. No key or provider access. HTTP to the relay, durable GoJournal, SQLite lock serialization and pinned reference tokenizer are real; upstream responses and the small SQLite authority callback are explicit test fixtures, not Run qualification evidence.

The optional controller `send_guard` preserves legacy/qualification behavior. The new Task producer must require it. Guard acquisition is outside relay condition; the guard covers Journal begin through actual HTTP stream entry, and ends before reading the response body. Exceptions from guard construction/entry/exit become `TASK_SEND_GUARD_REJECTED`, with no private exception text. Guards cannot suppress an underlying send failure. An already committed call remains counted/unknown and its owned grant is revoked on failure.

The 9 public cases cover rejection without a Journal slot/send, current authority at every call, SQLite withdrawal blocked at send but allowed during body read, condition lock ordering, close while a handler waits for authority, construction/exit errors, header/body failures and lost begin replies after actual commit. Windows final: 9 passed; Linux final: 9 passed. The related Windows regression group passed 196 cases; it preceded only the class documentation/LF finalization and test formatting. Final 9-case runs used the frozen product; the later test edit only clarified its module docstring.

History is retained without rewriting:

- `red-entry.xml`: initial environment lacked the configured tokenizer path, so 1 skipped; not red/green evidence.
- `red-entry-provisioned.xml`: actual initial red, unexpected `send_guard` keyword; 1 failed.
- `green-entry.xml`: implemented seam, 1 passed.
- `public-boundaries.xml`: 7 passed, 2 author test timing failures. Those tests read completion before the handler's finally finished; existing durable state was correctly `send_unknown` with no final outcome yet. No product fix was made for these failures.
- `public-boundaries-corrected.xml`: tests wait through public `relay.close()` before asserting final history, 9 passed.
- `regression.xml`: 196 passed, no skips, 18.38 seconds.
- `final-windows.xml`: final product, 9 passed, 4.17 seconds.
- `final-linux.xml`: final product, 9 passed, 6.14 seconds.

Ruff check, Ruff format and mypy of the owned product passed. This is author validation, not an independent review or evidence that PR88 CI failures were fixed. No existing nullable-name tests were changed.

Rerun from repository root after provisioning the fixed tokenizer and setting `KARAJAN_GO_TOKENIZER_DIRECTORY` plus `KARAJAN_REQUIRE_GO_TOKENIZER=1`:

```text
python -m pytest tests/adapters/opencode/test_go_send_guard.py -q -o "pythonpath=backend tests/adapters/opencode"
python -m ruff check backend/karajan/adapters/opencode/go_relay.py tests/adapters/opencode/test_go_send_guard.py
python -m ruff format --check backend/karajan/adapters/opencode/go_relay.py tests/adapters/opencode/test_go_send_guard.py
python -m mypy backend/karajan/adapters/opencode/go_relay.py --follow-imports=silent
```

Parent-side cancellation must commit and release its operation transaction before waiting for relay close. The callback is a code-only seam, not an HTTP request parameter. Local diagnostic receipt bounds remain separate from persistent provider send slots. No cross-database atomicity, remote stop or automatic retry is claimed.
