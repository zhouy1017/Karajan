# Independent existing-store review

Scope: root-authored #90 existing-only storage changes in `storage.py`, Project registry/qualification/demand/credential sources, Run planning, Capacity, Admission/routing, and Host constructor/reconnection/supervisor. CandidateStore is excluded because it is the reviewer's own author scope. The WIP deployment factory/bootstrap is not part of this source acceptance.

## Standards

0 findings. `AGENTS.md`, `docs/agents/issue-tracker.md`, `CONTEXT.md` and the code-review skill's Fowler baseline were considered. Required schema maps remain with each owning module; the common connection/schema helper does not introduce a second state authority. No tool-enforced lint/style results are counted as independent findings.

## Spec

1 P2 finding was reproduced and is now closed: **EXISTING-STORES-001**. An explicitly `existing_only=True` CredentialSourceStore accepted a bootstrap-mode ProjectRegistry. If that Project database disappeared, the credential constructor's first parent transaction created a new empty database before rejecting missing state. The original credential-existing-red.xml and before-sources.json are retained. No user credential was used; sources were empty and the original DB was renamed and preserved.

The corrected contract rejects the incompatible parent before opening a transaction (`CREDENTIAL_EXISTING_PROJECT_REQUIRED`). Run/Admission/routing additionally reject mixed existing/bootstrap parents, preventing the same borrowed-connection failure. New tests explicitly exercise this early rejection without disk changes. Missing-schema tests now provide wholly existing-mode parents so they still reach and verify the intended schema boundary; their assertions were not widened to hide the changed precondition.

Final public tests cover: missing/empty Run and Admission DBs; missing schema in derived qualification/estimate/routing services; missing or empty replacements on Project/Run/Capacity/Host/qualification/estimate reconnection; real approved Run ownership followed by missing Admission reconnect; detached read-only recovery of an actual persisted operation; and mixed-parent refusal. Original bootstrap construction is used for the positive setup, without model effects. Host supervisor's existing-only reopen was inspected and inherited constructor/reconnect semantics are exercised; this review does not claim a second real native-runner test.

27 Windows cases pass. Linux results and exact final source/test/XML digests are recorded in `review.json`. No open findings remain in this bounded review. Historical reads do not grant current execution authority; legacy refresh APIs retain their separately documented behavior.

```text
python -m pytest .cache/existing-stores-independent/test_independent.py -q -o "pythonpath=backend tests/runs tests/projects tests/routing tests/capacity tests/web" --junitxml=.cache/existing-stores-independent/final-windows.xml
```

Linux used the same selection with `-p no:cacheprovider --basetemp /tmp/karajan-existing-stores-review` and final-linux.xml. Actual Run fixtures use a declared synthetic qualification producer and scripted planning admission; the tested storage and Git/SQLite history are real. Product files and author tests were not edited by this reviewer.
