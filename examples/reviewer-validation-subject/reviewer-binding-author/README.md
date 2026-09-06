# Approved Reviewer binding author evidence

Issue: [#100](https://github.com/zhouy1017/Karajan/issues/100), parent #95;
dependencies #96 and #99. Combined starting commit:
`3d47194147edf153a0a48a183b34ff7222d674d4`.
Implementation commit is not assigned; these are uncommitted author results.

The two owned files are `backend/karajan/orchestration/reviewer_binding.py` and
`tests/runs/test_reviewer_binding.py`. The source compiles the original approved
Reviewer Task and Rulebook normal-stage membership using current qualification
facts. The ID-only controller stores the binding and claim in the original
operation before calling the existing Candidate CAS primitive. It does not
reserve Capacity, create a Reviewer Attempt, call a model, or record Review
success. Preparation attempt/context IDs only support membership comparison.

The public methods are `advance`, `get`, and `reconcile`, each receiving only
`run_id`, `worker_operation_id`, and keyword `principal`. The internal
`current_locked(project_db, run, operation, transition, *, principal)` callback
reuses the compiler under the Check consumer's operation → Run → Project guards.
Current qualification reads can verify credential material through the existing
private seal; no raw material enters binding records or reports. History reads
do not depend on current qualification, clock, or Candidate artifacts.

An unclaimed prepared intent can be archived and replaced when binding semantics
change. Its old ID, key, and document remain in `validation.intent_history`.
Only the invocation that receives a committed fresh claim can call CAS; a lost
claim reply, changed source after claim, or reopened claimed state allows exact
lookup only. Missing receipt remains `reconciliation_required`. Cross-store
commit and recovery are explicit, not a distributed atomic transaction.

An installed transition is rechecked against its direct predecessor while the
original capture A remains the permanent author anchor. Advancing observed time
alone does not generate another Candidate. Ready receipts are historical facts;
changed qualification prevents installation and subsequent Check effects.

## Results and reproducibility

Final input: `test_reviewer_binding.final.py.txt` (exact bytes of the formal test).
From the worktree, with the project Python runtime:

```text
python -m pytest tests/runs/test_reviewer_binding.py -q -o "pythonpath=backend tests/runs tests/projects tests/web tests/adapters/opencode" --basetemp=<fresh-controlled-temp-directory> --junitxml=<report.xml>
```

- Windows: `final-win.xml`, 18 passed, 27.41 seconds.
- WSL Ubuntu / Python 3.12: `final-linux.xml`, 18 passed, 15.66 seconds.
- Ruff check and format on both owned files: passed.
- Mypy on the producer, both `win32` and `linux` targets: passed.
- `git diff --check`: passed; it warned about CRLF in two unrelated planning
  documents. Both owned files contain LF only.

The WSL Python was `/tmp/karajan-candidate-mode-qy6_mqo2/venv/bin/python`; its
basetemp was `/tmp/karajan-review-binding-final`. WSL tests emitted one
PytestCacheWarning because the Windows-owned shared `.pytest_cache` was not
writable. JUnit and all test artifacts were created successfully. An initial
unprivileged WSL invocation was denied by the host; the approved retry ran the
tests above. `source-before-linux.json` and `source-after-linux.json` match for
all 141 recorded source/input paths.

The 18 cases exercise the public approved Run / operation / Candidate / Checks
chain, including production Worker-only qualification rejection, original
authors and normal grants, A→B→C, stable time-only replay, prepared replacement,
lost CAS reply, lost claim reply, source failure after claim, one-CAS concurrent
advance, both claim-versus-replacement commit orders, owner/ID rejection,
cancellation and unknown old stop, authentication source rejection, and current
qualification enforcement at ready installation and installed new Checks.

This is **C evidence** with real persistent stores, temporary Git/CAS, and
explicit trusted qualification and planning doubles. No provider, native
Reviewer, real credential, reservation, or Reviewer S was exercised. The real
ProfileQualificationStore currently exposes Worker facts and must remain
blocked for Reviewer membership. Running these C tests under Linux does not
qualify a real Reviewer or replace the separately owned actual Check P tests.

## Retained development results

`first-red.xml` records the initial missing-module failure; `first-green.xml`,
`positive.xml`, and `faults.xml` record earlier 1-, 3-, and 10-case inputs. Their
earlier input bytes were not separately retained and are not presented as final
source qualification. `auth-red.xml` records four malformed/foreign current
authentication-source failures before the producer enforced that binding.
`reviewer_binding.auth-before.py.txt` and
`test_reviewer_binding.auth-before.py.txt` retain the exact red source/input;
`auth-green.xml` records the same four cases after the guard. The explicit
source double is the only reason a positive Reviewer path exists in these
tests; the guard does not mint a real Reviewer qualification.

`freeze.json` hashes selected content-free evidence and current owned files.
No SQLite databases, Candidate artifacts, private credential stores, or test
temporary directories belong to this evidence selection. Independent review
and current candidate CI remain the root's release responsibilities.
