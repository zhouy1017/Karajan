# Independent reserved-routing review

**10 passed, 0 failed, 0 skipped / 3.48 s** from this published path. No new findings in root's fixed Profile routing integration. Base `405ce11`; independent reviewer `ui_spec_finalize`.

```text
python -m pytest examples/go-task-startup/routing-review/test_reserved_routing_public.py -o "pythonpath=backend tests/capacity tests/routing tests/runs tests/projects tests/web" -q --junitxml=examples/go-task-startup/routing-review/final.xml
```

The test has no `.cache` dependency. It uses real temporary Git repositories and Registry/Run/Estimate/Capacity stores through existing public fixtures. Positive qualification is an explicitly labeled source double; no actual runtime or credential qualification is claimed.

The review verifies persisted assessment identity, current Profile restrictions, replacement qualification-source rejection, pending versus approved Task changes, estimate expiry, blocked historical assessment rejection, and retaining capacity-only decisions for the real Capacity gate. The guard may succeed without a reservation and continues to return no activation/dispatch authority. The later operation/Workspace/Capacity/Host consumer must enforce its own complete binding and actual effect gate.

`review.json` records this published-path run, source hashes and evidence hashes. `history/` preserves initial/final pre-publication XML, original test bytes, the original independent review and the Task qualification dependency analysis. Paths in archived report JSON describe the original development-directory layout; the current manifest maps actual published files. No test imports or reads a `.cache` path.

The initial run had 9 passes and one fixture error: the lower-balance observation reused the original timestamp and was correctly ignored. The final fixture uses a strictly newer timestamp and asserts `applied=True`; no product change resolved this fixture error. The pre-publication final run passed all 10 cases / 3.35 s. An early manual estimate of 11 cases was corrected to pytest's collected count of 10.

The design note identifies the cycle if Task qualification requires an already-reserved Workspace, while reservation itself requires qualification. A separately qualified projection/meter mechanism can break that cycle; the approved Task Workspace still supplies actual path permissions. That direction does not grant unobserved capabilities or weaken the current fixed-Go scope restriction.

New text uses LF. Historical XML and original test bytes retain their recorded bytes under the leaf `.gitattributes`, which must remain with this evidence. No database, temporary repository, bytecode, credential or provider call is included.
