# Standards review

Fixed point: `0a30e0f17408f253a5e9ee2d758554af47cd24f9`, including the Go diagnostic WIP. **Passed: 0 open findings; one P2 corrected.** Final source and artifact hashes are in `standards-review.json`.

GO-STANDARDS-001 violated the documented credential boundary: valid upstream SSE could forward the provider credential to native. Five synthetic local-HTTP cases actually failed before repair: literal/JSON-escaped text and ordinary content, reasoning, or tool-argument fragments. The relay now rejects these before forwarding. Both original red reports are retained; the same five cases and three controls passed on the published test copy (8/8). Formatting preserved its exact Python AST. Ruff passed.

The bounded Go post-DONE cost exception preserves raw data and usage, rejects other trailing content, and labels the cost unit unknown. Failure-triggered abort, accepted-connection cleanup accounting, and exclusion of function type parameters were read and checked without expanding the test matrix.

Both published live reports match all five final source hashes and re-evaluate as passed: edit records three requests, read/edit and four passing cases; denied_read records two requests, explicit permission denial, unchanged files and no upstream canary. Both record a completed 28-file scan with no leaks. The earlier six successful HTTP responses rejected by the strict parser remain a failed diagnostic; the README distinguishes prototypes and missing cost values accurately.

This reviewer used only synthetic credentials and MockTransport. Live reports were inspected, not independently rerun. No real key was read, no product source was edited and no Git mutation occurred. These diagnostics do not establish full isolation, remote stop, quota/cash qualification or dispatch eligibility. No additional material smell-baseline finding remains.
