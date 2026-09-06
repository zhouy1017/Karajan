# Independent reserved-routing Spec result

Base `405ce11`, worktree `go-task-startup`. Reviewed root's `orchestration/routing.py` new `reserved_execution_guard` and `_build` integration, including public behavior through the new pure evaluator dependency. **10 independent cases passed / 3.35 s; no new findings in this boundary.** Current source hashes are recorded in `review.json`; orchestration/evaluator/export hashes match those captured before the first test run.

The public interface reads only Run/assessment identity and principal. Its selected Profile, Task, planned Attempt/Context and original source material come from persisted assessment. It keeps the Run → Project guard order, with the historical read before acquiring the longer Run guard. A current authority check can succeed without any reservation and still returns `activation_allowed=False`, `dispatch_enabled=False` and `quota_revalidation_required=True`. The separate consumer must verify operation/Workspace, original Capacity request and actual execution gate.

Independent cases use real temporary Git/Registry/Run/Estimate/Capacity stores. Positive qualification is explicitly synthetic; its controlled source replacement test proves that a different source record cannot replace the original selected source silently, not that any real runtime is qualified. Checks cover detached historical identity, current Profile restriction, source replacement, pending/approved scope changes, expired estimate, blocked-assessment rejection, and leaving capacity-only checks to the actual Capacity gate. No Host or provider execution occurred.

Initial run: 9 passed / 1 fixture failure. The zero-balance case submitted an observation with the **same** observed_at as the original; `CapacityStore.observe` intentionally retained the previous value (`applied=False`). The draft expectation that actual activation would then reject was therefore invalid. The final fixture advances the public clock to 1001, submits observed_at=1001 and explicitly asserts `applied=True`. The final actual Capacity activation rejects that lower balance while the authority-only fixed Profile guard correctly remains selected. Initial XML and test bytes are retained, with no product changes to fix the fixture.

Command:

```text
python -m pytest .cache/reserved-routing-review/test_reserved_routing_public.py -o "pythonpath=backend tests/runs" -q --junitxml=.cache/reserved-routing-review/final.xml
```

Ruff and format checks pass for the independent test. Existing root tests are not counted as this reviewer's additional coverage. The Task qualification/Workspace dependency cycle and minimal producer-versus-Task-permission separation are recorded separately in `notes.md`. A projection/meter qualification would still not automatically satisfy all Rulebook capabilities such as bounded implementation, candidate capture or independent review; those need their own scoped evidence. No finding is asserted against the deliberate current fail-closed fixed-Go scope.
