# Independent Host runner-identity Spec review

Base: `f2d639559e738dfbb951163c6e8d83b460d758fb`; worktree `codex/m3-go-task-runner`. Reviewed parent-authored `execution/host.py` runner registration/guard and `_supervisor.py` child registration against `.cache/go-task-runner-acceptance.md` and the explicit parent task. This is the independent Spec axis, not a full Standards review. No product, author test, key or provider changes/calls were made.

**Findings: 0 in this bounded scope.** Windows final: 11 passed, 0 skipped, 4.79s. Linux final: 11 passed, 0 skipped, 9.37s. The independently written public tests are `tests/execution/test_runner_identity_independent.py`; actual interpreter children, Host supervisors, process groups/jobs and SQLite transactions were exercised. The Windows child command uses `sys._base_executable` plus a controlled sys.path bootstrap so the direct child is the actual interpreter rather than a venv redirector.

Verified:

- The actual directly registered ProcessSpec child may enter its guard; its live same-group grandchild is denied both registration adoption and current runner authority.
- Public fence/auth/control changes after registration are rejected by that same actual child.
- A child-held guard blocks concurrent public cancellation/control writes until release. Raising inside the body releases the transaction; a later public write succeeds.
- Physical supervisor loss leaves no authorized child: the observed Windows child also exited; the Linux surviving child returned `RUNNER_CONTAINMENT_UNPROVEN`. The final XML retains each platform's observed result in captured stdout.
- A separate live-child test deliberately corrupts only the supervisor birth field and verifies refusal. This is an explicit damaged-ledger fixture, not a claim that ordinary API use mutates incarnation fields.
- A legacy schema fixture removes only the new child identity columns after an actual completed launch. Reopening migrates the schema, exact prepare/start replays retain the old snapshot, and the command's start counter remains one. No absent historical child identity is promoted.
- Registration read on a renamed-away ledger fails without creating a replacement DB, including a directory containing URI-reserved characters.

History: `initial-windows.xml` contains 9 pass / 1 fail. That test incorrectly required the child to survive physical supervisor termination and write a denial file. The corrected run directly observed the child exited instead, so the assertion now permits either proven exit or an explicit live-child denial; a separate live-child damaged-supervisor-identity case was added. No product fix was made. `corrected-windows.xml` records 11 pass with a JUnit-property warning; final tests use captured stdout instead and have no warnings. The final Windows/Linux runs used the same product hashes as `before-sources.json`.

Limits: tests do not prove OS identity cannot change after an observation, multi-database atomicity, business authorization, provider cancellation or native namespace termination. Runner registration remains a handshake; actual effects still require `current_runner_guard` and current operation/Run/Project/Capacity checks. The root controller is responsible for committing its one-time effect claim before native Popen.

Rerun from the repository root:

```text
python -m pytest tests/execution/test_runner_identity_independent.py -q -o "pythonpath=backend tests/execution" -o junit_logging=all
```

No independent finding was converted into a new product change, and no CI issue delegated to Copilot was touched.
