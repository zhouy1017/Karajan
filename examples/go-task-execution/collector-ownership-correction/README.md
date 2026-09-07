# Collector corrections and deferred historical handle

Follow-up to `.cache/collector-author` for Issue #90. The original freeze, tests and red/green XML remain historical evidence and were not replaced.

Independent review by `capacity_facts` established two P2 findings on the original source. Author reruns preserved both failures in `red.xml`, and four expanded public cases failed in `author-red.xml` before repair.

- **COLLECTOR-001:** `CandidateStore(existing_only=True)` now validates the existing SQLite ledger independently of artifact/Git directory availability. Historical exact lookup remains read-only and can reopen after those directories disappear. Actual materialization still requires verified artifacts; freezing still requires all resources it uses. Missing directories are not recreated. Materialization remains possible when only Git metadata is missing and the required CAS bytes are intact.
- **COLLECTOR-002:** duplicate `allowed_paths` are rejected by `freeze_projection` before any Candidate/Git/artifact changes. The request is not silently normalized. The ordinary legacy freeze API is unchanged.

The parent additionally requested `defer_validation=True`, only with `existing_only=True`, so historical cleanup can hold an unavailable optional Candidate ledger without blocking other independent resource cleanup. That constructor only retains controlled paths and does not initialize or validate the missing ledger. Every actual connection still uses existing-only SQLite modes, rejects links and validates its schema. The default constructor remains strict. `deferred-red.xml`/`deferred-green.xml` record this separate small interface addition; it is not attributed to the Collector reviewer's two findings.

Final author validation: **52 passed, 0 skipped on Windows; 52 passed, 0 skipped on Linux/WSL**. Ruff/format passed for the 5 owned files; mypy passed for the 3 product files. The independent reviewer separately reran the original 11 cases on Windows and WSL and closed both findings; that independent evidence is in `.cache/collector-independent/final.json` and its XML, not counted again as author tests.

```text
python -m pytest tests/candidates/test_capture_recovery.py tests/candidates/test_projected_capture.py tests/runs/test_go_task_collector.py -q -o "pythonpath=backend tests/candidates tests/runs tests/projects tests/routing tests/capacity tests/web" --junitxml=.cache/collector-ownership-correction/final-windows.xml
```

Linux uses the same files, `-p no:cacheprovider --basetemp /tmp/karajan-collector-correction-final`, and final-linux.xml. No credentials, provider calls, native runner or validation/Reviewer effect was executed in this group. Actual stores, Git/CAS and Journal are tested; Collector Host/native/source authority remains explicitly synthetic, as documented in the tests. The consumer owns the separate actual native/HTTP fixture integration.

`green.xml` contains 29 passing intermediate author plus original-review tests before the deferred-handle addition. `freeze.json` binds the final source and final results; intermediate XML is not relabeled as final-source verification.
