# Approved Workspace input compiler — author evidence

`build_task_input(workspace, candidates, *, native_source_sha256, runner_source_digest)` accepts the controller's persisted ApprovedTaskWorkspace snapshot. It checks both Workspace digests, internal Plan/Approval/routing/ExecutionPolicy binding, exact complete CAS baseline, and recomputed approved read/write paths. It requires exactly the Task tools read/edit and covering plan/policy permissions. It uses the approved Task duration, with no invented policy timeout. Stable JSON carries the complete requirement, plan summary, selected Task, and exact paths; text longer than 8192 characters is rejected, never truncated.

No original repository is read by the compiler. CandidateStore.materialize_baseline verifies all immutable baseline artifacts and restores them into a private temporary sibling of its control storage. The compiler reads only approved projection files into immutable byte inputs, and removes temporary restoration on success and failure. CandidateStore and qualification-source modules were unchanged.

The tests use real local Git, ProjectRegistry, Run approval, Workspace, CandidateStore and Capacity stores. Qualification and planning admission have explicitly named synthetic producers; these tests are not evidence that a real model qualified or that an actual provider was called. Fully rehashed adversarial documents are used to test consistency checks, not to pretend a new owner approved them. The compiler does not establish current authority: root must fetch its persisted snapshot and hold current start/send/acceptance guards.

TDD evidence retains the missing-module tracer, two digest rejection reds, ten rehashed binding reds, and the incomplete task-requirements red. The initial tracer expected the wrong existing fixture acceptance wording; the expectation was corrected to the actual approved `Report is repeatable` text. A temporary exception-wrapper ordering regression hid RunError's stable reason; the specific RunError branch fixed it. No test was weakened.

Final checks: 24 public tests on Windows and Linux; Ruff and strict mypy for both platform targets. Cases include the original repository becoming unavailable, frozen bytes and prompt stability, two digest layers, path expansion, baseline/repository/Plan/Approval/policy substitution, duplicate task, complete routing requirements, missing/changed/hard-linked artifacts, narrow/extra tools, missing authorization, oversized input, and source-digest syntax.

Reproduce from this worktree using Python 3.12:

    PYTHONPATH=backend:tests/runs:tests/projects:tests/web python -m pytest tests/runs/test_go_task_input.py -q -p no:cacheprovider --basetemp=<private-test-directory>

On Windows use semicolons between PYTHONPATH entries. No pinned model runtime, tokenizer, provider access or user credential is needed for this compiler test file.
