# Fixed projected observer author evidence

The new `go_projected_probe.py` and private `_go_projected_evidence.py` use actual
Linux OpenCode 1.18.29, namespace projection, GoRelay, GoCallJournal v2, pinned
offline Go tokenizer accounting, stopped projection capture, and CandidateStore.
Only the upstream HTTP responses are fixtures. No provider request or real key
was used.

The first public test failed at collection because the module did not yet exist.
The initial vertical test then passed: three tests, 30.64 seconds. The expanded
Linux group passed all eleven tests in 58.60 seconds. Two additional cross-platform
legacy/Task-grant rejection cases and exact-grant replay assertions were then
added without changing production source. The four affected Linux cases passed
in 32.07 seconds; Windows final collection passed three and skipped ten
Linux-only cases. The final test module contains thirteen cases.

Evidence XML files are `.cache/projected-observer-first.xml`,
`.cache/projected-observer-final.xml`, `.cache/projected-observer-replay.xml`, and
`.cache/projected-observer-windows.xml`. All public tests use synthetic data.
Actual native reports and stores stay in their explicitly separate `/tmp/karajan-
projected-observer-*` test roots; SQLite and temporary repositories are not
publishable evidence.

The edit fixture causes four actual HTTP requests: read reference, read target,
edit target, final response. The denied-read fixture causes two. Actual measured
payloads prove initial-input and complete previous-message-prefix retention;
their text is not serialized into reports. The reference and initial source read
results are identified in final measured tool history. Missing provider usage
fails the observation and revokes the exact grant before confirmed local cleanup.

Capture reconstructs four complete baseline files, including an unprojected
binary and executable file. It checks file bytes, manifests, Git trees and mode
preservation by materialization. Candidate checks and independent review remain
missing; the observer never promotes fixture observations into production
qualification. Remote provider stop and budget bounds remain unknown.

Read-only `authenticate_grant` is only a preflight identity check. Revocation may
race native startup, but each actual HTTP send still requires a fresh durable
Journal `begin_call`. It is not an atomic check-to-start grant lock.

Reproduction (from this worktree, provisioned Linux environment):

```sh
PYTHONPATH=backend:tests/isolation:tests/adapters/opencode \
KARAJAN_REQUIRE_OPENCODE_ISOLATION=1 \
KARAJAN_OPENCODE_LINUX_BINARY=/path/to/pinned/opencode \
KARAJAN_GO_TOKENIZER_DIRECTORY=/path/to/pinned/tokenizer \
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
python -m pytest tests/isolation/test_go_projected_probe.py -q \
  --basetemp=/tmp/karajan-projected-independent-reproduction
```

Ruff check/format and mypy for Linux and Windows passed for both new source files.
`freeze.json` binds the final production source and current public test bytes.
