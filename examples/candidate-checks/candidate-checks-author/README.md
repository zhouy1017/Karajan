# #94 controller author evidence

Worktree base: `624ad8b8490003f155baf7842ba91b9975b9526a`.
Author: `ui_spec_finalize`. This is author evidence, not an independent review.
No provider request or credential read was performed by these tests.

## Current checks

The final formatted public test file is `tests/runs/test_candidate_checks.py`.
Its 26 tests cover real Project/Run/operation/Candidate persistence and CAS, an
actual Host prepare/start/reopen, plus explicitly labelled trusted-native and
direct-child identity doubles for fault ordering. The latter do not establish
runtime sandbox qualification. `candidate_checks_case.py` constructs its subject
through public registration/approval/Collector interfaces, with the inherited
planning/model-author fixture explicitly identified.

Run from this worktree:

```powershell
C:/Users/Chooo/Playground/Karajan/.venv/Scripts/python.exe -m pytest -q -o "pythonpath=backend tests/capacity tests/routing tests/runs tests/projects tests/web" tests/runs/test_candidate_checks.py
```

`final-formatted-windows.xml` is the final 26-case Windows run. `ruff.txt`,
`format.txt` and `mypy.txt` are static checks of the five affected products and
two test files (mypy: five products only). `final-writer-regression.xml` checks
the existing public Go execution, intent/replay and Collector after the final
Writer deadline consumer calls were added. Earlier 57/68-case regressions are
historical checks, not an assertion of final-source coverage.

The separate root-owned `../check-composition-current.xml` records two actual
Linux fixed-entry/Host/namespace/CAS tests: all checks pass on a correct
Candidate, and the behavioral check fails on a defective Candidate while the
independent syntax check still executes and passes. Original CAS/user files
remain unchanged and historical reopening adds no Evidence. Root verified all
136 backend source hashes before/after that run. This is local P evidence with
explicit planning/model-author fixtures, not a real Commander or provider run.

Independent budget evidence is in `../shared-budget-independent/`: ten public
C cases on Windows and Linux, zero findings. Full facade independence/recovery
review is separately owned by `capacity_facts` and must not be counted as this
author's review.

## Retained red and correction history

- `history-read-red`, `writer-budget-red`, `prepare-red`, `check-claim-red`,
  `cancel-red`, `host-prepare-red`, `host-start-red`, `consume-identity-red` and
  `evidence-red` are actual feature-first failures, paired with their green XMLs.
- `all-checks-initial.xml` is a fixture directory setup failure.
  `all-checks-fixture-correction.xml` is a fixture clock-domain mismatch between
  the real Check clock and synthetic estimate/qualification epoch. The helper
  was corrected without weakening production time rules.
- `typing-refactor-check.xml` is an actual author regression: copying the
  mutable lifecycle row to satisfy an Any return annotation lost state updates.
  The typed-reference correction is green in `typing-correction.xml`. It did
  not modify the public input or relax a test assertion.
- `current-gates.xml` initially assumed that merely proposing another Plan
  revokes an already approved Plan. Existing Run semantics preserve the old
  approval until the new owner approval. The test now performs that actual
  authority transition; `current-gates-correction.xml` records the three green
  approval/source cases. This fixture expectation correction is not a product
  finding or a claim that pending proposals revoke authorization.
- `final-windows.xml` precedes the final test-only import blank line;
  `final-formatted-windows.xml` runs the exact final test bytes.

All original XMLs remain unmodified. They contain synthetic input only. No
SQLite database, process/session directory, native workspace, key or bytecode
belongs in a published evidence package.

## Explicit remaining boundaries

Subject revision 1 binds the original capture. The trusted same-content
validation-policy/Candidate rebind is a #95 dependency and is not implemented
here. There is no Reviewer, repair/retry loop, complete Run/PR gate or CI-success
claim in this evidence. Writer budget checks bound controller effect admission,
not later transport acceptance or hard termination of an already running
Writer. Late safely stopped capture remains independent of a new-effect
deadline check.
