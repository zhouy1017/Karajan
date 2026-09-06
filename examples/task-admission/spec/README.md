# Independent Spec evidence: approved task admission

Reviewed against base `5818835`. The published suite actually ran from this directory: **22 passed** (16 public admission checks, 5 expiry checks, 1 in-process ASGI boundary check). One independently discovered P2 expiry defect is fixed; no open Spec findings remain.

The tests use real persisted ProjectRegistry, RunPlanner, AttemptEstimateStore, CapacityStore and ApprovedTaskAdmission interfaces. Positive cases use an explicitly named `IndependentQualificationDouble`, which retains the real project lock and supplies synthetic qualification facts. It is not installed in production app wiring. The production-wiring check uses the real unqualified source and must create no reservation. The published source helper originated in the preceding independent routing Spec fixture; this suite does not import author tests.

Three tests terminate an actual child process with `os._exit(91)` immediately after a Capacity transaction commits, then reopen stores and recover by read-only command receipt. Thread barriers exercise actual SQLite locking for admission/cancellation, estimate revocation and competing activation. The shared-capacity test creates a real Capacity admission carrying a distinct Run identity; it does not create a second approved Run in RunPlanner. No Host, provider request or model effect occurs. External activation in negative tests establishes an unsafe-to-release state, not a production execution path.

`test_http_boundary.py` uses TestClient against the real FastAPI app in process. It checks production wiring, strict command bodies, session/CSRF boundaries and all four admission endpoints; it is not a live HTTP server or browser test. The synthetic bootstrap token and databases are created only in pytest temporary directories.

Run from the repository root with its Python environment:

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'backend'
python -m pytest examples/task-admission/spec -q -o 'pythonpath=backend tests/runs tests/web' --basetemp=.cache/v2-admission-spec/replay
python -m ruff check examples/task-admission/spec
python -m ruff format --check examples/task-admission/spec
```

Helpers use unique `admission_spec_*` module names so this suite can be collected with the Standards suite. Source lookup finds the nearest ancestor with `pyproject.toml`; no original workstation path or author-test imports are required. Temporary Git repositories are entirely synthetic; their init/add/commit steps do not alter the product repository's Git metadata.

History is not additional passing coverage:

- `history/expiry.red.junit.xml`: the real product defect, 13 passing checks and one failure. Once the unsent reservation expired, CapacityFacts excluded it but public admission state remained reserved. The implementation now projects current capacity state and permits a new operation; the suite also verifies expiry after response loss and that active/unknown holds remain protected.
- `history/fixture-capability-correction.junit.xml`: test preparation error, 12 failed and 1 passed. The first synthetic qualification double omitted capability facts. Explicit synthetic capability observations corrected it. This is not a product finding.
- `history/fixture-window-correction.junit.xml`: test preparation error, 1 failed and 12 passed. The attempted new fixed window preceded the current reset and was correctly not applied. The corrected test crosses the real reset, verifies `applied=true`, and keeps its explicit estimate valid. This is not a product finding.

`source-final.json` pins reviewed product and executable test bytes. `review.json` records scope, finding closure and verification. `final.junit.xml` is the published-path run. Runtime state, SQLite files, repositories, session stores, bootstrap state and caches are excluded from the evidence manifest. Dependency deprecation warnings from Starlette/httpx/anyio are recorded by the runner; they did not fail tests.
