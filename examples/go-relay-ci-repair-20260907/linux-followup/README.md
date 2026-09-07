# Independent Linux relay follow-up

Frozen code commit: `03984aa08aecf79c33c44a9a054a707ae04748e0`.
Relay SHA-256: `deb0239590d67ddfb077f025773bb9c7c2947a7f3379c693f7ee469d0aab2453`.

The reviewer ran the same **196 formal cases** recorded in the author's
`../luna-relay-author/formal.xml`, then added the **3 cases** from
`tests/adapters/opencode/test_go_unix_relay.py`, which was absent from that
formal set. `formal.log` contains the aggregate output; its companion XML
provides the exact source files and case identities. The final Linux XML was
compared against those identities: all original 196 appear exactly, and the
only extra cases are the three Unix transport cases.

**Result: 199 passed, zero skipped, zero failures, 17.04 seconds.**
This was an independent reviewer execution in WSL Ubuntu using
`/tmp/karajan-candidate-mode-qy6_mqo2/venv/bin/python`, not a relabelled author
result. The before/after hashes match for all 125 recorded backend/test paths.
The commit remained unchanged.

`run.sh` contains the complete command and environment. It runs these seven
formal files with `-o "pythonpath=backend tests/adapters/opencode"`:

- `test_go_relay.py`: 74 cases.
- `test_go_relay_context.py`: 14 cases.
- `test_go_relay_nullable_names.py`: 13 cases.
- `test_go_relay_journal.py`: 3 cases.
- `test_go_context.py`: 39 cases.
- `test_go_journal.py`: 53 cases.
- `test_go_unix_relay.py`: 3 additional cases.

The private temporary root is `/tmp/relay-linux-followup`. Disabling pytest's
cache plugin avoids writes to the shared Windows-owned `.pytest_cache`; it
does not deselect tests. The pinned tokenizer files came from the existing
`go-task-execution/.cache/go-context-artifacts` directory with the tokenizer
required and both Hugging Face and Transformers offline modes enabled. Their
observed hashes are retained in `result.json`; the accounting tests also enforce
the fixed artifact contract.

The extra Linux tests use real pathname Unix sockets and HTTPX UDS transport.
They verify the fixed endpoint/capability boundary, real durable grant usage,
safe preservation of a replacement socket-path file, and refusal to overwrite
an existing path. The formal set also covers the rejection deadline and exact
body extent plus the lost committed metered-begin recovery on Linux.

All credentials and upstream responses are synthetic. No provider, native
OpenCode session, new Profile qualification, or actual project execution was
run. This is bounded Linux local transport/accounting/Journal evidence, not
model-service acceptance. Existing Windows independent results and the old
author/independent directories were neither rerun nor changed.

Evidence: `linux.log`, `linux.xml`, `before-sources.json`, `after-sources.json`,
`result.json`, `run.sh`, and `freeze.json`. No temporary databases or runtime
state are included in the evidence selection. Product, formal tests,
dependencies, Git, and remote state were not modified.
