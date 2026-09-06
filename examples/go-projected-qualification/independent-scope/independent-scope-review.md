# Independent review of projected Go qualification consumption

Reviewed `orchestration/routing.py`, `orchestration/go_scope.py`, and
`projects/qualification.py` against the current projected qualification design.
The observer and credential implementation authored by this reviewer are excluded
from independent claims. Current source and test hashes are in
`independent-scope-freeze.json`.

Standards findings: **0**. Spec findings: **0**.

Code inspection confirmed that the current qualification Store, held under the
project transaction, is the source of routing facts. Catalog capability
declarations are replaced; exact current registration, source, controller bytes,
credential generation/material, observation time and revocation remain checked.
The latest start in the requested exact suite/scope/profile is selected before
examining its result. An unfinished, failed or revoked newer start does not fall
back to a prior success. Historical command replay remains readable without
serving as current authority.

The routing resolver requires the exact projected v2 official scope, Worker/T1,
read/edit, existing regular file projection, no new-file support and capture.
Task accounting source, margins and fixed output capacity must be compatible;
input and operating limits are narrowed to the approved Task. Fixture scope and
legacy fixed fixture qualification do not qualify runtime routing. This does not
itself prove concrete paths exist: the approved Workspace projection and fresh
execution/capacity guards remain the separate required effect boundary.

Independent public behavior tests exercise complete persisted Run approval,
qualification, estimates and an actual Capacity reservation. Only the planning
admission reader and qualification producer are explicit synthetic substitutes;
these tests do not qualify a model or call a provider. After reserving, each of
the following makes repeated fresh reserved-execution guards block while the
original successful receipt remains readable and reservation state is unchanged:

1. A newer qualification completed without candidate-capture evidence.
2. A newer qualification committed its start, then the synthetic controller was
   interrupted before completing an observation.
3. Credential material changed at the same file path, byte length and nanosecond
   mtime, without registering a replacement generation.

All three tests passed on Windows in 1.64 seconds and Linux in 2.71 seconds. The
published test is `examples/go-projected-qualification/test_independent_scope.py`.
Run it with `python -m pytest examples/go-projected-qualification/test_independent_scope.py
-q -o 'pythonpath=backend tests/projects tests/runs tests/web'`. No product files
were changed by this review. Ruff passes. Raw databases and synthetic key files
remain only in temporary test directories, excluded from publishable evidence.
