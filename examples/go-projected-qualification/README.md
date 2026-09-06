# Projected Go qualification evidence

This slice qualifies the existing-file projection, request accounting and stopped
Candidate capture mechanism through revision 2 of the existing durable Store.
It does not qualify a Commander or Reviewer, validate arbitrary new paths, or
complete the approved Run to PR execution chain.

`run_live.py` requires `--live`, a fixed runtime, a controller credential file, a
new private diagnostic directory and the verified local tokenizer directory.
Without `--live` it returns `not_run` before reading credentials or importing the
runtime. The key remains at the controller-selected path and is not copied into
native workspaces or evidence. HTTP fixture tests never use that key.

The live harness records the public Store result, exact source/start, current
qualification facts, same-command replay and unchanged send journal. A report
can be published only after its content is checked for secrets. Local databases,
credential-private material and native storage are not publication artifacts.

## Official observation on 2026-09-06

`official-passed.json` records the complete public Store run on the final source.
The edit scenario made three HTTP 200 requests; denied_read made two. All five
requests were measured before sending and reported provider usage within the
accounted limits. Original input and tool history were retained. The reference
and target read results were observed in the edit conversation.

Only `src/fixture.py` changed in the edit candidate; the denied scenario changed
nothing. Both complete candidates retained readonly and unprojected files and
executable mode. The native processes stopped, both grants were revoked, and
same-command replay left the journal unchanged. Candidate checks and independent
Review remain explicitly missing, so neither candidate is deliverable.

The public routing guard returned no qualification reason for the bounded
Worker/T1/read/edit scope. This is current executor qualification in the private
diagnostic project, not an actual approved Task execution or PR delivery.

The first attempt remains in `official-first-failed.json`: one HTTP 200 response
was rejected with INVALID_TOOL_NAME, no tools executed, cleanup completed and
the second scenario was not run. Its raw response was not retained, so the exact
invalid fragment remains unknown. Local diagnostics separately reproduced and
fixed valid nullable-name compatibility. The later successful official responses
contained **zero** null-name fragments; their success does not establish the
cause of the first failure. Six official requests were attempted across the two
records, and no old grant or failed command was reused for sending.

## Local and independent validation

- `suite-author/`: real Journal and native suite checks, retained semantic-review
  failure and correction, final Linux 26 passes and Windows 133 passes/21 platform
  skips. Earlier pre-effect start rejections remain recorded; their exact cause
  was not established from the available timestamps.
- `store-author/` and `routing-integration/`: durable Store, approved Run, current
  qualification and actual Capacity reservation. Planning receipts and the
  official-policy producer substitute are explicitly synthetic. The final native
  HTTP-fixture chain exercises four nullable continuations in six requests.
- `observer-author/`: fixed probe, actual native capture and rejection boundaries.
- `nullable-sse/`: minimal stream compatibility correction and protocol regressions.
- `independent-scope/`: no Standards or Spec findings; latest failed/unknown
  qualification and changed credential material block an existing reservation.
- `root-projected-boundaries*.xml`: 79 public boundary tests passed on each OS.

The final HTTP-fixture native composition uses an injected monotonic-derived test
clock to keep this non-clock test independent of WSL wall-time resynchronization.
Production time guards are unchanged. The official harness uses the default
production clock. Interrupted or initially failed local runs are retained and
are not counted as passes.

The root freeze manifest binds the current production sources and published
reports. GitHub CI runs the ordinary tests and the independent scope cases
without provider credentials. Work is tracked in [Issue #87](https://github.com/zhouy1017/Karajan/issues/87).
