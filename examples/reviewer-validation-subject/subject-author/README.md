# Subject consumer author evidence (Issues #94 / #101)

Only the original operation supplies a transition. Candidate rebind alone is not authority.
The configured current Reviewer producer is required for ready installation and each new
Check effect. Source qualification in these consumer C/P tests is explicitly synthetic.
Production factory refusal with real unsupported Reviewer qualification is covered separately
by the root factory tests; no provider/model call is made here.

## Current results

- `final-c-windows.xml`: 16 consumer C cases passed, 26.30 seconds.
- `current-and-legacy.xml`: previous 14 consumer plus 26 existing Checks cases passed, 63.12 seconds.
  The later additions change only tests; backend hashes remain those in `source-before-p.json`.
- `static.txt`: Ruff/format six files and mypy three backend files passed.
- `final-linux.xml`: original single WSL invocation completed 18 passed / 107.30 seconds:
  16 consumer C plus two actual namespace P cases. No duplicate P invocation was started.
- `native-passed.json`: A and B each ran both approved Checks, with four distinct Attempt and
  Evidence identities. All four passed; budget retains the original start and five total claims
  (one fixture Writer plus four actual Checks). Capture, full Candidate manifest and user tree
  remain unchanged; Review is still not_run.
- `active-cancel-passed.json`: an observed running namespace PID/birth and command marker
  preceded a deliberately premature ready fixture receipt. The consumer refused switching;
  two advance calls and cancel left A current, confirmed stop and never claimed the second Check.
  Actual observation is completed / exit -9 / local_stop confirmed: this is not success, and
  business state is cancelled. The final trusted log independently contains the command marker.
- `subject_check_fixture.py` is a separate fixed test child, with its exact bytes included in
  every persisted execution controller source. Reviewer qualification is an explicit double;
  Host processes and Check namespaces are real. There is no production fixture flag.
- `backend-source-before.json` and `backend-source-after.json` match for all 139 backend files.
  All five actual executions' persisted controller descriptors match this same source set.
  Each copied Check log matches its trusted receipt digest and length.
- Independent Standards/Spec: zero confirmed findings in `../subject-independent-review`.
  That independent suite preserves its own inputs/results; it is not counted as author tests.

## Retained development history

`initial.xml` is the missing shared-interface failure. `behavior-red.xml` demonstrates that
terminal A remained revision 1 despite the actual #96 B commit; `first-green.xml` closes that
consumer omission. `late-observation-red.xml` demonstrates the unsubmitted stopped old
observation after archive; `late-observation-green.xml` closes it. `fault-first.xml` retains
13 passed plus one cancellation error-priority mismatch; cancellation already denied installation,
and the consumer now checks cancellation before quiescence. No history report was overwritten.
`boundary-first.xml` records the first eight consumer boundary cases. XML elapsed values are
retained as generated rather than rounded to terminal timings.

## Reproduction

From this worktree, use the configured Python environment:

```
python -m pytest tests/runs/test_candidate_subjects.py tests/runs/test_candidate_checks.py -q -o "pythonpath=backend tests/runs tests/projects tests/routing tests/capacity tests/web"
```

The producer tests separately cover prepared replacement, lost rebind commit responses, and
qualification provenance. Consumer history recovery does not call current qualification and
never resubmits a Candidate rebind or a previously claimed Check effect. No SQLite, runtime
work directory, private bootstrap, or credential material belongs in the publishable evidence.

Linux P reproduction uses the same Python environment and pytest path settings, adding
`tests/runs/test_candidate_subjects_native.py`. The two P cases are independently reproducible.
`freeze.json` binds selected reports and sources. Raw generated reports and historical XMLs
are retained byte-for-byte; publisher should preserve their bytes with leaf Git attributes.
No database, runtime directory, bootstrap contents, or private helper script is selected.
