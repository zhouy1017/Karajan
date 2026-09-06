# Independent execution-policy contract review

This directory preserves the reviewer's original `before.xml`, `before-sources.json`, and `test_before.py.txt` (unchanged bytes), plus a runnable version of those nine public-interface cases. `legacy-execution-policy.py.txt` is the exact old source from base `33d98d391393d848a8a062beb253e7c1f064d2a6`, loaded only to compare the v1 document shape/digest. The `.txt` suffix prevents recursive pytest collection of historical code.

The public tests use real Registry/Run Git/SQLite fixtures and the shared official offline tokenizer fixture. They do not create a passed Profile qualification, execute a validation command, read credentials or invoke a provider.

Run from the repository root after provisioning the pinned tokenizer:

```text
python -m pytest -o "pythonpath=backend tests/projects tests/adapters/opencode" examples/go-task-context/policy-review -q
```

CI should keep `KARAJAN_REQUIRE_GO_TOKENIZER=1` and `KARAJAN_GO_TOKENIZER_DIRECTORY` set to the provisioned directory; missing required artifacts must fail. Tokenizer files are not copied into this evidence directory.

Initial result: 4 failures / 5 passes. Findings: POLICY-V2-001 accepted ratio 10001 which its bound accounting source rejects; POLICY-V2-002 non-object registration regressed to `AttributeError`. The author's two fixes were independently rechecked: **9 passed / 0 skipped / 4.48s**, both findings closed. See [report.md](report.md), [after.xml](after.xml), and [after-sources.json](after-sources.json). Original red inputs and evidence remain unchanged.
